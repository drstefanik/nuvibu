from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable

from PIL import Image, UnidentifiedImageError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PACK_ROOT = PROJECT_ROOT / "reference-packs"
REFERENCE_PRESET_ROLES = ("friends", "world")


class ReferencePresetCatalogError(RuntimeError):
    """Raised when a bundled reference preset is incomplete or corrupted."""


@dataclass(frozen=True, slots=True)
class ReferencePreset:
    id: str
    display_name: str
    description: str
    friends_path: Path
    world_path: Path
    friends_sha256: str
    world_sha256: str
    keywords: tuple[str, ...]
    catalog_root: Path

    def path_for(self, role: str) -> Path:
        if role == "friends":
            return self.friends_path
        if role == "world":
            return self.world_path
        raise ValueError(f"Unknown reference preset role: {role!r}")

    def validated_path_for(self, role: str) -> Path:
        path = self.path_for(role)
        expected_sha256 = self.sha256_for(role)
        validated, _digest = _validated_png(
            path,
            context=f"{self.id}.{role}",
            expected_sha256=expected_sha256,
            root=self.catalog_root,
        )
        return validated

    def sha256_for(self, role: str) -> str:
        if role == "friends":
            return self.friends_sha256
        if role == "world":
            return self.world_sha256
        raise ValueError(f"Unknown reference preset role: {role!r}")

    def image_url(self, role: str) -> str:
        digest = self.sha256_for(role)
        return f"/reference-presets/{self.id}/{role}?v={digest[:12]}"

    @property
    def sources(self) -> dict[str, Path]:
        return {
            "friends": self.validated_path_for("friends"),
            "world": self.validated_path_for("world"),
        }


_PRESET_DEFINITIONS = (
    {
        "id": "la-fattoria-v1",
        "display_name": "La fattoria",
        "description": (
            "Nuvi, gatto, cane, mucca e pecora con la scenografia "
            "della fattoria."
        ),
        "directory": "la-fattoria",
        "friends_filename": "02-amici-episodio.png",
        "world_filename": "03-mondo-episodio.png",
        "friends_sha256": (
            "f68fd46ef79cde2dbd8d9970a97c8371eeaf28a6fce4bbcfb949ed45a8e15ccb"
        ),
        "world_sha256": (
            "d097b2fcb1661221912877b1d3b17faf34666e75d2d264aba8d69817caa81e77"
        ),
        "keywords": (
            "fattoria",
            "gatto",
            "cane",
            "mucca",
            "pecora",
            "animali",
        ),
    },
    {
        "id": "nanna-arcobaleno-v1",
        "display_name": "Nanna arcobaleno",
        "description": (
            "Nuvi, culla volante, arcobaleno e nuvolette nel cielo "
            "notturno di Nuvibù."
        ),
        "directory": "nanna-arcobaleno",
        "friends_filename": "02-amici-elementi-episodio.png",
        "world_filename": "03-mondo-episodio.png",
        "friends_sha256": (
            "3428a1673afac9ad41e08bba2811d3d4ab41971cd9c105cbf3ab32cbbbf1eb02"
        ),
        "world_sha256": (
            "b1b82b91d68c208e746b7538ce4b48c2b3078fe9b4ce9db660e4082fb317ba49"
        ),
        "keywords": (
            "nanna",
            "arcobaleno",
            "nuvolette",
            "culla",
            "sogna",
            "palloncini",
        ),
    },
)


def _validated_png(
    path: Path,
    *,
    context: str,
    expected_sha256: str,
    root: Path = REFERENCE_PACK_ROOT,
) -> tuple[Path, str]:
    if (
        len(expected_sha256) != 64
        or expected_sha256.lower() != expected_sha256
        or any(
            character not in "0123456789abcdef"
            for character in expected_sha256
        )
    ):
        raise ReferencePresetCatalogError(
            f"{context} has an invalid pinned SHA-256"
        )
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ReferencePresetCatalogError(
            f"{context} is missing or outside reference-packs"
        ) from exc
    if not resolved.is_file():
        raise ReferencePresetCatalogError(f"{context} is not a regular file")
    actual_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ReferencePresetCatalogError(
            f"{context} does not match its approved SHA-256"
        )
    try:
        with Image.open(resolved) as image:
            image.verify()
        with Image.open(resolved) as image:
            image.load()
            actual = (image.format, image.mode, image.size)
    except (OSError, UnidentifiedImageError) as exc:
        raise ReferencePresetCatalogError(
            f"{context} is not a complete image"
        ) from exc
    expected = ("PNG", "RGB", (1280, 720))
    if actual != expected:
        raise ReferencePresetCatalogError(
            f"{context} must be a complete RGB PNG at 1280x720, got {actual!r}"
        )
    return resolved, actual_sha256


def _load_reference_presets(
    definitions: tuple[dict[str, object], ...] = _PRESET_DEFINITIONS,
    root: Path = REFERENCE_PACK_ROOT,
) -> tuple[ReferencePreset, ...]:
    presets: list[ReferencePreset] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for definition in definitions:
        preset_id = str(definition["id"])
        if preset_id in seen_ids:
            raise ReferencePresetCatalogError(
                f"Duplicate reference preset ID: {preset_id}"
            )
        seen_ids.add(preset_id)
        directory = root / str(definition["directory"])
        friends_path, friends_sha256 = _validated_png(
            directory / str(definition["friends_filename"]),
            context=f"{preset_id}.friends",
            expected_sha256=str(definition["friends_sha256"]),
            root=root,
        )
        world_path, world_sha256 = _validated_png(
            directory / str(definition["world_filename"]),
            context=f"{preset_id}.world",
            expected_sha256=str(definition["world_sha256"]),
            root=root,
        )
        if friends_path == world_path or {
            friends_path,
            world_path,
        } & seen_paths:
            raise ReferencePresetCatalogError(
                f"{preset_id} reuses a reference preset image path"
            )
        seen_paths.update({friends_path, world_path})
        presets.append(
            ReferencePreset(
                id=preset_id,
                display_name=str(definition["display_name"]),
                description=str(definition["description"]),
                friends_path=friends_path,
                world_path=world_path,
                friends_sha256=friends_sha256,
                world_sha256=world_sha256,
                keywords=tuple(str(value) for value in definition["keywords"]),
                catalog_root=root.resolve(strict=True),
            )
        )
    return tuple(presets)


REFERENCE_PRESETS = _load_reference_presets()
_PRESETS_BY_ID = MappingProxyType(
    {preset.id: preset for preset in REFERENCE_PRESETS}
)


def get_reference_preset(preset_id: str) -> ReferencePreset:
    if not isinstance(preset_id, str):
        raise ValueError("Reference preset ID must be a string")
    try:
        return _PRESETS_BY_ID[preset_id]
    except KeyError as exc:
        raise ValueError(f"Unknown reference preset ID: {preset_id!r}") from exc


def recommend_reference_preset_id(parts: Iterable[object]) -> str | None:
    searchable = " ".join(
        str(value).casefold()
        for value in parts
        if value is not None and str(value).strip()
    )
    scored = [
        (
            sum(searchable.count(keyword.casefold()) for keyword in preset.keywords),
            preset.id,
        )
        for preset in REFERENCE_PRESETS
    ]
    best_score = max((score for score, _preset_id in scored), default=0)
    if best_score <= 0:
        return None
    winners = [
        preset_id for score, preset_id in scored if score == best_score
    ]
    return winners[0] if len(winners) == 1 else None


__all__ = [
    "REFERENCE_PRESETS",
    "REFERENCE_PRESET_ROLES",
    "ReferencePreset",
    "ReferencePresetCatalogError",
    "get_reference_preset",
    "recommend_reference_preset_id",
]
