from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


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


def concatenate_scenes(scene_paths: list[Path], output_path: Path, target_duration: int) -> None:
    if not scene_paths:
        raise ValueError("At least one scene is required")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output_path.with_suffix(".concat.txt")
    concat_file.write_text("\n".join(f"file '{p.resolve()}'" for p in scene_paths), encoding="utf-8")
    run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-t", str(target_duration), "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p",
        "-r", "25", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(output_path),
    ])
    concat_file.unlink(missing_ok=True)


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


def create_thumbnail(title: str, output_path: Path, seed: int = 0) -> None:
    """Build a premium mock thumbnail from approved Nuvibù concept art.

    Production thumbnails should be generated and art-directed from the episode's final scene
    references. This function deliberately avoids the old geometric placeholder mascot.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parents[2]
    normalized = title.casefold()
    if "pulcin" in normalized or "color" in normalized:
        source_path = project_root / "brand" / "concepts" / "pulcini-arcobaleno.png"
        concept_has_title = True
    elif "cuc" in normalized or "nuvol" in normalized:
        source_path = project_root / "brand" / "concepts" / "cucu-dietro-la-nuvola.png"
        concept_has_title = True
    else:
        source_path = project_root / "brand" / "source" / "nuvibu-key-art.png"
        concept_has_title = False

    if not source_path.exists():
        raise FileNotFoundError(f"Thumbnail concept missing: {source_path}")

    image = _cover(Image.open(source_path).convert("RGB"), (1280, 720))
    image = ImageEnhance.Color(image).enhance(1.05)
    image = ImageEnhance.Contrast(image).enhance(1.04)
    draw = ImageDraw.Draw(image, "RGBA")

    if not concept_has_title:
        # Create a strong title panel for generic episodes.
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rounded_rectangle((44, 72, 720, 648), radius=42, fill=(47, 22, 96, 220))
        overlay = overlay.filter(ImageFilter.GaussianBlur(0.4))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(image, "RGBA")
        wrapped = _wrap_title(title)
        font = _fit_text(draw, wrapped, 600, 96, minimum=54)
        draw.multiline_text(
            (84, 150), wrapped, font=font, fill=(255, 255, 255, 255),
            spacing=8, stroke_width=4, stroke_fill=(91, 40, 166, 255),
        )
        draw.text((88, 555), "NUVIBÙ • CANZONI ORIGINALI", font=_font(27, bold=True), fill=(255, 222, 74, 255))

    # Keep mock outputs clearly separate from publish-ready creative.
    label = "CONCEPT PREVIEW"
    label_font = _font(22, bold=True)
    bbox = draw.textbbox((0, 0), label, font=label_font)
    width = bbox[2] - bbox[0]
    x = 1280 - width - 54
    draw.rounded_rectangle((x - 15, 24, 1258, 68), radius=14, fill=(24, 19, 55, 175))
    draw.text((x, 33), label, font=label_font, fill=(255, 255, 255, 245))
    image.save(output_path, quality=95)
