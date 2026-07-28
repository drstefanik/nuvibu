from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from app.providers.veo import VeoProvider, VeoTerminalError, veo_price_per_second


class FakeResponse:
    def __init__(self, payload: dict | None = None, content: bytes = b""):
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_pricing_covers_gemini_and_vertex_models():
    assert veo_price_per_second("gemini", "veo-3.1-fast-generate-preview") == 0.10
    assert veo_price_per_second("gemini", "veo-3.1-generate-preview") == 0.40
    assert veo_price_per_second("vertex", "veo-3.1-fast-generate-001") == 0.08
    assert veo_price_per_second("vertex", "veo-3.1-generate-001") == 0.20


def test_gemini_uses_api_key_reference_schema_and_nested_response(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("app.providers.veo.is_valid_video", lambda _path: True)
    post_calls: list[dict] = []
    get_calls: list[dict] = []
    operation_name = "models/veo-3.1-fast-generate-preview/operations/abc123"
    video_uri = "https://generativelanguage.googleapis.com/v1beta/files/video:download"

    def fake_post(url: str, *, headers: dict, json: dict, timeout: int):
        post_calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return FakeResponse({"name": operation_name})

    def fake_get(
        url: str,
        *,
        headers: dict,
        timeout: int,
        follow_redirects: bool = False,
    ):
        get_calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
            }
        )
        if url.endswith(operation_name):
            return FakeResponse(
                {
                    "done": True,
                    "response": {
                        "generateVideoResponse": {
                            "generatedSamples": [{"video": {"uri": video_uri}}]
                        }
                    },
                }
            )
        assert url == video_uri
        return FakeResponse(content=b"gemini-video")

    monkeypatch.setattr("app.providers.veo.httpx.post", fake_post)
    monkeypatch.setattr("app.providers.veo.httpx.get", fake_get)

    reference = tmp_path / "nuvibu.png"
    reference.write_bytes(b"reference")
    output = tmp_path / "scene.mp4"
    provider = VeoProvider(
        backend="gemini",
        gemini_api_key="gemini-secret",
        project="",
        location="us-central1",
        model="veo-3.1-fast-generate-preview",
        output_gcs_uri=None,
        credentials_file=None,
    )

    result = provider.generate(
        prompt="Nuvibù waves",
        duration_seconds=4,
        output_path=output,
        seed=173,
        reference_image=reference,
    )

    start = post_calls[0]
    assert start["url"].endswith(
        "/models/veo-3.1-fast-generate-preview:predictLongRunning"
    )
    assert start["headers"]["x-goog-api-key"] == "gemini-secret"
    image = start["json"]["instances"][0]["referenceImages"][0]["image"]
    assert image["inlineData"]["mimeType"] == "image/png"
    assert base64.b64decode(image["inlineData"]["data"]) == b"reference"
    assert "bytesBase64Encoded" not in image
    assert start["json"]["parameters"]["durationSeconds"] == 8
    assert "generateAudio" not in start["json"]["parameters"]
    assert get_calls[-1]["headers"]["x-goog-api-key"] == "gemini-secret"
    assert get_calls[-1]["follow_redirects"] is True
    assert output.read_bytes() == b"gemini-video"
    assert result.cost_usd == 0.8
    assert result.metadata["backend"] == "gemini"
    assert not provider.operation_sidecar_path(output).exists()


def test_retry_resumes_saved_operation_without_new_paid_request(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("app.providers.veo.is_valid_video", lambda _path: True)
    output = tmp_path / "scene.mp4"
    encoded_video = base64.b64encode(b"resumed-video").decode("ascii")

    first = VeoProvider(
        backend="gemini",
        gemini_api_key="secret",
        project="",
        location="us-central1",
        model="veo-3.1-fast-generate-preview",
        output_gcs_uri=None,
        credentials_file=None,
    )
    monkeypatch.setattr(
        first,
        "_start_operation",
        lambda **_kwargs: "models/veo-3.1-fast-generate-preview/operations/resume-me",
    )

    def time_out(_operation_name: str):
        raise TimeoutError("worker stopped")

    monkeypatch.setattr(first, "_poll_operation", time_out)
    with pytest.raises(TimeoutError, match="worker stopped"):
        first.generate(
            prompt="first attempt",
            duration_seconds=4,
            output_path=output,
            seed=173,
        )

    sidecar = first.operation_sidecar_path(output)
    state = json.loads(sidecar.read_text(encoding="utf-8"))
    assert state["operation_name"].endswith("/resume-me")

    resumed = VeoProvider(
        backend="gemini",
        gemini_api_key="secret",
        project="",
        location="us-central1",
        model="veo-3.1-fast-generate-preview",
        output_gcs_uri=None,
        credentials_file=None,
    )

    def must_not_start(**_kwargs):
        raise AssertionError("retry submitted a duplicate paid generation")

    monkeypatch.setattr(resumed, "_start_operation", must_not_start)
    monkeypatch.setattr(
        resumed,
        "_poll_operation",
        lambda operation_name: {
            "generatedSamples": [
                {"video": {"bytesBase64Encoded": encoded_video}},
            ]
        },
    )

    result = resumed.generate(
        prompt="first attempt",
        duration_seconds=4,
        output_path=output,
        seed=173,
    )

    assert output.read_bytes() == b"resumed-video"
    assert result.metadata["operation"].endswith("/resume-me")
    assert not sidecar.exists()


def test_saved_operation_cannot_be_reused_for_a_different_scene(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "scene.mp4"
    provider = VeoProvider(
        backend="gemini",
        gemini_api_key="secret",
        project="",
        location="us-central1",
        model="veo-3.1-fast-generate-preview",
        output_gcs_uri=None,
        credentials_file=None,
    )
    fingerprint = provider._request_fingerprint(
        prompt="scene one",
        generation_duration=4,
        seed=173,
        reference_image=None,
    )
    provider._save_operation(output, "operations/existing", 4, fingerprint)
    monkeypatch.setattr(
        provider,
        "_start_operation",
        lambda **_kwargs: pytest.fail("must not buy another operation"),
    )

    with pytest.raises(RuntimeError, match="different scene request"):
        provider.generate(
            prompt="scene two",
            duration_seconds=4,
            output_path=output,
            seed=173,
        )


def test_ambiguous_start_timeout_blocks_duplicate_submission(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "scene.mp4"
    provider = VeoProvider(
        backend="gemini",
        gemini_api_key="secret",
        project="",
        location="us-central1",
        model="veo-3.1-fast-generate-preview",
        output_gcs_uri=None,
        credentials_file=None,
    )
    monkeypatch.setattr(
        provider,
        "_start_operation",
        lambda **_kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("unknown outcome")),
    )
    with pytest.raises(httpx.ReadTimeout):
        provider.generate(
            prompt="scene one",
            duration_seconds=4,
            output_path=output,
            seed=173,
        )

    state = json.loads(
        provider.operation_sidecar_path(output).read_text(encoding="utf-8")
    )
    assert state["state"] == "submitting"

    with pytest.raises(RuntimeError, match="ambiguous outcome"):
        provider.generate(
            prompt="scene one",
            duration_seconds=4,
            output_path=output,
            seed=173,
        )


def test_terminal_failure_allows_next_explicit_attempt(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("app.providers.veo.is_valid_video", lambda _path: True)
    output = tmp_path / "scene.mp4"
    provider = VeoProvider(
        backend="gemini",
        gemini_api_key="secret",
        project="",
        location="us-central1",
        model="veo-3.1-fast-generate-preview",
        output_gcs_uri=None,
        credentials_file=None,
    )
    starts: list[str] = []
    monkeypatch.setattr(
        provider,
        "_start_operation",
        lambda **_kwargs: starts.append("start") or "operations/failed",
    )
    monkeypatch.setattr(
        provider,
        "_poll_operation",
        lambda _operation: (_ for _ in ()).throw(
            VeoTerminalError("Veo generation failed: safety")
        ),
    )

    with pytest.raises(VeoTerminalError):
        provider.generate(
            prompt="scene one",
            duration_seconds=4,
            output_path=output,
            seed=173,
        )
    state = json.loads(
        provider.operation_sidecar_path(output).read_text(encoding="utf-8")
    )
    assert state["state"] == "terminal_error"

    monkeypatch.setattr(
        provider,
        "_start_operation",
        lambda **_kwargs: starts.append("start") or "operations/retry",
    )
    monkeypatch.setattr(
        provider,
        "_poll_operation",
        lambda _operation: {
            "videos": [
                {"bytesBase64Encoded": base64.b64encode(b"retry-video").decode("ascii")}
            ]
        },
    )
    provider.generate(
        prompt="scene one",
        duration_seconds=4,
        output_path=output,
        seed=173,
    )

    assert starts == ["start", "start"]
    assert output.read_bytes() == b"retry-video"
    assert list(tmp_path.glob("scene.mp4.failed.*.json"))


def test_invalid_download_never_replaces_existing_video(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "scene.mp4"
    output.write_bytes(b"previous-complete-video")
    inspected: list[Path] = []

    def reject_video(path: Path) -> bool:
        inspected.append(path)
        assert path != output
        assert output.read_bytes() == b"previous-complete-video"
        return False

    monkeypatch.setattr("app.providers.veo.is_valid_video", reject_video)
    provider = VeoProvider(
        backend="gemini",
        gemini_api_key="secret",
        project="",
        location="us-central1",
        model="veo-3.1-fast-generate-preview",
        output_gcs_uri=None,
        credentials_file=None,
    )

    with pytest.raises(RuntimeError, match="incomplete or invalid video"):
        provider._download_output(
            {
                "videos": [
                    {
                        "bytesBase64Encoded": base64.b64encode(
                            b"partial-new-video"
                        ).decode("ascii")
                    }
                ]
            },
            output,
        )

    assert inspected
    assert output.read_bytes() == b"previous-complete-video"
    assert not list(tmp_path.glob("*.downloading"))


def test_valid_download_is_promoted_only_after_validation(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "scene.mp4"
    output.write_bytes(b"previous-complete-video")

    def accept_video(path: Path) -> bool:
        assert path != output
        assert path.read_bytes() == b"complete-new-video"
        assert output.read_bytes() == b"previous-complete-video"
        return True

    monkeypatch.setattr("app.providers.veo.is_valid_video", accept_video)
    provider = VeoProvider(
        backend="gemini",
        gemini_api_key="secret",
        project="",
        location="us-central1",
        model="veo-3.1-fast-generate-preview",
        output_gcs_uri=None,
        credentials_file=None,
    )

    provider._download_output(
        {
            "videos": [
                {
                    "bytesBase64Encoded": base64.b64encode(
                        b"complete-new-video"
                    ).decode("ascii")
                }
            ]
        },
        output,
    )

    assert output.read_bytes() == b"complete-new-video"
    assert not list(tmp_path.glob("*.downloading"))


@pytest.mark.parametrize(
    ("payload", "expected_bytes", "expected_uri"),
    [
        (
            {"videos": [{"bytesBase64Encoded": "dmVydGV4"}]},
            "dmVydGV4",
            None,
        ),
        (
            {"generatedSamples": [{"video": {"uri": "https://example.test/video"}}]},
            None,
            "https://example.test/video",
        ),
        (
            {"predictions": [{"videos": [{"gcsUri": "gs://bucket/prediction.mp4"}]}]},
            None,
            "gs://bucket/prediction.mp4",
        ),
        (
            {"gcsUris": ["gs://bucket/output.mp4"]},
            None,
            "gs://bucket/output.mp4",
        ),
    ],
)
def test_output_parser_accepts_all_supported_vertex_shapes(
    payload: dict, expected_bytes: str | None, expected_uri: str | None
):
    found = VeoProvider._find_video_output(payload)

    assert found is not None
    video_bytes, uri, _item = found
    assert video_bytes == expected_bytes
    assert uri == expected_uri
