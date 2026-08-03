from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.reference_presets import get_reference_preset
from app.services.lyrics_engine import _verbs
from app.services.pipeline import PipelineService
from app.services.safety import _sung_meter_profile
from scripts.create_wimbledon_consistency_episode import (
    EMMA_LOOK_ID,
    LYRICS,
    REFERENCE_PRESET_ID,
    STORYBOARD,
    WORKING_SLUG,
    upsert_episode,
)


def _settings(tmp_path: Path) -> Settings:
    database_path = tmp_path / "wimbledon-consistency.db"
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{database_path}",
        storage_root=tmp_path / "storage",
        provider_mode="mock",
    )
    settings.ensure_directories()
    return settings


def _session(settings: Settings):
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_wimbledon_consistency_script_loads_from_an_isolated_workdir(
    tmp_path: Path,
) -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "create_wimbledon_consistency_episode.py"
    )
    command = (
        "import runpy; "
        f"runpy.run_path({str(script)!r}, run_name='wimbledon_release_import_test')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_wimbledon_consistency_content_is_structurally_safe() -> None:
    assert sum(int(scene["duration_seconds"]) for scene in STORYBOARD) == 75
    assert all(4 <= int(scene["duration_seconds"]) <= 12 for scene in STORYBOARD)
    assert all(str(scene["lyric_cue"]) in LYRICS for scene in STORYBOARD)
    assert all(3 <= len(str(scene["action"])) <= 800 for scene in STORYBOARD)
    assert all("exactly one Emma" in str(scene["action"]) for scene in STORYBOARD)
    assert all("No cloud characters" in str(scene["action"]) for scene in STORYBOARD)
    assert all("never a cloud" in str(scene["action"]) for scene in STORYBOARD)
    assert sum("racket strings" in str(scene["action"]) for scene in STORYBOARD) >= 5

    meter = _sung_meter_profile(LYRICS)
    assert meter["syllables"]
    assert all(4 <= int(value) <= 20 for value in meter["syllables"])
    assert int(meter["max_span"]) <= 2

    detected = set(_verbs(" ".join(str(scene["action"]) for scene in STORYBOARD)))
    assert {
        "bounce",
        "follow",
        "move",
        "point",
        "raise",
        "roll",
        "serve",
        "toss",
        "touch",
        "turn",
        "wave",
    } <= detected


def test_release_creates_approved_episode_and_copies_reference_pack(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    Session = _session(settings)

    with Session() as db:
        episode, changes = upsert_episode(db, settings)
        service = PipelineService(db, settings)
        preset = get_reference_preset(REFERENCE_PRESET_ID)

        assert episode.working_slug == WORKING_SLUG
        assert changes["created"] is True
        assert changes["reference_pack_updated"] is True
        assert service.reference_pack_complete(episode) is True
        assert service.selected_emma_look_id(episode) == EMMA_LOOK_ID
        assert service.content_is_approved(episode, "lyrics") is True
        assert service.content_is_approved(episode, "storyboard") is True
        assert episode.concept_json["editorial_qc_preflight"]["result"]["passed"] is True
        assert len(episode.storyboard_json) == 10
        assert sum(
            int(scene["duration_seconds"])
            for scene in episode.storyboard_json
        ) == 75
        assert all(
            "no flashing" in str(scene["prompt"]).casefold()
            and "no frightening" in str(scene["prompt"]).casefold()
            and "never add a default cloud companion" in str(scene["prompt"]).casefold()
            for scene in episode.storyboard_json
        )
        assets = {
            service.explicit_reference_role(asset): asset
            for asset in service.reference_pack_assets(episode)
        }
        assert {
            role: (assets[role].metadata_json or {})["reference_preset_id"]
            for role in ("friends", "world")
        } == {
            "friends": REFERENCE_PRESET_ID,
            "world": REFERENCE_PRESET_ID,
        }
        assert {
            role: (assets[role].metadata_json or {})["source_sha256"]
            for role in ("friends", "world")
        } == {
            role: preset.sha256_for(role)
            for role in ("friends", "world")
        }

        same_episode, second_changes = upsert_episode(db, settings)
        assert same_episode.id == episode.id
        assert second_changes == {
            "created": False,
            "reference_pack_updated": False,
            "lyrics_updated": False,
            "storyboard_updated": False,
            "editorial_score": changes["editorial_score"],
        }
