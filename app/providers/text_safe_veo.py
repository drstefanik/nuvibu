from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import VideoResult
from .veo import VeoProvider


_AUDIO_CUE_RE = re.compile(
    r"\s*(?:Sung lyric cue|Lyric cue|Spoken line|Dialogue):.*?(?=\s+Main action:)",
    flags=re.IGNORECASE | re.DOTALL,
)
_LITERAL_LYRIC_RE = re.compile(
    r"\bthat visually enacts the literal meaning of the lyric cue\b",
    flags=re.IGNORECASE,
)


class TextSafeVeoProvider(VeoProvider):
    """Veo adapter that keeps production lyrics out of the generated picture.

    Veo can interpret quoted lyric cues as instructions to draw karaoke captions.
    Nuvibù adds the soundtrack during final rendering, so the video request should
    contain only visual direction. The approved ``nuvibu`` print on Emma's shirt is
    the sole permitted lettering.
    """

    TEXT_OVERLAY_GUARD = (
        "VISUAL-ONLY OUTPUT. The soundtrack and lyrics are added later during editing. "
        "Never generate subtitles, captions, closed captions, karaoke lyrics, lyric "
        "overlays, lower thirds, title cards, credits, labels, speech bubbles, signs, "
        "watermarks, interface elements, letters, words or any other text overlay. "
        "Do not place text at the bottom of the frame. The only permitted lettering "
        "is the small physical lowercase white 'nuvibu' print already present on "
        "Emma's blue T-shirt in reference image 1; preserve that garment detail "
        "exactly and do not repeat it anywhere else."
    )

    NEGATIVE_TEXT_GUARD = (
        "subtitles, captions, closed captions, karaoke lyrics, lyric overlays, "
        "lower thirds, title cards, credits, speech bubbles, signs, labels, "
        "watermarks, user interface, bottom-screen text, random letters, "
        "misspelled words, duplicated lettering, added logos"
    )

    @classmethod
    def sanitize_prompt(cls, prompt: str) -> str:
        """Remove audio copy that can be mistaken for visible typography."""

        cleaned = _AUDIO_CUE_RE.sub("", prompt, count=1)
        cleaned = _LITERAL_LYRIC_RE.sub(
            "that communicates the featured concept through clear physical action",
            cleaned,
        )
        cleaned = cleaned.replace(
            "No empty minimalist scene, no generic flat clip-art look, no text, no logos,",
            (
                "No empty minimalist scene, no generic flat clip-art look, no "
                "subtitles, no captions, no title cards, no signage, no added logos,"
            ),
        )
        return re.sub(
            r"\s+",
            " ",
            f"{cleaned.strip()} {cls.TEXT_OVERLAY_GUARD}",
        ).strip()

    def _request_payload(
        self,
        *,
        prompt: str,
        generation_duration: int,
        seed: int,
        reference_images: list[Path],
    ) -> dict[str, Any]:
        payload = super()._request_payload(
            prompt=prompt,
            generation_duration=generation_duration,
            seed=seed,
            reference_images=reference_images,
        )
        parameters = payload["parameters"]
        existing = str(parameters.get("negativePrompt", ""))
        base_parts = [part.strip() for part in existing.split(",") if part.strip()]
        # Generic "text" and "logos" conflict with Emma's approved shirt print.
        # Replace them with precise overlay bans that preserve the physical garment.
        base_parts = [
            part
            for part in base_parts
            if part.casefold() not in {"text", "logos"}
        ]
        base_parts.extend(
            part.strip()
            for part in self.NEGATIVE_TEXT_GUARD.split(",")
            if part.strip()
        )
        parameters["negativePrompt"] = ", ".join(dict.fromkeys(base_parts))
        return payload

    def generate(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        output_path: Path,
        seed: int,
        reference_image: Path | None = None,
        reference_images: list[Path] | None = None,
    ) -> VideoResult:
        result = super().generate(
            prompt=self.sanitize_prompt(prompt),
            duration_seconds=duration_seconds,
            output_path=output_path,
            seed=seed,
            reference_image=reference_image,
            reference_images=reference_images,
        )
        result.metadata = {
            **result.metadata,
            "text_overlay_guard": "visual-only-v1",
        }
        return result
