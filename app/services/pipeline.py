from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..emma_looks import (
    CATALOG_VERSION as EMMA_LOOK_CATALOG_VERSION,
    LEGACY_DEFAULT_LOOK_ID,
    EmmaLook,
    get_emma_look,
)
from ..media import is_valid_video, music_arrangement_quality
from ..models import Asset, AssetKind, Episode, EpisodeStatus, Job, JobStatus
from ..providers import get_music_provider, get_video_provider
from ..providers.base import MusicProvider, MusicResult, VideoProvider, VideoResult
from ..providers.elevenlabs import music_receipt_path, music_request_fingerprint
from ..providers.veo import veo_price_per_second
from .prompts import (
    emma_visual_guard,
    generate_lyrics,
    generate_storyboard,
    music_prompt,
    publish_metadata,
)
from .render import concatenate_scenes, create_thumbnail, make_vertical_short, mux_music
from .safety import review_episode


STEP_ORDER = ["lyrics", "storyboard", "music", "scenes", "render", "qc"]
REFERENCE_ROLE_ORDER = ("emma", "friends", "world")
REFERENCE_ROLE_LABELS = {
    "emma": "Emma — reference ufficiale",
    "friends": "Amici dell’episodio",
    "world": "Mondo dell’episodio",
}
REFERENCE_ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}
REFERENCE_MAX_IMAGE_DIMENSION = 8192
REFERENCE_MAX_IMAGE_PIXELS = 40_000_000
LEGACY_REFERENCE_ROLE_ALIASES = {
    "nuvibu": "emma",
    "cast": "friends",
}
EMMA_LOOK_ID_KEY = "emma_look_id"
REFERENCE_DEPENDENT_ASSET_KINDS = {
    AssetKind.VIDEO_SCENE,
    AssetKind.RENDER,
    AssetKind.SHORT,
    AssetKind.THUMBNAIL,
    AssetKind.REPORT,
}
CONTENT_APPROVALS_KEY = "content_approvals"
BUDGET_RESERVED_USD_KEY = "budget_reserved_usd"
BUDGET_ACTUAL_BASELINE_USD_KEY = "budget_actual_baseline_usd"
BUDGET_RESERVED_AT_KEY = "budget_reserved_at"
BUDGET_RESERVATION_STEP_KEY = "budget_reservation_step"
# A transaction-scoped PostgreSQL advisory lock serializes the very short
# "measure commitments -> reserve" section across every episode. It works with
# Neon pooled connections because it is released by COMMIT/ROLLBACK.
DAILY_BUDGET_ADVISORY_LOCK_ID = 0x4E5556494255
_SQLITE_DAILY_BUDGET_LOCK = threading.RLock()
EDITABLE_DRAFT_ASSET_KINDS = {
    AssetKind.LYRICS,
    AssetKind.STORYBOARD,
    AssetKind.CHARACTER_REFERENCE,
}


class ReferenceChangeConflictError(RuntimeError):
    """The character reference cannot be changed safely yet."""


class ActiveJobError(ReferenceChangeConflictError):
    """A mutable episode operation conflicts with its active pipeline job."""


def slugify(value: str) -> str:
    value = value.lower().strip()
    replacements = str.maketrans("àáâäèéêëìíîïòóôöùúûü", "aaaaeeeeiiiioooouuuu")
    value = value.translate(replacements)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "episodio"


class PipelineService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self._music_provider: MusicProvider | None = None
        self._video_provider: VideoProvider | None = None
        self._image_validation_cache: dict[
            tuple[str, int, int],
            bool,
        ] = {}

    @property
    def music_provider(self) -> MusicProvider:
        if self._music_provider is None:
            self._music_provider = get_music_provider(self.settings)
        return self._music_provider

    @property
    def video_provider(self) -> VideoProvider:
        if self._video_provider is None:
            self._video_provider = get_video_provider(self.settings)
        return self._video_provider

    def _asset_episode_dir(self, episode: Episode) -> Path:
        path = self.settings.asset_dir / episode.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _render_episode_dir(self, episode: Episode) -> Path:
        path = self.settings.render_dir / episode.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _generation_output_path(
        self,
        episode: Episode,
        *,
        kind: AssetKind,
        variant: int,
        canonical: Path,
    ) -> Path:
        """Use a stable new path after each immutable paid ledger row.

        A retry of an interrupted, not-yet-ledgered request gets the same path
        (and therefore the same provider receipt/operation). Once a paid Asset
        row exists, a later replacement moves to ``-retry-N`` and can never be
        confused with that historical spend.
        """

        prior_count = sum(
            asset.variant == variant
            for asset in self._assets(episode, kind)
        )
        if prior_count == 0:
            return canonical
        return canonical.with_name(
            f"{canonical.stem}-retry-{prior_count + 1}{canonical.suffix}"
        )

    def _remove_assets(self, episode: Episode, kinds: set[AssetKind]) -> None:
        targets = list(
            self.db.scalars(
                select(Asset).where(
                    Asset.episode_id == episode.id,
                    Asset.kind.in_(kinds),
                )
            )
        )
        paid_targets = [asset for asset in targets if asset.cost_usd > 0]
        if paid_targets:
            raise RuntimeError(
                "Refusing to delete cost-bearing asset ledger rows: "
                + ", ".join(
                    sorted({asset.kind.value for asset in paid_targets})
                )
            )
        removable_paths = [
            Path(asset.path)
            for asset in targets
            if asset.kind != AssetKind.CHARACTER_REFERENCE
        ]
        # Commit database truth before touching storage. A crash can then leave
        # only an unreferenced file, never a row pointing to a deleted object.
        for asset in targets:
            self.db.delete(asset)
        self.db.commit()
        for path in removable_paths:
            path.unlink(missing_ok=True)

    def _assets(self, episode: Episode, kind: AssetKind) -> list[Asset]:
        return list(
            self.db.scalars(
                select(Asset)
                .where(Asset.episode_id == episode.id, Asset.kind == kind)
                .order_by(Asset.variant.asc(), Asset.created_at.asc())
            )
        )

    @staticmethod
    def _complete_image_file(path: Path) -> bool:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "error",
                    Image.DecompressionBombWarning,
                )
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    image.load()
        except (
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            return False
        return True

    @staticmethod
    def _normalized_reference_image(source: Path, role: str) -> Image.Image:
        label = REFERENCE_ROLE_LABELS[role]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "error",
                    Image.DecompressionBombWarning,
                )
                with Image.open(source) as probe:
                    actual_format = probe.format
                    width, height = probe.size
                    frame_count = int(getattr(probe, "n_frames", 1))
                    if actual_format not in REFERENCE_ALLOWED_IMAGE_FORMATS:
                        raise ValueError(
                            f"{label}: usa un’immagine PNG, JPEG o WebP"
                        )
                    if (
                        width < 1
                        or height < 1
                        or width > REFERENCE_MAX_IMAGE_DIMENSION
                        or height > REFERENCE_MAX_IMAGE_DIMENSION
                        or width * height > REFERENCE_MAX_IMAGE_PIXELS
                    ):
                        raise ValueError(
                            f"{label}: le dimensioni dell’immagine sono eccessive"
                        )
                    if frame_count != 1:
                        raise ValueError(
                            f"{label}: usa un’immagine statica, non animata"
                        )
                    probe.verify()
                with Image.open(source) as image:
                    image.load()
                    return ImageOps.exif_transpose(image).convert("RGB")
        except ValueError:
            raise
        except (
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise ValueError(
                f"{label}: il file è incompleto o danneggiato"
            ) from exc

    def _asset_file_is_valid(self, asset: Asset) -> bool:
        if (asset.metadata_json or {}).get("invalidated_at"):
            return False
        path = Path(asset.path)
        try:
            stat = path.stat()
            if not path.is_file() or stat.st_size <= 0:
                return False
        except OSError:
            return False
        if asset.mime_type.startswith("video/") or path.suffix.lower() == ".mp4":
            return is_valid_video(path)
        if asset.mime_type.startswith("image/") or path.suffix.lower() in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }:
            cache_key = (
                str(path),
                stat.st_size,
                stat.st_mtime_ns,
            )
            cached = self._image_validation_cache.get(cache_key)
            if cached is None:
                cached = self._complete_image_file(path)
                expected_sha256 = (asset.metadata_json or {}).get(
                    "stored_sha256"
                )
                if cached and isinstance(expected_sha256, str):
                    actual_sha256 = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    cached = actual_sha256 == expected_sha256
                self._image_validation_cache[cache_key] = cached
            return cached
        return True

    def _valid_assets(self, episode: Episode, kind: AssetKind) -> list[Asset]:
        return [
            asset
            for asset in self._assets(episode, kind)
            if self._asset_file_is_valid(asset)
        ]

    def has_valid_asset(self, episode: Episode, kind: AssetKind) -> bool:
        """Return whether the episode has at least one usable asset of ``kind``."""

        return bool(self._valid_assets(episode, kind))

    def selected_valid_asset(
        self,
        episode: Episode,
        kind: AssetKind,
    ) -> Asset | None:
        """Return the newest selected usable asset for a media kind."""

        return next(
            (
                asset
                for asset in reversed(self._valid_assets(episode, kind))
                if asset.selected
            ),
            None,
        )

    def _discard_invalid_assets(self, episode: Episode, kind: AssetKind) -> None:
        invalid = [
            asset
            for asset in self._assets(episode, kind)
            if not self._asset_file_is_valid(asset)
        ]
        if not invalid:
            return
        preserved = [
            asset
            for asset in invalid
            if asset.cost_usd > 0
            or (asset.metadata_json or {}).get("invalidation_reason")
            == "user_requested_regeneration"
        ]
        removable = [asset for asset in invalid if asset not in preserved]
        if preserved:
            invalidated_at = datetime.now(timezone.utc).isoformat()
            with self._daily_budget_lock():
                for asset in preserved:
                    metadata = dict(asset.metadata_json or {})
                    metadata.setdefault("invalidated_at", invalidated_at)
                    metadata.setdefault(
                        "invalidation_reason",
                        "missing_or_invalid_media",
                    )
                    asset.metadata_json = metadata
                    asset.selected = False
                for asset in removable:
                    self.db.delete(asset)
                self.db.commit()
        else:
            for asset in removable:
                self.db.delete(asset)
            self.db.commit()

    def _invalidate_unacceptable_music_assets(self, episode: Episode) -> None:
        """Preserve paid history while making failed arrangements retryable."""

        if self.settings.provider_mode != "live":
            return
        changed = False
        invalidated_at = datetime.now(timezone.utc).isoformat()
        for asset in self._assets(episode, AssetKind.MUSIC):
            metadata = dict(asset.metadata_json or {})
            if metadata.get("invalidated_at"):
                continue
            quality = metadata.get("arrangement_qc")
            if not isinstance(quality, dict) or "passed" not in quality:
                quality = music_arrangement_quality(Path(asset.path))
                metadata["arrangement_qc"] = quality
                changed = True
            if not quality.get("passed"):
                metadata["invalidated_at"] = invalidated_at
                metadata["invalidation_reason"] = (
                    "insufficient_instrumental_arrangement"
                )
                asset.selected = False
                changed = True
            asset.metadata_json = metadata
        if changed:
            self.db.commit()

    def _update_actual_cost(self, episode: Episode) -> None:
        episode.actual_cost_usd = float(
            self.db.scalar(
                select(func.coalesce(func.sum(Asset.cost_usd), 0.0)).where(
                    Asset.episode_id == episode.id
                )
            )
            or 0.0
        )

    @staticmethod
    def _copy_completed_file(source: Path, destination: Path) -> None:
        """Copy through a unique temporary name and verify the completed object."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.uploading")
        try:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    shutil.copyfile(source, temporary)
                    if temporary.stat().st_size != source.stat().st_size:
                        raise OSError("copied object size does not match source")
                    os.replace(temporary, destination)
                    return
                except OSError as exc:
                    last_error = exc
                    temporary.unlink(missing_ok=True)
                    if attempt < 2:
                        time.sleep(2**attempt)
            raise RuntimeError(f"Could not store completed artifact: {last_error}")
        finally:
            temporary.unlink(missing_ok=True)

    def _asset(
        self,
        episode: Episode,
        *,
        kind: AssetKind,
        path: Path,
        mime_type: str,
        provider: str,
        variant: int = 1,
        selected: bool = False,
        duration_seconds: float | None = None,
        width: int | None = None,
        height: int | None = None,
        cost_usd: float = 0.0,
        metadata: dict | None = None,
    ) -> Asset:
        asset = Asset(
            episode=episode,
            kind=kind,
            path=str(path.resolve()),
            mime_type=mime_type,
            provider=provider,
            variant=variant,
            selected=selected,
            duration_seconds=duration_seconds,
            width=width,
            height=height,
            cost_usd=cost_usd,
            metadata_json=metadata or {},
        )
        self.db.add(asset)
        self.db.flush()
        return asset

    def _generation_duration(self, scene: dict, *, uses_reference: bool) -> int:
        if uses_reference:
            return 8
        duration = int(scene["duration_seconds"])
        return 4 if duration <= 4 else 6 if duration <= 6 else 8

    def estimate_music_cost(self, episode: Episode) -> float:
        """Estimate only the music variants that have not been durably stored."""

        if self.settings.provider_mode == "mock":
            return 0.0
        existing_variants = {
            asset.variant for asset in self._valid_assets(episode, AssetKind.MUSIC)
        }
        missing_variants = sum(
            variant not in existing_variants
            for variant in range(1, self.settings.max_music_variants + 1)
        )
        return round(
            missing_variants * (episode.duration_seconds / 60) * 0.15,
            2,
        )

    def estimate_music_regeneration_cost(self, episode: Episode) -> float:
        """Estimate a deliberate replacement of every configured music variant."""

        if self.settings.provider_mode == "mock":
            return 0.0
        return round(
            self.settings.max_music_variants
            * (episode.duration_seconds / 60)
            * 0.15,
            2,
        )

    def validate_music_regeneration(self, episode: Episode) -> None:
        if not self.content_is_approved(episode, "lyrics"):
            raise ReferenceChangeConflictError(
                "Approve the current lyrics before regenerating music"
            )
        if not self.content_is_approved(episode, "storyboard"):
            raise ReferenceChangeConflictError(
                "Approve the current storyboard before regenerating music"
            )
        if not self.has_valid_asset(episode, AssetKind.MUSIC):
            raise ReferenceChangeConflictError(
                "There is no current music asset to regenerate"
            )
        downstream_kinds = {
            AssetKind.VIDEO_SCENE,
            AssetKind.RENDER,
            AssetKind.SHORT,
            AssetKind.THUMBNAIL,
            AssetKind.SUBTITLES,
            AssetKind.REPORT,
        }
        downstream = [
            kind.value
            for kind in downstream_kinds
            if self.has_valid_asset(episode, kind)
        ]
        if downstream or episode.qc_json:
            details = ", ".join(sorted(downstream)) or "quality report"
            raise ReferenceChangeConflictError(
                "Cannot regenerate music after video production has started: "
                f"{details}"
            )

    def can_regenerate_music(self, episode: Episode) -> bool:
        try:
            self.validate_music_regeneration(episode)
        except ReferenceChangeConflictError:
            return False
        return self.active_job(episode) is None

    def _invalidate_music_for_regeneration(self, episode: Episode) -> None:
        """Archive current music in place so a new immutable output can be made."""

        self.validate_music_regeneration(episode)
        invalidated_at = datetime.now(timezone.utc).isoformat()
        for asset in self._valid_assets(episode, AssetKind.MUSIC):
            metadata = dict(asset.metadata_json or {})
            metadata["invalidated_at"] = invalidated_at
            metadata["invalidation_reason"] = "user_requested_regeneration"
            asset.metadata_json = metadata
            asset.selected = False
        episode.status = EpisodeStatus.STORYBOARD_READY
        self._update_actual_cost(episode)
        self.db.flush()

    def prepare_music_regeneration(self, episode: Episode) -> None:
        """Prepare a synchronous/local music retry while preserving history."""

        self._lock_episode(episode)
        active = self.active_job(episode)
        if active is not None:
            self.db.rollback()
            raise ActiveJobError(
                f"Cannot regenerate music while job {active.id} "
                f"is {active.status.value}"
            )
        try:
            self._invalidate_music_for_regeneration(episode)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _estimate_video_cost(
        self,
        episode: Episode,
        *,
        remaining_only: bool,
    ) -> float:
        if self.settings.provider_mode == "mock":
            return 0.0
        storyboard = episode.storyboard_json or generate_storyboard(episode)
        uses_reference = self.character_reference(episode) is not None
        existing_variants = (
            {
                asset.variant
                for asset in self._valid_assets(episode, AssetKind.VIDEO_SCENE)
            }
            if remaining_only
            else set()
        )
        generated_seconds = sum(
            self._generation_duration(scene, uses_reference=uses_reference)
            for scene in storyboard
            if int(scene["index"]) + 1 not in existing_variants
        )
        # Reserve the full configured retry ceiling before the first paid request.
        return (
            generated_seconds
            * self._video_price_per_second()
            * (self.settings.max_scene_retries + 1)
        )

    def estimate_cost(self, episode: Episode) -> float:
        if self.settings.provider_mode == "mock":
            return 0.0
        full_music = (
            self.settings.max_music_variants
            * (episode.duration_seconds / 60)
            * 0.15
        )
        return round(
            full_music
            + self._estimate_video_cost(episode, remaining_only=False),
            2,
        )

    def estimate_remaining_cost(self, episode: Episode) -> float:
        """Estimate additional provider spend needed for a complete render."""

        return round(
            self.estimate_music_cost(episode)
            + self._estimate_video_cost(episode, remaining_only=True),
            2,
        )

    def _video_price_per_second(self) -> float:
        return veo_price_per_second(self.settings.veo_backend, self.settings.veo_model)

    def assert_budget(
        self,
        episode: Episode,
        *,
        additional_cost: float = 0.0,
    ) -> None:
        actual = max(0.0, self._episode_actual_cost(episode.id))
        remaining = max(
            0.0,
            self.estimate_remaining_cost(episode)
            + max(0.0, float(additional_cost)),
        )
        projected = actual + remaining
        episode.actual_cost_usd = actual
        episode.estimated_cost_usd = round(projected, 4)
        if projected > self.settings.max_estimated_cost_usd_per_episode + 1e-9:
            raise RuntimeError(
                f"Projected episode cost ${projected:.2f} "
                f"(actual ${actual:.2f} + remaining ${remaining:.2f}) exceeds "
                "MAX_ESTIMATED_COST_USD_PER_EPISODE="
                f"${self.settings.max_estimated_cost_usd_per_episode:.2f}"
            )

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _json_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return PipelineService._as_utc(parsed)

    def job_is_stale(
        self,
        job: Job,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether an active job can be safely replaced.

        Pending dispatches use the latest Cloud Run dispatch timestamp so a
        same-step retry does not discard a job that is merely waiting to start.
        Running jobs use the latest committed pipeline heartbeat. Provider
        calls never keep a database transaction open; the production timeout is
        deliberately shorter than ``job_stale_after_seconds``.
        """

        if job.status not in {JobStatus.PENDING, JobStatus.RUNNING}:
            return False
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(
            seconds=self.settings.job_stale_after_seconds
        )
        created_at = self._as_utc(job.created_at)
        if job.status == JobStatus.PENDING:
            candidates = [created_at] if created_at is not None else []
            dispatched_at = self._json_datetime(
                (job.result_json or {}).get("cloud_run_dispatched_at")
            )
            if dispatched_at is not None:
                candidates.append(dispatched_at)
        else:
            started_at = self._as_utc(job.started_at)
            candidates = (
                [started_at]
                if started_at is not None
                else ([created_at] if created_at is not None else [])
            )
            heartbeat_at = self._json_datetime(
                (job.result_json or {}).get("pipeline_heartbeat_at")
            )
            if heartbeat_at is not None:
                candidates.append(heartbeat_at)
        return not candidates or max(candidates) < cutoff

    @contextmanager
    def _daily_budget_lock(self) -> Iterator[None]:
        """Serialize the short budget reservation transaction.

        PostgreSQL uses a transaction-scoped advisory lock, which is safe with
        Neon's pooled endpoint. SQLite is supported for local/tests with a
        process-wide reentrant lock; production validation already requires
        PostgreSQL.
        """

        dialect = self.db.get_bind().dialect.name
        if dialect == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": DAILY_BUDGET_ADVISORY_LOCK_ID},
            )
            yield
            return
        with _SQLITE_DAILY_BUDGET_LOCK:
            yield

    def _active_pipeline_jobs(self) -> list[Job]:
        return list(
            self.db.scalars(
                select(Job)
                .where(
                    Job.job_type == "pipeline",
                    Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                )
                .order_by(Job.created_at.asc(), Job.id.asc())
                # Revalidate after waiting for a worker's short claim
                # transaction. This prevents stale cleanup from overwriting a
                # fresh PENDING -> RUNNING transition.
                .with_for_update()
            )
        )

    def _release_budget_reservation(
        self,
        job: Job,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        result = dict(job.result_json or {})
        result["budget_reservation_released_at"] = now.isoformat()
        result["budget_reservation_release_reason"] = reason
        job.result_json = result

    def _expire_stale_jobs(
        self,
        jobs: list[Job],
        *,
        now: datetime,
        exclude_job_id: str | None = None,
    ) -> list[Job]:
        active: list[Job] = []
        for job in jobs:
            if job.id == exclude_job_id or not self.job_is_stale(job, now=now):
                active.append(job)
                continue
            episode = self.db.get(Episode, job.episode_id)
            try:
                unresolved = (
                    self._unresolved_paid_artifacts(episode)
                    if episode is not None
                    else []
                )
            except Exception as reconciliation_exc:
                unresolved = [
                    f"provider-state-reconciliation-error: "
                    f"{reconciliation_exc}"
                ]
            if unresolved:
                job.status = JobStatus.PENDING
                job.started_at = None
                job.finished_at = None
                job.error_text = (
                    "Stale worker retained for safe provider-state recovery"
                )
                result = dict(job.result_json or {})
                result["provider_reconciliation_required"] = unresolved
                result["retryable_provider_error_at"] = now.isoformat()
                job.result_json = result
                active.append(job)
                continue
            prior_status = job.status.value
            job.status = JobStatus.FAILED
            job.finished_at = now
            job.error_text = (
                f"{prior_status.capitalize()} worker execution became stale; "
                "its spend reservation was released"
            )
            self._release_budget_reservation(
                job,
                reason="stale",
                now=now,
            )
        self.db.flush()
        return active

    def estimate_job_incremental_cost(
        self,
        episode: Episode,
        through_step: str,
    ) -> float:
        """Estimate provider spend introduced by one controlled pipeline job."""

        if through_step not in STEP_ORDER:
            raise ValueError(f"Unknown pipeline step: {through_step}")
        if through_step in {"lyrics", "storyboard"}:
            return 0.0
        if through_step == "music":
            return self.estimate_music_cost(episode)
        return self.estimate_remaining_cost(episode)

    def _episode_actual_cost(self, episode_id: str) -> float:
        return float(
            self.db.scalar(
                select(func.coalesce(func.sum(Asset.cost_usd), 0.0)).where(
                    Asset.episode_id == episode_id
                )
            )
            or 0.0
        )

    @staticmethod
    def _reservation_value(job: Job, key: str) -> float | None:
        value = (job.payload_json or {}).get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _set_budget_reservation(
        self,
        job: Job,
        episode: Episode,
        *,
        amount: float,
        through_step: str,
        now: datetime,
    ) -> None:
        payload = dict(job.payload_json or {})
        payload[BUDGET_RESERVED_USD_KEY] = round(max(0.0, amount), 4)
        payload[BUDGET_ACTUAL_BASELINE_USD_KEY] = max(
            0.0,
            self._episode_actual_cost(episode.id),
        )
        payload[BUDGET_RESERVED_AT_KEY] = now.isoformat()
        payload[BUDGET_RESERVATION_STEP_KEY] = through_step
        job.payload_json = payload

    def _increase_budget_reservation(
        self,
        job: Job,
        *,
        additional: float,
        now: datetime,
    ) -> None:
        if additional <= 1e-9:
            return
        payload = dict(job.payload_json or {})
        reserved = self._reservation_value(
            job,
            BUDGET_RESERVED_USD_KEY,
        ) or 0.0
        payload[BUDGET_RESERVED_USD_KEY] = round(
            reserved + additional,
            4,
        )
        payload["budget_reservation_updated_at"] = now.isoformat()
        job.payload_json = payload
        self.db.flush()

    def _ensure_budget_reservation(
        self,
        job: Job,
        *,
        now: datetime,
        amount: float | None = None,
    ) -> None:
        reserved = self._reservation_value(job, BUDGET_RESERVED_USD_KEY)
        baseline = self._reservation_value(
            job,
            BUDGET_ACTUAL_BASELINE_USD_KEY,
        )
        reserved_at = self._json_datetime(
            (job.payload_json or {}).get(BUDGET_RESERVED_AT_KEY)
        )
        if (
            reserved is not None
            and reserved >= 0
            and baseline is not None
            and baseline >= 0
            and reserved_at is not None
        ):
            return
        episode = self.db.get(Episode, job.episode_id)
        if episode is None:
            raise RuntimeError(
                f"Cannot reserve daily spend for missing episode {job.episode_id}"
            )
        through_step = str((job.payload_json or {}).get("through_step", "qc"))
        requested = (
            self.estimate_job_incremental_cost(episode, through_step)
            if amount is None
            else max(0.0, float(amount))
        )
        self._set_budget_reservation(
            job,
            episode,
            amount=requested,
            through_step=through_step,
            now=now,
        )
        self.db.flush()

    def _daily_budget_commitment(
        self,
        jobs: list[Job],
        *,
        now: datetime,
    ) -> tuple[float, dict[str, float]]:
        cutoff = now - timedelta(hours=24)
        spent = float(
            self.db.scalar(
                select(func.coalesce(func.sum(Asset.cost_usd), 0.0)).where(
                    Asset.created_at >= cutoff
                )
            )
            or 0.0
        )
        outstanding: dict[str, float] = {}
        for job in jobs:
            self._ensure_budget_reservation(job, now=now)
            reserved = self._reservation_value(
                job,
                BUDGET_RESERVED_USD_KEY,
            ) or 0.0
            baseline = self._reservation_value(
                job,
                BUDGET_ACTUAL_BASELINE_USD_KEY,
            ) or 0.0
            actual_delta = max(
                0.0,
                self._episode_actual_cost(job.episode_id) - baseline,
            )
            outstanding[job.id] = max(0.0, reserved - actual_delta)
        return spent, outstanding

    def _daily_limit(self) -> float:
        return float(
            getattr(
                self.settings,
                "max_daily_estimated_cost_usd",
                self.settings.max_estimated_cost_usd_per_episode,
            )
        )

    def _raise_daily_budget_error(
        self,
        *,
        spent: float,
        reserved: float,
        incremental: float,
    ) -> None:
        daily_limit = self._daily_limit()
        raise RuntimeError(
            f"Rolling 24-hour spend ${spent:.2f}, active reservations "
            f"${reserved:.2f}, plus estimated ${incremental:.2f} exceeds "
            f"MAX_DAILY_ESTIMATED_COST_USD=${daily_limit:.2f}"
        )

    def assert_daily_budget(
        self,
        incremental_cost: float,
        *,
        reservation_job: Job | None = None,
    ) -> None:
        """Check rolling spend plus atomic reservations.

        The web process uses this as a user-facing preflight. ``enqueue`` repeats
        the check while durably creating the reservation, so this method alone
        is never the concurrency boundary. A worker passes its active job to
        avoid counting the same reserved provider spend twice.
        """

        incremental = max(0.0, float(incremental_cost))
        now = datetime.now(timezone.utc)
        with self._daily_budget_lock():
            jobs = self._expire_stale_jobs(
                self._active_pipeline_jobs(),
                now=now,
                exclude_job_id=reservation_job.id if reservation_job else None,
            )
            if (
                reservation_job is not None
                and reservation_job.status
                in {JobStatus.PENDING, JobStatus.RUNNING}
                and all(job.id != reservation_job.id for job in jobs)
            ):
                jobs.append(reservation_job)
            spent, outstanding = self._daily_budget_commitment(
                jobs,
                now=now,
            )
            own_outstanding = (
                outstanding.get(reservation_job.id, 0.0)
                if reservation_job is not None
                else 0.0
            )
            unreserved_incremental = max(
                0.0,
                incremental - own_outstanding,
            )
            reserved_total = sum(outstanding.values())
            if (
                spent + reserved_total + unreserved_incremental
                > self._daily_limit() + 1e-9
            ):
                self._raise_daily_budget_error(
                    spent=spent,
                    reserved=reserved_total,
                    incremental=unreserved_incremental,
                )
            if reservation_job is not None:
                self._increase_budget_reservation(
                    reservation_job,
                    additional=unreserved_incremental,
                    now=now,
                )

    @staticmethod
    def _content_kind(kind: str) -> str:
        if kind not in {"lyrics", "storyboard"}:
            raise ValueError("Content kind must be 'lyrics' or 'storyboard'")
        return kind

    def _content_fingerprint(self, episode: Episode, kind: str) -> str | None:
        kind = self._content_kind(kind)
        if kind == "lyrics":
            if not episode.lyrics_text:
                return None
            payload = episode.lyrics_text.encode("utf-8")
        else:
            if not episode.storyboard_json:
                return None
            # Bind the storyboard approval to the exact lyrics it visualizes.
            payload = json.dumps(
                {
                    "lyrics": episode.lyrics_text or "",
                    "storyboard": episode.storyboard_json,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _clear_content_approvals(
        self,
        episode: Episode,
        *kinds: str,
    ) -> None:
        concept = dict(episode.concept_json or {})
        approvals = dict(concept.get(CONTENT_APPROVALS_KEY) or {})
        for kind in kinds:
            approvals.pop(self._content_kind(kind), None)
        if approvals:
            concept[CONTENT_APPROVALS_KEY] = approvals
        else:
            concept.pop(CONTENT_APPROVALS_KEY, None)
        episode.concept_json = concept

    def content_is_approved(self, episode: Episode, kind: str) -> bool:
        kind = self._content_kind(kind)
        fingerprint = self._content_fingerprint(episode, kind)
        if fingerprint is None:
            return False
        asset_kind = (
            AssetKind.LYRICS if kind == "lyrics" else AssetKind.STORYBOARD
        )
        if not self.has_valid_asset(episode, asset_kind):
            return False
        approval = (
            (episode.concept_json or {})
            .get(CONTENT_APPROVALS_KEY, {})
            .get(kind)
        )
        return (
            isinstance(approval, dict)
            and approval.get("fingerprint") == fingerprint
        )

    def approve_content(self, episode: Episode, kind: str) -> str:
        """Approve the exact current lyrics or storyboard revision."""

        kind = self._content_kind(kind)
        self._lock_episode(episode)
        active = self.active_job(episode)
        if active is not None:
            self.db.rollback()
            raise ActiveJobError(
                f"Cannot approve {kind} while job {active.id} "
                f"is {active.status.value}"
            )
        asset_kind = (
            AssetKind.LYRICS if kind == "lyrics" else AssetKind.STORYBOARD
        )
        fingerprint = self._content_fingerprint(episode, kind)
        if fingerprint is None or not self.has_valid_asset(episode, asset_kind):
            self.db.rollback()
            raise ReferenceChangeConflictError(
                f"Cannot approve missing or invalid {kind}"
            )
        if kind == "storyboard" and not self.content_is_approved(
            episode, "lyrics"
        ):
            self.db.rollback()
            raise ReferenceChangeConflictError(
                "Approve the current lyrics before the storyboard"
            )

        concept = dict(episode.concept_json or {})
        approvals = dict(concept.get(CONTENT_APPROVALS_KEY) or {})
        approvals[kind] = {
            "fingerprint": fingerprint,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        concept[CONTENT_APPROVALS_KEY] = approvals
        episode.concept_json = concept
        self.db.commit()
        return fingerprint

    def _draft_change_artifacts(self, episode: Episode) -> list[Path]:
        asset_root = self.settings.asset_dir / episode.id
        render_root = self.settings.render_dir / episode.id
        paths: list[Path] = []
        if asset_root.is_dir():
            paths.extend(asset_root.glob("music-v*.mp3*"))
            scene_root = asset_root / "scenes"
            if scene_root.is_dir():
                paths.extend(path for path in scene_root.iterdir() if path.is_file())
        if render_root.is_dir():
            paths.extend(path for path in render_root.iterdir() if path.is_file())
        return paths

    def update_lyrics_draft(self, episode: Episode, text: str) -> Asset:
        """Replace an unpaid lyrics draft and invalidate its free storyboard."""

        lyrics = text.strip()
        if not lyrics:
            raise ValueError("Lyrics draft cannot be empty")

        self._lock_episode(episode)
        active = self.active_job(episode)
        if active is not None:
            self.db.rollback()
            raise ActiveJobError(
                f"Cannot edit lyrics while job {active.id} "
                f"is {active.status.value}"
            )

        assets = list(
            self.db.scalars(
                select(Asset).where(Asset.episode_id == episode.id)
            )
        )
        blocking_assets = [
            asset
            for asset in assets
            if asset.cost_usd > 0
            or asset.kind not in EDITABLE_DRAFT_ASSET_KINDS
        ]
        orphaned_downstream = self._draft_change_artifacts(episode)
        if blocking_assets or orphaned_downstream:
            self.db.rollback()
            details = (
                ", ".join(sorted({asset.kind.value for asset in blocking_assets}))
                or ", ".join(path.name for path in orphaned_downstream[:3])
            )
            raise ReferenceChangeConflictError(
                "Cannot edit lyrics after paid or downstream production exists"
                + (f": {details}" if details else "")
            )

        replaced_assets = [
            asset
            for asset in assets
            if asset.kind in {AssetKind.LYRICS, AssetKind.STORYBOARD}
        ]
        replaced_paths = {Path(asset.path) for asset in replaced_assets}
        for asset in replaced_assets:
            self.db.delete(asset)

        episode.lyrics_text = lyrics
        episode.storyboard_json = []
        episode.qc_json = {}
        self._clear_content_approvals(episode, "lyrics", "storyboard")
        title, description, tags = publish_metadata(episode)
        episode.publish_title = title
        episode.publish_description = description
        episode.publish_tags = tags

        path = (
            self._asset_episode_dir(episode)
            / f"lyrics-draft-{uuid.uuid4().hex}.txt"
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as draft:
            draft.write(lyrics)
            draft.flush()
            self._copy_completed_file(Path(draft.name), path)
        asset = self._asset(
            episode,
            kind=AssetKind.LYRICS,
            path=path,
            mime_type="text/plain",
            provider="user-edited-draft",
            selected=True,
        )
        episode.status = EpisodeStatus.LYRICS_READY
        self._update_actual_cost(episode)
        self.db.commit()
        for old_path in replaced_paths:
            if old_path != path:
                old_path.unlink(missing_ok=True)
        return asset

    def generate_lyrics(self, episode: Episode) -> None:
        if episode.lyrics_text and self._valid_assets(episode, AssetKind.LYRICS):
            episode.status = EpisodeStatus.LYRICS_READY
            self.db.commit()
            return
        current_assets = list(
            self.db.scalars(
                select(Asset).where(Asset.episode_id == episode.id)
            )
        )
        blocking_assets = [
            asset
            for asset in current_assets
            if asset.cost_usd > 0
            or asset.kind not in EDITABLE_DRAFT_ASSET_KINDS
        ]
        if blocking_assets or self._draft_change_artifacts(episode):
            raise RuntimeError(
                "Cannot regenerate lyrics after paid or downstream production exists"
            )
        self._remove_assets(
            episode,
            {AssetKind.LYRICS, AssetKind.STORYBOARD},
        )
        self._clear_content_approvals(episode, "lyrics", "storyboard")
        episode.storyboard_json = []
        episode.qc_json = {}
        episode.lyrics_text = generate_lyrics(episode)
        title, description, tags = publish_metadata(episode)
        episode.publish_title = title
        episode.publish_description = description
        episode.publish_tags = tags
        path = self._asset_episode_dir(episode) / "lyrics.txt"
        path.write_text(episode.lyrics_text, encoding="utf-8")
        self._asset(episode, kind=AssetKind.LYRICS, path=path, mime_type="text/plain", provider="rule-guided-writer", selected=True)
        episode.status = EpisodeStatus.LYRICS_READY
        self.db.commit()

    def generate_music(self, episode: Episode) -> None:
        if not episode.lyrics_text:
            self.generate_lyrics(episode)
        if self.settings.provider_mode == "live":
            if not self.content_is_approved(episode, "lyrics"):
                raise RuntimeError("Approve the current lyrics before buying music")
            if not self.content_is_approved(episode, "storyboard"):
                raise RuntimeError(
                    "Approve the current storyboard before buying music"
                )
        # Re-evaluate legacy/live assets created before the arrangement gate.
        # A paid but voice-only asset stays in the ledger and is deselected,
        # allowing an explicit retry to use a new immutable output path.
        self._invalidate_unacceptable_music_assets(episode)
        self.assert_budget(episode)
        self.assert_daily_budget(
            self.estimate_music_cost(episode),
            reservation_job=self.active_job(episode),
        )
        variants = min(self.settings.max_music_variants, 2 if self.settings.provider_mode == "mock" else self.settings.max_music_variants)
        self._discard_invalid_assets(episode, AssetKind.MUSIC)
        existing = {asset.variant: asset for asset in self._valid_assets(episode, AssetKind.MUSIC)}
        provider_prompt = music_prompt(episode)
        for variant in range(1, variants + 1):
            if variant in existing:
                continue
            canonical = (
                self._asset_episode_dir(episode)
                / f"music-v{variant}.mp3"
            )
            path = self._generation_output_path(
                episode,
                kind=AssetKind.MUSIC,
                variant=variant,
                canonical=canonical,
            )
            if path.is_file() and path.stat().st_size > 1024:
                # The provider completed but the process stopped before the
                # ledger commit. Reuse the paid output instead of buying it twice.
                receipt: dict = {}
                if self.settings.provider_mode == "live":
                    receipt_path = music_receipt_path(path)
                    if not receipt_path.is_file():
                        raise RuntimeError(
                            f"Ambiguous paid music output without receipt: {path}. "
                            "Reconcile it manually before retrying."
                        )
                    try:
                        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise RuntimeError(f"Invalid music receipt: {receipt_path}") from exc
                    if receipt.get("state") != "complete":
                        raise RuntimeError(
                            "Paid music submission has an ambiguous outcome; "
                            f"reconcile it before retrying: {receipt_path}"
                        )
                    expected_fingerprint = music_request_fingerprint(
                        lyrics=episode.lyrics_text or "",
                        prompt=provider_prompt,
                        duration_seconds=episode.duration_seconds,
                        bpm=episode.bpm,
                        variant=variant,
                        model_id=self.settings.elevenlabs_music_model,
                        output_format=self.settings.elevenlabs_output_format,
                    )
                    if receipt.get("request_fingerprint") != expected_fingerprint:
                        raise RuntimeError(
                            f"Music receipt belongs to another request: {receipt_path}"
                        )
                result = MusicResult(
                    path=path,
                    provider=(
                        "recovered-elevenlabs-music"
                        if self.settings.provider_mode == "live"
                        else "recovered-mock-music"
                    ),
                    variant=variant,
                    duration_seconds=float(episode.duration_seconds),
                    cost_usd=float(receipt.get("estimated_cost_usd", 0.0)),
                    metadata={**receipt, "recovered_after_interruption": True},
                )
            else:
                # End any implicit read transaction before the paid HTTP call.
                self.db.commit()
                result = self.music_provider.generate(
                    lyrics=episode.lyrics_text or "",
                    prompt=provider_prompt,
                    duration_seconds=episode.duration_seconds,
                    bpm=episode.bpm,
                    output_path=path,
                    variant=variant,
                )
            quality_failure: dict | None = None
            if self.settings.provider_mode == "live":
                quality = music_arrangement_quality(result.path)
                result.metadata = {
                    **result.metadata,
                    "arrangement_qc": quality,
                }
                if not quality.get("passed"):
                    quality_failure = quality
                    result.metadata.update(
                        {
                            "invalidated_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                            "invalidation_reason": (
                                "insufficient_instrumental_arrangement"
                            ),
                        }
                    )
            # Lock before the paid Asset flush. This keeps the rolling spend
            # read and reservation consumption on one side of the same short
            # serialization boundary.
            with self._daily_budget_lock():
                self._asset(
                    episode, kind=AssetKind.MUSIC, path=result.path, mime_type="audio/mpeg", provider=result.provider,
                    variant=result.variant, selected=result.variant == 1 and quality_failure is None, duration_seconds=result.duration_seconds,
                    cost_usd=result.cost_usd, metadata=result.metadata,
                )
                self.db.commit()
            if quality_failure is not None:
                self._update_actual_cost(episode)
                self.db.commit()
                ratio = quality_failure.get("low_band_energy_ratio", 0.0)
                raise RuntimeError(
                    "ElevenLabs produced a paid audio file, but Nuvibù rejected "
                    "it because the instrumental backing is too sparse "
                    f"(low-band energy ratio {ratio:.6f}). The spend was "
                    "preserved in the ledger; an explicit music retry is safe."
                )
        valid_music_ids = {
            asset.id
            for asset in self._valid_assets(episode, AssetKind.MUSIC)
        }
        for asset in self._assets(episode, AssetKind.MUSIC):
            asset.selected = asset.variant == 1 and asset.id in valid_music_ids
        self._update_actual_cost(episode)
        episode.status = EpisodeStatus.MUSIC_READY
        self.db.commit()

    def generate_storyboard(self, episode: Episode) -> None:
        if episode.storyboard_json and self._valid_assets(episode, AssetKind.STORYBOARD):
            episode.status = EpisodeStatus.STORYBOARD_READY
            self.db.commit()
            return
        self._remove_assets(episode, {AssetKind.STORYBOARD})
        self._clear_content_approvals(episode, "storyboard")
        episode.storyboard_json = generate_storyboard(episode)
        path = self._asset_episode_dir(episode) / "storyboard.json"
        path.write_text(json.dumps(episode.storyboard_json, ensure_ascii=False, indent=2), encoding="utf-8")
        self._asset(episode, kind=AssetKind.STORYBOARD, path=path, mime_type="application/json", provider="rule-guided-storyboard", selected=True)
        episode.status = EpisodeStatus.STORYBOARD_READY
        self.db.commit()

    @staticmethod
    def explicit_reference_role(asset: Asset) -> str | None:
        role = str((asset.metadata_json or {}).get("reference_role", ""))
        role = LEGACY_REFERENCE_ROLE_ALIASES.get(role, role)
        return role if role in REFERENCE_ROLE_ORDER else None

    @staticmethod
    def official_emma_reference() -> Path:
        """Return the deterministic classic reference for legacy callers."""

        return get_emma_look(LEGACY_DEFAULT_LOOK_ID).reference_path

    def selected_emma_look_id(self, episode: Episode) -> str:
        """Resolve a pinned look, falling back only for pre-catalog episodes."""

        stored = (episode.concept_json or {}).get(EMMA_LOOK_ID_KEY)
        if stored is not None:
            return get_emma_look(str(stored)).id
        for asset in self.reference_pack_assets(episode):
            raw_role = str((asset.metadata_json or {}).get("reference_role", ""))
            look_id = (asset.metadata_json or {}).get(EMMA_LOOK_ID_KEY)
            if raw_role == "emma" and look_id is not None:
                return get_emma_look(str(look_id)).id
        return LEGACY_DEFAULT_LOOK_ID

    def selected_emma_look(self, episode: Episode) -> EmmaLook:
        return get_emma_look(self.selected_emma_look_id(episode))

    def reference_pack_assets(self, episode: Episode) -> list[Asset]:
        selected = [
            asset
            for asset in self._valid_assets(
                episode,
                AssetKind.CHARACTER_REFERENCE,
            )
            if asset.selected
        ]
        by_role: dict[str, Asset] = {}
        for asset in selected:
            role = self.explicit_reference_role(asset)
            if role is not None:
                by_role[role] = asset
        return [
            by_role[role]
            for role in REFERENCE_ROLE_ORDER
            if role in by_role
        ]

    def legacy_reference_asset(self, episode: Episode) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(
                    self._valid_assets(
                        episode,
                        AssetKind.CHARACTER_REFERENCE,
                    )
                )
                if asset.selected
                and self.explicit_reference_role(asset) is None
            ),
            None,
        )

    def reference_images(self, episode: Episode) -> list[Path]:
        assets = {
            self.explicit_reference_role(asset): asset
            for asset in self.reference_pack_assets(episode)
        }
        if assets:
            references: list[Path] = []
            for role in REFERENCE_ROLE_ORDER:
                if role == "emma":
                    emma_asset = assets.get(role)
                    raw_role = (
                        str(
                            (emma_asset.metadata_json or {}).get(
                                "reference_role",
                                "",
                            )
                        )
                        if emma_asset is not None
                        else ""
                    )
                    # Old packs used `nuvibu` for a cloud/hero image. Never
                    # send that retired image as Emma. Explicit `emma` rows,
                    # however, are frozen episode copies and must be honored.
                    if emma_asset is not None and raw_role == "emma":
                        references.append(Path(emma_asset.path))
                    else:
                        references.append(self.official_emma_reference())
                    continue
                asset = assets.get(role)
                if asset is not None:
                    references.append(Path(asset.path))
            return references
        legacy = self.legacy_reference_asset(episode)
        return [Path(legacy.path)] if legacy is not None else []

    def reference_pack_complete(self, episode: Episode) -> bool:
        roles = {
            self.explicit_reference_role(asset)
            for asset in self.reference_pack_assets(episode)
        }
        return roles == set(REFERENCE_ROLE_ORDER)

    def character_reference(self, episode: Episode) -> Path | None:
        """Backward-compatible accessor for the primary Emma reference."""

        references = self.reference_images(episode)
        return references[0] if references else None

    def reference_pack_mutable(self, episode: Episode) -> bool:
        """Return whether the pack can still be changed without invalidation."""

        if self.active_job(episode) is not None or episode.qc_json:
            return False
        dependent_asset = self.db.scalar(
            select(Asset.id)
            .where(
                Asset.episode_id == episode.id,
                Asset.kind.in_(REFERENCE_DEPENDENT_ASSET_KINDS),
            )
            .limit(1)
        )
        if dependent_asset is not None:
            return False
        scene_dir = self.settings.asset_dir / episode.id / "scenes"
        if scene_dir.is_dir() and any(scene_dir.iterdir()):
            return False
        render_dir = self.settings.render_dir / episode.id
        return not (render_dir.is_dir() and any(render_dir.iterdir()))

    def set_emma_look(self, episode: Episode, look_id: str) -> str:
        """Pin one allowlisted look and freeze its bytes when a pack exists."""

        look = get_emma_look(look_id)
        self._lock_episode(episode)
        active_job = self.active_job(episode)
        if active_job is not None:
            self.db.rollback()
            raise ActiveJobError(
                f"Cannot replace Emma's look while job {active_job.id} "
                f"is {active_job.status.value}"
            )
        if not self.reference_pack_mutable(episode):
            self.db.rollback()
            raise ReferenceChangeConflictError(
                "Cannot replace Emma's look after reference-dependent "
                "production has started"
            )
        assets = {
            self.explicit_reference_role(asset): asset
            for asset in self.reference_pack_assets(episode)
        }
        if set(assets) == set(REFERENCE_ROLE_ORDER):
            self.save_reference_pack(
                episode,
                {
                    "emma": look.reference_path,
                    "friends": Path(assets["friends"].path),
                    "world": Path(assets["world"].path),
                },
                emma_look_id=look.id,
                source_metadata={
                    role: {
                        key: value
                        for key in (
                            "reference_preset_id",
                            "source_sha256",
                        )
                        if (
                            value
                            := (assets[role].metadata_json or {}).get(key)
                        )
                        is not None
                    }
                    for role in ("friends", "world")
                },
            )
            return look.id

        concept = dict(episode.concept_json or {})
        concept[EMMA_LOOK_ID_KEY] = look.id
        episode.concept_json = concept
        self.db.commit()
        return look.id

    def _lock_episode(self, episode: Episode) -> None:
        locked_id = self.db.scalar(
            select(Episode.id)
            .where(Episode.id == episode.id)
            .with_for_update()
        )
        if locked_id is None:
            raise RuntimeError(f"Episode {episode.id} no longer exists")

    def active_job(self, episode: Episode) -> Job | None:
        return self.db.scalar(
            select(Job)
            .where(
                Job.episode_id == episode.id,
                Job.job_type == "pipeline",
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
            .order_by(Job.created_at.asc(), Job.id.asc())
            .limit(1)
        )

    def _active_job(self, episode: Episode) -> Job | None:
        """Backward-compatible private alias for older internal callers."""

        return self.active_job(episode)

    def _unresolved_paid_artifacts(self, episode: Episode) -> list[str]:
        """Find provider state that may already represent an unledgered charge."""

        if self.settings.provider_mode != "live":
            return []
        paid_kinds = {AssetKind.MUSIC, AssetKind.VIDEO_SCENE}
        recorded_paths = {
            str(Path(asset.path).resolve())
            for asset in self.db.scalars(
                select(Asset).where(
                    Asset.episode_id == episode.id,
                    Asset.kind.in_(paid_kinds),
                )
            )
        }
        unresolved: set[str] = set()
        episode_dir = self.settings.asset_dir / episode.id

        for receipt in episode_dir.glob("music-v*.mp3.receipt.json"):
            output = Path(
                str(receipt)[: -len(".receipt.json")]
            )
            if str(output.resolve()) in recorded_paths:
                continue
            # Both "submitting" and "complete" can represent a charge before
            # the Asset ledger commit. Malformed/unknown states fail closed.
            unresolved.add(str(receipt))

        scene_dir = episode_dir / "scenes"
        if scene_dir.is_dir():
            for sidecar in scene_dir.glob("scene-*.mp4.operation.json"):
                try:
                    state = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    unresolved.add(str(sidecar))
                    continue
                if state.get("state") != "terminal_error":
                    unresolved.add(str(sidecar))

        for pattern in ("music-v*.mp3", "scenes/scene-*.mp4"):
            for output in episode_dir.glob(pattern):
                try:
                    has_output = output.is_file() and output.stat().st_size > 0
                except OSError:
                    unresolved.add(str(output))
                    continue
                if (
                    has_output
                    and str(output.resolve()) not in recorded_paths
                ):
                    unresolved.add(str(output))
        return sorted(unresolved)

    def _job_step_is_durably_complete(
        self,
        episode: Episode,
        job: Job,
    ) -> bool:
        """Prove a stale job's authorized terminal step already committed.

        This permits the staged UI to advance after a worker dies between the
        output commit and its final Job status commit, while incomplete stale
        work remains restricted to an exact same-step recovery.
        """

        step = str((job.payload_json or {}).get("through_step", "qc"))
        if step == "lyrics":
            return bool(
                episode.lyrics_text
                and self.has_valid_asset(episode, AssetKind.LYRICS)
            )
        if step == "storyboard":
            return bool(
                episode.storyboard_json
                and self.has_valid_asset(episode, AssetKind.STORYBOARD)
            )
        if step == "music":
            variants = {
                asset.variant
                for asset in self._valid_assets(episode, AssetKind.MUSIC)
            }
            return all(
                variant in variants
                for variant in range(
                    1,
                    self.settings.max_music_variants + 1,
                )
            )
        if step == "scenes":
            if not episode.storyboard_json:
                return False
            expected = {
                int(scene["index"]) + 1
                for scene in episode.storyboard_json
            }
            actual = {
                asset.variant
                for asset in self._valid_assets(
                    episode,
                    AssetKind.VIDEO_SCENE,
                )
            }
            return bool(expected) and expected.issubset(actual)
        if step == "render":
            return all(
                any(asset.selected for asset in self._valid_assets(episode, kind))
                for kind in (
                    AssetKind.RENDER,
                    AssetKind.SHORT,
                    AssetKind.THUMBNAIL,
                )
            )
        if step == "qc":
            return bool(
                episode.qc_json
                and self.has_valid_asset(episode, AssetKind.REPORT)
            )
        return False

    def _status_after_reference_change(self, episode: Episode) -> EpisodeStatus:
        if self._valid_assets(episode, AssetKind.MUSIC):
            return EpisodeStatus.MUSIC_READY
        if episode.storyboard_json and self._valid_assets(
            episode, AssetKind.STORYBOARD
        ):
            return EpisodeStatus.STORYBOARD_READY
        if episode.lyrics_text and self._valid_assets(episode, AssetKind.LYRICS):
            return EpisodeStatus.LYRICS_READY
        return EpisodeStatus.DRAFT

    def _assert_no_veo_sidecars(self, episode: Episode) -> None:
        scene_dir = self.settings.asset_dir / episode.id / "scenes"
        if not scene_dir.is_dir():
            return
        # Include canonical resumable markers and archived failed-operation
        # receipts: both prove that Veo has already seen the old reference.
        for sidecar in scene_dir.glob("scene-*.mp4*.json"):
            try:
                state = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReferenceChangeConflictError(
                    "Cannot replace the character reference while a Veo operation "
                    f"receipt requires manual reconciliation: {sidecar}"
                ) from exc
            if state.get("state") != "terminal_error":
                raise ReferenceChangeConflictError(
                    "Cannot replace the character reference while a Veo operation "
                    f"is unresolved ({state.get('state', 'unknown')}): {sidecar}"
                )
            raise ReferenceChangeConflictError(
                "Cannot replace the character reference after a Veo operation "
                f"receipt exists: {sidecar}"
            )

    def _save_reference_sources(
        self,
        episode: Episode,
        sources: dict[str, Path],
        *,
        emma_look: EmmaLook | None = None,
        source_metadata: dict[str, dict[str, object]] | None = None,
    ) -> list[Asset]:
        if not sources or any(role not in REFERENCE_ROLE_ORDER for role in sources):
            raise ValueError("Unknown or empty reference role set")
        if source_metadata and any(
            role not in REFERENCE_ROLE_ORDER for role in source_metadata
        ):
            raise ValueError("Unknown reference source metadata role")
        self._lock_episode(episode)
        active_job = self.active_job(episode)
        if active_job is not None:
            self.db.rollback()
            raise ActiveJobError(
                f"Cannot replace the character reference while job {active_job.id} "
                f"is {active_job.status.value}"
            )

        # A Veo marker wins over the generic asset conflict because it may
        # represent an accepted but not yet reconciled provider charge.
        self._assert_no_veo_sidecars(episode)
        dependent_assets = list(
            self.db.scalars(
                select(Asset).where(
                    Asset.episode_id == episode.id,
                    Asset.kind.in_(REFERENCE_DEPENDENT_ASSET_KINDS),
                )
            )
        )
        scene_dir = self.settings.asset_dir / episode.id / "scenes"
        orphaned_scenes = (
            list(scene_dir.glob("scene-*.mp4")) if scene_dir.is_dir() else []
        )
        render_dir = self.settings.render_dir / episode.id
        orphaned_renders = (
            [path for path in render_dir.iterdir() if path.is_file()]
            if render_dir.is_dir()
            else []
        )
        if (
            dependent_assets
            or orphaned_scenes
            or orphaned_renders
            or episode.qc_json
        ):
            self.db.rollback()
            raise ReferenceChangeConflictError(
                "Cannot replace the character reference after "
                "reference-dependent production exists"
            )

        targets = list(
            self.db.scalars(
                select(Asset).where(
                    Asset.episode_id == episode.id,
                    Asset.kind == AssetKind.CHARACTER_REFERENCE,
                )
            )
        )
        old_reference_paths = {
            Path(asset.path)
            for asset in targets
            if asset.kind == AssetKind.CHARACTER_REFERENCE
        }
        destinations: dict[str, Path] = {}
        try:
            with tempfile.TemporaryDirectory(
                prefix="nuvibu-reference-",
            ) as staging_directory:
                staged: dict[str, Path] = {}
                for role in REFERENCE_ROLE_ORDER:
                    source = sources.get(role)
                    if source is None:
                        continue
                    staged_path = Path(staging_directory) / f"{role}.png"
                    with self._normalized_reference_image(
                        source,
                        role,
                    ) as image:
                        with Image.new(
                            "RGB",
                            (1280, 720),
                            "white",
                        ) as canvas:
                            image.thumbnail(
                                (1280, 720),
                                Image.Resampling.LANCZOS,
                            )
                            canvas.paste(
                                image,
                                (
                                    (1280 - image.width) // 2,
                                    (720 - image.height) // 2,
                                ),
                            )
                            canvas.save(staged_path, "PNG")
                    staged[role] = staged_path

                for role, staged_path in staged.items():
                    destination = (
                        self.settings.upload_dir
                        / episode.id
                        / f"reference-{role}-{uuid.uuid4().hex}.png"
                    )
                    destinations[role] = destination
                    self._copy_completed_file(
                        staged_path,
                        destination,
                    )
        except Exception:
            self.db.rollback()
            for destination in destinations.values():
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        try:
            for target in targets:
                self.db.delete(target)
            self.db.flush()

            def reference_metadata(role: str) -> dict[str, object]:
                metadata = dict((source_metadata or {}).get(role, {}))
                metadata.update(
                    {
                        "reference_role": role,
                        "reference_label": REFERENCE_ROLE_LABELS[role],
                        "stored_sha256": hashlib.sha256(
                            destinations[role].read_bytes()
                        ).hexdigest(),
                    }
                )
                if role == "emma" and emma_look is not None:
                    metadata.update(
                        {
                            EMMA_LOOK_ID_KEY: emma_look.id,
                            "emma_look_catalog_version": (
                                EMMA_LOOK_CATALOG_VERSION
                            ),
                            "source_sha256": emma_look.reference_sha256,
                        }
                    )
                return metadata

            assets = [
                self._asset(
                    episode,
                    kind=AssetKind.CHARACTER_REFERENCE,
                    path=destinations[role],
                    mime_type="image/png",
                    provider="user-approved-reference-pack",
                    variant=index,
                    selected=True,
                    width=1280,
                    height=720,
                    metadata=reference_metadata(role),
                )
                for index, role in enumerate(REFERENCE_ROLE_ORDER, start=1)
                if role in destinations
            ]
            if emma_look is not None:
                concept = dict(episode.concept_json or {})
                concept[EMMA_LOOK_ID_KEY] = emma_look.id
                episode.concept_json = concept
            episode.status = self._status_after_reference_change(episode)
            self._update_actual_cost(episode)
            self.db.commit()
        except Exception:
            self.db.rollback()
            for destination in destinations.values():
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        for old_path in old_reference_paths:
            if old_path in destinations.values():
                continue
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                # The database already points exclusively at the new reference;
                # an orphaned old image is harmless and can be cleaned later.
                pass
        return assets

    def save_reference_pack(
        self,
        episode: Episode,
        sources: dict[str, Path],
        *,
        emma_look_id: str | None = None,
        source_metadata: dict[str, dict[str, object]] | None = None,
    ) -> list[Asset]:
        if set(sources) != set(REFERENCE_ROLE_ORDER):
            missing = ", ".join(
                REFERENCE_ROLE_LABELS[role]
                for role in REFERENCE_ROLE_ORDER
                if role not in sources
            )
            raise ValueError(f"Reference pack incomplete: {missing}")
        look = get_emma_look(
            emma_look_id or self.selected_emma_look_id(episode)
        )
        trusted_sources = dict(sources)
        # Emma can only originate from the validated, bundled catalog.
        trusted_sources["emma"] = look.reference_path
        return self._save_reference_sources(
            episode,
            trusted_sources,
            emma_look=look,
            source_metadata=source_metadata,
        )

    def save_character_reference(self, episode: Episode, source: Path) -> Asset:
        """Store one legacy subject reference for older callers and tests."""

        return self._save_reference_sources(
            episode,
            {"emma": source},
        )[0]

    def generate_scenes(self, episode: Episode) -> None:
        if not episode.storyboard_json:
            self.generate_storyboard(episode)
        if (
            self.settings.provider_mode == "live"
            and not self.content_is_approved(episode, "storyboard")
        ):
            raise RuntimeError(
                "Approve the current storyboard before starting Veo"
            )
        references = self.reference_images(episode)
        if self.settings.provider_mode == "live" and not references:
            raise RuntimeError(
                "A valid selected character reference is required before "
                "starting Veo"
            )
        selected_look = self.selected_emma_look(episode)
        locked_emma_guard = emma_visual_guard(selected_look.outfit_prompt)
        emma_reference_sha256 = (
            hashlib.sha256(references[0].read_bytes()).hexdigest()
            if references
            else None
        )
        self.assert_budget(episode)
        self.assert_daily_budget(
            self._estimate_video_cost(episode, remaining_only=True),
            reservation_job=self.active_job(episode),
        )
        self._discard_invalid_assets(episode, AssetKind.VIDEO_SCENE)
        existing = {
            asset.variant: asset
            for asset in self._valid_assets(episode, AssetKind.VIDEO_SCENE)
        }
        scene_dir = self._asset_episode_dir(episode) / "scenes"
        scene_dir.mkdir(parents=True, exist_ok=True)
        for scene in episode.storyboard_json:
            index = int(scene["index"])
            variant = index + 1
            if variant in existing:
                continue
            path = self._generation_output_path(
                episode,
                kind=AssetKind.VIDEO_SCENE,
                variant=variant,
                canonical=scene_dir / f"scene-{index:03d}.mp4",
            )
            result: VideoResult | None = None
            last_error: Exception | None = None
            if is_valid_video(path):
                generation_duration = 8 if references else (
                    4 if int(scene["duration_seconds"]) <= 4
                    else 6 if int(scene["duration_seconds"]) <= 6
                    else 8
                )
                result = VideoResult(
                    path=path,
                    provider=f"recovered-google-veo-{self.settings.veo_backend}",
                    duration_seconds=float(generation_duration),
                    cost_usd=round(generation_duration * self._video_price_per_second(), 4),
                    metadata={
                        "model": self.settings.veo_model,
                        "recovered_after_interruption": True,
                        "planned_duration_seconds": int(scene["duration_seconds"]),
                    },
                )
            else:
                for attempt in range(self.settings.max_scene_retries + 1):
                    try:
                        # Avoid holding a Neon connection while a Veo operation
                        # can run for several minutes.
                        self.db.commit()
                        reference_context = (
                            "\nReference mapping: image 1 is the official Emma "
                            "character sheet, image 2 is the exact supporting "
                            "friend cast, and image 3 is the empty episode world. "
                            "Emma is the main protagonist in every shot. Preserve "
                            "identities, colors, wardrobe, materials and "
                            "environment exactly."
                            if len(references) == 3
                            else ""
                        )
                        result = self.video_provider.generate(
                            prompt=(
                                scene["prompt"]
                                + "\nNon-negotiable series rule: "
                                + locked_emma_guard
                                + reference_context
                            ),
                            duration_seconds=int(scene["duration_seconds"]),
                            output_path=path,
                            seed=173 + index,
                            reference_images=references,
                        )
                        result.metadata["attempt"] = attempt + 1
                        result.metadata["planned_duration_seconds"] = int(scene["duration_seconds"])
                        break
                    except Exception as exc:
                        last_error = exc
                if result is None:
                    raise RuntimeError(f"Scene {index} failed after retries: {last_error}") from last_error
            result.metadata["emma_look_id"] = selected_look.id
            result.metadata["emma_look_catalog_version"] = (
                EMMA_LOOK_CATALOG_VERSION
            )
            if emma_reference_sha256 is not None:
                result.metadata["emma_reference_sha256"] = (
                    emma_reference_sha256
                )
            with self._daily_budget_lock():
                self._asset(
                    episode, kind=AssetKind.VIDEO_SCENE, path=result.path, mime_type="video/mp4", provider=result.provider,
                    variant=variant, selected=True, duration_seconds=result.duration_seconds,
                    width=result.width, height=result.height, cost_usd=result.cost_usd, metadata=result.metadata,
                )
                self.db.commit()
        self._update_actual_cost(episode)
        episode.status = EpisodeStatus.SCENES_READY
        self.db.commit()

    def render_episode(self, episode: Episode) -> None:
        if not episode.storyboard_json:
            self.generate_storyboard(episode)
        completed_render_kinds = {
            kind: [a for a in self._valid_assets(episode, kind) if a.selected]
            for kind in (AssetKind.RENDER, AssetKind.SHORT, AssetKind.THUMBNAIL)
        }
        if all(completed_render_kinds.values()):
            episode.status = EpisodeStatus.RENDER_READY
            self.db.commit()
            return
        music = next((a for a in self._valid_assets(episode, AssetKind.MUSIC) if a.selected), None)
        scenes = sorted(
            [a for a in self._valid_assets(episode, AssetKind.VIDEO_SCENE) if a.selected],
            key=lambda a: a.variant,
        )
        if music is None:
            self.generate_music(episode)
            music = next(a for a in self._valid_assets(episode, AssetKind.MUSIC) if a.selected)
        expected_scene_variants = {
            int(scene["index"]) + 1 for scene in episode.storyboard_json
        }
        if {scene.variant for scene in scenes} != expected_scene_variants:
            self.generate_scenes(episode)
            scenes = sorted(
                [a for a in self._valid_assets(episode, AssetKind.VIDEO_SCENE) if a.selected],
                key=lambda a: a.variant,
            )
        if {scene.variant for scene in scenes} != expected_scene_variants:
            raise RuntimeError("Not all storyboard scenes are available for rendering")
        self._remove_assets(episode, {AssetKind.RENDER, AssetKind.SHORT, AssetKind.THUMBNAIL, AssetKind.REPORT})
        out_dir = self._render_episode_dir(episode)
        stem = episode.working_slug
        final_path = out_dir / f"{stem}.mp4"
        short_path = out_dir / f"{stem}-short.mp4"
        thumbnail_path = out_dir / f"{stem}-thumbnail.png"
        # FFmpeg seeks while finalizing MP4 files. Render locally, then copy the
        # completed artifacts to the shared Cloud Storage mount.
        with tempfile.TemporaryDirectory(prefix=f"nuvibu-{episode.id}-") as temp_dir:
            temp_root = Path(temp_dir)
            silent_temp = temp_root / f"{stem}-silent.mp4"
            final_temp = temp_root / final_path.name
            short_temp = temp_root / short_path.name
            thumbnail_temp = temp_root / thumbnail_path.name
            planned_durations = {
                int(scene["index"]) + 1: float(scene["duration_seconds"])
                for scene in episode.storyboard_json
            }
            concatenate_scenes(
                [Path(a.path) for a in scenes],
                silent_temp,
                episode.duration_seconds,
                scene_durations=[planned_durations[a.variant] for a in scenes],
            )
            mux_music(silent_temp, Path(music.path), final_temp, episode.duration_seconds)
            make_vertical_short(final_temp, short_temp, min(25, episode.duration_seconds))
            create_thumbnail(
                episode.title,
                thumbnail_temp,
                seed=len(episode.title),
                # This is still concept art, even when the episode video is live.
                preview_label=True,
            )
            for source, destination in (
                (final_temp, final_path),
                (short_temp, short_path),
                (thumbnail_temp, thumbnail_path),
            ):
                self._copy_completed_file(source, destination)
        self._asset(episode, kind=AssetKind.RENDER, path=final_path, mime_type="video/mp4", provider="ffmpeg", selected=True, duration_seconds=episode.duration_seconds, width=1280, height=720)
        self._asset(episode, kind=AssetKind.SHORT, path=short_path, mime_type="video/mp4", provider="ffmpeg", selected=True, duration_seconds=min(25, episode.duration_seconds), width=1080, height=1920)
        self._asset(episode, kind=AssetKind.THUMBNAIL, path=thumbnail_path, mime_type="image/png", provider="template-renderer", selected=True, width=1280, height=720)
        episode.status = EpisodeStatus.RENDER_READY
        self.db.commit()

    def run_qc(self, episode: Episode) -> None:
        if episode.qc_json and self._valid_assets(episode, AssetKind.REPORT):
            episode.status = (
                EpisodeStatus.QC_REVIEW
                if episode.qc_json.get("passed")
                else EpisodeStatus.FAILED
            )
            self.db.commit()
            return
        self._remove_assets(episode, {AssetKind.REPORT})
        result = review_episode(episode)
        episode.qc_json = result.to_dict()
        episode.status = EpisodeStatus.QC_REVIEW if result.passed else EpisodeStatus.FAILED
        path = self._render_episode_dir(episode) / "qc-report.json"
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self._asset(episode, kind=AssetKind.REPORT, path=path, mime_type="application/json", provider="deterministic-qc", selected=True, metadata=result.to_dict())
        self.db.commit()

    def run_through(
        self,
        episode: Episode,
        through_step: str = "qc",
        *,
        progress_job: Job | None = None,
    ) -> None:
        if through_step not in STEP_ORDER:
            raise ValueError(f"Unknown pipeline step: {through_step}")
        for step in STEP_ORDER[: STEP_ORDER.index(through_step) + 1]:
            if progress_job is not None:
                progress_job.result_json = {
                    **progress_job.result_json,
                    "current_step": step,
                    "pipeline_heartbeat_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }
                self.db.commit()
            if step == "lyrics": self.generate_lyrics(episode)
            elif step == "music": self.generate_music(episode)
            elif step == "storyboard": self.generate_storyboard(episode)
            elif step == "scenes": self.generate_scenes(episode)
            elif step == "render": self.render_episode(episode)
            elif step == "qc": self.run_qc(episode)
            if progress_job is not None:
                progress_job.result_json = {
                    **progress_job.result_json,
                    "completed_step": step,
                    "pipeline_heartbeat_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }
                self.db.commit()

    def enqueue(
        self,
        episode: Episode,
        through_step: str = "qc",
        *,
        estimated_incremental_cost: float | None = None,
        replace_existing_music: bool = False,
    ) -> Job:
        if through_step not in STEP_ORDER:
            raise ValueError(f"Unknown pipeline step: {through_step}")
        if replace_existing_music and through_step != "music":
            raise ValueError(
                "Existing music can only be replaced by a music job"
            )
        now = datetime.now(timezone.utc)
        job: Job | None = None
        try:
            with self._daily_budget_lock():
                # Serialize enqueue with reference/draft replacement. Acquiring
                # the SQLite process lock first also prevents a waiting session
                # from pinning an obsolete read snapshot.
                self._lock_episode(episode)
                requested = (
                    self.estimate_job_incremental_cost(episode, through_step)
                    if estimated_incremental_cost is None
                    else max(0.0, float(estimated_incremental_cost))
                )
                if replace_existing_music:
                    requested = max(
                        requested,
                        self.estimate_music_regeneration_cost(episode),
                    )
                active_jobs = self._expire_stale_jobs(
                    self._active_pipeline_jobs(),
                    now=now,
                )
                existing = next(
                    (
                        candidate
                        for candidate in active_jobs
                        if candidate.episode_id == episode.id
                        and candidate.job_type == "pipeline"
                    ),
                    None,
                )
                if existing is not None:
                    current_step = existing.payload_json.get(
                        "through_step",
                        "qc",
                    )
                    if current_step != through_step:
                        raise ActiveJobError(
                            f"Job {existing.id} is already active for "
                            f"{current_step}; cannot replace it with "
                            f"{through_step}"
                        )
                    self._ensure_budget_reservation(
                        existing,
                        now=now,
                        amount=requested,
                    )
                    spent, outstanding = self._daily_budget_commitment(
                        active_jobs,
                        now=now,
                    )
                    own_outstanding = outstanding.get(existing.id, 0.0)
                    additional = max(0.0, requested - own_outstanding)
                    reserved_total = sum(outstanding.values())
                    if (
                        spent + reserved_total + additional
                        > self._daily_limit() + 1e-9
                    ):
                        self._raise_daily_budget_error(
                            spent=spent,
                            reserved=reserved_total,
                            incremental=additional,
                        )
                    self._increase_budget_reservation(
                        existing,
                        additional=additional,
                        now=now,
                    )
                    self.db.commit()
                    return existing

                if replace_existing_music:
                    self.assert_budget(
                        episode,
                        additional_cost=requested,
                    )
                    self._invalidate_music_for_regeneration(episode)

                latest_job = self.db.scalar(
                    select(Job)
                    .where(
                        Job.episode_id == episode.id,
                        Job.job_type == "pipeline",
                    )
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .limit(1)
                )
                if (
                    latest_job is not None
                    and latest_job.status == JobStatus.FAILED
                    and (latest_job.result_json or {}).get(
                        "budget_reservation_release_reason"
                    )
                    == "stale"
                ):
                    recovery_step = (latest_job.payload_json or {}).get(
                        "through_step",
                        "qc",
                    )
                    if (
                        recovery_step != through_step
                        and not self._job_step_is_durably_complete(
                            episode,
                            latest_job,
                        )
                    ):
                        # Preserve the stale release but refuse to turn a retry
                        # into a broader, newly authorized production run.
                        self.db.commit()
                        raise ActiveJobError(
                            f"Stale job {latest_job.id} was authorized for "
                            f"{recovery_step}; recovery cannot upgrade it to "
                            f"{through_step}"
                        )

                spent, outstanding = self._daily_budget_commitment(
                    active_jobs,
                    now=now,
                )
                reserved_total = sum(outstanding.values())
                if (
                    spent + reserved_total + requested
                    > self._daily_limit() + 1e-9
                ):
                    self._raise_daily_budget_error(
                        spent=spent,
                        reserved=reserved_total,
                        incremental=requested,
                    )

                job = Job(
                    episode_id=episode.id,
                    job_type="pipeline",
                    status=JobStatus.PENDING,
                    payload_json={"through_step": through_step},
                )
                self.db.add(job)
                self.db.flush()
                self._set_budget_reservation(
                    job,
                    episode,
                    amount=requested,
                    through_step=through_step,
                    now=now,
                )
                self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(
                select(Job)
                .where(
                    Job.episode_id == episode.id,
                    Job.job_type == "pipeline",
                    Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                )
                .order_by(Job.created_at.asc(), Job.id.asc())
                .limit(1)
            )
            if existing is None:
                raise
            current_step = existing.payload_json.get("through_step", "qc")
            if current_step != through_step:
                raise ActiveJobError(
                    f"Job {existing.id} is already active for {current_step}; "
                    f"cannot replace it with {through_step}"
                )
            return existing
        except Exception:
            self.db.rollback()
            raise
        if job is None:
            raise RuntimeError("Pipeline job reservation did not create a job")
        self.db.refresh(job)
        return job

    def process_job(self, job: Job, *, already_claimed: bool = False) -> Job:
        episode = self.db.get(Episode, job.episode_id)
        if episode is None:
            job.status = JobStatus.FAILED
            job.error_text = "Episode not found"
            job.finished_at = datetime.now(timezone.utc)
            self._release_budget_reservation(
                job,
                reason="episode_missing",
                now=job.finished_at,
            )
            self.db.commit()
            raise RuntimeError(job.error_text)
        if already_claimed:
            if job.status != JobStatus.RUNNING:
                raise RuntimeError(f"Claimed job {job.id} is not running")
        else:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            job.attempt += 1
            self.db.commit()
        try:
            through_step = job.payload_json.get("through_step", "qc")
            self.assert_budget(episode)
            self.assert_daily_budget(
                self.estimate_job_incremental_cost(
                    episode,
                    through_step,
                ),
                reservation_job=job,
            )
            # Persist a backfilled reservation and release the transaction-level
            # advisory lock before any provider or FFmpeg work begins.
            self.db.commit()
            self.run_through(
                episode,
                through_step,
                progress_job=job,
            )
            job.status = JobStatus.SUCCEEDED
            finished_at = datetime.now(timezone.utc)
            result = dict(job.result_json or {})
            result.pop("provider_reconciliation_required", None)
            result.pop("retryable_provider_error_at", None)
            result["episode_status"] = episode.status.value
            job.result_json = result
            self._release_budget_reservation(
                job,
                reason="succeeded",
                now=finished_at,
            )
        except Exception as exc:
            # Provider/network failures can leave the Session in an unusable
            # transaction. Reconcile durable receipts/files only after rollback.
            job_id = job.id
            episode_id = episode.id
            self.db.rollback()
            job = self.db.get(Job, job_id)
            episode = self.db.get(Episode, episode_id)
            if job is None or episode is None:
                raise
            try:
                unresolved = self._unresolved_paid_artifacts(episode)
            except Exception as reconciliation_exc:
                # A storage read failure is itself ambiguous: retaining the
                # reservation is safer than permitting unrelated new spend.
                unresolved = [
                    f"provider-state-reconciliation-error: {reconciliation_exc}"
                ]
            failed_at = datetime.now(timezone.utc)
            self._update_actual_cost(episode)
            if unresolved:
                job.status = JobStatus.PENDING
                job.started_at = None
                job.finished_at = None
                job.error_text = str(exc)
                result = dict(job.result_json or {})
                result["provider_reconciliation_required"] = unresolved
                result["retryable_provider_error_at"] = failed_at.isoformat()
                job.result_json = result
            else:
                job.status = JobStatus.FAILED
                job.error_text = str(exc)
                episode.status = EpisodeStatus.FAILED
                job.finished_at = failed_at
                result = dict(job.result_json or {})
                result.pop("provider_reconciliation_required", None)
                result.pop("retryable_provider_error_at", None)
                job.result_json = result
                self._release_budget_reservation(
                    job,
                    reason="failed",
                    now=failed_at,
                )
            self.db.commit()
            raise
        job.finished_at = finished_at
        self._update_actual_cost(episode)
        self.db.commit()
        return job
