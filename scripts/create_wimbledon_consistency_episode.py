from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

# Cloud Run invokes maintenance scripts by file path, which otherwise places
# only /app/scripts on sys.path and makes the sibling app package unavailable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import AssetKind, Episode
from app.services.pipeline import PipelineService


SOURCE_EPISODE_ID = "33070fd0-6106-42f8-a856-b8e8b773ce5e"
WORKING_SLUG = "tap-tap-tennis-con-emma-wimbledon"
EMMA_LOOK_ID = "emma-wimbledon-tennis-v1"

TITLE = "Tap-Tap Tennis con Emma – Wimbledon"
THEME = "baby dance tennis a Wimbledon"
HOOK = (
    "Ace perde il ritmo del rimbalzo ed Emma glielo fa ritrovare con due "
    "piccoli tap sul campo da tennis di Wimbledon"
)
TARGET_WORDS = ["tap-tap", "bounce", "low", "high", "Wimbledon"]
FEATURED_CHARACTERS = ["Emma", "Ace"]

MUSIC_DIRECTION = """136 BPM, polished modern electro-pop baby dance with an immediate two-beat “tap-tap” hook, springy bass, crisp handclaps, light tennis-ball percussion, bright synth plucks and a clean four-on-the-floor pulse. Adult female lead vocal in clear British English, energetic, smiling and rhythmically precise. Ace has one consistent short spoken voice used only for the line “I can do it too!”; no continuous children's choir. Verses should feel like a playful mini-story, the pre-chorus should briefly pull back, and every chorus should return with the same strong melodic hook. Avoid nursery-rhyme melody, stadium crowd noise, long introductions, orchestral scoring, dense vocal layers and improvised lyrics. Preserve every approved word exactly and end cleanly at 75 seconds."""

LYRICS = """[Spoken Intro]

Oh!

Ace forgot the beat!

Emma, can you help?

Tap-tap!

Let's go!

[Chorus]

Tap-tap, Ace, stay on the beat!

Bounce-bounce-bounce by Emma's feet!

Low to the grass, high to the sky,

tap-tap tennis, give it a try!

[Verse 1]

Ace rolls slowly, quiet and round.

Emma taps her racket on the ground.

One little tap, then tap number two.

Ace starts bouncing: “I can do it too!”

[Pre-Chorus]

Hold it steady, watch it go.

Tap it gently, high then low.

[Chorus]

Tap-tap, Ace, stay on the beat!

Bounce-bounce-bounce by Emma's feet!

Low to the grass, high to the sky,

tap-tap tennis, give it a try!

[Verse 2]

Ace hops left and Ace hops right.

Emma points with a smile so bright.

Under the racket, over the line.

Ace keeps bouncing right on time.

[Dance Break]

Tap your knees and touch your toes.

Turn around, then strike a pose.

Racket up and racket down.

Ace makes one small circle round.

[Final Chorus]

Tap-tap, Ace, stay on the beat!

Bounce-bounce-bounce by Emma's feet!

Low to the grass, high to the sky,

Emma and Ace wave bye-bye!

[Spoken Outro]

Match point!

Tap-tap!"""


_CONTINUITY_LOCK = (
    "Continuity lock: exactly one Emma, one Ace and one racket; no other people "
    "or characters. Emma must match reference 1 exactly: same face, green eyes, "
    "baby proportions, high ponytail, headband, white-and-green dress, nappy "
    "cover and shoes. Ace must match reference 2 exactly; never add or remove "
    "features. The racket must match reference 1. Keep the same empty grass "
    "court, net, lines and daylight from reference 3. Never change face, scale, "
    "colors, wardrobe, props or layout. No logos, text, trophy, crowd or "
    "confetti. "
)


def _action(text: str) -> str:
    return _CONTINUITY_LOCK + text


STORYBOARD: list[dict[str, Any]] = [
    {
        "index": 0,
        "duration_seconds": 8,
        "word": "tap-tap",
        "lyric_cue": "Ace forgot the beat!",
        "action": _action(
            "Ace rests still beside the near baseline. Emma enters with two "
            "small steps, stops beside him, points down, then taps the racket "
            "head twice on the grass without touching Ace."
        ),
        "shot": "stable medium-wide opening at child eye level with a very gentle push-in",
    },
    {
        "index": 1,
        "duration_seconds": 8,
        "word": "stay on the beat",
        "lyric_cue": "Tap-tap, Ace, stay on the beat!",
        "action": _action(
            "Emma holds the racket in both hands and makes two clear downward "
            "taps on the same grass mark; Ace bounces twice beside her right "
            "shoe and settles in the same place."
        ),
        "shot": "front-facing full-body shot with a fixed camera and readable rhythm",
    },
    {
        "index": 2,
        "duration_seconds": 8,
        "word": "low and high",
        "lyric_cue": "Low to the grass, high to the sky,",
        "action": _action(
            "Emma lowers one open hand and then raises it slowly; Ace makes one "
            "low bounce followed by one shoulder-high bounce, staying beside "
            "Emma and never leaving the frame."
        ),
        "shot": "stable medium-wide shot with one restrained upward camera tilt",
    },
    {
        "index": 3,
        "duration_seconds": 8,
        "word": "roll",
        "lyric_cue": "Ace rolls slowly, quiet and round.",
        "action": _action(
            "Ace rolls slowly along the near white baseline for one metre and "
            "stops. Emma follows with three small steps, turns toward him and "
            "keeps the racket low at her side."
        ),
        "shot": "single lateral tracking shot at constant child-eye-level distance",
    },
    {
        "index": 4,
        "duration_seconds": 8,
        "word": "one two",
        "lyric_cue": "One little tap, then tap number two.",
        "action": _action(
            "Emma plants both feet, taps the racket head once on the left and "
            "once on the right; Ace begins a steady two-bounce rhythm and ends "
            "beside the same baseline."
        ),
        "shot": "locked full-body two-shot with no cut and no change of angle",
    },
    {
        "index": 5,
        "duration_seconds": 7,
        "word": "high then low",
        "lyric_cue": "Tap it gently, high then low.",
        "action": _action(
            "Emma holds the racket vertically and watches closely. Ace rises "
            "once to racket-head height, descends into one small bounce and "
            "stops while Emma smiles."
        ),
        "shot": "stable medium two-shot with a slow, minimal push-in",
    },
    {
        "index": 6,
        "duration_seconds": 7,
        "word": "bounce-bounce-bounce",
        "lyric_cue": "Bounce-bounce-bounce by Emma's feet!",
        "action": _action(
            "Emma repeats the signature two taps in the same court position; "
            "Ace performs exactly three small bounces beside her shoes while "
            "Emma nods on each beat."
        ),
        "shot": "front-facing medium-wide chorus shot with the camera locked off",
    },
    {
        "index": 7,
        "duration_seconds": 7,
        "word": "left and right",
        "lyric_cue": "Ace hops left and Ace hops right.",
        "action": _action(
            "Ace makes one short hop left and one short hop right across a "
            "single white line. Emma remains planted, points left and right, "
            "then returns the racket to the same resting position."
        ),
        "shot": "stable full-body shot with a very small left-to-right pan",
    },
    {
        "index": 8,
        "duration_seconds": 7,
        "word": "tennis pose",
        "lyric_cue": "Tap your knees and touch your toes.",
        "action": _action(
            "Emma taps both knees, bends once to touch her toes, turns only "
            "halfway and freezes with the racket raised. Ace rolls in one small "
            "circle on the grass and stops beside her."
        ),
        "shot": "fixed full-body movement shot with no montage and no close-up",
    },
    {
        "index": 9,
        "duration_seconds": 7,
        "word": "match point",
        "lyric_cue": "Emma and Ace wave bye-bye!",
        "action": _action(
            "Emma returns to the centre mark, taps the grass once and raises "
            "the racket in a clean final pose. Ace makes one final bounce, "
            "settles beside her, and Emma waves directly to the camera."
        ),
        "shot": "stable front-facing finale with a slow pull-back over the unchanged court",
    },
]


def _scene_signature(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ("index", "duration_seconds", "word", "lyric_cue", "action", "shot")
    return [
        {field: scene.get(field) for field in fields}
        for scene in scenes
    ]


def _episode_values() -> dict[str, Any]:
    return {
        "title": TITLE,
        "age_min_months": 9,
        "age_max_months": 36,
        "theme": THEME,
        "hook": HOOK,
        "target_words": TARGET_WORDS,
        "featured_characters": FEATURED_CHARACTERS,
        "duration_seconds": 75,
        "bpm": 136,
        "music_direction": MUSIC_DIRECTION,
        "visual_pacing": "energetic",
        "language": "en",
    }


def _generation_metadata() -> dict[str, Any]:
    return {
        "format": "baby_dance",
        "archetype": "errore_e_correzione",
        "concept": "Ace perde il ritmo e lo ritrova seguendo i tap di Emma",
        "gag": "Ace parte immobile e scopre un rimbalzo alla volta",
        "progression": [
            "Ace resta fermo accanto alla linea mentre Emma scopre il problema",
            "Emma prova due tap e Ace risponde con due piccoli rimbalzi",
            "Emma insegna ad Ace la differenza tra basso e alto",
            "Ace completa la sequenza sinistra-destra senza perdere il ritmo",
            "Emma e Ace chiudono sul punto centrale con un ultimo tap",
        ],
    }


def _visual_consistency_metadata() -> dict[str, Any]:
    return {
        "cast_count": 2,
        "characters": FEATURED_CHARACTERS,
        "single_world": True,
        "single_emma_look": EMMA_LOOK_ID,
        "fixed_props": ["one wooden racket", "one tennis ball named Ace"],
        "forbidden_elements": [
            "extra people",
            "crowd",
            "trophy",
            "confetti",
            "logos",
            "costume changes",
        ],
        "camera_policy": "stable shots; no montage; no rapid cuts",
    }


def _destination_reference_pack_matches(
    service: PipelineService,
    episode: Episode,
) -> bool:
    if service.selected_emma_look_id(episode) != EMMA_LOOK_ID:
        return False
    assets = {
        service.explicit_reference_role(asset): asset
        for asset in service.reference_pack_assets(episode)
    }
    if set(assets) != {"emma", "friends", "world"}:
        return False
    return all(
        (assets[role].metadata_json or {}).get("source_episode_id")
        == SOURCE_EPISODE_ID
        for role in ("friends", "world")
    )


def _copy_source_reference_pack(
    source_episode: Episode,
    destination_episode: Episode,
    service: PipelineService,
) -> None:
    source_assets = {
        service.explicit_reference_role(asset): asset
        for asset in service.reference_pack_assets(source_episode)
    }
    missing = {"emma", "friends", "world"} - set(source_assets)
    if missing:
        raise RuntimeError(
            "The approved Wimbledon source reference pack is incomplete: "
            + ", ".join(sorted(missing))
        )

    service.save_reference_pack(
        destination_episode,
        {
            "emma": Path(source_assets["emma"].path),
            "friends": Path(source_assets["friends"].path),
            "world": Path(source_assets["world"].path),
        },
        emma_look_id=EMMA_LOOK_ID,
        source_metadata={
            role: {
                "source_episode_id": SOURCE_EPISODE_ID,
                "source_asset_id": source_assets[role].id,
                "source_sha256": (
                    source_assets[role].metadata_json or {}
                ).get("stored_sha256"),
            }
            for role in ("friends", "world")
        },
    )


def upsert_episode(db, settings: Settings) -> tuple[Episode, dict[str, Any]]:
    source_episode = db.get(Episode, SOURCE_EPISODE_ID)
    if source_episode is None:
        raise RuntimeError(
            f"Wimbledon source episode not found: {SOURCE_EPISODE_ID}"
        )

    episode = db.scalar(
        select(Episode).where(Episode.working_slug == WORKING_SLUG)
    )
    created = episode is None
    if episode is None:
        episode = Episode(
            working_slug=WORKING_SLUG,
            concept_json={
                "emma_look_id": EMMA_LOOK_ID,
                "editorial_generation": _generation_metadata(),
                "visual_consistency": _visual_consistency_metadata(),
            },
            **_episode_values(),
        )
        db.add(episode)
        db.commit()
        db.refresh(episode)

    service = PipelineService(db, settings)
    active = service.active_job(episode)
    if active is not None:
        raise RuntimeError(
            f"Refusing editorial release while job {active.id} is {active.status.value}"
        )

    if not created:
        for field, value in _episode_values().items():
            setattr(episode, field, value)
        concept = dict(episode.concept_json or {})
        concept["emma_look_id"] = EMMA_LOOK_ID
        concept["editorial_generation"] = _generation_metadata()
        concept["visual_consistency"] = _visual_consistency_metadata()
        episode.concept_json = concept
        db.commit()

    reference_changed = not _destination_reference_pack_matches(
        service, episode
    )
    if reference_changed:
        _copy_source_reference_pack(source_episode, episode, service)
        episode = db.scalar(
            select(Episode).where(Episode.working_slug == WORKING_SLUG)
        )
        if episode is None:
            raise RuntimeError("Episode disappeared after reference pack update")
        service = PipelineService(db, settings)

    lyrics_changed = (
        (episode.lyrics_text or "").strip() != LYRICS
        or not service.has_valid_asset(episode, AssetKind.LYRICS)
    )
    if lyrics_changed:
        service.update_lyrics_draft(episode, LYRICS)
        episode = db.scalar(
            select(Episode).where(Episode.working_slug == WORKING_SLUG)
        )
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
        episode = db.scalar(
            select(Episode).where(Episode.working_slug == WORKING_SLUG)
        )
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

    return episode, {
        "created": created,
        "reference_pack_updated": reference_changed,
        "lyrics_updated": lyrics_changed,
        "storyboard_updated": storyboard_changed,
        "editorial_score": result.get("score"),
    }


def main() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        episode, changes = upsert_episode(db, settings)
        service = PipelineService(db, settings)
        print(
            json.dumps(
                {
                    "episode_id": episode.id,
                    "working_slug": episode.working_slug,
                    "emma_look_id": service.selected_emma_look_id(episode),
                    "reference_pack_complete": service.reference_pack_complete(
                        episode
                    ),
                    "lyrics_approved": service.content_is_approved(
                        episode, "lyrics"
                    ),
                    "storyboard_approved": service.content_is_approved(
                        episode, "storyboard"
                    ),
                    "status": episode.status.value,
                    **changes,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
