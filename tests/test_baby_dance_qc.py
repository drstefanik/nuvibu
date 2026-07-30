from app.schemas import EpisodeCreate
from app.services.safety import _format_bpm_range, _sung_meter_profile


def test_baby_dance_accepts_turbo_bpm() -> None:
    assert _format_bpm_range("baby_dance") == (128, 155)
    episode = EpisodeCreate(
        title="Emma e Robot Bumbo",
        theme="baby dance",
        hook="Robot Bumbo parte in modalità turbo",
        bpm=148,
    )
    assert episode.bpm == 148


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
