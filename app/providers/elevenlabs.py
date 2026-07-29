from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from .base import MusicResult


SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")


def _parse_lyric_sections(lyrics: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    name = "Song"
    lines: list[str] = []
    for raw_line in lyrics.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = SECTION_RE.match(line)
        if heading:
            if lines:
                sections.append((name, lines))
            name = heading.group(1).strip()
            lines = []
            continue
        lines.append(line)
    if lines:
        sections.append((name, lines))
    if not sections:
        raise ValueError("Lyrics must contain at least one sung line")
    return sections


def build_music_v2_composition_plan(
    *,
    lyrics: str,
    duration_seconds: int,
    bpm: int,
) -> dict:
    """Bind approved lyrics and exact section timing for Music v2."""

    sections = _parse_lyric_sections(lyrics)
    total_ms = duration_seconds * 1000
    if len(sections) > total_ms // 1000:
        raise ValueError(
            "The lyric has too many sections for the requested song duration"
        )
    # Give every section a minimum phrase while distributing the remainder by
    # sung-line count. The final correction makes the total exact.
    minimum_ms = min(3000, total_ms // len(sections))
    remaining_ms = total_ms - minimum_ms * len(sections)
    total_lines = sum(len(lines) for _name, lines in sections)
    durations: list[int] = []
    assigned = 0
    for index, (_name, lines) in enumerate(sections):
        if index == len(sections) - 1:
            duration_ms = total_ms - assigned
        else:
            share = remaining_ms * len(lines) // total_lines
            duration_ms = minimum_ms + share
        durations.append(duration_ms)
        assigned += duration_ms

    plan_sections = []
    for (name, lines), duration_ms in zip(sections, durations, strict=True):
        local_styles = [
            "clear warm lead vocal",
            "highly intelligible approved lyrics",
            "simple melody for very young children",
        ]
        if "ritornello" in name.casefold() or "chorus" in name.casefold():
            local_styles.extend(
                ["memorable singalong hook", "gentle hand claps"]
            )
        plan_sections.append(
            {
                "section_name": name,
                "duration_ms": duration_ms,
                "lines": lines,
                "positive_local_styles": local_styles,
                "negative_local_styles": [
                    "spoken narration",
                    "unapproved words",
                    "dense vocal runs",
                ],
            }
        )
    return {
        "positive_global_styles": [
            f"{bpm} BPM",
            "original modern preschool pop",
            "bright major key",
            "toy percussion and glockenspiel",
            "polished warm production",
        ],
        "negative_global_styles": [
            "dark",
            "aggressive",
            "frightening",
            "rapid tempo changes",
            "imitation of an existing song or artist",
        ],
        "sections": plan_sections,
    }


def music_request_fingerprint(
    *,
    lyrics: str,
    prompt: str,
    duration_seconds: int,
    bpm: int,
    variant: int,
    model_id: str,
    output_format: str,
) -> str:
    digest = hashlib.sha256()
    for value in (
        lyrics,
        prompt,
        str(duration_seconds),
        str(bpm),
        str(variant),
        model_id,
        output_format,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def music_receipt_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.name}.receipt.json")


class ElevenLabsMusicProvider:
    def __init__(self, *, api_key: str, model_id: str, output_format: str):
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is required in live mode")
        self.api_key = api_key
        self.model_id = model_id
        self.output_format = output_format

    def generate(
        self,
        *,
        lyrics: str,
        prompt: str,
        duration_seconds: int,
        bpm: int,
        output_path: Path,
        variant: int,
    ) -> MusicResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.model_id == "music_v2":
            payload = {
                "composition_plan": build_music_v2_composition_plan(
                    lyrics=lyrics,
                    duration_seconds=duration_seconds,
                    bpm=bpm,
                ),
                "model_id": self.model_id,
                "sign_with_c2pa": True,
            }
        else:
            full_prompt = (
                f"{prompt}\nTempo: {bpm} BPM. Exact target duration: "
                f"{duration_seconds} seconds. Original nursery song, gentle "
                "dynamics, clean lead vocal, highly intelligible lyrics, no "
                "imitation of existing artists or songs. Lyrics:\n" + lyrics
            )
            payload = {
                "prompt": full_prompt,
                "music_length_ms": duration_seconds * 1000,
                "model_id": self.model_id,
            }
        fingerprint = music_request_fingerprint(
            lyrics=lyrics,
            prompt=prompt,
            duration_seconds=duration_seconds,
            bpm=bpm,
            variant=variant,
            model_id=self.model_id,
            output_format=self.output_format,
        )
        receipt_path = music_receipt_path(output_path)
        if receipt_path.exists():
            try:
                prior_state = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Cannot safely reconcile ElevenLabs submission: {receipt_path}"
                ) from exc
            if prior_state.get("request_fingerprint") != fingerprint:
                raise RuntimeError(
                    f"ElevenLabs receipt belongs to another request: {receipt_path}"
                )
            if prior_state.get("state") != "complete":
                raise RuntimeError(
                    "A previous ElevenLabs submission has an ambiguous outcome; "
                    f"refusing to buy a duplicate song: {receipt_path}"
                )
            raise RuntimeError(
                f"Completed ElevenLabs receipt exists without a reusable ledger asset: {receipt_path}"
            )
        receipt_path.write_text(
            json.dumps(
                {
                    "state": "submitting",
                    "request_fingerprint": fingerprint,
                    "model": self.model_id,
                    "output_format": self.output_format,
                    "duration_seconds": duration_seconds,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        response: httpx.Response | None = None
        for attempt in range(3):
            response = httpx.post(
                "https://api.elevenlabs.io/v1/music",
                headers={"xi-api-key": self.api_key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
                params={"output_format": self.output_format},
                json=payload,
                timeout=600,
            )
            # Only retry an explicit rate-limit rejection. Retrying an
            # ambiguous 5xx could buy the same song twice.
            if response.status_code != 429:
                break
            if attempt < 2:
                retry_after = response.headers.get("retry-after")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt)
        assert response is not None
        if 400 <= response.status_code < 500:
            receipt_path.unlink(missing_ok=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("audio/") or len(response.content) < 1024:
            raise RuntimeError(
                f"ElevenLabs returned an invalid music payload: content-type={content_type!r}, "
                f"bytes={len(response.content)}"
            )
        output_path.write_bytes(response.content)
        # The exact account charge depends on the active ElevenLabs plan; store a transparent estimate only.
        estimated_cost = duration_seconds / 60 * 0.15
        song_id = response.headers.get("song-id") or response.headers.get("x-song-id")
        receipt_path.write_text(
            json.dumps(
                {
                    "state": "complete",
                    "request_fingerprint": fingerprint,
                    "song_id": song_id,
                    "model": self.model_id,
                    "output_format": self.output_format,
                    "duration_seconds": duration_seconds,
                    "estimated_cost_usd": round(estimated_cost, 4),
                    "bytes": len(response.content),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return MusicResult(
            path=output_path,
            provider="elevenlabs-music",
            variant=variant,
            duration_seconds=float(duration_seconds),
            cost_usd=round(estimated_cost, 4),
            metadata={
                "model": self.model_id,
                "output_format": self.output_format,
                "song_id": song_id,
                "request_fingerprint": fingerprint,
            },
        )
