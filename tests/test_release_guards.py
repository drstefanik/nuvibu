from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.main import worker_dispatch_due
from app.models import EpisodeStatus, Job, JobStatus
from app.providers.base import VideoResult
from app.providers.elevenlabs import ElevenLabsMusicProvider, music_receipt_path
from app.services.pipeline import PipelineService
from app.services.render import concatenate_scenes
from app.services.worker_dispatch import dispatch_worker_job
from tests.test_pipeline import make_episode


def make_session(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'release-guards.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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

        def generate(self, *, output_path: Path, duration_seconds: int, **_kwargs):
            self.calls += 1
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
        service.generate_storyboard(episode)

        service.generate_scenes(episode)
        first_call_count = provider.calls
        service.generate_scenes(episode)

        assert first_call_count == len(episode.storyboard_json)
        assert provider.calls == first_call_count


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

        def fail(*_args, **_kwargs):
            raise RuntimeError("provider failed")

        monkeypatch.setattr(service, "run_through", fail)
        with pytest.raises(RuntimeError, match="provider failed"):
            service.process_job(job)

        db.refresh(job)
        db.refresh(episode)
        assert job.status == JobStatus.FAILED
        assert job.error_text == "provider failed"
        assert episode.status == EpisodeStatus.FAILED


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
