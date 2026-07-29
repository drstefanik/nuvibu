from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.main import worker_dispatch_due
from app.models import Asset, AssetKind, EpisodeStatus, Job, JobStatus
from app.providers.base import VideoResult
from app.providers.elevenlabs import (
    ElevenLabsMusicProvider,
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


def test_music_v2_uses_exact_composition_plan_and_approved_lines(
    tmp_path: Path,
    monkeypatch,
):
    calls: list[dict] = []

    class Response:
        status_code = 200
        headers = {
            "content-type": "audio/mpeg",
            "song-id": "song-structured",
        }
        content = b"x" * 2048

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

    result = provider.generate(
        lyrics=lyrics,
        prompt="This prompt must not replace approved lyrics",
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
    plan = payload["composition_plan"]
    assert sum(section["duration_ms"] for section in plan["sections"]) == 75_000
    assert [
        line for section in plan["sections"] for line in section["lines"]
    ] == [
        "Pio pio, eccoci qua!",
        "Splash splash, salta anche tu!",
        "Arcobaleno con Nuvibù!",
    ]
    assert result.path == output
    assert result.metadata["song_id"] == "song-structured"


def test_composition_plan_keeps_short_sections_positive():
    plan = build_music_v2_composition_plan(
        lyrics="[Intro]\nCiao!\n\n[Ritornello]\nCanta con me!",
        duration_seconds=15,
        bpm=92,
    )

    assert sum(section["duration_ms"] for section in plan["sections"]) == 15_000
    assert all(section["duration_ms"] > 0 for section in plan["sections"])


def test_composition_plan_rejects_too_many_sections_for_duration():
    lyrics = "\n\n".join(f"[Part {index}]\nCiao" for index in range(16))

    with pytest.raises(ValueError, match="too many sections"):
        build_music_v2_composition_plan(
            lyrics=lyrics,
            duration_seconds=15,
            bpm=92,
        )
