from __future__ import annotations

from app.providers.text_safe_veo import TextSafeVeoProvider


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
    assert "'nuvibu'" in safe_prompt


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
