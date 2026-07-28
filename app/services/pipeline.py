from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..media import is_valid_video
from ..models import Asset, AssetKind, Episode, EpisodeStatus, Job, JobStatus
from ..providers import get_music_provider, get_video_provider
from ..providers.base import MusicProvider, MusicResult, VideoProvider, VideoResult
from ..providers.elevenlabs import music_receipt_path, music_request_fingerprint
from ..providers.veo import veo_price_per_second
from .prompts import generate_lyrics, generate_storyboard, music_prompt, publish_metadata
from .render import concatenate_scenes, create_thumbnail, make_vertical_short, mux_music
from .safety import review_episode


STEP_ORDER = ["lyrics", "music", "storyboard", "scenes", "render", "qc"]
REFERENCE_DEPENDENT_ASSET_KINDS = {
    AssetKind.VIDEO_SCENE,
    AssetKind.RENDER,
    AssetKind.SHORT,
    AssetKind.THUMBNAIL,
    AssetKind.REPORT,
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

    def _remove_assets(self, episode: Episode, kinds: set[AssetKind]) -> None:
        targets = list(
            self.db.scalars(
                select(Asset).where(
                    Asset.episode_id == episode.id,
                    Asset.kind.in_(kinds),
                )
            )
        )
        removable_paths = [
            Path(asset.path)
            for asset in targets
            if asset.kind != AssetKind.CHARACTER_REFERENCE
        ]
        for asset in targets:
            self.db.delete(asset)
        # Commit database truth before touching storage. A crash can then leave
        # only an unreferenced file, never a row pointing to a deleted object.
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
    def _asset_file_is_valid(asset: Asset) -> bool:
        path = Path(asset.path)
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
        except OSError:
            return False
        if asset.mime_type.startswith("video/") or path.suffix.lower() == ".mp4":
            return is_valid_video(path)
        return True

    def _valid_assets(self, episode: Episode, kind: AssetKind) -> list[Asset]:
        return [
            asset
            for asset in self._assets(episode, kind)
            if self._asset_file_is_valid(asset)
        ]

    def _discard_invalid_assets(self, episode: Episode, kind: AssetKind) -> None:
        invalid = [
            asset
            for asset in self._assets(episode, kind)
            if not self._asset_file_is_valid(asset)
        ]
        if not invalid:
            return
        for asset in invalid:
            self.db.delete(asset)
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

    def estimate_cost(self, episode: Episode) -> float:
        if self.settings.provider_mode == "mock":
            return 0.0
        music = self.settings.max_music_variants * (episode.duration_seconds / 60) * 0.15
        price_per_second = self._video_price_per_second()
        storyboard = episode.storyboard_json or generate_storyboard(episode)
        uses_subject_reference = self.character_reference(episode) is not None
        generated_seconds = sum(
            8
            if uses_subject_reference
            else 4
            if int(scene["duration_seconds"]) <= 4
            else 6
            if int(scene["duration_seconds"]) <= 6
            else 8
            for scene in storyboard
        )
        # Reserve the full configured retry ceiling before the first paid request.
        video = generated_seconds * price_per_second * (self.settings.max_scene_retries + 1)
        return round(music + video, 2)

    def _video_price_per_second(self) -> float:
        return veo_price_per_second(self.settings.veo_backend, self.settings.veo_model)

    def assert_budget(self, episode: Episode) -> None:
        estimate = self.estimate_cost(episode)
        episode.estimated_cost_usd = estimate
        if estimate > self.settings.max_estimated_cost_usd_per_episode:
            raise RuntimeError(
                f"Estimated cost ${estimate:.2f} exceeds MAX_ESTIMATED_COST_USD_PER_EPISODE="
                f"${self.settings.max_estimated_cost_usd_per_episode:.2f}"
            )

    def generate_lyrics(self, episode: Episode) -> None:
        if episode.lyrics_text and self._valid_assets(episode, AssetKind.LYRICS):
            episode.status = EpisodeStatus.LYRICS_READY
            self.db.commit()
            return
        self._remove_assets(episode, {AssetKind.LYRICS})
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
        self.assert_budget(episode)
        variants = min(self.settings.max_music_variants, 2 if self.settings.provider_mode == "mock" else self.settings.max_music_variants)
        self._discard_invalid_assets(episode, AssetKind.MUSIC)
        existing = {asset.variant: asset for asset in self._valid_assets(episode, AssetKind.MUSIC)}
        provider_prompt = music_prompt(episode)
        for variant in range(1, variants + 1):
            if variant in existing:
                continue
            path = self._asset_episode_dir(episode) / f"music-v{variant}.mp3"
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
            self._asset(
                episode, kind=AssetKind.MUSIC, path=result.path, mime_type="audio/mpeg", provider=result.provider,
                variant=result.variant, selected=result.variant == 1, duration_seconds=result.duration_seconds,
                cost_usd=result.cost_usd, metadata=result.metadata,
            )
            self.db.commit()
        for asset in self._assets(episode, AssetKind.MUSIC):
            asset.selected = asset.variant == 1
        self._update_actual_cost(episode)
        episode.status = EpisodeStatus.MUSIC_READY
        self.db.commit()

    def generate_storyboard(self, episode: Episode) -> None:
        if episode.storyboard_json and self._valid_assets(episode, AssetKind.STORYBOARD):
            episode.status = EpisodeStatus.STORYBOARD_READY
            self.db.commit()
            return
        self._remove_assets(episode, {AssetKind.STORYBOARD})
        episode.storyboard_json = generate_storyboard(episode)
        path = self._asset_episode_dir(episode) / "storyboard.json"
        path.write_text(json.dumps(episode.storyboard_json, ensure_ascii=False, indent=2), encoding="utf-8")
        self._asset(episode, kind=AssetKind.STORYBOARD, path=path, mime_type="application/json", provider="rule-guided-storyboard", selected=True)
        episode.status = EpisodeStatus.STORYBOARD_READY
        self.db.commit()

    def character_reference(self, episode: Episode) -> Path | None:
        asset = next(
            (a for a in self._valid_assets(episode, AssetKind.CHARACTER_REFERENCE) if a.selected),
            None,
        )
        return Path(asset.path) if asset else None

    def _lock_episode(self, episode: Episode) -> None:
        locked_id = self.db.scalar(
            select(Episode.id)
            .where(Episode.id == episode.id)
            .with_for_update()
        )
        if locked_id is None:
            raise RuntimeError(f"Episode {episode.id} no longer exists")

    def _active_job(self, episode: Episode) -> Job | None:
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

    def _status_after_reference_change(self, episode: Episode) -> EpisodeStatus:
        if episode.storyboard_json and self._valid_assets(
            episode, AssetKind.STORYBOARD
        ):
            return EpisodeStatus.STORYBOARD_READY
        if self._valid_assets(episode, AssetKind.MUSIC):
            return EpisodeStatus.MUSIC_READY
        if episode.lyrics_text and self._valid_assets(episode, AssetKind.LYRICS):
            return EpisodeStatus.LYRICS_READY
        return EpisodeStatus.DRAFT

    def _archive_scene_operation_sidecars(self, episode: Episode) -> None:
        scene_dir = self.settings.asset_dir / episode.id / "scenes"
        if not scene_dir.is_dir():
            return
        for sidecar in scene_dir.glob("scene-*.mp4.operation.json"):
            try:
                state = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReferenceChangeConflictError(
                    "Cannot replace the character reference while a Veo operation "
                    f"receipt requires manual reconciliation: {sidecar}"
                ) from exc
            terminal_receipt_is_complete = (
                state.get("state") == "terminal_error"
                and isinstance(state.get("operation_name"), str)
                and bool(state.get("operation_name"))
                and isinstance(state.get("request_fingerprint"), str)
                and bool(state.get("request_fingerprint"))
                and isinstance(state.get("error"), str)
                and bool(state.get("error"))
            )
            if not terminal_receipt_is_complete:
                raise ReferenceChangeConflictError(
                    "Cannot replace the character reference while a Veo operation "
                    f"is unresolved ({state.get('state', 'unknown')}): {sidecar}"
                )
            archive = sidecar.with_name(
                f"{sidecar.name}.superseded.{uuid.uuid4().hex}.json"
            )
            os.replace(sidecar, archive)

    def save_character_reference(self, episode: Episode, source: Path) -> Asset:
        self._lock_episode(episode)
        active_job = self._active_job(episode)
        if active_job is not None:
            self.db.rollback()
            raise ActiveJobError(
                f"Cannot replace the character reference while job {active_job.id} "
                f"is {active_job.status.value}"
            )

        destination = (
            self.settings.upload_dir
            / episode.id
            / f"character-reference-{uuid.uuid4().hex}.png"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".png") as processed:
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                canvas = Image.new("RGB", (1280, 720), "white")
                image.thumbnail((1280, 720), Image.Resampling.LANCZOS)
                canvas.paste(
                    image,
                    ((1280 - image.width) // 2, (720 - image.height) // 2),
                )
                canvas.save(processed.name, "PNG")
            self._copy_completed_file(Path(processed.name), destination)

        targets = list(
            self.db.scalars(
                select(Asset).where(
                    Asset.episode_id == episode.id,
                    Asset.kind.in_(
                        {
                            AssetKind.CHARACTER_REFERENCE,
                            *REFERENCE_DEPENDENT_ASSET_KINDS,
                        }
                    ),
                )
            )
        )
        old_reference_paths = {
            Path(asset.path)
            for asset in targets
            if asset.kind == AssetKind.CHARACTER_REFERENCE
            and Path(asset.path) != destination
        }
        downstream_paths = {
            Path(asset.path)
            for asset in targets
            if asset.kind in REFERENCE_DEPENDENT_ASSET_KINDS
        }
        scene_dir = self.settings.asset_dir / episode.id / "scenes"
        if scene_dir.is_dir():
            downstream_paths.update(scene_dir.glob("scene-*.mp4"))
        render_dir = self.settings.render_dir / episode.id
        downstream_paths.update(
            {
                render_dir / f"{episode.working_slug}.mp4",
                render_dir / f"{episode.working_slug}-short.mp4",
                render_dir / f"{episode.working_slug}-thumbnail.png",
                render_dir / "qc-report.json",
            }
        )

        try:
            # An operation started for the old reference cannot be resumed with
            # the new fingerprint. Preserve its receipt for audit, but clear the
            # canonical sidecar so the explicit reference change can regenerate.
            self._archive_scene_operation_sidecars(episode)
            # Remove canonical media before committing the ledger invalidation:
            # a crash may leave a missing-file row, which is safely repairable,
            # but can never leave an old scene available for orphan recovery.
            for path in downstream_paths:
                path.unlink(missing_ok=True)
            for target in targets:
                self.db.delete(target)
            self.db.flush()
            asset = self._asset(
                episode,
                kind=AssetKind.CHARACTER_REFERENCE,
                path=destination,
                mime_type="image/png",
                provider="user-approved-reference",
                selected=True,
                width=1280,
                height=720,
            )
            episode.qc_json = {}
            episode.status = self._status_after_reference_change(episode)
            self._update_actual_cost(episode)
            self.db.commit()
        except Exception:
            self.db.rollback()
            destination.unlink(missing_ok=True)
            raise

        for old_path in old_reference_paths:
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                # The database already points exclusively at the new reference;
                # an orphaned old image is harmless and can be cleaned later.
                pass
        return asset

    def generate_scenes(self, episode: Episode) -> None:
        if not episode.storyboard_json:
            self.generate_storyboard(episode)
        self.assert_budget(episode)
        self._discard_invalid_assets(episode, AssetKind.VIDEO_SCENE)
        existing = {
            asset.variant: asset
            for asset in self._valid_assets(episode, AssetKind.VIDEO_SCENE)
        }
        scene_dir = self._asset_episode_dir(episode) / "scenes"
        scene_dir.mkdir(parents=True, exist_ok=True)
        reference = self.character_reference(episode)
        for scene in episode.storyboard_json:
            index = int(scene["index"])
            variant = index + 1
            if variant in existing:
                continue
            path = scene_dir / f"scene-{index:03d}.mp4"
            result: VideoResult | None = None
            last_error: Exception | None = None
            if is_valid_video(path):
                generation_duration = 8 if reference else (
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
                        result = self.video_provider.generate(
                            prompt=scene["prompt"], duration_seconds=int(scene["duration_seconds"]), output_path=path,
                            seed=173 + index, reference_image=reference,
                        )
                        result.metadata["attempt"] = attempt + 1
                        result.metadata["planned_duration_seconds"] = int(scene["duration_seconds"])
                        break
                    except Exception as exc:
                        last_error = exc
                if result is None:
                    raise RuntimeError(f"Scene {index} failed after retries: {last_error}") from last_error
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
                }
                self.db.commit()

    def enqueue(self, episode: Episode, through_step: str = "qc") -> Job:
        if through_step not in STEP_ORDER:
            raise ValueError(f"Unknown pipeline step: {through_step}")
        # Serialize enqueue with reference replacement. This guarantees that a
        # new pipeline cannot begin between the active-job check and invalidation.
        self._lock_episode(episode)
        existing = self._active_job(episode)
        if existing is not None:
            if existing.status == JobStatus.RUNNING:
                cutoff = datetime.now(timezone.utc) - timedelta(
                    seconds=self.settings.job_stale_after_seconds
                )
                started_at = existing.started_at
                if started_at is not None and started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                if started_at is None or started_at < cutoff:
                    existing.status = JobStatus.FAILED
                    existing.finished_at = datetime.now(timezone.utc)
                    existing.error_text = (
                        "Worker execution became stale; a resumable replacement job was created"
                    )
                    self.db.flush()
                    existing = None
            if existing is not None:
                if existing.status == JobStatus.PENDING:
                    current_step = existing.payload_json.get("through_step", "qc")
                    if STEP_ORDER.index(through_step) > STEP_ORDER.index(current_step):
                        existing.payload_json = {
                            **existing.payload_json,
                            "through_step": through_step,
                        }
                self.db.commit()
                return existing
        job = Job(episode_id=episode.id, job_type="pipeline", status=JobStatus.PENDING, payload_json={"through_step": through_step})
        self.db.add(job)
        try:
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
            return existing
        self.db.refresh(job)
        return job

    def process_job(self, job: Job, *, already_claimed: bool = False) -> Job:
        episode = self.db.get(Episode, job.episode_id)
        if episode is None:
            job.status = JobStatus.FAILED
            job.error_text = "Episode not found"
            job.finished_at = datetime.now(timezone.utc)
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
            self.run_through(
                episode,
                job.payload_json.get("through_step", "qc"),
                progress_job=job,
            )
            job.status = JobStatus.SUCCEEDED
            job.result_json = {
                **job.result_json,
                "episode_status": episode.status.value,
            }
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error_text = str(exc)
            episode.status = EpisodeStatus.FAILED
            job.finished_at = datetime.now(timezone.utc)
            self.db.commit()
            raise
        job.finished_at = datetime.now(timezone.utc)
        self.db.commit()
        return job
