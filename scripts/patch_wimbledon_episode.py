from __future__ import annotations

import json
from typing import Any

from app.config import get_settings
from app.database import SessionLocal
from app.models import AssetKind, Episode
from app.services.pipeline import PipelineService

EPISODE_ID = "33070fd0-6106-42f8-a856-b8e8b773ce5e"

LYRICS = """[Spoken Intro]

Hey!

Hey!

Emma!
Let's play!

Ready?

Go!

[Chorus]

Game!
Set!
Match!

Everybody clap!

Game!
Set!
Match!

Never look back!

Bounce!
Bounce!
One, two, three!

Come and play
with Emma and me!

Hey!

[Verse 1]

Bounce the ball,
up into the sky,

wave your racket
up to the sky!

Left foot,
right foot,

spin around,

everybody,
now touch the ground!

Hey!

[Pre-Chorus]

Ready...

Steady...

Serve!

Serve!

Serve!

GO!

[Chorus]

Game!
Set!
Match!

Everybody clap!

Game!
Set!
Match!

Never look back!

Bounce!
Bounce!
One, two, three!

Come and play
with Emma and me!

[Verse 2]

Run very fast,
touch the line,

smile because
you're doing fine!

Swing your racket,
left and then right,

everybody
shine so bright!

Bounce!

Bounce!

Hey!

[Break]

Clap!

Clap!

Jump!

Jump!

Spin!

Spin!

Game...

Set...

Match!

[Final Chorus]

Game!
Set!
Match!

Everybody clap!

Game!
Set!
Match!

Move like that!

Bounce!
Jump!
Spin around!

Champions
all together now!

Game!

Set!

Match!

Hey!

Hey!

One more time!

Game!

Set!

MATCH!"""

STORYBOARD: list[dict[str, Any]] = [
    {
        "index": 0,
        "duration_seconds": 8,
        "word": "Wimbledon",
        "lyric_cue": "Let's play!",
        "action": (
            "Emma raises her racket on the bright grass court while Ace "
            "appears, bounces toward her, and waves to invite everyone to play."
        ),
        "shot": "wide cinematic opening shot with a gentle push-in",
    },
    {
        "index": 1,
        "duration_seconds": 8,
        "word": "game set match",
        "lyric_cue": "Everybody clap!",
        "action": (
            "Emma claps twice, turns to the camera, and points forward while "
            "Ace jumps on the beat and the young players copy the signature move."
        ),
        "shot": "front-facing wide dance shot with bold rhythmic movement",
    },
    {
        "index": 2,
        "duration_seconds": 8,
        "word": "bounce",
        "lyric_cue": "One, two, three!",
        "action": (
            "Emma points to Ace, counts with her fingers, and touches the white "
            "line while the tennis ball bounces three times across the grass."
        ),
        "shot": "full-body tracking shot at child eye level",
    },
    {
        "index": 3,
        "duration_seconds": 8,
        "word": "racket",
        "lyric_cue": "wave your racket",
        "action": (
            "Emma lifts the racket, swings left, swings right, then spins once "
            "and smiles at the camera."
        ),
        "shot": "medium full-body tracking shot with clear gesture emphasis",
    },
    {
        "index": 4,
        "duration_seconds": 8,
        "word": "serve",
        "lyric_cue": "Serve!",
        "action": (
            "Emma tosses the ball, serves over the net, and follows Ace as he "
            "flies across the court with a short sparkling trail."
        ),
        "shot": "dynamic action shot following the serve across the net",
    },
    {
        "index": 5,
        "duration_seconds": 7,
        "word": "chorus",
        "lyric_cue": "Never look back!",
        "action": (
            "Emma dances at centre court, Ace circles around her, and the crowd "
            "claps in time with the chorus."
        ),
        "shot": "symmetrical wide dance shot with an energetic crowd reaction",
    },
    {
        "index": 6,
        "duration_seconds": 7,
        "word": "run",
        "lyric_cue": "Run very fast",
        "action": (
            "Emma runs to the sideline, touches it, turns back, and smiles with "
            "the two young players while Ace rolls beside them."
        ),
        "shot": "lateral tracking shot with playful sports energy",
    },
    {
        "index": 7,
        "duration_seconds": 7,
        "word": "rally",
        "lyric_cue": "left and then right",
        "action": (
            "Emma swings left and right during a playful rally; Ace skims the "
            "net, bounces once, and flies safely to the other side."
        ),
        "shot": "alternating medium action shots with clear left-and-right movement",
    },
    {
        "index": 8,
        "duration_seconds": 7,
        "word": "bridge",
        "lyric_cue": "Spin!",
        "action": (
            "Everyone claps twice, jumps twice, spins together, then freezes in "
            "a strong champion pose facing the camera."
        ),
        "shot": "fast rhythmic movement montage ending in a hero freeze",
    },
    {
        "index": 9,
        "duration_seconds": 7,
        "word": "finale",
        "lyric_cue": "all together now!",
        "action": (
            "Emma raises the racket, Ace flies toward the trophy, and everyone "
            "dances as golden confetti bursts above the grass court."
        ),
        "shot": "front-facing wide finale with a slow celebratory pull-back",
    },
]


def _scene_signature(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ("index", "duration_seconds", "word", "lyric_cue", "action", "shot")
    return [
        {field: scene.get(field) for field in fields}
        for scene in scenes
    ]


def main() -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        episode = db.get(Episode, EPISODE_ID)
        if episode is None:
            raise RuntimeError(f"Wimbledon episode not found: {EPISODE_ID}")
        service = PipelineService(db, settings)
        active = service.active_job(episode)
        if active is not None:
            raise RuntimeError(
                f"Refusing editorial patch while job {active.id} is {active.status.value}"
            )

        lyrics_changed = (
            (episode.lyrics_text or "").strip() != LYRICS
            or not service.has_valid_asset(episode, AssetKind.LYRICS)
        )
        if lyrics_changed:
            service.update_lyrics_draft(episode, LYRICS)
            episode = db.get(Episode, EPISODE_ID)
            if episode is None:
                raise RuntimeError("Episode disappeared after lyrics update")
            service = PipelineService(db, settings)

        if not service.content_is_approved(episode, "lyrics"):
            service.approve_content(episode, "lyrics")

        storyboard_changed = (
            _scene_signature(episode.storyboard_json or [])
            != _scene_signature(STORYBOARD)
            or not service.has_valid_asset(episode, AssetKind.STORYBOARD)
        )
        if storyboard_changed:
            service.update_storyboard_draft(episode, STORYBOARD)
            episode = db.get(Episode, EPISODE_ID)
            if episode is None:
                raise RuntimeError("Episode disappeared after storyboard update")
            service = PipelineService(db, settings)

        if not service.content_is_approved(episode, "storyboard"):
            service.approve_content(episode, "storyboard")

        snapshot = service.require_editorial_preflight(episode)
        result = snapshot.get("result", {})
        if not result.get("passed"):
            raise RuntimeError(
                "Editorial preflight did not pass: "
                + " | ".join(str(item) for item in result.get("findings", []))
            )

        print(
            json.dumps(
                {
                    "episode_id": episode.id,
                    "lyrics_updated": lyrics_changed,
                    "storyboard_updated": storyboard_changed,
                    "lyrics_approved": service.content_is_approved(episode, "lyrics"),
                    "storyboard_approved": service.content_is_approved(episode, "storyboard"),
                    "editorial_score": result.get("score"),
                    "status": episode.status.value,
                },
                ensure_ascii=False,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
