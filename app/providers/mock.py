from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from .base import MusicResult, VideoResult


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _run(cmd: list[str]) -> None:
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{completed.stderr[-2500:]}")


class MockMusicProvider:
    """Creates an original, simple instrumental nursery loop for technical tests."""

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
        del lyrics, prompt
        output_path.parent.mkdir(parents=True, exist_ok=True)
        wav_path = output_path.with_suffix(".wav")
        sample_rate = 48_000
        total = int(sample_rate * duration_seconds)
        t = np.arange(total, dtype=np.float64) / sample_rate
        beat = 60.0 / max(60, bpm)
        notes = [261.63, 329.63, 392.00, 329.63, 293.66, 349.23, 440.00, 349.23]
        if variant % 2 == 0:
            notes = [293.66, 369.99, 440.00, 369.99, 261.63, 329.63, 392.00, 329.63]
        signal = np.zeros(total, dtype=np.float64)
        note_len = beat / 2
        for i, start in enumerate(np.arange(0, duration_seconds, note_len)):
            freq = notes[i % len(notes)]
            begin = int(start * sample_rate)
            end = min(total, int((start + note_len) * sample_rate))
            local_t = np.arange(end - begin, dtype=np.float64) / sample_rate
            envelope = np.minimum(local_t / 0.04, 1.0) * np.minimum((note_len - local_t) / 0.08, 1.0)
            envelope = np.clip(envelope, 0, 1)
            tone = 0.21 * np.sin(2 * math.pi * freq * local_t)
            tone += 0.08 * np.sin(2 * math.pi * freq * 2 * local_t)
            signal[begin:end] += tone * envelope
        # A soft kick at each beat, deliberately low-energy.
        for start in np.arange(0, duration_seconds, beat):
            begin = int(start * sample_rate)
            end = min(total, begin + int(0.12 * sample_rate))
            local_t = np.arange(end - begin, dtype=np.float64) / sample_rate
            kick = 0.08 * np.sin(2 * math.pi * (85 - 45 * local_t) * local_t) * np.exp(-25 * local_t)
            signal[begin:end] += kick
        signal = np.clip(signal, -0.9, 0.9)
        pcm = (signal * 32767).astype(np.int16)
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm.tobytes())
        _run([
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-b:a", "192k", str(output_path),
        ])
        wav_path.unlink(missing_ok=True)
        return MusicResult(
            path=output_path,
            provider="mock-original-instrumental",
            variant=variant,
            duration_seconds=float(duration_seconds),
            metadata={"publication_ready": False, "vocal": False, "bpm": bpm},
        )


class MockVideoProvider:
    """Creates a polished moving preview from approved still concepts.

    Mock mode deliberately does not pretend to be final animation. It validates the complete
    media pipeline without provider credits, but it now uses the approved Nuvibù art direction
    instead of geometric placeholder faces.
    """

    def __init__(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.concepts = [
            project_root / "brand" / "concepts" / "pulcini-arcobaleno.png",
            project_root / "brand" / "concepts" / "cucu-dietro-la-nuvola.png",
            project_root / "brand" / "source" / "nuvibu-key-art.png",
        ]

    @staticmethod
    def _cover(image: Image.Image, size: tuple[int, int], seed: int) -> Image.Image:
        target_w, target_h = size
        scale = max(target_w / image.width, target_h / image.height)
        resized = image.resize(
            (max(target_w, round(image.width * scale)), max(target_h, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        max_x = max(0, resized.width - target_w)
        max_y = max(0, resized.height - target_h)
        # Deterministic crop offsets create a little scene variation while preserving the subject.
        fx = [0.50, 0.42, 0.58][seed % 3]
        fy = [0.50, 0.45, 0.55][seed % 3]
        left = int(max_x * fx)
        top = int(max_y * fy)
        return resized.crop((left, top, left + target_w, top + target_h))

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
        del prompt
        output_path.parent.mkdir(parents=True, exist_ok=True)
        still_path = output_path.with_suffix(".png")

        references = [
            path
            for path in (reference_images or [])
            if path.exists()
        ]
        if reference_image and reference_image.exists() and reference_image not in references:
            references.insert(0, reference_image)
        source_path = (
            references[0]
            if references
            else self.concepts[seed % len(self.concepts)]
        )
        if not source_path.exists():
            raise RuntimeError(f"Mock concept image missing: {source_path}")

        source = Image.open(source_path).convert("RGB")
        image = self._cover(source, (1280, 720), seed)
        image = ImageEnhance.Color(image).enhance(1.05)
        image = ImageEnhance.Contrast(image).enhance(1.03)

        # Mark previews unmistakably as technical, so they cannot be confused with publish-ready output.
        draw = ImageDraw.Draw(image, "RGBA")
        label_font = _font(24, bold=True)
        label = "PREVIEW TECNICA • NON PUBBLICARE"
        box = draw.textbbox((0, 0), label, font=label_font, stroke_width=0)
        box_w = box[2] - box[0]
        x = 1280 - box_w - 42
        y = 28
        draw.rounded_rectangle((x - 16, y - 10, 1260, y + 36), radius=14, fill=(24, 19, 55, 175))
        draw.text((x, y), label, font=label_font, fill=(255, 255, 255, 245))
        image.save(still_path, quality=95)

        frames = max(1, duration_seconds * 25)
        # Subtle motion only: the real live provider must create character animation.
        direction = 1 if seed % 2 == 0 else -1
        x_expr = "iw/2-(iw/zoom/2)" if direction > 0 else "iw/2-(iw/zoom/2)+8*sin(on/45)"
        _run([
            "ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(still_path),
            "-vf",
            f"zoompan=z='min(zoom+0.00045,1.06)':x='{x_expr}':y='ih/2-(ih/zoom/2)':d={frames}:s=1280x720:fps=25,format=yuv420p",
            "-t", str(duration_seconds), "-r", "25", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(output_path),
        ])
        still_path.unlink(missing_ok=True)
        return VideoResult(
            path=output_path,
            provider="mock-approved-concept-preview",
            duration_seconds=float(duration_seconds),
            metadata={
                "publication_ready": False,
                "reference_used": bool(references),
                "reference_count": len(references),
                "source_image": str(source_path),
                "animation": "slow-pan-preview-only",
            },
        )
