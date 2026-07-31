from __future__ import annotations

import base64

import pytest

from app.providers.text_safe_veo import TextSafeVeoProvider
from app.providers.veo import VeoTerminalError


def provider() -> TextSafeVeoProvider:
    return TextSafeVeoProvider(
        project="",
        location="us-central1",
        model="veo-test",
        output_gcs_uri=None,
        credentials_file=None,
        backend="gemini",
        gemini_api_key="test-key",
    )


def test_sanitize_prompt_removes_quoted_lyric_but_keeps_visual_direction() -> None:
    raw_prompt = (
        "Original premium preschool 3D animation. "
        "Sung lyric cue: 'Scodinzola felice e va,'. "
        "Main action: Emma leads the animals in a dance. "
        "Create readable motion that visually enacts the literal meaning of the lyric cue. "
        "No empty minimalist scene, no generic flat clip-art look, no text, no logos, "
        "no camera shake."
    )

    safe_prompt = provider().sanitize_prompt(raw_prompt)

    assert "Scodinzola felice" not in safe_prompt
    assert "Sung lyric cue" not in safe_prompt
    assert "Main action: Emma leads the animals in a dance" in safe_prompt
    assert "no subtitles" in safe_prompt.lower()
    assert "bottom of the frame" in safe_prompt.lower()
    assert "garment print" in safe_prompt
    assert "nuvibu" not in safe_prompt.casefold()


def test_negative_prompt_bans_overlays_without_generic_text_conflict() -> None:
    payload = provider()._request_payload(
        prompt="Emma dances with her farm friends.",
        generation_duration=8,
        seed=173,
        reference_images=[],
    )

    negative_prompt = payload["parameters"]["negativePrompt"]
    negative_parts = {
        part.strip().casefold()
        for part in negative_prompt.split(",")
        if part.strip()
    }

    assert "subtitles" in negative_parts
    assert "captions" in negative_parts
    assert "karaoke lyrics" in negative_parts
    assert "bottom-screen text" in negative_parts
    assert "random letters" in negative_parts
    assert "text" not in negative_parts
    assert "logos" not in negative_parts


def test_independent_original_prompt_uses_only_neutral_scene_direction() -> None:
    raw_prompt = (
        "Original premium preschool 3D animation, rich commercial YouTube quality. "
        "Episode story: Emma and Billo discover a famous-looking train. "
        "Theme: Nuvibù colors. Characters on model: Emma, Billo. "
        "Sung lyric cue: 'Uno fa POP! Mille bolle su'. "
        "Main action: Emma presses a button while Billo reveals bubbles. "
        "Feature the concept 'bolla'. Shot: wide establishing shot. "
        "Emma is the recurring main character. Nuvibù is the channel."
    )

    safe_prompt = provider().independent_original_prompt(raw_prompt)
    normalized = safe_prompt.casefold()

    assert "main visual action:" in normalized
    assert "presses a button" in normalized
    assert "featured concept: bolla" in normalized
    assert "wide establishing shot" in normalized
    assert "reference image 1" in normalized
    assert "reference image 2" in normalized
    assert "reference image 3" in normalized
    assert "youtube" not in normalized
    assert "nuvibù" not in normalized
    assert "nuvibu" not in normalized
    assert "emma" not in normalized
    assert "billo" not in normalized
    assert "uno fa pop" not in normalized
    assert "famous-looking train" not in normalized


def test_third_party_block_retries_once_with_original_content_prompt(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("app.providers.veo.is_valid_video", lambda _path: True)
    veo = provider()
    output = tmp_path / "scene.mp4"
    submitted_prompts: list[str] = []
    operations = iter(["operations/blocked", "operations/recovered"])

    def start_operation(*, prompt: str, **_kwargs) -> str:
        submitted_prompts.append(prompt)
        return next(operations)

    def poll_operation(operation_name: str):
        if operation_name == "operations/blocked":
            raise VeoTerminalError(
                "Veo generation failed: The prompt could not be submitted due "
                "to the interests of third-party content providers. Support "
                "codes: 35561575"
            )
        return {
            "videos": [
                {
                    "bytesBase64Encoded": base64.b64encode(
                        b"recovered-video"
                    ).decode("ascii")
                }
            ]
        }

    monkeypatch.setattr(veo, "_start_operation", start_operation)
    monkeypatch.setattr(veo, "_poll_operation", poll_operation)

    result = veo.generate(
        prompt=(
            "Original premium preschool 3D animation, rich commercial YouTube "
            "quality. Characters on model: Emma, Billo. Sung lyric cue: 'POP'. "
            "Main action: Emma touches the first button and reveals bubbles. "
            "Feature the concept 'bolla'. Shot: wide establishing shot."
        ),
        duration_seconds=8,
        output_path=output,
        seed=173,
    )

    assert len(submitted_prompts) == 2
    assert "youtube" in submitted_prompts[0].casefold()
    assert "youtube" not in submitted_prompts[1].casefold()
    assert "emma" not in submitted_prompts[1].casefold()
    assert "billo" not in submitted_prompts[1].casefold()
    assert result.metadata["original_content_prompt_fallback"] is True
    assert output.read_bytes() == b"recovered-video"
    assert list(tmp_path.glob("scene.mp4.failed.*.json"))


def test_unrelated_veo_terminal_error_is_not_retried(tmp_path, monkeypatch) -> None:
    veo = provider()
    starts: list[str] = []
    monkeypatch.setattr(
        veo,
        "_start_operation",
        lambda **_kwargs: starts.append("start") or "operations/failed",
    )
    monkeypatch.setattr(
        veo,
        "_poll_operation",
        lambda _operation: (_ for _ in ()).throw(
            VeoTerminalError("Veo generation failed: invalid output")
        ),
    )

    with pytest.raises(VeoTerminalError, match="invalid output"):
        veo.generate(
            prompt="An original scene",
            duration_seconds=8,
            output_path=tmp_path / "scene.mp4",
            seed=173,
        )

    assert starts == ["start"]
