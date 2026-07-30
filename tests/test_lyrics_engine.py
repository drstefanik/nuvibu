from __future__ import annotations

from pathlib import Path

from app.models import Asset, AssetKind, Episode
from app.services.lyrics_engine import (
    INITIAL_OVERUSE_WATCHLIST,
    editorial_audit,
    generate_song,
)
from app.services.prompts import generate_storyboard
from app.services.safety import review_episode
from tests.test_pipeline import make_episode


def make_pappi_episode() -> Episode:
    episode = make_episode(75)
    episode.title = "Pappì fa confusione"
    episode.theme = "animali e versi"
    episode.hook = "Pappì confonde i versi degli animali e poi trova il suo"
    episode.target_words = ["pappagallo", "cane", "gallina"]
    episode.featured_characters = ["Emma", "Pappì"]
    episode.bpm = 104
    return episode


def test_pappi_selects_error_and_correction_with_the_right_sound():
    generation = generate_song(make_pappi_episode())

    assert generation.selected.archetype == "errore_e_correzione"
    assert "cra cra" in generation.lyrics
    assert "bau bau" in generation.lyrics
    assert generation.selected.gag
    assert len(generation.selected.progression) == 5


def test_each_editorial_format_generates_four_distinct_archetypes():
    cases = [
        ("colori e trasformazioni", ["rosso", "giallo", "blu"]),
        ("baby dance", ["batti", "salta", "gira"]),
        ("cucù e sorpresa", ["stella", "luna"]),
        ("storia musicale", ["chiave", "ponte", "festa"]),
        ("nanna", ["luna", "nuvola", "stella"]),
    ]
    for index, (theme, targets) in enumerate(cases):
        episode = make_episode(75)
        episode.working_slug = f"format-{index}"
        episode.title = f"Format {index}"
        episode.theme = theme
        episode.target_words = targets
        episode.featured_characters = ["Emma", f"Amico {index}"]
        episode.bpm = 72 if theme == "nanna" else 100

        generation = generate_song(episode)

        assert len(generation.candidates) == 4
        assert len({candidate.archetype for candidate in generation.candidates}) == 4
        assert len({candidate.lyrics for candidate in generation.candidates}) == 4


def test_recent_catalog_blocks_overused_phrases_and_recycled_candidate():
    previous = make_episode(75)
    previous.title = "Modello vecchio"
    previous.lyrics_text = (
        "[Intro]\nGuarda bene: gatto è qui\n"
        "[Ritornello]\nBrilla piano, proprio così\n"
        "Conta fino a tre, cantalo con me"
    )
    previous.featured_characters = ["Emma", "Gatto"]
    previous.concept_json = {
        "editorial_generation": {
            "archetype": "domanda_e_risposta",
            "gag": "Il gatto risponde dal fienile",
            "progression": [
                "Emma sente un verso",
                "Il gatto compare dal fienile",
            ],
        }
    }
    episode = make_pappi_episode()

    generation = generate_song(
        episode,
        recent_episodes=[previous],
        catalog_episodes=[previous],
    )

    assert "guarda bene" in generation.memory.blocked_phrases
    assert "proprio così" in generation.memory.blocked_phrases
    assert "Gatto" in generation.memory.recent_characters
    assert generation.memory.recent_action_sequences
    lowered = generation.lyrics.casefold()
    assert all(phrase not in lowered for phrase in INITIAL_OVERUSE_WATCHLIST)


def test_identical_recent_lyrics_cannot_receive_a_perfect_qc_score(
    tmp_path: Path,
):
    previous = make_episode(75)
    previous.title = "Episodio precedente"
    generation = generate_song(previous)
    previous.lyrics_text = generation.lyrics

    current = make_episode(75)
    current.title = "Episodio riciclato"
    current.working_slug = "episodio-riciclato"
    current.lyrics_text = generation.lyrics
    current.storyboard_json = generate_storyboard(current)
    for kind in (
        AssetKind.MUSIC,
        AssetKind.RENDER,
        AssetKind.SHORT,
        AssetKind.THUMBNAIL,
    ):
        path = tmp_path / f"{kind.value}.bin"
        path.write_bytes(b"present")
        current.assets.append(
            Asset(
                kind=kind,
                path=str(path),
                mime_type="application/octet-stream",
                selected=True,
            )
        )

    result = review_episode(
        current,
        recent_episodes=[previous],
        catalog_episodes=[previous],
    )

    assert result.checks["catalog_originality"] is False
    assert result.score < 100
    assert result.passed is False
    assert any("troppo simile" in finding for finding in result.findings)


def test_editorial_audit_limits_recent_line_reuse_to_one():
    previous = make_episode(75)
    previous.lyrics_text = (
        "[Ritornello]\nIl gatto corre sopra il ponte\n"
        "Emma apre piano il portone\n"
        "La luna gira sopra il mare"
    )
    current = make_episode(75)
    current.lyrics_text = (
        "[Ritornello]\nIl gatto corre sopra il ponte\n"
        "Emma apre piano il portone\n"
        "Una stella cade nel prato"
    )

    audit = editorial_audit(
        current,
        recent_episodes=[previous],
        catalog_episodes=[previous],
    )

    assert len(audit["reused_phrases"]) == 2


def test_storyboard_uses_selected_narrative_progression():
    episode = make_pappi_episode()
    generation = generate_song(episode)
    episode.lyrics_text = generation.lyrics
    episode.concept_json = {
        "editorial_generation": generation.diagnostics(),
    }

    scenes = generate_storyboard(episode)

    expected_actions = set(generation.selected.progression)
    assert scenes[0]["action"] in expected_actions
    assert scenes[-1]["action"] in expected_actions
