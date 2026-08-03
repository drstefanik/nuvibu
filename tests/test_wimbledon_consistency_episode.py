from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Episode
from app.reference_presets import get_reference_preset
from app.services.lyrics_engine import _verbs
from app.services.pipeline import PipelineService
from app.services.safety import _sung_meter_profile
from scripts.create_wimbledon_consistency_episode import (
    EMMA_LOOK_ID,
    LYRICS,
    SOURCE_EPISODE_ID,
    STORYBOARD,
    WORKING_SLUG,
    upsert_episode,
)
from scripts.patch_wimbledon_episode import LYRICS as PREVIOUS_WIMBLEDON_LYRICS


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


def _source_episode(db, settings: Settings) -> Episode:
    source = Episode(
        id=SOURCE_EPISODE_ID,
        title="Game Set Match con Emma",
        working_slug="game-set-match-con-emma",
        age_min_months=9,
        age_max_months=36,
        theme="baby dance tennis a Wimbledon",
        hook="Emma e Ace giocano a tennis sul campo in erba",
        target_words=["game", "set", "match", "Wimbledon"],
        featured_characters=["Emma", "Ace"],
        duration_seconds=75,
        bpm=148,
        visual_pacing="energetic",
        language="en",
        lyrics_text=PREVIOUS_WIMBLEDON_LYRICS,
        concept_json={"emma_look_id": EMMA_LOOK_ID},
    )
    db.add(source)
    db.commit()
    preset = get_reference_preset("nanna-arcobaleno-v1")
    PipelineService(db, settings).save_reference_pack(
        source,
        {
            "emma": Path("ignored-by-the-look-catalog.png"),
            **preset.sources,
        },
        emma_look_id=EMMA_LOOK_ID,
    )
    return source


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
    assert all("No logos, text, trophy, crowd" in str(scene["action"]) for scene in STORYBOARD)

    meter = _sung_meter_profile(LYRICS)
    assert meter["syllables"]
    assert all(4 <= int(value) <= 20 for value in meter["syllables"])
    assert int(meter["max_span"]) <= 2

    detected = set(_verbs(" ".join(str(scene["action"]) for scene in STORYBOARD)))
    assert {
        "bounce",
        "point",
        "raise",
        "roll",
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
        _source_episode(db, settings)
        episode, changes = upsert_episode(db, settings)
        service = PipelineService(db, settings)

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
            for scene in episode.storyboard_json
        )

        same_episode, second_changes = upsert_episode(db, settings)
        assert same_episode.id == episode.id
        assert second_changes == {
            "created": False,
            "reference_pack_updated": False,
            "lyrics_updated": False,
            "storyboard_updated": False,
            "editorial_score": changes["editorial_score"],
        }
