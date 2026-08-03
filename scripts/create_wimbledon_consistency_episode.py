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
from app.reference_presets import get_reference_preset
from app.services.pipeline import PipelineService


WORKING_SLUG = "emma-gioca-a-tennis-wimbledon-tap-tap"
EMMA_LOOK_ID = "emma-wimbledon-tennis-v1"
REFERENCE_PRESET_ID = "wimbledon-tennis-v1"

TITLE = "Emma gioca a tennis a Wimbledon – Tap-Tap!"
THEME = "baby dance tennis a Wimbledon"
HOOK = (
    "Emma insegna ad Ace a rimbalzare sulle corde, servire e superare la "
    "rete sul campo in erba di Wimbledon"
)
TARGET_WORDS = ["tennis ball", "racket", "serve", "forehand", "Wimbledon"]
FEATURED_CHARACTERS = ["Emma", "Ace"]

MUSIC_DIRECTION = """136 BPM, polished modern electro-pop baby dance with an immediate two-beat “tap-tap” hook, springy bass, crisp handclaps, light tennis-ball percussion, bright synth plucks and a clean four-on-the-floor pulse. Adult female lead vocal in clear British English, energetic, smiling and rhythmically precise; use the same adult voice for the short spoken intro and outro. No character voice and no continuous children's choir. Verses should feel like a playful tennis mini-story, the pre-chorus should briefly pull back, and every chorus should return with the same strong melodic hook. Avoid nursery-rhyme melody, stadium crowd noise, long introductions, orchestral scoring, dense vocal layers and improvised lyrics. Preserve every approved word exactly and end cleanly at 75 seconds."""

LYRICS = """[Spoken Intro]

Ball on the grass!

Racket in hand!

Emma, are you ready?

Tap-tap!

Let's play!

[Chorus]

Tap-tap, Ace, bounce on the strings!

Up and down, watch how Emma swings!

Over the net, one, two, three!

Tap-tap tennis, play with me!

[Verse 1]

Ace rolls slowly onto the court.

Emma lifts her racket for sport.

One small bounce, then number two.

Up goes Ace, so bright and true!

[Pre-Chorus]

Hold it steady, watch the ball.

Tap it gently, not too tall.

[Chorus]

Tap-tap, Ace, bounce on the strings!

Up and down, watch how Emma swings!

Over the net, one, two, three!

Tap-tap tennis, play with me!

[Verse 2]

Forehand left and backhand right.

Emma keeps the ball in sight.

Over the net and on the line.

Ace keeps bouncing right on time.

[Dance Break]

Step to the left and touch the line.

Step to the right, you're doing fine.

Racket ready, bend down low.

Swing up softly, watch Ace go!

[Final Chorus]

Tap-tap, Ace, bounce on the strings!

Up and down, watch how Emma swings!

Over the net, one, two, three!

Emma and Ace wave bye-bye!

[Spoken Outro]

Game!

Set!

Match!

Tap-tap!"""


_CONTINUITY_LOCK = (
    "Continuity lock: exactly one Emma, one Ace tennis ball and one wooden "
    "racket; no other people or characters. Reference 1 is the only Emma "
    "identity and outfit. Reference 2 is the only Ace: one yellow-green felt "
    "tennis ball with white seams and one tiny face, never a cloud, fluffy "
    "mascot, animal, dinosaur or child; never duplicate or transform Ace. "
    "Reference 3 is the same empty grass court. Keep face, scale, colors, "
    "wardrobe, racket, court and daylight fixed. The racket may contact only "
    "Ace. No cloud characters, extra people, crowd, trophy, confetti, text or "
    "logos. "
)


def _action(text: str) -> str:
    return _CONTINUITY_LOCK + text


STORYBOARD: list[dict[str, Any]] = [
    {
        "index": 0,
        "duration_seconds": 8,
        "word": "tennis ball",
        "lyric_cue": "Ball on the grass!",
        "action": _action(
            "Ace rests on the near baseline as a clearly visible tennis ball. "
            "Emma walks in holding the racket, stops beside Ace, points to the "
            "ball and raises the racket into a ready position."
        ),
        "shot": "stable medium-wide opening at child eye level with a very gentle push-in",
    },
    {
        "index": 1,
        "duration_seconds": 8,
        "word": "bounce on the strings",
        "lyric_cue": "Tap-tap, Ace, bounce on the strings!",
        "action": _action(
            "Emma holds the racket face horizontal at waist height. Ace makes "
            "exactly three gentle vertical bounces on the centre of the racket "
            "strings; the racket touches the tennis ball on every bounce."
        ),
        "shot": "stable front three-quarter full-body shot with racket and ball always visible",
    },
    {
        "index": 2,
        "duration_seconds": 8,
        "word": "serve",
        "lyric_cue": "Over the net, one, two, three!",
        "action": _action(
            "Emma holds Ace in her free hand, tosses the tennis ball just above "
            "her head, then makes one gentle overhead serve. The racket strings "
            "contact Ace once and Ace travels visibly over the net."
        ),
        "shot": "single stable side-wide shot showing Emma, contact point, net and ball flight",
    },
    {
        "index": 3,
        "duration_seconds": 8,
        "word": "roll",
        "lyric_cue": "Ace rolls slowly onto the court.",
        "action": _action(
            "Ace rolls back under the net along the centre line and stops in "
            "front of Emma. Emma follows the ball with her eyes, takes two small "
            "tennis steps and lowers the racket beside Ace."
        ),
        "shot": "single lateral tracking shot at constant child-eye-level distance",
    },
    {
        "index": 4,
        "duration_seconds": 8,
        "word": "forehand",
        "lyric_cue": "Forehand left and backhand right.",
        "action": _action(
            "Ace makes one low bounce in front of Emma. Emma turns sideways and "
            "plays one slow forehand: the centre of the racket strings gently "
            "contacts Ace and sends the tennis ball over the net."
        ),
        "shot": "locked side full-body shot showing preparation, contact and follow-through",
    },
    {
        "index": 5,
        "duration_seconds": 7,
        "word": "backhand",
        "lyric_cue": "Emma keeps the ball in sight.",
        "action": _action(
            "Ace bounces back toward Emma at knee height. Emma watches the ball, "
            "moves the racket across her body and plays one gentle backhand. The "
            "racket strings contact Ace once and send him cleanly over the net."
        ),
        "shot": "stable opposite-side full-body shot with the tennis ball visible throughout",
    },
    {
        "index": 6,
        "duration_seconds": 7,
        "word": "mini rally",
        "lyric_cue": "Ace keeps bouncing right on time.",
        "action": _action(
            "Emma completes a tiny two-shot rally with Ace: one forehand racket "
            "strings contact, one visible bounce on the far court, then one "
            "backhand racket strings contact. Ace remains one tennis ball."
        ),
        "shot": "single wide side view of the same court with no cut and no camera shake",
    },
    {
        "index": 7,
        "duration_seconds": 7,
        "word": "tennis footwork",
        "lyric_cue": "Step to the left and touch the line.",
        "action": _action(
            "Emma keeps Ace balanced on the racket strings, takes one small step "
            "left to touch the white line, then one small step right and returns "
            "to the centre without dropping the tennis ball."
        ),
        "shot": "stable front full-body shot with a minimal left-to-right pan",
    },
    {
        "index": 8,
        "duration_seconds": 7,
        "word": "match point",
        "lyric_cue": "Swing up softly, watch Ace go!",
        "action": _action(
            "Emma bends her knees in a ready pose, gently tosses Ace and plays "
            "one clear match-point serve. The racket contacts the tennis ball, "
            "which crosses the net and bounces inside the service box."
        ),
        "shot": "stable wide hero shot showing the full serve and marked landing point",
    },
    {
        "index": 9,
        "duration_seconds": 7,
        "word": "game set match",
        "lyric_cue": "Emma and Ace wave bye-bye!",
        "action": _action(
            "Emma returns to the centre mark and holds the racket horizontal. "
            "Ace makes one final bounce onto the strings and stays there as a "
            "tennis ball while Emma raises her free hand and waves to camera."
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
        "archetype": "comandi",
        "concept": "Emma e Ace eseguono veri gesti di tennis, dal palleggio al servizio",
        "gag": "Ace impara a rimbalzare sulle corde senza mai cambiare personaggio",
        "progression": [
            "Emma presenta Ace come una vera pallina da tennis sul campo",
            "Emma fa rimbalzare Ace sulle corde della racchetta",
            "Emma esegue un servizio che supera chiaramente la rete",
            "Emma mostra un diritto, un rovescio e un piccolo scambio",
            "Emma chiude con il match point e Ace fermo sulle corde",
        ],
    }


def _visual_consistency_metadata() -> dict[str, Any]:
    return {
        "cast_count": 2,
        "characters": FEATURED_CHARACTERS,
        "single_world": True,
        "single_emma_look": EMMA_LOOK_ID,
        "reference_preset_id": REFERENCE_PRESET_ID,
        "fixed_props": ["one wooden racket", "one tennis ball named Ace"],
        "forbidden_elements": [
            "extra people",
            "cloud characters",
            "fluffy mascots",
            "dinosaurs",
            "animals",
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
    preset = get_reference_preset(REFERENCE_PRESET_ID)
    return all(
        (assets[role].metadata_json or {}).get("reference_preset_id")
        == REFERENCE_PRESET_ID
        and (assets[role].metadata_json or {}).get("source_sha256")
        == preset.sha256_for(role)
        for role in ("friends", "world")
    )


def _apply_wimbledon_reference_pack(
    destination_episode: Episode,
    service: PipelineService,
) -> None:
    preset = get_reference_preset(REFERENCE_PRESET_ID)
    service.save_reference_pack(
        destination_episode,
        {
            "emma": Path("ignored-by-the-look-catalog.png"),
            **preset.sources,
        },
        emma_look_id=EMMA_LOOK_ID,
        source_metadata={
            role: {
                "reference_preset_id": REFERENCE_PRESET_ID,
                "source_sha256": preset.sha256_for(role),
            }
            for role in ("friends", "world")
        },
    )


def upsert_episode(db, settings: Settings) -> tuple[Episode, dict[str, Any]]:
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
        _apply_wimbledon_reference_pack(episode, service)
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
