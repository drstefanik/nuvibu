from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.media import is_valid_video
from app.models import Asset, AssetKind, EpisodeStatus, Job, JobStatus
from app.services.pipeline import (
    ActiveJobError,
    PipelineService,
    ReferenceChangeConflictError,
)
from tests.test_pipeline import make_episode


def make_session(tmp_path: Path):
    database_path = tmp_path / "media-integrity.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'media-integrity.db'}",
        storage_root=tmp_path / "storage",
        provider_mode="mock",
    )
    settings.ensure_directories()
    return settings


def write_png(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path, "PNG")


def test_video_validation_rejects_large_corrupt_and_truncated_mp4(tmp_path: Path):
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"x" * 4096)
    assert is_valid_video(corrupt) is False

    complete = tmp_path / "complete.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(complete),
        ],
        check=True,
    )
    assert is_valid_video(complete) is True

    truncated = tmp_path / "truncated.mp4"
    content = complete.read_bytes()
    truncated.write_bytes(content[: len(content) // 2])
    assert truncated.stat().st_size > 1024
    assert is_valid_video(truncated) is False


def test_asset_validation_and_orphan_recovery_reject_corrupt_mp4(tmp_path: Path):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        corrupt = settings.asset_dir / episode.id / "scenes" / "scene-000.mp4"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_bytes(b"x" * 4096)
        db.add(
            Asset(
                episode=episode,
                kind=AssetKind.VIDEO_SCENE,
                provider="interrupted-download",
                path=str(corrupt),
                mime_type="video/mp4",
                variant=1,
                selected=True,
            )
        )
        db.commit()
        service = PipelineService(db, settings)

        assert service._valid_assets(episode, AssetKind.VIDEO_SCENE) == []
        service._discard_invalid_assets(episode, AssetKind.VIDEO_SCENE)
        assert db.scalars(
            select(Asset).where(Asset.kind == AssetKind.VIDEO_SCENE)
        ).all() == []


def test_reference_change_is_blocked_while_pipeline_job_is_active(tmp_path: Path):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        old_reference = tmp_path / "old-reference.png"
        new_reference = tmp_path / "new-reference.png"
        write_png(old_reference, "blue")
        write_png(new_reference, "red")
        original = Asset(
            episode=episode,
            kind=AssetKind.CHARACTER_REFERENCE,
            provider="test",
            path=str(old_reference),
            mime_type="image/png",
            selected=True,
        )
        db.add_all(
            [
                original,
                Job(
                    episode=episode,
                    job_type="pipeline",
                    status=JobStatus.PENDING,
                    payload_json={"through_step": "qc"},
                ),
            ]
        )
        db.commit()

        with pytest.raises(ActiveJobError, match="Cannot replace"):
            PipelineService(db, settings).save_character_reference(
                episode, new_reference
            )

        references = db.scalars(
            select(Asset).where(Asset.kind == AssetKind.CHARACTER_REFERENCE)
        ).all()
        assert [asset.id for asset in references] == [original.id]
        assert old_reference.exists()


def test_reference_change_preserves_dependent_outputs_and_spend(tmp_path: Path):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode(16)
        episode.status = EpisodeStatus.QC_REVIEW
        episode.storyboard_json = [{"index": 0, "duration_seconds": 8}]
        episode.qc_json = {"passed": True, "score": 100}
        db.add(episode)
        db.commit()

        files: dict[AssetKind, Path] = {}
        for kind, suffix in (
            (AssetKind.LYRICS, ".txt"),
            (AssetKind.MUSIC, ".mp3"),
            (AssetKind.STORYBOARD, ".json"),
            (AssetKind.CHARACTER_REFERENCE, ".png"),
            (AssetKind.VIDEO_SCENE, ".mp4"),
            (AssetKind.RENDER, ".mp4"),
            (AssetKind.SHORT, ".mp4"),
            (AssetKind.THUMBNAIL, ".png"),
            (AssetKind.REPORT, ".json"),
        ):
            if kind == AssetKind.VIDEO_SCENE:
                path = (
                    settings.asset_dir
                    / episode.id
                    / "scenes"
                    / "scene-000.mp4"
                )
            else:
                path = tmp_path / f"{kind.value}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind in {AssetKind.CHARACTER_REFERENCE, AssetKind.THUMBNAIL}:
                write_png(path, "blue")
            else:
                path.write_bytes(b"x" * 4096)
            files[kind] = path
            db.add(
                Asset(
                    episode=episode,
                    kind=kind,
                    provider="test",
                    path=str(path),
                    mime_type=(
                        "video/mp4"
                        if kind
                        in {AssetKind.VIDEO_SCENE, AssetKind.RENDER, AssetKind.SHORT}
                        else "application/octet-stream"
                    ),
                    selected=True,
                    cost_usd=(
                        0.5
                        if kind == AssetKind.MUSIC
                        else 1.0
                        if kind == AssetKind.VIDEO_SCENE
                        else 0.0
                    ),
                )
            )
        episode.lyrics_text = "Canta con Nuvibù"
        db.commit()

        scene_sidecar = files[AssetKind.VIDEO_SCENE].with_name(
            f"{files[AssetKind.VIDEO_SCENE].name}.operation.json"
        )
        scene_sidecar.write_text(
            (
                '{"state":"terminal_error","operation_name":"operations/failed",'
                '"request_fingerprint":"old-reference-request",'
                '"error":"safety filter"}'
            ),
            encoding="utf-8",
        )
        replacement = tmp_path / "replacement.png"
        write_png(replacement, "red")

        before = {
            asset.id: (asset.kind, asset.cost_usd, asset.path)
            for asset in db.scalars(
                select(Asset).where(Asset.episode_id == episode.id)
            )
        }

        with pytest.raises(
            ReferenceChangeConflictError, match="operation.*receipt"
        ):
            PipelineService(db, settings).save_character_reference(
                episode, replacement
            )

        after = {
            asset.id: (asset.kind, asset.cost_usd, asset.path)
            for asset in db.scalars(
                select(Asset).where(Asset.episode_id == episode.id)
            )
        }
        assert after == before
        assert all(path.exists() for path in files.values())
        assert scene_sidecar.exists()
        assert not list(
            scene_sidecar.parent.glob(f"{scene_sidecar.name}.superseded.*")
        )
        assert episode.qc_json == {"passed": True, "score": 100}
        assert sum(value[1] for value in after.values()) == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("receipt", "error_pattern"),
    [
        (
            '{"state":"running","operation_name":"operations/paid"}',
            "unresolved",
        ),
        (
            '{"state":"submitting","request_fingerprint":"ambiguous"}',
            "unresolved",
        ),
        ("not-json", "manual reconciliation"),
    ],
)
def test_reference_change_blocks_unresolved_veo_operation(
    tmp_path: Path, receipt: str, error_pattern: str
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        old_reference = tmp_path / "old-reference.png"
        replacement = tmp_path / "replacement.png"
        write_png(old_reference, "blue")
        write_png(replacement, "red")
        scene = (
            settings.asset_dir
            / episode.id
            / "scenes"
            / "scene-000.mp4"
        )
        scene.parent.mkdir(parents=True, exist_ok=True)
        scene.write_bytes(b"x" * 4096)
        sidecar = scene.with_name(f"{scene.name}.operation.json")
        sidecar.write_text(receipt, encoding="utf-8")
        original = Asset(
            episode=episode,
            kind=AssetKind.CHARACTER_REFERENCE,
            provider="test",
            path=str(old_reference),
            mime_type="image/png",
            selected=True,
        )
        db.add_all(
            [
                original,
                Asset(
                    episode=episode,
                    kind=AssetKind.VIDEO_SCENE,
                    provider="test",
                    path=str(scene),
                    mime_type="video/mp4",
                    selected=True,
                ),
            ]
        )
        db.commit()

        with pytest.raises(
            ReferenceChangeConflictError, match=error_pattern
        ):
            PipelineService(db, settings).save_character_reference(
                episode, replacement
            )

        assert sidecar.exists()
        assert scene.exists()
        references = db.scalars(
            select(Asset).where(Asset.kind == AssetKind.CHARACTER_REFERENCE)
        ).all()
        assert [asset.id for asset in references] == [original.id]
