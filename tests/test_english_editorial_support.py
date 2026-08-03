from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Episode
from app.services.lyrics_engine import _verbs
from app.services.pipeline import PipelineService
from app.services.safety import _sung_meter_profile
from scripts.patch_wimbledon_episode import EMMA_LOOK_ID, LYRICS, STORYBOARD


def test_wimbledon_patch_can_load_when_invoked_by_file_path(tmp_path: Path) -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "patch_wimbledon_episode.py"
    )
    command = (
        "import runpy; "
        f"runpy.run_path({str(script)!r}, run_name='editorial_patch_import_test')"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_english_visual_verbs_are_recognised() -> None:
    actions = " ".join(str(scene["action"]) for scene in STORYBOARD)
    detected = set(_verbs(actions))
    assert {
        "raise",
        "bounce",
        "clap",
        "turn",
        "jump",
        "serve",
        "run",
        "touch",
        "spin",
        "dance",
    } <= detected


def test_wimbledon_revision_passes_structural_editorial_rules() -> None:
    assert sum(int(scene["duration_seconds"]) for scene in STORYBOARD) == 75
    assert all(str(scene["lyric_cue"]) in LYRICS for scene in STORYBOARD)
    assert all(4 <= int(scene["duration_seconds"]) <= 12 for scene in STORYBOARD)

    meter = _sung_meter_profile(LYRICS)
    assert meter["syllables"]
    assert all(4 <= int(value) <= 20 for value in meter["syllables"])
    assert int(meter["max_span"]) <= 8


def test_meter_ignores_short_english_movement_fragments() -> None:
    meter = _sung_meter_profile(
        """[Verse]
Swing your racket,
left then right,
everybody shine so bright!
"""
    )

    assert meter["syllables"]
    assert 3 not in meter["syllables"]
    assert all(4 <= int(value) <= 20 for value in meter["syllables"])


def test_wimbledon_storyboard_is_approved_by_the_real_quality_gate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "wimbledon-approval.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{database_path}",
        storage_root=tmp_path / "storage",
        provider_mode="mock",
    )
    settings.ensure_directories()

    with Session() as db:
        episode = Episode(
            title="Game Set Match con Emma",
            working_slug="game-set-match-con-emma",
            age_min_months=9,
            age_max_months=36,
            theme="baby dance",
            hook="Emma e Ace giocano a tennis sul prato di Wimbledon",
            target_words=["game", "set", "match", "Wimbledon"],
            featured_characters=["Emma", "Ace"],
            duration_seconds=75,
            bpm=148,
            visual_pacing="energetic",
            language="en",
        )
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)

        service.set_emma_look(episode, EMMA_LOOK_ID)
        service.update_lyrics_draft(episode, LYRICS)
        service.approve_content(episode, "lyrics")
        service.update_storyboard_draft(episode, STORYBOARD)
        service.approve_content(episode, "storyboard")

        assert service.content_is_approved(episode, "storyboard") is True
        assert service.selected_emma_look_id(episode) == EMMA_LOOK_ID
        result = episode.concept_json["editorial_qc_preflight"]["result"]
        assert result["passed"] is True
        assert result["checks"]["verb_variety"] is True
        assert result["checks"]["meter"] is True
