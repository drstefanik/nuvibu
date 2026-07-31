from app.models import Episode
from app.schemas import EpisodeCreate
from app.services.safety import (
    _format_bpm_range,
    _sung_meter_profile,
    review_episode,
)


def test_baby_dance_accepts_turbo_bpm() -> None:
    assert _format_bpm_range("baby_dance") == (128, 155)
    episode = EpisodeCreate(
        title="Emma e Robot Bumbo",
        theme="baby dance",
        hook="Robot Bumbo parte in modalità turbo",
        bpm=148,
    )
    assert episode.bpm == 148


def test_format_bpm_upper_limits_match_the_episode_form_guidance() -> None:
    assert {
        song_format: _format_bpm_range(song_format)[1]
        for song_format in (
            "animali_e_versi",
            "colori_e_trasformazioni",
            "baby_dance",
            "cucu_e_sorpresa",
            "storia_musicale",
            "nanna",
        )
    } == {
        "animali_e_versi": 128,
        "colori_e_trasformazioni": 132,
        "baby_dance": 155,
        "cucu_e_sorpresa": 122,
        "storia_musicale": 128,
        "nanna": 82,
    }


def test_trenino_delle_bolle_accepts_recommended_bpm_and_visual_actions() -> None:
    assert _format_bpm_range("colori_e_trasformazioni") == (80, 132)

    lyrics = """[Intro - ritmico]
Tre bottoni sulla parete:
uno, due, tre... pronti? Premete!
Emma alza il dito, Billo fa: Ciuf ciuf!
Via col primo... POP! Le bolle vanno su!

[Ritornello]
Uno fa POP! Mille bolle su,
due fa CIUF CIUF! Parte il treno blu,
tre fa FLASH! Tutto rosso diverrà:
premi con Emma e la magia comincerà!

[Strofa 1]
Il primo fa bolle, leggere e rotonde,
saltano in alto come piccole onde.
Il secondo fischia: arriva il trenino!
Billo alza la paletta: Sali sul vagone!

Ma il terzo resta spento...
Silenzio: che farà?
Emma lo sfiora e... ROSSO!
Tutta la stanza ballerà!

[Strofa 2]
Emma ricomincia, piano e poi più in fretta:
POP, CIUF CIUF, FLASH! La parete scatta.
Si apre un grande tunnel, acceso e colorato,
il trenino delle bolle è pronto sul tracciato!
"""
    actions = [
        "Il primo tocco rivela una bolla",
        "Il secondo tocco rivela il trenino",
        "Il terzo tocco sembra non funzionare",
        "Rosso compare a sorpresa dietro Emma",
        "I tre tocchi attivano la trasformazione finale",
    ]
    episode = Episode(
        title="Emma e il Trenino delle Bolle",
        working_slug="emma-e-il-trenino-delle-bolle",
        age_min_months=9,
        age_max_months=48,
        theme="colori e trasformazioni",
        hook="Tre bottoni avviano il trenino delle bolle",
        target_words=["bolla", "trenino", "rosso"],
        featured_characters=["Emma", "Billo"],
        duration_seconds=75,
        bpm=132,
        visual_pacing="energetic",
        language="it",
        lyrics_text=lyrics,
        storyboard_json=[
            {
                "duration_seconds": 8,
                "lyric_cue": "Tre bottoni sulla parete:",
                "action": action,
                "prompt": "no flashing; no frightening imagery",
            }
            for action in actions
        ],
    )

    result = review_episode(episode, include_media=False)

    assert result.checks["format_bpm"] is True
    assert result.checks["verb_variety"] is True
    assert result.metrics["editorial"]["unique_verb_count"] >= 3


def test_meter_ignores_spoken_breaks_and_short_sound_effects() -> None:
    lyrics = """[Intro parlato]
Sistema acceso!
Tre, due, uno... TURBO!

[Strofa 1]
Bumbo corre fuori dal portone,
lascia impronte sopra il pallone.
Curva a destra, scatta più veloce,
Emma segue il ritmo e alza la voce.

[Ritornello]
Bumbo, Bumbo, bum bum bum!
Corri forte, zum zum zum!
Gira rapido, non fermarti mai,
premi il rosso e riparti: vai!

[Finale secco - voce robotica]
Missione completata!
BUM!
"""

    profile = _sung_meter_profile(lyrics)

    assert profile["syllables"]
    assert all(
        "parlato" not in section["section"].casefold()
        and "finale secco" not in section["section"].casefold()
        for section in profile["sections"]
    )
    assert profile["max_span"] <= 8
