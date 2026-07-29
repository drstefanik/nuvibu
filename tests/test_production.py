from __future__ import annotations

import base64
from pathlib import Path

import google.auth
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Asset, AssetKind, JobStatus
from app.providers.veo import VeoProvider
from app.services.pipeline import PipelineService
from tests.test_pipeline import make_episode


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_veo_uses_adc_without_a_json_key(monkeypatch):
    class Credentials:
        valid = False
        token = None

        def refresh(self, _request) -> None:
            self.valid = True
            self.token = "short-lived-token"

    credentials = Credentials()
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda scopes: (credentials, "nuvibu"),
    )
    provider = VeoProvider(
        project="nuvibu",
        location="us-central1",
        model="veo-3.1-generate-001",
        output_gcs_uri=None,
        credentials_file=None,
    )

    assert provider._token() == "short-lived-token"


def test_veo_sends_subject_reference_and_uses_full_model_price(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.providers.veo.is_valid_video", lambda _path: True)
    calls: list[dict] = []
    encoded_video = base64.b64encode(b"video-bytes").decode("ascii")

    def fake_post(url: str, *, headers: dict, json: dict, timeout: int):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if url.endswith(":predictLongRunning"):
            return FakeResponse({"name": "projects/nuvibu/locations/us-central1/operations/test"})
        return FakeResponse(
            {
                "done": True,
                "response": {
                    "videos": [
                        {
                            "bytesBase64Encoded": encoded_video,
                            "mimeType": "video/mp4",
                        }
                    ]
                },
            }
        )

    monkeypatch.setattr("app.providers.veo.httpx.post", fake_post)
    references = [
        tmp_path / "nuvibu.png",
        tmp_path / "cast.png",
        tmp_path / "world.png",
    ]
    for index, reference in enumerate(references, start=1):
        reference.write_bytes(f"reference-{index}".encode())
    output = tmp_path / "scene.mp4"
    provider = VeoProvider(
        project="nuvibu",
        location="us-central1",
        model="veo-3.1-generate-001",
        output_gcs_uri=None,
        credentials_file=None,
    )
    monkeypatch.setattr(provider, "_headers", lambda: {"Authorization": "Bearer test"})

    result = provider.generate(
        prompt="Nuvibu waves",
        duration_seconds=4,
        output_path=output,
        seed=173,
        reference_images=references,
    )

    instance = calls[0]["json"]["instances"][0]
    parameters = calls[0]["json"]["parameters"]
    assert "image" not in instance
    assert len(instance["referenceImages"]) == 3
    assert all(
        reference["referenceType"] == "asset"
        for reference in instance["referenceImages"]
    )
    assert [
        base64.b64decode(reference["image"]["bytesBase64Encoded"])
        for reference in instance["referenceImages"]
    ] == [b"reference-1", b"reference-2", b"reference-3"]
    assert parameters["durationSeconds"] == 8
    assert output.read_bytes() == b"video-bytes"
    assert result.cost_usd == 1.6
    assert result.metadata["reference_count"] == 3


def test_live_enqueue_is_lazy_idempotent_and_budget_uses_reference_cost(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'production-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'production-test.db'}",
        storage_root=tmp_path / "storage",
        provider_mode="live",
        veo_backend="vertex",
        veo_model="veo-3.1-generate-001",
        max_music_variants=1,
        max_scene_retries=0,
    )
    settings.ensure_directories()

    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        db.refresh(episode)
        reference = tmp_path / "reference.png"
        reference.write_bytes(b"png")
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

        first = service.enqueue(episode, "qc")
        second = service.enqueue(episode, "qc")

        assert first.id == second.id
        assert first.status == JobStatus.PENDING
        assert service.estimate_cost(episode) == 3.24
