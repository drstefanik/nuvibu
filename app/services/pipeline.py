from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Asset, AssetKind, Episode, EpisodeStatus, Job, JobStatus
from ..providers import get_music_provider, get_video_provider
from ..providers.base import MusicResult, VideoResult
from .prompts import generate_lyrics, generate_storyboard, music_prompt, publish_metadata
from .render import concatenate_scenes, create_thumbnail, make_vertical_short, mux_music
from .safety import review_episode


STEP_ORDER = ["lyrics", "music", "storyboard", "scenes", "render", "qc"]


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
        self.music_provider = get_music_provider(settings)
        self.video_provider = get_video_provider(settings)

    def _asset_episode_dir(self, episode: Episode) -> Path:
        path = self.settings.asset_dir / episode.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _render_episode_dir(self, episode: Episode) -> Path:
        path = self.settings.render_dir / episode.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _remove_assets(self, episode: Episode, kinds: set[AssetKind]) -> None:
        targets = [asset for asset in list(episode.assets) if asset.kind in kinds]
        for asset in targets:
            if asset.kind != AssetKind.CHARACTER_REFERENCE:
                Path(asset.path).unlink(missing_ok=True)
            self.db.delete(asset)
        self.db.flush()

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
        video = episode.duration_seconds * 0.03 * 1.5
        return round(music + video, 2)

    def assert_budget(self, episode: Episode) -> None:
        estimate = self.estimate_cost(episode)
        episode.estimated_cost_usd = estimate
        if estimate > self.settings.max_estimated_cost_usd_per_episode:
            raise RuntimeError(
                f"Estimated cost ${estimate:.2f} exceeds MAX_ESTIMATED_COST_USD_PER_EPISODE="
                f"${self.settings.max_estimated_cost_usd_per_episode:.2f}"
            )

    def generate_lyrics(self, episode: Episode) -> None:
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
        self._remove_assets(episode, {AssetKind.MUSIC})
        variants = min(self.settings.max_music_variants, 2 if self.settings.provider_mode == "mock" else self.settings.max_music_variants)
        results: list[MusicResult] = []
        for variant in range(1, variants + 1):
            path = self._asset_episode_dir(episode) / f"music-v{variant}.mp3"
            result = self.music_provider.generate(
                lyrics=episode.lyrics_text or "",
                prompt=music_prompt(episode),
                duration_seconds=episode.duration_seconds,
                bpm=episode.bpm,
                output_path=path,
                variant=variant,
            )
            results.append(result)
        for result in results:
            self._asset(
                episode, kind=AssetKind.MUSIC, path=result.path, mime_type="audio/mpeg", provider=result.provider,
                variant=result.variant, selected=result.variant == 1, duration_seconds=result.duration_seconds,
                cost_usd=result.cost_usd, metadata=result.metadata,
            )
        episode.actual_cost_usd = sum(a.cost_usd for a in episode.assets)
        episode.status = EpisodeStatus.MUSIC_READY
        self.db.commit()

    def generate_storyboard(self, episode: Episode) -> None:
        self._remove_assets(episode, {AssetKind.STORYBOARD})
        episode.storyboard_json = generate_storyboard(episode)
        path = self._asset_episode_dir(episode) / "storyboard.json"
        path.write_text(json.dumps(episode.storyboard_json, ensure_ascii=False, indent=2), encoding="utf-8")
        self._asset(episode, kind=AssetKind.STORYBOARD, path=path, mime_type="application/json", provider="rule-guided-storyboard", selected=True)
        episode.status = EpisodeStatus.STORYBOARD_READY
        self.db.commit()

    def character_reference(self, episode: Episode) -> Path | None:
        asset = next((a for a in episode.assets if a.kind == AssetKind.CHARACTER_REFERENCE and a.selected and Path(a.path).exists()), None)
        return Path(asset.path) if asset else None

    def save_character_reference(self, episode: Episode, source: Path) -> Asset:
        self._remove_assets(episode, {AssetKind.CHARACTER_REFERENCE})
        destination = self.settings.upload_dir / episode.id / "character-reference.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            canvas = Image.new("RGB", (1280, 720), "white")
            image.thumbnail((1280, 720), Image.Resampling.LANCZOS)
            canvas.paste(image, ((1280-image.width)//2, (720-image.height)//2))
            canvas.save(destination, "PNG")
        asset = self._asset(
            episode, kind=AssetKind.CHARACTER_REFERENCE, path=destination, mime_type="image/png",
            provider="user-approved-reference", selected=True, width=1280, height=720,
        )
        self.db.commit()
        return asset

    def generate_scenes(self, episode: Episode) -> None:
        if not episode.storyboard_json:
            self.generate_storyboard(episode)
        self.assert_budget(episode)
        self._remove_assets(episode, {AssetKind.VIDEO_SCENE})
        scene_dir = self._asset_episode_dir(episode) / "scenes"
        scene_dir.mkdir(parents=True, exist_ok=True)
        reference = self.character_reference(episode)
        for scene in episode.storyboard_json:
            index = int(scene["index"])
            path = scene_dir / f"scene-{index:03d}.mp4"
            result: VideoResult | None = None
            last_error: Exception | None = None
            for attempt in range(self.settings.max_scene_retries + 1):
                try:
                    result = self.video_provider.generate(
                        prompt=scene["prompt"], duration_seconds=int(scene["duration_seconds"]), output_path=path,
                        seed=173 + index + attempt, reference_image=reference,
                    )
                    result.metadata["attempt"] = attempt + 1
                    break
                except Exception as exc:
                    last_error = exc
            if result is None:
                raise RuntimeError(f"Scene {index} failed after retries: {last_error}") from last_error
            self._asset(
                episode, kind=AssetKind.VIDEO_SCENE, path=result.path, mime_type="video/mp4", provider=result.provider,
                variant=index + 1, selected=True, duration_seconds=result.duration_seconds,
                width=result.width, height=result.height, cost_usd=result.cost_usd, metadata=result.metadata,
            )
        episode.actual_cost_usd = sum(a.cost_usd for a in episode.assets)
        episode.status = EpisodeStatus.SCENES_READY
        self.db.commit()

    def render_episode(self, episode: Episode) -> None:
        music = next((a for a in episode.assets if a.kind == AssetKind.MUSIC and a.selected and Path(a.path).exists()), None)
        scenes = sorted(
            [a for a in episode.assets if a.kind == AssetKind.VIDEO_SCENE and a.selected and Path(a.path).exists()],
            key=lambda a: a.variant,
        )
        if music is None:
            self.generate_music(episode)
            music = next(a for a in episode.assets if a.kind == AssetKind.MUSIC and a.selected)
        if not scenes:
            self.generate_scenes(episode)
            scenes = sorted([a for a in episode.assets if a.kind == AssetKind.VIDEO_SCENE and a.selected], key=lambda a: a.variant)
        self._remove_assets(episode, {AssetKind.RENDER, AssetKind.SHORT, AssetKind.THUMBNAIL, AssetKind.REPORT})
        out_dir = self._render_episode_dir(episode)
        stem = episode.working_slug
        silent_path = out_dir / f"{stem}-silent.mp4"
        final_path = out_dir / f"{stem}.mp4"
        short_path = out_dir / f"{stem}-short.mp4"
        thumbnail_path = out_dir / f"{stem}-thumbnail.png"
        concatenate_scenes([Path(a.path) for a in scenes], silent_path, episode.duration_seconds)
        mux_music(silent_path, Path(music.path), final_path, episode.duration_seconds)
        make_vertical_short(final_path, short_path, min(25, episode.duration_seconds))
        create_thumbnail(episode.title, thumbnail_path, seed=len(episode.title))
        silent_path.unlink(missing_ok=True)
        self._asset(episode, kind=AssetKind.RENDER, path=final_path, mime_type="video/mp4", provider="ffmpeg", selected=True, duration_seconds=episode.duration_seconds, width=1280, height=720)
        self._asset(episode, kind=AssetKind.SHORT, path=short_path, mime_type="video/mp4", provider="ffmpeg", selected=True, duration_seconds=min(25, episode.duration_seconds), width=1080, height=1920)
        self._asset(episode, kind=AssetKind.THUMBNAIL, path=thumbnail_path, mime_type="image/png", provider="template-renderer", selected=True, width=1280, height=720)
        episode.status = EpisodeStatus.RENDER_READY
        self.db.commit()

    def run_qc(self, episode: Episode) -> None:
        result = review_episode(episode)
        episode.qc_json = result.to_dict()
        episode.status = EpisodeStatus.QC_REVIEW if result.passed else EpisodeStatus.FAILED
        path = self._render_episode_dir(episode) / "qc-report.json"
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self._remove_assets(episode, {AssetKind.REPORT})
        self._asset(episode, kind=AssetKind.REPORT, path=path, mime_type="application/json", provider="deterministic-qc", selected=True, metadata=result.to_dict())
        self.db.commit()

    def run_through(self, episode: Episode, through_step: str = "qc") -> None:
        if through_step not in STEP_ORDER:
            raise ValueError(f"Unknown pipeline step: {through_step}")
        for step in STEP_ORDER[: STEP_ORDER.index(through_step) + 1]:
            if step == "lyrics": self.generate_lyrics(episode)
            elif step == "music": self.generate_music(episode)
            elif step == "storyboard": self.generate_storyboard(episode)
            elif step == "scenes": self.generate_scenes(episode)
            elif step == "render": self.render_episode(episode)
            elif step == "qc": self.run_qc(episode)

    def enqueue(self, episode: Episode, through_step: str = "qc") -> Job:
        job = Job(episode_id=episode.id, job_type="pipeline", status=JobStatus.PENDING, payload_json={"through_step": through_step})
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def process_job(self, job: Job) -> Job:
        episode = self.db.get(Episode, job.episode_id)
        if episode is None:
            job.status = JobStatus.FAILED
            job.error_text = "Episode not found"
            self.db.commit()
            return job
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        job.attempt += 1
        self.db.commit()
        try:
            self.run_through(episode, job.payload_json.get("through_step", "qc"))
            job.status = JobStatus.SUCCEEDED
            job.result_json = {"episode_status": episode.status.value}
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error_text = str(exc)
            episode.status = EpisodeStatus.FAILED
        job.finished_at = datetime.now(timezone.utc)
        self.db.commit()
        return job
