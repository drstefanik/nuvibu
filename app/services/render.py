from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageStat,
)


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def run(cmd: list[str]) -> None:
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{completed.stderr[-3500:]}")


def probe(path: Path) -> dict:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)
    return json.loads(completed.stdout)


def concatenate_scenes(
    scene_paths: list[Path],
    output_path: Path,
    target_duration: int,
    *,
    scene_durations: list[float] | None = None,
) -> None:
    if not scene_paths:
        raise ValueError("At least one scene is required")
    if scene_durations is None:
        scene_durations = [target_duration / len(scene_paths)] * len(scene_paths)
    if len(scene_durations) != len(scene_paths):
        raise ValueError("Each scene must have one planned duration")
    if any(duration <= 0 for duration in scene_durations):
        raise ValueError("Scene durations must be positive")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for index, (path, duration) in enumerate(zip(scene_paths, scene_durations, strict=True)):
        inputs.extend(["-i", str(path)])
        label = f"v{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[{index}:v]trim=start=0:duration={duration:.3f},setpts=PTS-STARTPTS,"
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            f"pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,format=yuv420p[{label}]"
        )
    filters.append(f"{''.join(labels)}concat=n={len(scene_paths)}:v=1:a=0[outv]")
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-t",
            str(target_duration),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def mux_music(video_path: Path, music_path: Path, output_path: Path, duration_seconds: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fade_out = max(0, duration_seconds - 1.8)
    run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-stream_loop", "-1", "-i", str(music_path),
        "-t", str(duration_seconds), "-map", "0:v:0", "-map", "1:a:0",
        "-af", f"afade=t=in:st=0:d=0.4,afade=t=out:st={fade_out}:d=1.8,loudnorm=I=-18:TP=-2:LRA=7,aresample=48000",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output_path),
    ])


def make_vertical_short(source_path: Path, output_path: Path, duration_seconds: int = 25) -> None:
    run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source_path), "-t", str(duration_seconds),
        "-vf", "scale=-2:1920,crop=1080:1920:(iw-1080)/2:0,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output_path),
    ])


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, minimum: int = 44):
    size = start_size
    while size >= minimum:
        font = _font(size, bold=True)
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=4, align="left", stroke_width=2)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 4
    return _font(minimum, bold=True)


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    scale = max(target_w / image.width, target_h / image.height)
    resized = image.resize(
        (max(target_w, round(image.width * scale)), max(target_h, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def _wrap_title(title: str, max_chars: int = 15) -> str:
    words = title.upper().split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and len(candidate) > max_chars:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines[:3])


def _video_duration_seconds(video_path: Path) -> float:
    payload = probe(video_path)
    duration = (payload.get("format") or {}).get("duration")
    try:
        parsed = float(duration)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Video duration is unavailable for thumbnail source: {video_path}"
        ) from exc
    if parsed <= 0:
        raise RuntimeError(
            f"Video duration is invalid for thumbnail source: {video_path}"
        )
    return parsed


def _extract_video_frame(
    video_path: Path,
    output_path: Path,
    *,
    timestamp_seconds: float,
) -> None:
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp_seconds:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            (
                "scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720,setsar=1"
            ),
            str(output_path),
        ]
    )


def _thumbnail_frame_score(image: Image.Image) -> float:
    """Prefer a bright, colourful and sharp real episode frame."""

    sample = image.resize((320, 180), Image.Resampling.BILINEAR)
    grayscale = sample.convert("L")
    edge_variance = ImageStat.Stat(
        grayscale.filter(ImageFilter.FIND_EDGES)
    ).var[0]
    hsv = sample.convert("HSV")
    saturation = ImageStat.Stat(hsv.getchannel("S")).mean[0]
    brightness = ImageStat.Stat(grayscale).mean[0]
    exposure_penalty = abs(brightness - 145.0) * 0.35
    return edge_variance + saturation * 0.9 - exposure_penalty


def _best_episode_frame(video_path: Path) -> Image.Image:
    duration = _video_duration_seconds(video_path)
    fractions = (0.18, 0.38, 0.58, 0.78)
    with tempfile.TemporaryDirectory(prefix="nuvibu-thumbnail-") as temp_dir:
        candidates: list[tuple[float, Image.Image]] = []
        for index, fraction in enumerate(fractions):
            timestamp = min(
                max(0.25, duration * fraction),
                max(0.25, duration - 0.25),
            )
            frame_path = Path(temp_dir) / f"frame-{index}.png"
            _extract_video_frame(
                video_path,
                frame_path,
                timestamp_seconds=timestamp,
            )
            with Image.open(frame_path) as frame:
                loaded = frame.convert("RGB")
                loaded.load()
            candidates.append((_thumbnail_frame_score(loaded), loaded))
        if not candidates:
            raise RuntimeError(
                f"No thumbnail frame could be extracted from {video_path}"
            )
        return max(candidates, key=lambda item: item[0])[1]


def _title_gradient(size: tuple[int, int]) -> Image.Image:
    width, height = size
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    solid_until = int(width * 0.34)
    fade_until = int(width * 0.66)
    for x in range(fade_until):
        if x <= solid_until:
            alpha = 210
        else:
            progress = (x - solid_until) / max(1, fade_until - solid_until)
            alpha = round(210 * (1 - progress))
        draw.line((x, 0, x, height), fill=(39, 15, 83, alpha))
    return overlay


def create_thumbnail(
    title: str,
    output_path: Path,
    *,
    source_video_path: Path,
) -> None:
    """Build a publish-ready thumbnail from the final episode itself."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = _cover(_best_episode_frame(source_video_path), (1280, 720))
    image = ImageEnhance.Color(image).enhance(1.12)
    image = ImageEnhance.Contrast(image).enhance(1.07).convert("RGBA")
    image = Image.alpha_composite(image, _title_gradient(image.size))
    draw = ImageDraw.Draw(image, "RGBA")

    draw.rounded_rectangle(
        (54, 46, 438, 96),
        radius=18,
        fill=(255, 255, 255, 232),
    )
    draw.text(
        (76, 57),
        "NUVIBÙ  •  EMMA & FRIENDS",
        font=_font(21, bold=True),
        fill=(70, 29, 125, 255),
    )
    wrapped = _wrap_title(title, max_chars=14)
    font = _fit_text(draw, wrapped, 540, 94, minimum=50)
    draw.multiline_text(
        (58, 170),
        wrapped,
        font=font,
        fill=(255, 255, 255, 255),
        spacing=6,
        stroke_width=4,
        stroke_fill=(70, 29, 125, 255),
    )
    draw.text(
        (62, 626),
        "UNA CANZONE ORIGINALE",
        font=_font(26, bold=True),
        fill=(255, 226, 83, 255),
    )
    image.convert("RGB").save(output_path, "PNG", optimize=True)
