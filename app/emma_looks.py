from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from PIL import Image, UnidentifiedImageError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_MANIFEST_PATH = PROJECT_ROOT / "data" / "emma_look_catalog.json"

_SAFE_ID = re.compile(r"^emma-[a-z0-9]+(?:-[a-z0-9]+)*-v[1-9][0-9]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_LOOKS = (
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
)
_EXPECTED_LEGACY_DEFAULT = "emma-classic-nuvibu-v1"
_EXPECTED_NEW_EPISODE_DEFAULT = "emma-pink-dress-v1"
_LOOK_FIELDS = frozenset(
    {
        "id",
        "display_name",
        "reference_path",
        "thumbnail_path",
        "reference_sha256",
        "outfit_prompt",
        "alt_text",
    }
)


class EmmaLookCatalogError(RuntimeError):
    """Raised when the locked Emma look catalog is incomplete or tampered with."""


@dataclass(frozen=True, slots=True)
class EmmaLook:
    id: str
    display_name: str
    reference_path: Path
    thumbnail_path: Path
    reference_sha256: str
    outfit_prompt: str
    alt_text: str

    @property
    def path(self) -> Path:
        """Compatibility alias for consumers that need the full reference image."""

        return self.reference_path

    @property
    def thumbnail_url(self) -> str:
        """Static URL with a content-derived cache-busting token."""

        return (
            f"/static/emma-looks/{self.id}.webp"
            f"?v={self.reference_sha256[:12]}"
        )


@dataclass(frozen=True, slots=True)
class _LoadedCatalog:
    version: int
    legacy_default: str
    new_episode_default: str
    looks: tuple[EmmaLook, ...]


def _require_mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EmmaLookCatalogError(f"{context} must be a JSON object")
    return value


def _require_nonempty_string(
    record: Mapping[str, Any],
    field: str,
    *,
    context: str,
) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise EmmaLookCatalogError(
            f"{context}.{field} must be a non-empty trimmed string"
        )
    return value


def _resolve_catalog_path(
    project_root: Path,
    raw_path: str,
    *,
    expected_parent: PurePosixPath,
    expected_filename: str,
    context: str,
) -> Path:
    if "\\" in raw_path:
        raise EmmaLookCatalogError(f"{context} must use POSIX separators")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise EmmaLookCatalogError(f"{context} is not a safe relative path")
    if relative.parent != expected_parent or relative.name != expected_filename:
        raise EmmaLookCatalogError(
            f"{context} must be {expected_parent / expected_filename}"
        )

    try:
        resolved_root = project_root.resolve(strict=True)
        candidate = (resolved_root / Path(*relative.parts)).resolve(strict=True)
    except OSError as exc:
        raise EmmaLookCatalogError(f"{context} does not exist") from exc
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise EmmaLookCatalogError(f"{context} escapes the project root") from exc
    if not candidate.is_file():
        raise EmmaLookCatalogError(f"{context} is not a regular file")
    return candidate


def _validate_image(
    path: Path,
    *,
    expected_format: str,
    expected_size: tuple[int, int],
    context: str,
) -> None:
    try:
        with Image.open(path) as image:
            image.load()
            actual_format = image.format
            actual_size = image.size
            actual_mode = image.mode
    except (OSError, UnidentifiedImageError) as exc:
        raise EmmaLookCatalogError(f"{context} is not a valid image") from exc

    if actual_format != expected_format:
        raise EmmaLookCatalogError(
            f"{context} must be {expected_format}, got {actual_format!r}"
        )
    if actual_size != expected_size:
        raise EmmaLookCatalogError(
            f"{context} must be {expected_size[0]}x{expected_size[1]}, "
            f"got {actual_size[0]}x{actual_size[1]}"
        )
    if actual_mode != "RGB":
        raise EmmaLookCatalogError(
            f"{context} must use RGB mode, got {actual_mode!r}"
        )


def _load_catalog(
    manifest_path: Path = CATALOG_MANIFEST_PATH,
    project_root: Path = PROJECT_ROOT,
) -> _LoadedCatalog:
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EmmaLookCatalogError(
            f"Unable to read Emma look catalog: {manifest_path}"
        ) from exc

    manifest = _require_mapping(raw_manifest, context="catalog")
    expected_top_level = {
        "catalog_version",
        "legacy_default",
        "new_episode_default",
        "looks",
    }
    if set(manifest) != expected_top_level:
        raise EmmaLookCatalogError(
            "catalog fields must be exactly: "
            + ", ".join(sorted(expected_top_level))
        )

    version = manifest["catalog_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise EmmaLookCatalogError(
            "catalog.catalog_version must be a positive integer"
        )
    legacy_default = _require_nonempty_string(
        manifest, "legacy_default", context="catalog"
    )
    new_episode_default = _require_nonempty_string(
        manifest, "new_episode_default", context="catalog"
    )

    raw_looks = manifest["looks"]
    if not isinstance(raw_looks, list):
        raise EmmaLookCatalogError("catalog.looks must be a JSON array")
    if len(raw_looks) != len(_EXPECTED_LOOKS):
        raise EmmaLookCatalogError(
            f"catalog.looks must contain exactly {len(_EXPECTED_LOOKS)} looks"
        )

    looks: list[EmmaLook] = []
    seen_ids: set[str] = set()
    seen_reference_paths: set[Path] = set()
    seen_thumbnail_paths: set[Path] = set()
    for position, (raw_look, expected) in enumerate(
        zip(raw_looks, _EXPECTED_LOOKS, strict=True),
        start=1,
    ):
        context = f"catalog.looks[{position - 1}]"
        record = _require_mapping(raw_look, context=context)
        if set(record) != _LOOK_FIELDS:
            raise EmmaLookCatalogError(
                f"{context} fields must be exactly: "
                + ", ".join(sorted(_LOOK_FIELDS))
            )

        look_id = _require_nonempty_string(record, "id", context=context)
        display_name = _require_nonempty_string(
            record, "display_name", context=context
        )
        if (look_id, display_name) != expected:
            raise EmmaLookCatalogError(
                f"{context} must be locked look {expected[0]!r} "
                f"named {expected[1]!r}"
            )
        if not _SAFE_ID.fullmatch(look_id):
            raise EmmaLookCatalogError(f"{context}.id is not a safe versioned ID")
        if look_id in seen_ids:
            raise EmmaLookCatalogError(f"Duplicate Emma look ID: {look_id}")
        seen_ids.add(look_id)

        raw_reference_path = _require_nonempty_string(
            record, "reference_path", context=context
        )
        raw_thumbnail_path = _require_nonempty_string(
            record, "thumbnail_path", context=context
        )
        reference_path = _resolve_catalog_path(
            project_root,
            raw_reference_path,
            expected_parent=PurePosixPath("brand/source/emma-looks"),
            expected_filename=f"{look_id}.png",
            context=f"{context}.reference_path",
        )
        thumbnail_path = _resolve_catalog_path(
            project_root,
            raw_thumbnail_path,
            expected_parent=PurePosixPath("app/static/emma-looks"),
            expected_filename=f"{look_id}.webp",
            context=f"{context}.thumbnail_path",
        )
        if reference_path in seen_reference_paths:
            raise EmmaLookCatalogError(
                f"Duplicate Emma reference path: {raw_reference_path}"
            )
        if thumbnail_path in seen_thumbnail_paths:
            raise EmmaLookCatalogError(
                f"Duplicate Emma thumbnail path: {raw_thumbnail_path}"
            )
        seen_reference_paths.add(reference_path)
        seen_thumbnail_paths.add(thumbnail_path)

        reference_sha256 = _require_nonempty_string(
            record, "reference_sha256", context=context
        )
        if not _SHA256.fullmatch(reference_sha256):
            raise EmmaLookCatalogError(
                f"{context}.reference_sha256 must be 64 lowercase hex characters"
            )
        actual_sha256 = hashlib.sha256(reference_path.read_bytes()).hexdigest()
        if not hmac.compare_digest(actual_sha256, reference_sha256):
            raise EmmaLookCatalogError(
                f"{context}.reference_sha256 does not match {raw_reference_path}"
            )

        _validate_image(
            reference_path,
            expected_format="PNG",
            expected_size=(1536, 1024),
            context=f"{context}.reference_path",
        )
        _validate_image(
            thumbnail_path,
            expected_format="WEBP",
            expected_size=(480, 320),
            context=f"{context}.thumbnail_path",
        )
        looks.append(
            EmmaLook(
                id=look_id,
                display_name=display_name,
                reference_path=reference_path,
                thumbnail_path=thumbnail_path,
                reference_sha256=reference_sha256,
                outfit_prompt=_require_nonempty_string(
                    record, "outfit_prompt", context=context
                ),
                alt_text=_require_nonempty_string(
                    record, "alt_text", context=context
                ),
            )
        )

    if legacy_default != _EXPECTED_LEGACY_DEFAULT:
        raise EmmaLookCatalogError(
            f"catalog.legacy_default must be {_EXPECTED_LEGACY_DEFAULT!r}"
        )
    if new_episode_default != _EXPECTED_NEW_EPISODE_DEFAULT:
        raise EmmaLookCatalogError(
            "catalog.new_episode_default must be "
            f"{_EXPECTED_NEW_EPISODE_DEFAULT!r}"
        )
    if legacy_default not in seen_ids or new_episode_default not in seen_ids:
        raise EmmaLookCatalogError("Emma look defaults must exist in catalog.looks")

    return _LoadedCatalog(
        version=version,
        legacy_default=legacy_default,
        new_episode_default=new_episode_default,
        looks=tuple(looks),
    )


_CATALOG = _load_catalog()

CATALOG_VERSION = _CATALOG.version
LEGACY_DEFAULT_LOOK_ID = _CATALOG.legacy_default
NEW_EPISODE_DEFAULT_LOOK_ID = _CATALOG.new_episode_default
LEGACY_DEFAULT = LEGACY_DEFAULT_LOOK_ID
NEW_EPISODE_DEFAULT = NEW_EPISODE_DEFAULT_LOOK_ID

EMMA_LOOKS = _CATALOG.looks
EMMA_LOOK_CATALOG = EMMA_LOOKS
_LOOKS_BY_ID = MappingProxyType({look.id: look for look in EMMA_LOOKS})


def get_emma_look(look_id: str) -> EmmaLook:
    """Return one locked look, rejecting arbitrary or unknown identifiers."""

    if not isinstance(look_id, str):
        raise ValueError("Emma look ID must be a string")
    try:
        return _LOOKS_BY_ID[look_id]
    except KeyError as exc:
        raise ValueError(f"Unknown Emma look ID: {look_id!r}") from exc


__all__ = [
    "CATALOG_VERSION",
    "EMMA_LOOKS",
    "EMMA_LOOK_CATALOG",
    "EmmaLook",
    "EmmaLookCatalogError",
    "LEGACY_DEFAULT",
    "LEGACY_DEFAULT_LOOK_ID",
    "NEW_EPISODE_DEFAULT",
    "NEW_EPISODE_DEFAULT_LOOK_ID",
    "get_emma_look",
]
