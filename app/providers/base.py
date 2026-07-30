from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class MusicResult:
    path: Path
    provider: str
    variant: int
    duration_seconds: float
    cost_usd: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class VideoResult:
    path: Path
    provider: str
    duration_seconds: float
    width: int = 1280
    height: int = 720
    cost_usd: float = 0.0
    metadata: dict = field(default_factory=dict)


class MusicProvider(Protocol):
    def generate(
        self,
        *,
        lyrics: str,
        prompt: str,
        music_direction: str = "",
        duration_seconds: int,
        bpm: int,
        output_path: Path,
        variant: int,
    ) -> MusicResult: ...


class VideoProvider(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        output_path: Path,
        seed: int,
        reference_image: Path | None = None,
        reference_images: list[Path] | None = None,
    ) -> VideoResult: ...
