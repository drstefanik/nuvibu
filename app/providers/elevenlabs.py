from __future__ import annotations

from pathlib import Path

import httpx

from .base import MusicResult


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
        full_prompt = (
            f"{prompt}\nTempo: {bpm} BPM. Exact target duration: {duration_seconds} seconds. "
            "Original nursery song, gentle dynamics, clean lead vocal, highly intelligible Italian, "
            "no imitation of existing artists or songs. Lyrics:\n" + lyrics
        )
        payload = {
            "prompt": full_prompt,
            "music_length_ms": duration_seconds * 1000,
            "model_id": self.model_id,
        }
        response = httpx.post(
            "https://api.elevenlabs.io/v1/music",
            headers={"xi-api-key": self.api_key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
            params={"output_format": self.output_format},
            json=payload,
            timeout=600,
        )
        response.raise_for_status()
        output_path.write_bytes(response.content)
        # The exact account charge depends on the active ElevenLabs plan; store a transparent estimate only.
        estimated_cost = duration_seconds / 60 * 0.15
        return MusicResult(
            path=output_path,
            provider="elevenlabs-music",
            variant=variant,
            duration_seconds=float(duration_seconds),
            cost_usd=round(estimated_cost, 4),
            metadata={"model": self.model_id, "output_format": self.output_format},
        )
