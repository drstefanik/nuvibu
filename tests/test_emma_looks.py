from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from PIL import Image

from app.emma_looks import (
    CATALOG_MANIFEST_PATH,
    CATALOG_VERSION,
    EMMA_LOOK_CATALOG,
    EMMA_LOOKS,
    LEGACY_DEFAULT,
    LEGACY_DEFAULT_LOOK_ID,
    NEW_EPISODE_DEFAULT,
    NEW_EPISODE_DEFAULT_LOOK_ID,
    PROJECT_ROOT,
    EmmaLookCatalogError,
    _load_catalog,
    get_emma_look,
)


EXPECTED_LOOKS = (
    ("emma-classic-nuvibu-v1", "Classico Nuvibù"),
    ("emma-pink-dress-v1", "Rosa confetto"),
    ("emma-lilac-overalls-v1", "Salopette lilla"),
    ("emma-sunshine-romper-v1", "Sole giallo"),
    ("emma-sky-sailor-v1", "Marinaretta cielo"),
    ("emma-mint-pinafore-v1", "Grembiulino menta"),
    ("emma-peach-rainbow-v1", "Arcobaleno pesca"),
    ("emma-starry-bedtime-v1", "Nanna stellata"),
    ("emma-coral-party-v1", "Festa corallo"),
    ("emma-cream-winter-v1", "Inverno crema"),
    ("emma-aegean-swimsuit-v1", "Costumino Egeo"),
    ("emma-cyclades-dress-v1", "Abitino Cicladi"),
    ("emma-santorini-lemon-v1", "Limone di Santorini"),
    ("emma-bougainvillea-summer-v1", "Bouganville"),
    ("emma-peach-shell-v1", "Conchiglia pesca"),
    ("emma-summer-sage-v1", "Salvia d'estate"),
    ("emma-lavender-sunset-v1", "Lavanda al tramonto"),
    ("emma-sea-breeze-v1", "Brezza marina"),
    ("emma-wimbledon-tennis-v1", "Wimbledon"),
)


def _write_manifest(tmp_path: Path, transform) -> Path:
    manifest = json.loads(CATALOG_MANIFEST_PATH.read_text(encoding="utf-8"))
    transform(manifest)
    path = tmp_path / "emma_look_catalog.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def test_catalog_is_locked_ordered_and_immutable():
    assert CATALOG_VERSION == 3
    assert isinstance(EMMA_LOOKS, tuple)
    assert EMMA_LOOK_CATALOG is EMMA_LOOKS
    assert tuple((look.id, look.display_name) for look in EMMA_LOOKS) == EXPECTED_LOOKS
    assert len({look.id for look in EMMA_LOOKS}) == 19

    with pytest.raises(FrozenInstanceError):
        EMMA_LOOKS[0].display_name = "Alterato"


def test_catalog_defaults_and_lookup_are_strict():
    assert LEGACY_DEFAULT_LOOK_ID == "emma-classic-nuvibu-v1"
    assert NEW_EPISODE_DEFAULT_LOOK_ID == "emma-pink-dress-v1"
    assert LEGACY_DEFAULT == LEGACY_DEFAULT_LOOK_ID
    assert NEW_EPISODE_DEFAULT == NEW_EPISODE_DEFAULT_LOOK_ID
    assert get_emma_look(LEGACY_DEFAULT_LOOK_ID) is EMMA_LOOKS[0]
    assert get_emma_look(NEW_EPISODE_DEFAULT_LOOK_ID) is EMMA_LOOKS[1]

    with pytest.raises(ValueError, match="Unknown Emma look ID"):
        get_emma_look("../../arbitrary-file")
    with pytest.raises(ValueError, match="must be a string"):
        get_emma_look(None)


def test_catalog_media_and_public_paths_are_integral():
    for look in EMMA_LOOKS:
        assert look.path is look.reference_path
        assert look.reference_path.is_absolute()
        assert look.thumbnail_path.is_absolute()
        assert look.reference_path.name == f"{look.id}.png"
        assert look.thumbnail_path.name == f"{look.id}.webp"
        assert look.thumbnail_url == (
            f"/static/emma-looks/{look.id}.webp"
            f"?v={look.reference_sha256[:12]}"
        )
        assert look.outfit_prompt
        assert look.alt_text

        with Image.open(look.reference_path) as image:
            image.load()
            assert (image.format, image.mode, image.size) == (
                "PNG",
                "RGB",
                (1536, 1024),
            )
        with Image.open(look.thumbnail_path) as image:
            image.load()
            assert (image.format, image.mode, image.size) == (
                "WEBP",
                "RGB",
                (480, 320),
            )


def test_loader_fails_closed_when_catalog_count_changes(tmp_path: Path):
    manifest_path = _write_manifest(
        tmp_path,
        lambda manifest: manifest["looks"].pop(),
    )

    with pytest.raises(EmmaLookCatalogError, match="exactly 19"):
        _load_catalog(manifest_path, PROJECT_ROOT)


def test_loader_fails_closed_when_reference_hash_changes(tmp_path: Path):
    def tamper(manifest):
        manifest["looks"][0]["reference_sha256"] = "0" * 64

    manifest_path = _write_manifest(tmp_path, tamper)

    with pytest.raises(EmmaLookCatalogError, match="does not match"):
        _load_catalog(manifest_path, PROJECT_ROOT)


def test_loader_fails_closed_for_unsafe_paths(tmp_path: Path):
    def tamper(manifest):
        manifest["looks"][0]["reference_path"] = (
            "../brand/source/emma-looks/emma-classic-nuvibu-v1.png"
        )

    manifest_path = _write_manifest(tmp_path, tamper)

    with pytest.raises(EmmaLookCatalogError, match="safe relative path"):
        _load_catalog(manifest_path, PROJECT_ROOT)


def test_loader_fails_closed_for_invalid_defaults(tmp_path: Path):
    def tamper(manifest):
        manifest["new_episode_default"] = "emma-not-in-catalog-v1"

    manifest_path = _write_manifest(tmp_path, tamper)

    with pytest.raises(EmmaLookCatalogError, match="new_episode_default"):
        _load_catalog(manifest_path, PROJECT_ROOT)
