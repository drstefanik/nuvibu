from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Asset, AssetKind, Episode, JobStatus
from app.providers.elevenlabs import music_receipt_path
from app.services.pipeline import (
    BUDGET_ACTUAL_BASELINE_USD_KEY,
    BUDGET_RESERVED_AT_KEY,
    BUDGET_RESERVED_USD_KEY,
    STEP_ORDER,
    ActiveJobError,
    PipelineService,
    ReferenceChangeConflictError,
)
from tests.test_pipeline import make_episode


def make_session(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pipeline-safety.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_settings(
    tmp_path: Path,
    *,
    provider_mode: str = "mock",
) -> Settings:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'pipeline-safety.db'}",
        storage_root=tmp_path / "storage",
        provider_mode=provider_mode,
        veo_backend="vertex",
        veo_model="veo-3.1-generate-001",
        max_music_variants=1,
        max_scene_retries=0,
    )
    settings.ensure_directories()
    return settings


def test_pipeline_order_keeps_free_storyboard_before_paid_music(
    tmp_path: Path, monkeypatch
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        calls: list[str] = []
        method_names = {
            "lyrics": "generate_lyrics",
            "storyboard": "generate_storyboard",
            "music": "generate_music",
            "scenes": "generate_scenes",
            "render": "render_episode",
            "qc": "run_qc",
        }
        for step, method_name in method_names.items():
            monkeypatch.setattr(
                service,
                method_name,
                lambda _episode, step=step: calls.append(step),
            )

        service.run_through(episode, "qc")

        assert STEP_ORDER == [
            "lyrics",
            "storyboard",
            "music",
            "scenes",
            "render",
            "qc",
        ]
        assert calls == STEP_ORDER


def test_pending_job_is_idempotent_only_for_the_same_requested_step(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)

        first = service.enqueue(episode, "lyrics")
        same = service.enqueue(episode, "lyrics")

        assert same.id == first.id
        assert same.payload_json["through_step"] == "lyrics"
        assert service.active_job(episode).id == first.id
        with pytest.raises(ActiveJobError, match="cannot replace.*storyboard"):
            service.enqueue(episode, "storyboard")
        db.refresh(first)
        assert first.status == JobStatus.PENDING
        assert first.payload_json["through_step"] == "lyrics"


def test_approvals_are_bound_to_exact_lyrics_and_storyboard(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        service.generate_lyrics(episode)
        service.generate_storyboard(episode)

        assert not service.content_is_approved(episode, "lyrics")
        assert not service.content_is_approved(episode, "storyboard")
        with pytest.raises(RuntimeError, match="lyrics before the storyboard"):
            service.approve_content(episode, "storyboard")

        service.approve_content(episode, "lyrics")
        service.approve_content(episode, "storyboard")
        assert service.content_is_approved(episode, "lyrics")
        assert service.content_is_approved(episode, "storyboard")

        episode.lyrics_text = f"{episode.lyrics_text}\nUna nuova riga"
        assert not service.content_is_approved(episode, "lyrics")
        assert not service.content_is_approved(episode, "storyboard")


def test_updating_unpaid_lyrics_invalidates_storyboard_and_approvals(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        service.generate_lyrics(episode)
        service.generate_storyboard(episode)
        service.approve_content(episode, "lyrics")
        service.approve_content(episode, "storyboard")
        old_storyboard_paths = {
            Path(asset.path)
            for asset in episode.assets
            if asset.kind == AssetKind.STORYBOARD
        }

        updated = service.update_lyrics_draft(
            episode,
            "Nuvibù saluta piano\nPoi torna a cantare",
        )

        assert Path(updated.path).is_file()
        assert episode.lyrics_text.startswith("Nuvibù saluta")
        assert episode.storyboard_json == []
        assert not service.has_valid_asset(episode, AssetKind.STORYBOARD)
        assert not service.content_is_approved(episode, "lyrics")
        assert not service.content_is_approved(episode, "storyboard")
        assert all(not path.exists() for path in old_storyboard_paths)


def test_lyrics_update_rejects_active_job_or_downstream_spend(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        service.generate_lyrics(episode)
        job = service.enqueue(episode, "lyrics")
        with pytest.raises(ActiveJobError, match="Cannot edit lyrics"):
            service.update_lyrics_draft(episode, "Testo modificato")
        job.status = JobStatus.CANCELLED
        db.commit()

        music = settings.asset_dir / episode.id / "music-v1.mp3"
        music.write_bytes(b"paid music")
        paid = Asset(
            episode=episode,
            kind=AssetKind.MUSIC,
            provider="test",
            path=str(music),
            mime_type="audio/mpeg",
            selected=True,
            cost_usd=0.5,
        )
        db.add(paid)
        db.commit()

        with pytest.raises(RuntimeError, match="paid or downstream"):
            service.update_lyrics_draft(episode, "Altro testo")
        assert db.get(Asset, paid.id) is not None
        assert music.exists()


def test_live_paid_stages_require_current_content_approvals(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        service.generate_lyrics(episode)
        service.generate_storyboard(episode)

        with pytest.raises(RuntimeError, match="Approve the current lyrics"):
            service.generate_music(episode)
        with pytest.raises(RuntimeError, match="Approve the current storyboard"):
            service.generate_scenes(episode)

        service.approve_content(episode, "lyrics")
        with pytest.raises(RuntimeError, match="Approve the current storyboard"):
            service.generate_music(episode)


def test_live_scene_generation_requires_valid_selected_reference(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        service.generate_lyrics(episode)
        service.generate_storyboard(episode)
        service.approve_content(episode, "lyrics")
        service.approve_content(episode, "storyboard")

        with pytest.raises(
            RuntimeError,
            match="valid selected character reference",
        ):
            service.generate_scenes(episode)

        assert not service.has_valid_asset(episode, AssetKind.VIDEO_SCENE)


def test_cost_helpers_count_only_missing_provider_assets(
    tmp_path: Path, monkeypatch
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        reference = tmp_path / "reference.png"
        Image.new("RGB", (32, 32), "blue").save(reference)
        db.add(
            Asset(
                episode=episode,
                kind=AssetKind.CHARACTER_REFERENCE,
                provider="test",
                path=str(reference),
                mime_type="image/png",
                selected=True,
            )
        )
        db.commit()
        service = PipelineService(db, settings)

        assert service.estimate_music_cost(episode) == 0.04
        assert service.estimate_cost(episode) == 3.24
        assert service.estimate_remaining_cost(episode) == 3.24

        music = tmp_path / "music.mp3"
        music.write_bytes(b"music")
        db.add(
            Asset(
                episode=episode,
                kind=AssetKind.MUSIC,
                provider="test",
                path=str(music),
                mime_type="audio/mpeg",
                variant=1,
                selected=True,
                cost_usd=0.04,
            )
        )
        db.commit()
        assert service.estimate_music_cost(episode) == 0.0
        assert service.estimate_remaining_cost(episode) == 3.2

        monkeypatch.setattr(
            service,
            "_asset_file_is_valid",
            lambda asset: Path(asset.path).is_file(),
        )
        episode.storyboard_json = [
            {"index": 0, "duration_seconds": 8},
            {"index": 1, "duration_seconds": 8},
        ]
        scene = tmp_path / "scene.mp4"
        scene.write_bytes(b"video")
        db.add(
            Asset(
                episode=episode,
                kind=AssetKind.VIDEO_SCENE,
                provider="test",
                path=str(scene),
                mime_type="video/mp4",
                variant=1,
                selected=True,
                cost_usd=1.6,
            )
        )
        db.commit()
        assert service.estimate_remaining_cost(episode) == 1.6


def test_episode_cap_includes_historical_paid_ledger_and_replacement(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    object.__setattr__(
        settings,
        "max_estimated_cost_usd_per_episode",
        4.0,
    )
    object.__setattr__(settings, "max_daily_estimated_cost_usd", 100.0)
    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        historical = Asset(
            episode=episode,
            kind=AssetKind.MUSIC,
            provider="test",
            path=str(tmp_path / "missing-historical-music.mp3"),
            mime_type="audio/mpeg",
            variant=1,
            selected=False,
            cost_usd=1.0,
            metadata_json={
                "invalidated_at": (
                    datetime.now(timezone.utc) - timedelta(hours=25)
                ).isoformat(),
                "invalidation_reason": "missing_or_invalid_media",
            },
            created_at=datetime.now(timezone.utc) - timedelta(hours=25),
        )
        db.add(historical)
        db.commit()
        service = PipelineService(db, settings)

        assert service.estimate_cost(episode) == 3.24
        assert service.estimate_remaining_cost(episode) == 3.24
        service.assert_daily_budget(3.24)
        with pytest.raises(RuntimeError, match="Projected episode cost"):
            service.assert_budget(episode)

        assert episode.actual_cost_usd == pytest.approx(1.0)
        assert episode.estimated_cost_usd == pytest.approx(4.24)


def test_daily_budget_uses_rolling_asset_spend(tmp_path: Path):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    object.__setattr__(settings, "max_daily_estimated_cost_usd", 10.0)
    with Session() as db:
        recent_episode = make_episode()
        recent_episode.working_slug = "recent"
        old_episode = make_episode()
        old_episode.working_slug = "old"
        db.add_all([recent_episode, old_episode])
        db.commit()
        db.add_all(
            [
                Asset(
                    episode=recent_episode,
                    kind=AssetKind.MUSIC,
                    provider="test",
                    path=str(tmp_path / "recent.mp3"),
                    mime_type="audio/mpeg",
                    cost_usd=8.0,
                    created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                ),
                Asset(
                    episode=old_episode,
                    kind=AssetKind.MUSIC,
                    provider="test",
                    path=str(tmp_path / "old.mp3"),
                    mime_type="audio/mpeg",
                    cost_usd=100.0,
                    created_at=datetime.now(timezone.utc) - timedelta(hours=25),
                ),
            ]
        )
        db.commit()
        service = PipelineService(db, settings)

        service.assert_daily_budget(2.0)
        with pytest.raises(RuntimeError, match="Rolling 24-hour spend"):
            service.assert_daily_budget(2.01)


def test_enqueue_reserves_daily_budget_across_episodes_and_releases_on_failure(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    object.__setattr__(settings, "max_daily_estimated_cost_usd", 5.0)
    with Session() as db:
        first_episode = make_episode()
        first_episode.working_slug = "first-reservation"
        second_episode = make_episode()
        second_episode.working_slug = "second-reservation"
        db.add_all([first_episode, second_episode])
        db.commit()
        service = PipelineService(db, settings)

        first = service.enqueue(
            first_episode,
            "music",
            estimated_incremental_cost=4.0,
        )

        assert first.payload_json[BUDGET_RESERVED_USD_KEY] == 4.0
        assert first.payload_json[BUDGET_ACTUAL_BASELINE_USD_KEY] == 0.0
        assert first.payload_json[BUDGET_RESERVED_AT_KEY]
        with pytest.raises(RuntimeError, match="active reservations"):
            service.enqueue(
                second_episode,
                "music",
                estimated_incremental_cost=2.0,
            )

        first.status = JobStatus.FAILED
        db.commit()
        second = service.enqueue(
            second_episode,
            "music",
            estimated_incremental_cost=4.0,
        )
        assert second.status == JobStatus.PENDING


def test_paid_asset_consumes_reservation_without_double_counting(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    object.__setattr__(settings, "max_daily_estimated_cost_usd", 5.0)
    with Session() as db:
        first_episode = make_episode()
        first_episode.working_slug = "reservation-consumer"
        second_episode = make_episode()
        second_episode.working_slug = "reservation-follower"
        db.add_all([first_episode, second_episode])
        db.commit()
        service = PipelineService(db, settings)
        first = service.enqueue(
            first_episode,
            "music",
            estimated_incremental_cost=4.0,
        )
        paid_music = tmp_path / "paid.mp3"
        paid_music.write_bytes(b"paid")
        db.add(
            Asset(
                episode=first_episode,
                kind=AssetKind.MUSIC,
                provider="test",
                path=str(paid_music),
                mime_type="audio/mpeg",
                cost_usd=4.0,
            )
        )
        db.commit()

        second = service.enqueue(
            second_episode,
            "music",
            estimated_incremental_cost=1.0,
        )

        assert first.status == JobStatus.PENDING
        assert second.status == JobStatus.PENDING
        with pytest.raises(RuntimeError, match="active reservations"):
            service.assert_daily_budget(0.01)


def test_invalid_paid_asset_remains_an_immutable_spend_ledger_row(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    object.__setattr__(settings, "max_daily_estimated_cost_usd", 5.0)
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        missing = settings.asset_dir / episode.id / "music-v1.mp3"
        paid = Asset(
            episode=episode,
            kind=AssetKind.MUSIC,
            provider="test",
            path=str(missing),
            mime_type="audio/mpeg",
            selected=True,
            cost_usd=4.0,
        )
        db.add(paid)
        db.commit()
        missing.parent.mkdir(parents=True, exist_ok=True)
        music_receipt_path(missing).write_text(
            '{"state":"complete","request_fingerprint":"historical"}',
            encoding="utf-8",
        )
        service = PipelineService(db, settings)

        service._discard_invalid_assets(episode, AssetKind.MUSIC)

        db.refresh(paid)
        assert db.get(Asset, paid.id) is paid
        assert paid.selected is False
        invalidated_at = paid.metadata_json["invalidated_at"]
        assert paid.metadata_json["invalidation_reason"] == (
            "missing_or_invalid_media"
        )
        missing.write_bytes(b"later replacement at the same path")
        assert service._asset_file_is_valid(paid) is False
        missing.unlink()
        service._discard_invalid_assets(episode, AssetKind.MUSIC)
        db.refresh(paid)
        assert paid.metadata_json["invalidated_at"] == invalidated_at

        service.assert_daily_budget(1.0)
        with pytest.raises(RuntimeError, match="cost-bearing asset ledger"):
            service._remove_assets(episode, {AssetKind.MUSIC})
        assert db.get(Asset, paid.id) is paid

        replacement = service._generation_output_path(
            episode,
            kind=AssetKind.MUSIC,
            variant=1,
            canonical=missing,
        )
        assert replacement.name == "music-v1-retry-2.mp3"
        replacement.write_bytes(b"replacement")
        assert service._unresolved_paid_artifacts(episode)
        new_asset = Asset(
            episode=episode,
            kind=AssetKind.MUSIC,
            provider="test",
            path=str(replacement),
            mime_type="audio/mpeg",
            variant=1,
            selected=True,
            cost_usd=1.0,
        )
        db.add(new_asset)
        db.commit()
        assert service._unresolved_paid_artifacts(episode) == []
        with pytest.raises(RuntimeError, match="Rolling 24-hour spend"):
            service.assert_daily_budget(0.01)


def test_concurrent_cross_episode_enqueues_cannot_oversubscribe_sqlite_budget(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    object.__setattr__(settings, "max_daily_estimated_cost_usd", 5.0)
    with Session() as db:
        episodes = [make_episode(), make_episode()]
        episodes[0].working_slug = "concurrent-one"
        episodes[1].working_slug = "concurrent-two"
        db.add_all(episodes)
        db.commit()
        episode_ids = [episode.id for episode in episodes]

    barrier = Barrier(2)

    def reserve(episode_id: str) -> str:
        with Session() as db:
            episode = db.get(Episode, episode_id)
            assert episode is not None
            barrier.wait()
            try:
                PipelineService(db, settings).enqueue(
                    episode,
                    "music",
                    estimated_incremental_cost=4.0,
                )
            except RuntimeError:
                return "rejected"
            return "reserved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, episode_ids))

    assert sorted(results) == ["rejected", "reserved"]


def test_paid_ledger_commit_cannot_split_budget_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path, provider_mode="live")
    object.__setattr__(settings, "max_daily_estimated_cost_usd", 5.0)
    with Session() as db:
        producer = make_episode()
        producer.working_slug = "serialized-producer"
        contender = make_episode()
        contender.working_slug = "serialized-contender"
        db.add_all([producer, contender])
        db.commit()
        producer_id = producer.id
        contender_id = contender.id
        PipelineService(db, settings).enqueue(
            producer,
            "music",
            estimated_incremental_cost=4.0,
        )

    start_writer = Event()
    writer_waiting = Event()

    def commit_paid_asset() -> None:
        with Session() as db:
            episode = db.get(Episode, producer_id)
            assert episode is not None
            paid_path = tmp_path / "serialized-paid.mp3"
            paid_path.write_bytes(b"paid")
            start_writer.wait(timeout=2)
            writer_waiting.set()
            service = PipelineService(db, settings)
            with service._daily_budget_lock():
                db.add(
                    Asset(
                        episode=episode,
                        kind=AssetKind.MUSIC,
                        provider="test",
                        path=str(paid_path),
                        mime_type="audio/mpeg",
                        cost_usd=4.0,
                    )
                )
                db.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(commit_paid_asset)
        with Session() as db:
            contender = db.get(Episode, contender_id)
            assert contender is not None
            service = PipelineService(db, settings)
            original_actual_cost = service._episode_actual_cost

            def pause_between_snapshot_queries(episode_id: str) -> float:
                start_writer.set()
                assert writer_waiting.wait(timeout=2)
                return original_actual_cost(episode_id)

            monkeypatch.setattr(
                service,
                "_episode_actual_cost",
                pause_between_snapshot_queries,
            )
            with pytest.raises(RuntimeError, match="active reservations"):
                service.enqueue(
                    contender,
                    "music",
                    estimated_incremental_cost=2.0,
                )
        writer.result(timeout=2)

    with Session() as db:
        assert (
            db.scalar(
                select(Asset).where(Asset.episode_id == producer_id)
            )
            is not None
        )
        assert (
            db.scalar(
                select(Asset).where(Asset.episode_id == contender_id)
            )
            is None
        )


def test_reference_can_change_before_video_and_preserves_music_spend(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        old_reference = tmp_path / "old.png"
        replacement = tmp_path / "replacement.png"
        Image.new("RGB", (32, 32), "blue").save(old_reference)
        Image.new("RGB", (32, 32), "red").save(replacement)
        music = tmp_path / "music.mp3"
        music.write_bytes(b"music")
        original = Asset(
            episode=episode,
            kind=AssetKind.CHARACTER_REFERENCE,
            provider="test",
            path=str(old_reference),
            mime_type="image/png",
            selected=True,
        )
        paid = Asset(
            episode=episode,
            kind=AssetKind.MUSIC,
            provider="test",
            path=str(music),
            mime_type="audio/mpeg",
            selected=True,
            cost_usd=0.5,
        )
        db.add_all([original, paid])
        db.commit()

        new_reference = PipelineService(
            db, settings
        ).save_character_reference(episode, replacement)

        assert Path(new_reference.path).exists()
        assert not old_reference.exists()
        assert db.get(Asset, paid.id) is not None
        assert music.exists()
        assert episode.actual_cost_usd == pytest.approx(0.5)


def test_reference_change_is_blocked_by_dependent_asset_without_deleting_it(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = make_settings(tmp_path)
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        old_reference = tmp_path / "old.png"
        replacement = tmp_path / "replacement.png"
        scene = tmp_path / "scene.mp4"
        Image.new("RGB", (32, 32), "blue").save(old_reference)
        Image.new("RGB", (32, 32), "red").save(replacement)
        scene.write_bytes(b"paid scene")
        original = Asset(
            episode=episode,
            kind=AssetKind.CHARACTER_REFERENCE,
            provider="test",
            path=str(old_reference),
            mime_type="image/png",
            selected=True,
        )
        paid_scene = Asset(
            episode=episode,
            kind=AssetKind.VIDEO_SCENE,
            provider="veo",
            path=str(scene),
            mime_type="video/mp4",
            selected=True,
            cost_usd=1.6,
        )
        db.add_all([original, paid_scene])
        db.commit()

        with pytest.raises(
            ReferenceChangeConflictError,
            match="reference-dependent production",
        ):
            PipelineService(db, settings).save_character_reference(
                episode, replacement
            )

        assert db.get(Asset, original.id) is not None
        assert db.get(Asset, paid_scene.id) is not None
        assert old_reference.exists()
        assert scene.exists()
