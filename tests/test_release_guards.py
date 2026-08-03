from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.emma_looks import CATALOG_VERSION, get_emma_look
from app.main import worker_dispatch_due
from app.media import (
    MUSIC_MIN_LOW_BAND_ENERGY_RATIO,
    _music_arrangement_metrics_from_samples,
)
from app.models import Asset, AssetKind, EpisodeStatus, Job, JobStatus
from app.providers.base import VideoResult
from app.providers.elevenlabs import (
    ElevenLabsMusicProvider,
    _music_vocal_quality,
    build_music_v2_composition_plan,
    music_receipt_path,
    music_request_fingerprint,
)
from app.services.pipeline import ActiveJobError, PipelineService
from app.services.prompts import music_prompt
from app.services.render import concatenate_scenes
from app.services.worker_dispatch import dispatch_worker_job
from scripts.run_worker import job_claim_query
from tests.test_pipeline import make_episode


def make_session(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'release-guards.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def write_png(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path, "PNG")


def test_production_configuration_fails_closed_by_default():
    settings = Settings(app_env="production")

    errors = settings.production_errors()

    assert any("ADMIN_USERNAME" in error for error in errors)
    assert any("PostgreSQL" in error for error in errors)
    assert any("PROVIDER_MODE" in error for error in errors)
    assert any("mounted filesystem" in error or "absolute path" in error for error in errors)
    assert any("CLOUD_RUN_JOB_NAME" in error for error in errors)

    worker_errors = Settings(
        app_env="production",
        runtime_role="worker",
    ).production_errors(require_dispatch=False)
    assert any("GOOGLE_CLOUD_PROJECT" in error for error in worker_errors)
    assert any("VEO_OUTPUT_GCS_URI" in error for error in worker_errors)

    gemini_worker_errors = Settings(
        app_env="production",
        runtime_role="worker",
        veo_backend="gemini",
    ).production_errors(require_dispatch=False)
    assert any("GEMINI_API_KEY" in error for error in gemini_worker_errors)


def test_maintenance_production_configuration_requires_only_shared_runtime_inputs(
    tmp_path: Path, monkeypatch
):
    mount = tmp_path / "nuvibu"
    mount.mkdir()
    monkeypatch.setattr("app.config.os.path.ismount", lambda path: path == mount)
    settings = Settings(
        app_env="production",
        runtime_role="maintenance",
        database_url="postgresql://example.test/neondb",
        storage_root=mount,
        storage_backend="gcs_mount",
        provider_mode="live",
    )

    assert settings.production_errors(require_dispatch=False) == []
    settings.validate_production(require_dispatch=False)


def test_production_budget_keeps_episode_cap_and_disables_daily_cap():
    settings = Settings()
    deploy_script = (
        Path(__file__).resolve().parents[1] / "deploy" / "cloud-run.sh"
    ).read_text(encoding="utf-8")

    assert settings.max_estimated_cost_usd_per_episode == 40.0
    assert settings.max_daily_estimated_cost_usd == 0.0
    assert "MAX_ESTIMATED_COST_USD_PER_EPISODE=40" in deploy_script
    assert "MAX_DAILY_ESTIMATED_COST_USD=0" in deploy_script


def test_cloud_run_web_service_keeps_capacity_ready():
    deploy_script = (
        Path(__file__).resolve().parents[1] / "deploy" / "cloud-run.sh"
    ).read_text(encoding="utf-8")
    web_deploy = deploy_script.split(
        'gcloud run deploy "${WEB_SERVICE}"',
        maxsplit=1,
    )[1].split('SERVICE_URL="$(', maxsplit=1)[0]

    assert "--min 1" in web_deploy
    assert "--max 3" in web_deploy
    assert "--min-instances default" in web_deploy
    assert "--max-instances default" in web_deploy
    assert "--concurrency 20" in web_deploy
    assert "--cpu-boost" in web_deploy
    assert "--min-instances 1" not in web_deploy
    assert "--max-instances 3" not in web_deploy
    assert "--min-instances 0" not in web_deploy
    assert "--max-instances 1" not in web_deploy


def test_complete_gemini_production_configuration_is_accepted(
    tmp_path: Path, monkeypatch
):
    mount = tmp_path / "nuvibu"
    mount.mkdir()
    monkeypatch.setattr("app.config.os.path.ismount", lambda path: path == mount)
    settings = Settings(
        app_env="production",
        app_base_url="https://nuvibu.example",
        secret_key="long-random-production-secret-with-entropy",
        admin_username="admin",
        admin_password="different-long-secret",
        database_url="postgresql://example.test/neondb",
        storage_root=mount,
        storage_backend="gcs_mount",
        provider_mode="live",
        elevenlabs_api_key="elevenlabs-secret",
        veo_backend="gemini",
        gemini_api_key="gemini-secret",
        google_cloud_project="nuvibu",
        cloud_run_job_name="nuvibu-worker",
    )

    settings.validate_production()


def test_veo_backend_selects_and_validates_its_model_namespace():
    assert Settings(veo_backend="vertex").veo_model == "veo-3.1-generate-001"
    with pytest.raises(ValidationError, match="Vertex Veo models"):
        Settings(
            veo_backend="vertex",
            veo_model="veo-3.1-fast-generate-preview",
        )


def test_scene_concatenation_trims_every_clip_to_storyboard_duration(
    tmp_path: Path, monkeypatch
):
    commands: list[list[str]] = []
    monkeypatch.setattr("app.services.render.run", commands.append)

    concatenate_scenes(
        [tmp_path / "one.mp4", tmp_path / "two.mp4"],
        tmp_path / "joined.mp4",
        11,
        scene_durations=[5, 6],
    )

    command = commands[0]
    graph = command[command.index("-filter_complex") + 1]
    assert "trim=start=0:duration=5.000" in graph
    assert "trim=start=0:duration=6.000" in graph
    assert "[v0][v1]concat=n=2:v=1:a=0[outv]" in graph


def test_scene_generation_is_resumable_without_duplicate_provider_calls(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(
        "app.services.pipeline.is_valid_video",
        lambda path: path.is_file() and path.stat().st_size > 0,
    )
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        provider_mode="mock",
        max_scene_retries=0,
    )
    settings.ensure_directories()

    class CountingVideoProvider:
        def __init__(self):
            self.calls = 0
            self.prompts: list[str] = []
            self.seeds: list[int] = []

        def generate(
            self,
            *,
            output_path: Path,
            duration_seconds: int,
            prompt: str,
            seed: int,
            **_kwargs,
        ):
            self.calls += 1
            self.prompts.append(prompt)
            self.seeds.append(seed)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"video")
            return VideoResult(
                path=output_path,
                provider="counting-test",
                duration_seconds=float(duration_seconds),
            )

    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        provider = CountingVideoProvider()
        service._video_provider = provider
        look = get_emma_look("emma-starry-bedtime-v1")
        reference_sources = {
            "emma": tmp_path / "untrusted-emma.png",
            "friends": tmp_path / "friends.png",
            "world": tmp_path / "world.png",
        }
        write_png(reference_sources["emma"], "black")
        write_png(reference_sources["friends"], "red")
        write_png(reference_sources["world"], "green")
        service.save_reference_pack(
            episode,
            reference_sources,
            emma_look_id=look.id,
        )
        service.generate_storyboard(episode)
        for scene in episode.storyboard_json:
            scene["prompt"] = "Legacy cloud-led storyboard prompt"
        db.commit()

        service.generate_scenes(episode)
        first_call_count = provider.calls
        service.generate_scenes(episode)

        assert first_call_count == len(episode.storyboard_json)
        assert provider.calls == first_call_count
        assert all(
            "Emma is the recurring main character" in prompt
            for prompt in provider.prompts
        )
        assert all(
            "Nuvibù is the name of the platform" in prompt
            for prompt in provider.prompts
        )
        assert all(
            "never add a default cloud companion" in prompt
            for prompt in provider.prompts
        )
        assert provider.seeds == [173] * len(episode.storyboard_json)
        assert all(look.outfit_prompt in prompt for prompt in provider.prompts)
        assert all(
            "locked candy-pink outfit" not in prompt
            and "pastel-pink short-sleeved baby dress" not in prompt
            for prompt in provider.prompts
        )
        scene_assets = db.scalars(
            select(Asset)
            .where(
                Asset.episode_id == episode.id,
                Asset.kind == AssetKind.VIDEO_SCENE,
            )
            .order_by(Asset.variant)
        ).all()
        assert len(scene_assets) == len(episode.storyboard_json)
        assert all(
            asset.metadata_json["emma_look_id"] == look.id
            for asset in scene_assets
        )
        assert all(
            asset.metadata_json["emma_look_catalog_version"]
            == CATALOG_VERSION
            for asset in scene_assets
        )
        frozen_emma = service.reference_images(episode)[0]
        frozen_emma_sha256 = hashlib.sha256(
            frozen_emma.read_bytes()
        ).hexdigest()
        assert all(
            asset.metadata_json["emma_reference_sha256"]
            == frozen_emma_sha256
            for asset in scene_assets
        )


def test_stale_running_job_is_failed_and_replaced(tmp_path: Path):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        job_stale_after_seconds=900,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        stale = Job(
            episode_id=episode.id,
            job_type="pipeline",
            status=JobStatus.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(stale)
        db.commit()

        replacement = PipelineService(db, settings).enqueue(episode)

        db.refresh(stale)
        assert stale.status == JobStatus.FAILED
        assert replacement.id != stale.id
        assert replacement.status == JobStatus.PENDING
        assert (
            stale.result_json["budget_reservation_release_reason"]
            == "stale"
        )


def test_stale_job_with_unresolved_provider_state_keeps_same_reservation(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        provider_mode="live",
        job_stale_after_seconds=900,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        stale = Job(
            episode_id=episode.id,
            job_type="pipeline",
            status=JobStatus.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            payload_json={
                "through_step": "music",
                "budget_reserved_usd": 1.0,
                "budget_actual_baseline_usd": 0.0,
                "budget_reserved_at": (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat(),
            },
        )
        db.add(stale)
        db.commit()
        output = settings.asset_dir / episode.id / "music-v1.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        music_receipt_path(output).write_text(
            '{"state":"submitting","request_fingerprint":"ambiguous"}',
            encoding="utf-8",
        )

        recovered = PipelineService(db, settings).enqueue(
            episode,
            "music",
            estimated_incremental_cost=1.0,
        )

        assert recovered.id == stale.id
        assert recovered.status == JobStatus.PENDING
        assert recovered.payload_json["budget_reserved_usd"] == 1.0
        assert recovered.result_json["provider_reconciliation_required"]
        assert "budget_reservation_release_reason" not in recovered.result_json


def test_stale_job_recovery_cannot_upgrade_the_authorized_step(tmp_path: Path):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        job_stale_after_seconds=900,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        stale = Job(
            episode_id=episode.id,
            job_type="pipeline",
            status=JobStatus.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            payload_json={"through_step": "music"},
        )
        db.add(stale)
        db.commit()
        service = PipelineService(db, settings)

        with pytest.raises(ActiveJobError, match="cannot upgrade"):
            service.enqueue(episode, "qc")

        db.refresh(stale)
        assert stale.status == JobStatus.FAILED
        replacement = service.enqueue(episode, "music")
        assert replacement.payload_json["through_step"] == "music"


def test_stale_music_job_with_durable_output_can_advance_to_qc(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        job_stale_after_seconds=900,
        max_music_variants=1,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        music = settings.asset_dir / episode.id / "music-v1.mp3"
        music.parent.mkdir(parents=True, exist_ok=True)
        music.write_bytes(b"durably ledgered music")
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
        stale = Job(
            episode_id=episode.id,
            job_type="pipeline",
            status=JobStatus.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            payload_json={"through_step": "music"},
        )
        db.add(stale)
        db.commit()

        next_job = PipelineService(db, settings).enqueue(episode, "qc")

        db.refresh(stale)
        assert stale.status == JobStatus.FAILED
        assert next_job.id != stale.id
        assert next_job.payload_json["through_step"] == "qc"


def test_stale_lyrics_job_with_durable_output_can_advance_to_storyboard(
    tmp_path: Path,
):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        job_stale_after_seconds=900,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        lyrics = settings.asset_dir / episode.id / "lyrics.txt"
        lyrics.parent.mkdir(parents=True, exist_ok=True)
        lyrics.write_text("Nuvibù saluta piano", encoding="utf-8")
        episode.lyrics_text = lyrics.read_text(encoding="utf-8")
        db.add(
            Asset(
                episode=episode,
                kind=AssetKind.LYRICS,
                provider="test",
                path=str(lyrics),
                mime_type="text/plain",
                selected=True,
            )
        )
        stale = Job(
            episode_id=episode.id,
            job_type="pipeline",
            status=JobStatus.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            payload_json={"through_step": "lyrics"},
        )
        db.add(stale)
        db.commit()

        next_job = PipelineService(db, settings).enqueue(
            episode,
            "storyboard",
        )

        db.refresh(stale)
        assert stale.status == JobStatus.FAILED
        assert next_job.id != stale.id
        assert next_job.payload_json["through_step"] == "storyboard"


def test_failed_pipeline_marks_job_and_exits_nonzero_path(
    tmp_path: Path, monkeypatch
):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        job = Job(
            episode_id=episode.id,
            job_type="pipeline",
            status=JobStatus.PENDING,
        )
        db.add(job)
        db.commit()
        service = PipelineService(db, settings)

        def fail(failed_episode, *_args, **_kwargs):
            paid_output = tmp_path / "paid-before-failure.mp3"
            paid_output.write_bytes(b"paid")
            db.add(
                Asset(
                    episode=failed_episode,
                    kind=AssetKind.MUSIC,
                    provider="test",
                    path=str(paid_output),
                    mime_type="audio/mpeg",
                    cost_usd=0.75,
                )
            )
            # Paid stages ledger each completed provider result before later
            # pipeline work can fail.
            db.commit()
            raise RuntimeError("provider failed")

        monkeypatch.setattr(service, "run_through", fail)
        with pytest.raises(RuntimeError, match="provider failed"):
            service.process_job(job)

        db.refresh(job)
        db.refresh(episode)
        assert job.status == JobStatus.FAILED
        assert job.error_text == "provider failed"
        assert episode.status == EpisodeStatus.FAILED
        assert episode.actual_cost_usd == pytest.approx(0.75)
        assert (
            job.result_json["budget_reservation_release_reason"]
            == "failed"
        )


@pytest.mark.parametrize(
    "artifact_kind",
    ["music_submitting", "veo_running", "veo_invalid"],
)
def test_ambiguous_provider_state_keeps_job_pending_and_reserved(
    tmp_path: Path,
    monkeypatch,
    artifact_kind: str,
):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        provider_mode="live",
        max_scene_retries=0,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode()
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        through_step = "music" if artifact_kind.startswith("music") else "qc"
        job = service.enqueue(
            episode,
            through_step,
            estimated_incremental_cost=1.0,
        )

        def fail_with_marker(*_args, **_kwargs):
            if artifact_kind.startswith("music"):
                output = (
                    settings.asset_dir / episode.id / "music-v1.mp3"
                )
                output.parent.mkdir(parents=True, exist_ok=True)
                music_receipt_path(output).write_text(
                    '{"state":"submitting","request_fingerprint":"ambiguous"}',
                    encoding="utf-8",
                )
            else:
                sidecar = (
                    settings.asset_dir
                    / episode.id
                    / "scenes"
                    / "scene-000.mp4.operation.json"
                )
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(
                    (
                        '{"state":"running","operation_name":"operations/paid"}'
                        if artifact_kind == "veo_running"
                        else "{not-json"
                    ),
                    encoding="utf-8",
                )
            raise RuntimeError("ambiguous provider failure")

        monkeypatch.setattr(service, "run_through", fail_with_marker)
        with pytest.raises(RuntimeError, match="ambiguous provider failure"):
            service.process_job(job)

        db.refresh(job)
        db.refresh(episode)
        assert job.status == JobStatus.PENDING
        assert job.started_at is None
        assert job.finished_at is None
        assert job.error_text == "ambiguous provider failure"
        assert job.result_json["provider_reconciliation_required"]
        assert "budget_reservation_release_reason" not in job.result_json
        assert job.payload_json["budget_reserved_usd"] >= 1.0
        assert episode.status != EpisodeStatus.FAILED


def test_completed_music_before_ledger_resumes_the_same_reserved_job(
    tmp_path: Path,
    monkeypatch,
):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        provider_mode="live",
        max_music_variants=1,
        max_scene_retries=0,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode(24)
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        service.generate_lyrics(episode)
        service.generate_storyboard(episode)
        service.approve_content(episode, "lyrics")
        service.approve_content(episode, "storyboard")
        job = service.enqueue(episode, "music")
        original_job_id = job.id

        def crash_after_provider(*_args, **_kwargs):
            output = settings.asset_dir / episode.id / "music-v1.mp3"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"x" * 2048)
            fingerprint = music_request_fingerprint(
                lyrics=episode.lyrics_text or "",
                prompt=music_prompt(episode),
                music_direction=episode.music_direction,
                duration_seconds=episode.duration_seconds,
                bpm=episode.bpm,
                variant=1,
                model_id=settings.elevenlabs_music_model,
                output_format=settings.elevenlabs_output_format,
            )
            music_receipt_path(output).write_text(
                json.dumps(
                    {
                        "state": "complete",
                        "request_fingerprint": fingerprint,
                        "estimated_cost_usd": 0.06,
                        "vocal_qc": {
                            "passed": True,
                            "reason": "sung_lyrics_detected",
                            "coverage_ratio": 1.0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            raise RuntimeError("crash before Asset ledger")

        monkeypatch.setattr(service, "run_through", crash_after_provider)
        with pytest.raises(RuntimeError, match="crash before Asset ledger"):
            service.process_job(job)
        db.refresh(job)
        assert job.status == JobStatus.PENDING

        monkeypatch.setattr(
            "app.services.pipeline.music_arrangement_quality",
            lambda _path: {
                "passed": True,
                "reason": "instrumental_low_end_present",
                "low_band_energy_ratio": 0.02,
            },
        )
        resumed = PipelineService(db, settings).process_job(job)

        assert resumed.id == original_job_id
        assert resumed.status == JobStatus.SUCCEEDED
        assert resumed.result_json["budget_reservation_release_reason"] == (
            "succeeded"
        )
        music_assets = db.scalars(
            select(Asset).where(
                Asset.episode_id == episode.id,
                Asset.kind == AssetKind.MUSIC,
            )
        ).all()
        assert len(music_assets) == 1
        assert music_assets[0].cost_usd == pytest.approx(0.06)


def test_cloud_run_dispatch_targets_the_exact_database_job(monkeypatch):
    calls: list[dict] = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"name": "operations/cloud-run-execution"}

    class Session:
        def __init__(self, _credentials):
            pass

        def post(self, url: str, *, json: dict, timeout: int):
            calls.append({"url": url, "json": json, "timeout": timeout})
            return Response()

    monkeypatch.setattr("google.auth.default", lambda scopes: (object(), "nuvibu"))
    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", Session)
    settings = Settings(
        google_cloud_project="nuvibu",
        cloud_run_job_name="nuvibu-worker",
        cloud_run_job_location="europe-west1",
    )

    operation = dispatch_worker_job(settings, "job-123")

    assert operation == "operations/cloud-run-execution"
    assert calls[0]["url"].endswith(
        "/projects/nuvibu/locations/europe-west1/jobs/nuvibu-worker:run"
    )
    overrides = calls[0]["json"]["overrides"]
    assert overrides["taskCount"] == 1
    assert overrides["containerOverrides"][0]["env"] == [
        {"name": "NUVIBU_JOB_ID", "value": "job-123"}
    ]


def test_exact_worker_claim_waits_instead_of_skipping_locked_job():
    from sqlalchemy.dialects import postgresql

    exact_sql = str(
        job_claim_query("job-123").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    generic_sql = str(
        job_claim_query(None).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FOR UPDATE" in exact_sql
    assert "SKIP LOCKED" not in exact_sql
    assert "FOR UPDATE SKIP LOCKED" in generic_sql


def test_pending_job_can_be_redispatched_after_startup_failure_timeout():
    now = datetime.now(timezone.utc)
    job = Job(
        episode_id="episode",
        job_type="pipeline",
        status=JobStatus.PENDING,
        result_json={
            "cloud_run_operation": "operations/failed-startup",
            "cloud_run_dispatched_at": (now - timedelta(seconds=181)).isoformat(),
        },
    )

    assert worker_dispatch_due(job, now=now, retry_after_seconds=180) is True
    job.result_json["cloud_run_dispatched_at"] = now.isoformat()
    assert worker_dispatch_due(job, now=now, retry_after_seconds=180) is False


def test_ambiguous_music_submission_blocks_duplicate_purchase(
    tmp_path: Path, monkeypatch
):
    calls = 0

    def time_out(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("unknown outcome")

    monkeypatch.setattr("app.providers.elevenlabs.httpx.post", time_out)
    provider = ElevenLabsMusicProvider(
        api_key="secret",
        model_id="music_v2",
        output_format="mp3_48000_192",
    )
    output = tmp_path / "song.mp3"
    request = {
        "lyrics": "Canta con Nuvibù",
        "prompt": "Original preschool song",
        "duration_seconds": 24,
        "bpm": 92,
        "output_path": output,
        "variant": 1,
    }

    with pytest.raises(httpx.ReadTimeout):
        provider.generate(**request)
    state = json.loads(music_receipt_path(output).read_text(encoding="utf-8"))
    assert state["state"] == "submitting"

    with pytest.raises(RuntimeError, match="ambiguous outcome"):
        provider.generate(**request)
    assert calls == 1


def test_empty_music_direction_preserves_legacy_request_fingerprint():
    values = (
        "Canta con Nuvibù",
        "Original preschool song",
        "24",
        "92",
        "1",
        "music_v2",
        "mp3_48000_192",
    )
    legacy_digest = hashlib.sha256()
    for value in values:
        legacy_digest.update(value.encode("utf-8"))
        legacy_digest.update(b"\0")

    fingerprint = music_request_fingerprint(
        lyrics=values[0],
        prompt=values[1],
        duration_seconds=24,
        bpm=92,
        variant=1,
        model_id=values[5],
        output_format=values[6],
    )

    assert fingerprint == legacy_digest.hexdigest()


def test_music_v2_uses_exact_composition_plan_and_approved_lines(
    tmp_path: Path,
    monkeypatch,
):
    calls: list[dict] = []

    boundary = "nuvibu-music-boundary"
    timestamp_metadata = {
        "words_timestamps": [
            {"word": word, "start": index * 0.2, "end": index * 0.2 + 0.1}
            for index, word in enumerate(
                "Pio pio eccoci qua Splash splash salta anche tu Arcobaleno con Nuvibù".split()
            )
        ],
        "song_metadata": {"title": "Test song", "languages": ["it"]},
    }
    multipart = (
        f"--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode()
        + json.dumps(timestamp_metadata).encode()
        + f"\r\n--{boundary}\r\nContent-Type: audio/mpeg\r\n".encode()
        + b'Content-Disposition: attachment; filename="song.mp3"\r\n\r\n'
        + b"x" * 2048
        + f"\r\n--{boundary}--\r\n".encode()
    )

    class Response:
        status_code = 200
        headers = {
            "content-type": f"multipart/mixed; boundary={boundary}",
            "song-id": "song-structured",
        }
        content = multipart

        def raise_for_status(self):
            return None

    def compose(url: str, *, headers: dict, params: dict, json: dict, timeout: int):
        calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
                "timeout": timeout,
            }
        )
        return Response()

    monkeypatch.setattr("app.providers.elevenlabs.httpx.post", compose)
    lyrics = (
        "[Intro]\nPio pio, eccoci qua!\n\n"
        "[Ritornello]\nSplash splash, salta anche tu!\n"
        "Arcobaleno con Nuvibù!"
    )
    provider = ElevenLabsMusicProvider(
        api_key="secret",
        model_id="music_v2",
        output_format="mp3_48000_192",
    )
    output = tmp_path / "song.mp3"
    direction = (
        "Electro-pop energico con synth arcade. Voce femminile adulta; "
        "robot parlato con vocoder; cori infantili solo per risposte brevi."
    )

    result = provider.generate(
        lyrics=lyrics,
        prompt="Original Italian song; use the separate direction as authoritative.",
        music_direction=direction,
        duration_seconds=75,
        bpm=112,
        output_path=output,
        variant=1,
    )

    payload = calls[0]["json"]
    assert "prompt" not in payload
    assert "music_length_ms" not in payload
    assert payload["model_id"] == "music_v2"
    assert payload["sign_with_c2pa"] is True
    assert payload["with_timestamps"] is True
    assert calls[0]["url"].endswith("/v1/music/detailed")
    assert calls[0]["headers"]["Accept"] == "multipart/mixed"
    plan = payload["composition_plan"]
    assert set(plan) == {"chunks"}
    assert sum(chunk["duration_ms"] for chunk in plan["chunks"]) == 75_000
    assert [
        line
        for chunk in plan["chunks"]
        for line in chunk["text"].splitlines()[1:]
    ] == [
        "Pio pio, eccoci qua!",
        "Splash splash, salta anche tu!",
        "Arcobaleno con Nuvibù!",
    ]
    assert all(
        set(chunk)
        == {
            "text",
            "duration_ms",
            "positive_styles",
            "negative_styles",
            "context_adherence",
        }
        for chunk in plan["chunks"]
    )
    assert all(chunk["context_adherence"] == "high" for chunk in plan["chunks"])
    first_styles = set(plan["chunks"][0]["positive_styles"])
    assert direction not in first_styles
    assert "electro-pop" in first_styles
    assert "bright adult female lead vocalist" in first_styles
    assert all(direction not in chunk["text"] for chunk in plan["chunks"])
    assert "full instrumental backing under every sung line" in first_styles
    assert "the designated lead singer performs every supplied lyric line" in first_styles
    assert "bright ukulele chord strumming throughout" not in first_styles
    assert "simple melody for very young children" not in first_styles
    assert (
        "opening follows the supplied direction with no generic slow nursery intro"
        in first_styles
    )
    assert "a cappella" in plan["chunks"][0]["negative_styles"]
    assert "instrumental-only track" in plan["chunks"][0]["negative_styles"]
    assert "no vocals" in plan["chunks"][0]["negative_styles"]
    chorus_styles = set(plan["chunks"][1]["positive_styles"])
    assert "full-arrangement chorus lift" in chorus_styles
    assert "light child backing vocals behind the lead" not in chorus_styles
    assert result.path == output
    assert result.metadata["song_id"] == "song-structured"
    assert result.metadata["music_direction"] == direction
    assert result.metadata["vocal_qc"]["passed"] is True
    assert result.metadata["vocal_qc"]["coverage_ratio"] >= 0.30


def test_music_v2_speaker_labels_become_inline_cues_without_touching_literal_colons():
    plan = build_music_v2_composition_plan(
        lyrics='[Intro]\nNiko: “I-ò!”\nPorta azzurra: click!',
        duration_seconds=12,
        bpm=120,
    )
    lines = plan["chunks"][0]["text"].splitlines()[1:]
    assert lines == ["{brief character response} I-ò!", "Porta azzurra: click!"]


def test_vocal_qc_rejects_zero_words_and_accepts_matching_lyrics():
    lyrics = "[Intro]\nSole su, mare blu!\nEmma, Niko, via!"
    failed, failed_timestamps = _music_vocal_quality(lyrics, {})
    assert failed["passed"] is False
    assert failed["reason"] == "no_sung_words_detected"
    assert failed_timestamps == []

    passed, timestamps = _music_vocal_quality(
        lyrics,
        {
            "words_timestamps": [
                {"word": word, "start": index * 0.1, "end": index * 0.1 + 0.05}
                for index, word in enumerate("Sole su mare blu Emma Niko via".split())
            ]
        },
    )
    assert passed["passed"] is True
    assert passed["coverage_ratio"] >= 0.90
    assert len(timestamps) == 7


def test_music_v2_legacy_fallback_keeps_default_arrangement():
    plan = build_music_v2_composition_plan(
        lyrics="[Intro]\nCiao!\n\n[Ritornello]\nCanta con me!",
        duration_seconds=15,
        bpm=92,
    )

    first_styles = set(plan["chunks"][0]["positive_styles"])
    chorus_styles = set(plan["chunks"][1]["positive_styles"])
    assert "bright ukulele chord strumming throughout" in first_styles
    assert "instrumental hook starts in the first second" in first_styles
    assert "light child backing vocals behind the lead" in chorus_styles


def test_composition_plan_keeps_short_sections_positive():
    plan = build_music_v2_composition_plan(
        lyrics="[Intro]\nCiao!\n\n[Ritornello]\nCanta con me!",
        duration_seconds=15,
        bpm=92,
    )

    assert sum(chunk["duration_ms"] for chunk in plan["chunks"]) == 15_000
    assert all(chunk["duration_ms"] >= 3000 for chunk in plan["chunks"])


def test_composition_plan_rejects_too_many_sections_for_duration():
    lyrics = "\n\n".join(f"[Part {index}]\nCiao" for index in range(16))

    with pytest.raises(ValueError, match="too many sections"):
        build_music_v2_composition_plan(
            lyrics=lyrics,
            duration_seconds=15,
            bpm=92,
        )


def test_music_validation_error_is_actionable_and_retryable(
    tmp_path: Path,
    monkeypatch,
):
    class Response:
        status_code = 422
        headers = {"content-type": "application/json"}
        content = b'{"detail":[{"loc":["body","composition_plan","chunks"],"msg":"Field required"}]}'
        text = content.decode()

        def json(self):
            return json.loads(self.content)

    monkeypatch.setattr(
        "app.providers.elevenlabs.httpx.post",
        lambda *_args, **_kwargs: Response(),
    )
    provider = ElevenLabsMusicProvider(
        api_key="secret",
        model_id="music_v2",
        output_format="mp3_48000_192",
    )
    output = tmp_path / "song.mp3"

    with pytest.raises(
        RuntimeError,
        match=r"ElevenLabs rejected the music request \(422\).*chunks",
    ):
        provider.generate(
            lyrics="[Intro]\nCanta con Nuvibù!",
            prompt="Original preschool song",
            duration_seconds=15,
            bpm=92,
            output_path=output,
            variant=1,
        )

    assert not music_receipt_path(output).exists()


def test_music_arrangement_gate_rejects_voice_only_and_accepts_bass_and_drums():
    sample_rate = 24_000
    time_axis = np.arange(sample_rate * 4, dtype=np.float32) / sample_rate
    voice_only = 0.4 * np.sin(2 * np.pi * 440 * time_axis)
    voice_metrics = _music_arrangement_metrics_from_samples(
        voice_only,
        sample_rate=sample_rate,
    )

    arranged = (
        voice_only
        + 0.28 * np.sin(2 * np.pi * 90 * time_axis)
        + 0.10 * np.sin(2 * np.pi * 120 * time_axis)
    )
    arranged_metrics = _music_arrangement_metrics_from_samples(
        arranged,
        sample_rate=sample_rate,
    )

    assert voice_metrics["passed"] is False
    assert (
        voice_metrics["low_band_energy_ratio"]
        < MUSIC_MIN_LOW_BAND_ENERGY_RATIO
    )
    assert arranged_metrics["passed"] is True
    assert (
        arranged_metrics["low_band_energy_ratio"]
        >= MUSIC_MIN_LOW_BAND_ENERGY_RATIO
    )


def test_paid_voice_only_music_is_preserved_deselected_and_retryable(
    tmp_path: Path,
    monkeypatch,
):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'release-guards.db'}",
        storage_root=tmp_path / "storage",
        provider_mode="live",
    )
    settings.ensure_directories()
    monkeypatch.setattr(
        "app.services.pipeline.music_arrangement_quality",
        lambda _path: {
            "passed": False,
            "reason": "instrumental_backing_too_sparse_or_voice_only",
            "low_band_energy_ratio": 0.0001,
        },
    )

    with Session() as db:
        episode = make_episode(75)
        db.add(episode)
        db.commit()
        music_path = settings.asset_dir / episode.id / "music-v1.mp3"
        music_path.parent.mkdir(parents=True, exist_ok=True)
        music_path.write_bytes(b"x" * 2048)
        asset = Asset(
            episode=episode,
            kind=AssetKind.MUSIC,
            provider="elevenlabs-music",
            path=str(music_path),
            mime_type="audio/mpeg",
            variant=1,
            selected=True,
            duration_seconds=75,
            cost_usd=0.19,
        )
        db.add(asset)
        db.commit()

        PipelineService(
            db,
            settings,
        )._invalidate_unacceptable_music_assets(episode)

        db.refresh(asset)
        assert asset.selected is False
        assert asset.cost_usd == pytest.approx(0.19)
        assert asset.metadata_json["arrangement_qc"]["passed"] is False
        assert (
            asset.metadata_json["invalidation_reason"]
            == "insufficient_instrumental_arrangement"
        )
        assert asset.metadata_json["invalidated_at"]


def test_paid_instrumental_music_failed_vocal_qc_is_preserved_and_deselected(
    tmp_path: Path,
    monkeypatch,
):
    Session = make_session(tmp_path)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'vocal-qc.db'}",
        storage_root=tmp_path / "storage-vocal-qc",
        provider_mode="live",
    )
    settings.ensure_directories()
    monkeypatch.setattr(
        "app.services.pipeline.music_arrangement_quality",
        lambda _path: {
            "passed": True,
            "reason": "instrumental_arrangement_detected",
            "low_band_energy_ratio": 0.1,
        },
    )
    with Session() as db:
        episode = make_episode(75)
        db.add(episode)
        db.commit()
        music_path = settings.asset_dir / episode.id / "music-v1.mp3"
        music_path.parent.mkdir(parents=True, exist_ok=True)
        music_path.write_bytes(b"x" * 2048)
        asset = Asset(
            episode=episode,
            kind=AssetKind.MUSIC,
            provider="elevenlabs-music",
            path=str(music_path),
            mime_type="audio/mpeg",
            variant=1,
            selected=True,
            duration_seconds=75,
            cost_usd=0.19,
            metadata_json={
                "vocal_qc": {
                    "passed": False,
                    "reason": "no_sung_words_detected",
                    "coverage_ratio": 0.0,
                }
            },
        )
        db.add(asset)
        db.commit()

        PipelineService(db, settings)._invalidate_unacceptable_music_assets(episode)

        db.refresh(asset)
        assert asset.selected is False
        assert asset.cost_usd == pytest.approx(0.19)
        assert asset.metadata_json["arrangement_qc"]["passed"] is True
        assert asset.metadata_json["invalidation_reason"] == "missing_sung_lyrics"
        assert asset.metadata_json["invalidated_at"]
