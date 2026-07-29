from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "brand" / "source" / "nuvibu-key-art.png"
BRAND = ROOT / "brand"
STATIC = ROOT / "app" / "static"


def font_path() -> Path:
    candidates = [
        Path("/usr/share/fonts/opentype/comic-neue/ComicNeue-Bold.otf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Install Comic Neue or DejaVu Sans to build the banner")


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(f"Missing approved key art: {SOURCE}")

    BRAND.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)
    source = Image.open(SOURCE).convert("RGB")

    source.resize((800, 800), Image.Resampling.LANCZOS).save(BRAND / "nuvibu-avatar-800.png", quality=95)
    source.resize((512, 512), Image.Resampling.LANCZOS).save(STATIC / "nuvibu-avatar.png", quality=95)

    width, height = 2560, 1440
    safe_width, safe_height = 1546, 423
    safe_x = (width - safe_width) // 2
    safe_y = (height - safe_height) // 2

    scale = max(width / source.width, height / source.height)
    background = source.resize(
        (round(source.width * scale), round(source.height * scale)), Image.Resampling.LANCZOS
    )
    left = (background.width - width) // 2
    top = (background.height - height) // 2
    background = background.crop((left, top, left + width, top + height)).filter(ImageFilter.GaussianBlur(24))
    background = ImageEnhance.Brightness(background).enhance(0.92)
    canvas = Image.alpha_composite(background.convert("RGBA"), Image.new("RGBA", (width, height), (255, 255, 255, 45)))
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle(
        (safe_x, safe_y, safe_x + safe_width, safe_y + safe_height),
        radius=80,
        fill=(255, 255, 255, 218),
        outline=(255, 255, 255, 238),
        width=5,
    )

    crop = source.crop((75, 5, source.width - 75, int(source.height * 0.72)))
    side = min(crop.size)
    crop = crop.crop(
        ((crop.width - side) // 2, (crop.height - side) // 2, (crop.width + side) // 2, (crop.height + side) // 2)
    ).resize((388, 388), Image.Resampling.LANCZOS)
    mask = Image.new("L", (388, 388), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 387, 387), fill=255)
    crop = crop.convert("RGBA")
    crop.putalpha(mask)
    canvas.alpha_composite(crop, (safe_x + 50, safe_y + 17))

    fp = font_path()
    title_font = ImageFont.truetype(str(fp), 150)
    subtitle_font = ImageFont.truetype(str(fp), 62)
    detail_font = ImageFont.truetype(str(fp), 30)
    text_x = safe_x + 500
    draw.text(
        (text_x, safe_y + 49),
        "NUVIBÙ",
        font=title_font,
        fill=(111, 64, 217, 255),
        stroke_width=6,
        stroke_fill=(255, 255, 255, 255),
    )
    draw.text(
        (text_x + 5, safe_y + 216),
        "EMMA & FRIENDS",
        font=subtitle_font,
        fill=(52, 48, 88, 255),
    )
    draw.text(
        (text_x + 5, safe_y + 304),
        "CANZONI  •  AVVENTURE  •  AMICI",
        font=detail_font,
        fill=(112, 88, 153, 255),
    )
    canvas.convert("RGB").save(BRAND / "nuvibu-youtube-banner-2560x1440.png", quality=94)

    print("Generated Emma-led avatar, app icon and YouTube banner.")


if __name__ == "__main__":
    main()
