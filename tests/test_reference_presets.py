from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from app.reference_presets import (
    REFERENCE_PACK_ROOT,
    ReferencePresetCatalogError,
    _load_reference_presets,
    _validated_png,
)


def write_png(
    path: Path,
    *,
    mode: str = "RGB",
    size: tuple[int, int] = (1280, 720),
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    color: str | int = "navy" if mode == "RGB" else 1
    Image.new(mode, size, color).save(path, "PNG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preset_definition(
    *,
    preset_id: str,
    directory: str,
    friends_sha256: str,
    world_sha256: str,
) -> dict[str, object]:
    return {
        "id": preset_id,
        "display_name": preset_id,
        "description": "Test preset",
        "directory": directory,
        "friends_filename": "friends.png",
        "world_filename": "world.png",
        "friends_sha256": friends_sha256,
        "world_sha256": world_sha256,
        "keywords": ("test",),
    }


def test_pinned_catalog_loads_complete_approved_images(tmp_path: Path):
    root = tmp_path / "reference-packs"
    friends_sha256 = write_png(root / "test-pack" / "friends.png")
    world_sha256 = write_png(root / "test-pack" / "world.png")

    presets = _load_reference_presets(
        (
            preset_definition(
                preset_id="test-pack-v1",
                directory="test-pack",
                friends_sha256=friends_sha256,
                world_sha256=world_sha256,
            ),
        ),
        root,
    )

    assert len(presets) == 1
    assert presets[0].friends_sha256 == friends_sha256
    assert presets[0].world_sha256 == world_sha256


@pytest.mark.parametrize("cutoff", [229_376, -12])
def test_catalog_rejects_truncated_png_even_when_its_digest_is_pinned(
    tmp_path: Path,
    cutoff: int,
):
    source = REFERENCE_PACK_ROOT / (
        "nanna-arcobaleno/03-mondo-episodio.png"
    )
    content = source.read_bytes()
    truncated = content[:cutoff]
    root = tmp_path / "reference-packs"
    path = root / "test-pack" / "world.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(truncated)

    with pytest.raises(
        ReferencePresetCatalogError,
        match="not a complete image",
    ):
        _validated_png(
            path,
            context="test-pack-v1.world",
            expected_sha256=hashlib.sha256(truncated).hexdigest(),
            root=root,
        )


def test_catalog_rejects_valid_image_with_unapproved_digest(tmp_path: Path):
    root = tmp_path / "reference-packs"
    path = root / "test-pack" / "world.png"
    write_png(path)

    with pytest.raises(
        ReferencePresetCatalogError,
        match="approved SHA-256",
    ):
        _validated_png(
            path,
            context="test-pack-v1.world",
            expected_sha256="0" * 64,
            root=root,
        )


@pytest.mark.parametrize(
    ("mode", "size"),
    [
        ("P", (1280, 720)),
        ("RGB", (640, 360)),
    ],
)
def test_catalog_rejects_wrong_mode_or_dimensions(
    tmp_path: Path,
    mode: str,
    size: tuple[int, int],
):
    root = tmp_path / "reference-packs"
    path = root / "test-pack" / "world.png"
    digest = write_png(path, mode=mode, size=size)

    with pytest.raises(
        ReferencePresetCatalogError,
        match="must be a complete RGB PNG at 1280x720",
    ):
        _validated_png(
            path,
            context="test-pack-v1.world",
            expected_sha256=digest,
            root=root,
        )


def test_catalog_rejects_reused_image_paths(tmp_path: Path):
    root = tmp_path / "reference-packs"
    friends_sha256 = write_png(root / "shared" / "friends.png")
    world_sha256 = write_png(root / "shared" / "world.png")
    definitions = tuple(
        preset_definition(
            preset_id=preset_id,
            directory="shared",
            friends_sha256=friends_sha256,
            world_sha256=world_sha256,
        )
        for preset_id in ("first-v1", "second-v1")
    )

    with pytest.raises(
        ReferencePresetCatalogError,
        match="reuses a reference preset image path",
    ):
        _load_reference_presets(definitions, root)


def test_runtime_validation_detects_file_replacement(tmp_path: Path):
    root = tmp_path / "reference-packs"
    friends_path = root / "test-pack" / "friends.png"
    world_path = root / "test-pack" / "world.png"
    friends_sha256 = write_png(friends_path)
    world_sha256 = write_png(world_path)
    preset = _load_reference_presets(
        (
            preset_definition(
                preset_id="test-pack-v1",
                directory="test-pack",
                friends_sha256=friends_sha256,
                world_sha256=world_sha256,
            ),
        ),
        root,
    )[0]
    Image.new("RGB", (1280, 720), "red").save(world_path, "PNG")

    with pytest.raises(
        ReferencePresetCatalogError,
        match="approved SHA-256",
    ):
        preset.validated_path_for("world")
