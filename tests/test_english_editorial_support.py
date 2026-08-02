from __future__ import annotations

from app.services.lyrics_engine import _verbs
from app.services.safety import _sung_meter_profile
from scripts.patch_wimbledon_episode import LYRICS, STORYBOARD


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
