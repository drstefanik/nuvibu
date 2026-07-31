from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import VideoResult
from .veo import VeoProvider, VeoTerminalError


_AUDIO_CUE_RE = re.compile(
    r"\s*(?:Sung lyric cue|Lyric cue|Spoken line|Dialogue):.*?(?=\s+Main action:)",
    flags=re.IGNORECASE | re.DOTALL,
)
_LITERAL_LYRIC_RE = re.compile(
    r"\bthat visually enacts the literal meaning of the lyric cue\b",
    flags=re.IGNORECASE,
)
_MAIN_ACTION_RE = re.compile(
    r"\bMain action:\s*(.*?)(?=\s+Feature the concept\b|\s+Shot:|$)",
    flags=re.IGNORECASE | re.DOTALL,
)
_FEATURE_RE = re.compile(
    r"\bFeature the concept\s+['\"]?(.*?)['\"]?\.\s+Shot:",
    flags=re.IGNORECASE | re.DOTALL,
)
_SHOT_RE = re.compile(
    r"\bShot:\s*(.*?)(?=\s+Emma is\b|\s+Preserve\b|$)",
    flags=re.IGNORECASE | re.DOTALL,
)
_CHARACTERS_RE = re.compile(
    r"\bCharacters on model:\s*(.*?)(?=\.\s+(?:Sung lyric cue|Main action):)",
    flags=re.IGNORECASE | re.DOTALL,
)
_THIRD_PARTY_BLOCK_MARKERS = (
    "third-party content providers",
    "third party content providers",
    "interests of third-party",
    "interests of third party",
    "35561575",
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
        "Do not place text at the bottom of the frame. Preserve any small physical "
        "garment print already present in reference image 1 exactly as pictured, "
        "but do not create or repeat that lettering anywhere else."
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

    @staticmethod
    def _error_text(exc: Exception) -> str:
        parts = [str(exc)]
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                parts.append(str(response.text))
            except Exception:
                pass
        return " ".join(parts).casefold()

    @classmethod
    def is_third_party_prompt_block(cls, exc: Exception) -> bool:
        """Recognize the provider's original-content policy rejection."""

        message = cls._error_text(exc)
        return any(marker in message for marker in _THIRD_PARTY_BLOCK_MARKERS)

    @staticmethod
    def _match_text(pattern: re.Pattern[str], prompt: str, fallback: str) -> str:
        match = pattern.search(prompt)
        value = match.group(1).strip(" \t\r\n.'\"") if match else ""
        return re.sub(r"\s+", " ", value).strip() or fallback

    @classmethod
    def independent_original_prompt(cls, prompt: str) -> str:
        """Build a neutral visual brief after a third-party-content rejection.

        The fallback intentionally excludes the episode story, lyric, channel,
        platform and commercial-style wording.  It retains only the approved
        physical action, featured concept, camera direction and reference map.
        """

        action = cls._match_text(
            _MAIN_ACTION_RE,
            prompt,
            "The baby protagonist performs one clear playful action",
        )
        feature = cls._match_text(_FEATURE_RE, prompt, "the featured object")
        shot = cls._match_text(
            _SHOT_RE,
            prompt,
            "wide child-eye-level shot with gentle stable movement",
        )

        character_match = _CHARACTERS_RE.search(prompt)
        character_names = (
            [name.strip() for name in character_match.group(1).split(",")]
            if character_match
            else []
        )
        replacements = {
            "emma": "the baby protagonist from reference image 1",
            "nuvibù": "the small companion shown in the references",
            "nuvibu": "the small companion shown in the references",
        }
        for index, name in enumerate(character_names):
            if not name:
                continue
            replacement = (
                "the baby protagonist from reference image 1"
                if index == 0 or name.casefold() == "emma"
                else "a supporting friend from reference image 2"
            )
            replacements[name.casefold()] = replacement
        for name, replacement in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            action = re.sub(
                rf"\b{re.escape(name)}\b",
                replacement,
                action,
                flags=re.IGNORECASE,
            )
            feature = re.sub(
                rf"\b{re.escape(name)}\b",
                "the featured original subject",
                feature,
                flags=re.IGNORECASE,
            )

        fallback = (
            "Create an entirely original, independent preschool 3D animation in "
            "16:9. Use only the supplied reference images as the source for the "
            "character and environment designs. Do not imitate or recreate any "
            "existing film, television series, game, toy franchise, mascot, "
            "celebrity, artist or studio style. Reference image 1 defines the baby "
            "protagonist, reference image 2 defines the supporting cast, and "
            "reference image 3 defines the empty world. "
            f"Main visual action: {action}. Featured concept: {feature}. "
            f"Camera: {shot}. Keep identities, face, proportions, wardrobe, colors, "
            "materials, cast count and environment consistent with the references. "
            "Use soft cinematic lighting, vivid colors, clear readable motion and "
            "one focal action. No new characters, no frightening imagery, no rapid "
            "strobe, no flashing and no camera shake."
        )
        return re.sub(
            r"\s+",
            " ",
            f"{fallback} {cls.TEXT_OVERLAY_GUARD}",
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
        sanitized_prompt = self.sanitize_prompt(prompt)
        used_original_content_fallback = False
        try:
            result = super().generate(
                prompt=sanitized_prompt,
                duration_seconds=duration_seconds,
                output_path=output_path,
                seed=seed,
                reference_image=reference_image,
                reference_images=reference_images,
            )
        except Exception as exc:
            if not self.is_third_party_prompt_block(exc):
                raise
            # Google rejects this class of operation before it produces a
            # billable video. Retry once with a brand- and lyric-free brief;
            # all other provider errors remain fail-closed.
            result = super().generate(
                prompt=self.independent_original_prompt(prompt),
                duration_seconds=duration_seconds,
                output_path=output_path,
                seed=seed,
                reference_image=reference_image,
                reference_images=reference_images,
            )
            used_original_content_fallback = True
        result.metadata = {
            **result.metadata,
            "text_overlay_guard": "visual-only-v1",
            "original_content_prompt_fallback": used_original_content_fallback,
        }
        return result
