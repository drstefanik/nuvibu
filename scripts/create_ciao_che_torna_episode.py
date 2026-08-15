from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.database import SessionLocal
from app.models import AssetKind, Episode
from app.services.pipeline import PipelineService


WORKING_SLUG = "emma-il-ciao-che-torna"
EMMA_LOOK_ID = "emma-pink-dress-v1"

TITLE = "Emma e il Ciao che Torna"
THEME = "saluti, gentilezza e reciprocità"
HOOK = "Emma saluta le persone che incontra nella piazza e scopre che ogni saluto gentile torna indietro."
TARGET_WORDS = ["ciao", "buongiorno", "saluta", "sorridi", "aspetta", "gentile"]
FEATURED_CHARACTERS = [
    "Emma",
    "Postina",
    "Signora con cagnolino",
    "Bambino sul monopattino",
    "Fornaio",
    "Bambino timido",
]

MUSIC_DIRECTION = """120 BPM, polished sunny walking-pop for preschool children, light funky groove, muted rhythmic guitar, warm elastic bass, crisp handclaps, small brass accents and dry playful percussion. Adult female lead vocal, bright, warm, clear and rhythmically precise. Emma only has short spoken or sung greetings such as 'Ciao!' and 'Buongiorno!'. Each passer-by must audibly answer Emma with a distinct natural voice. Use a tiny half-beat musical pause immediately before each returned greeting so the call-and-response is unmistakable. Children's choir only in the final 16 seconds. Avoid generic nursery-rhyme melody, ukulele-led preschool clichés, fantasy sound effects, continuous children's choir and improvised lyrics. Keep the approved Italian lyrics exact and end cleanly at 160 seconds."""

LYRICS = """[Intro]
Chi passa di qua?
Emma fa: «Ciao!»
E ogni saluto
poi ritorna qua!

[Strofa 1]
Passa la postina
con la borsa gialla,
cammina tic e tac
mentre una busta balla.

Emma dice: «Buongiorno!»
con voce allegra e chiara.
«Buongiorno a te, Emma!»
e la piazza si rischiara.

Passa una signora
col cagnolino accanto,
Emma muove la mano,
lui scodinzola tanto.

«Ciao, signora!»
«Ciao, Emma!»
Il saluto se ne va,
fa un piccolo giro
e poi ritorna qua!

[Ritornello]
Ciao, ciao,
chi passa di qua?
Emma fa un sorriso
e saluta: «Ciao!»

Ciao parte,
ciao torna,
vola su e giù,
quando un ciao ritorna
il giorno splende di più!

Mano in alto,
occhi attenti,
aspetta un po’...
«Ciao, Emma!»
Eccolo qua:
il ciao tornò!

[Movimento]
Guarda, sorridi,
saluta così,
poi aspetta un momento
e rimani lì.

Forte o pianino,
veloce o lento,
ogni ciao
ha il suo momento.

[Strofa 2]
Arriva un bambino
sul monopattino,
rallenta nella piazza,
passa pian pianino.

Emma dice: «Ciao!»
«Ciao, Emma!» risponde,
due manine nell’aria,
due facce gioconde.

Poi passa il fornaio
col pane profumato.
Emma: «Buongiorno!»
Lui si è già voltato.

«Buongiorno, Emma!»
Uno, due, tre:
il ciao fa un giro
e ritorna da te!

[Ritornello breve]
Ciao, ciao,
chi passa di qua?
Emma fa un sorriso
e saluta: «Ciao!»

Ciao parte,
ciao torna:
«Ciao, Emma!»
E il giorno
splende di più!

[Bridge]
C’è chi dice ciao forte,
chi lo dice piano,
chi sorride soltanto
e alza una mano.

Emma aspetta
senza fretta...
poi un bimbo un po’ timido
sussurra: «Ciao!»

[Finale]
Ciao, ciao,
tutta la città,
un saluto gentile
va e ritornerà.

«Ciao, Emma!»
«Ciao a tutti!»
«Ciao anche a te!»

A presto, amici!
Uno, due, tre...
Ciao!"""


CONTINUITY_LOCK = (
    "Continuity lock: exactly one Emma throughout the episode. Emma keeps the same face, "
    "cerulean blue-green eyes, hairstyle, proportions and pink-dress reference look in all scenes. "
    "The same sunny pedestrian square is used for all 160 seconds: bakery storefront, bench, tree "
    "and small fountain remain in fixed positions with constant morning light. Recurring passers-by "
    "must remain identical whenever they reappear: postal worker in turquoise jacket with mustard-yellow "
    "mailbag; elderly woman in coral cardigan with one small white dog on a red leash; curly-haired child "
    "with red helmet and blue scooter; baker in green shirt with cream apron and one bread bag; shy child "
    "with yellow backpack. No dinosaurs, fantasy creatures, talking objects, character transformations, "
    "floating objects, tennis equipment, random clouds, duplicate Emma, costume changes, extra animals, "
    "aggressive movements, physical contact with strangers, random crowds, text or logos. Every person "
    "greeted by Emma must visibly look at her and return the greeting before leaving the scene. "
)


def action(text: str) -> str:
    return CONTINUITY_LOCK + text


SCENES: list[dict[str, Any]] = [
    {"index": 0, "duration_seconds": 8, "word": "ciao", "lyric_cue": "Chi passa di qua? Emma fa: Ciao!", "action": action("Emma enters the square, notices the postal worker approaching, stops, smiles and raises one hand in a clear greeting. The postal worker sees Emma and begins raising her hand back."), "shot": "wide cinematic opening, gentle forward tracking at Emma eye level"},
    {"index": 1, "duration_seconds": 8, "word": "postina", "lyric_cue": "Passa la postina con la borsa gialla", "action": action("The same postal worker walks steadily past Emma carrying the same mustard-yellow mailbag. One envelope moves slightly inside the bag. Emma watches her approach without changing position."), "shot": "medium side-tracking two-shot"},
    {"index": 2, "duration_seconds": 8, "word": "buongiorno", "lyric_cue": "Emma dice: Buongiorno!", "action": action("Emma clearly says Buongiorno and waves once. The postal worker stops briefly, looks directly at Emma, smiles and clearly answers Buongiorno a te, Emma, returning the wave."), "shot": "clean child-eye-level two-shot, no rapid cutting"},
    {"index": 3, "duration_seconds": 8, "word": "signora", "lyric_cue": "Passa una signora col cagnolino accanto", "action": action("The elderly woman in the coral cardigan walks past with the same small white dog on the red leash. Emma raises her hand. The woman turns toward Emma while the dog gently wags its tail."), "shot": "wide lateral shot"},
    {"index": 4, "duration_seconds": 8, "word": "ciao torna", "lyric_cue": "Ciao, signora! Ciao, Emma!", "action": action("Emma says Ciao, signora. The woman clearly replies Ciao, Emma, smiling and waving, then continues walking with the dog. Emma follows the returning wave with her eyes."), "shot": "medium two-shot with gentle pan"},
    {"index": 5, "duration_seconds": 8, "word": "chi passa", "lyric_cue": "Ciao, ciao, chi passa di qua?", "action": action("Emma walks slowly along the same path, turns toward a familiar passer-by, smiles and performs one large easy-to-copy wave."), "shot": "medium frontal tracking shot moving backward"},
    {"index": 6, "duration_seconds": 8, "word": "parte e torna", "lyric_cue": "Ciao parte, ciao torna", "action": action("The postal worker and elderly woman are farther across the same square. Emma waves from a distance. Both recognize her and return the greeting one after the other without approaching."), "shot": "wide spatial continuity shot"},
    {"index": 7, "duration_seconds": 8, "word": "aspetta", "lyric_cue": "Mano in alto, occhi attenti, aspetta un po’", "action": action("Emma raises her hand, looks attentively and waits. After a brief pause, the postal worker clearly calls Ciao, Emma. Emma reacts with a delighted smile and remains in place."), "shot": "medium close-up on Emma with postal worker visible behind"},
    {"index": 8, "duration_seconds": 8, "word": "guarda sorridi", "lyric_cue": "Guarda, sorridi, saluta così", "action": action("Facing the audience, Emma slowly demonstrates the full sequence: look ahead, smile, lift the hand, wave once, then stay still while waiting for a response."), "shot": "static symmetrical medium imitation shot"},
    {"index": 9, "duration_seconds": 8, "word": "forte o piano", "lyric_cue": "Forte o pianino, veloce o lento", "action": action("Emma first performs a cheerful energetic greeting, then a smaller gentle wave. Her expression stays friendly in both versions. She does not spin, jump or change position."), "shot": "medium close-up with subtle push-in"},
    {"index": 10, "duration_seconds": 8, "word": "monopattino", "lyric_cue": "Arriva un bambino sul monopattino", "action": action("The curly-haired child wearing the same red helmet approaches slowly on the same blue scooter. He reduces speed before reaching Emma and keeps a safe distance."), "shot": "wide side-tracking shot"},
    {"index": 11, "duration_seconds": 8, "word": "due manine", "lyric_cue": "Emma dice: Ciao! Ciao, Emma! risponde", "action": action("Emma says Ciao and waves. The child stops the scooter with both feet on the ground, looks at Emma, replies Ciao, Emma and waves with one hand. Emma mirrors the gesture."), "shot": "stable medium two-shot, scooter stopped"},
    {"index": 12, "duration_seconds": 8, "word": "fornaio", "lyric_cue": "Poi passa il fornaio col pane profumato", "action": action("The same baker walks from the bakery carrying one paper bag with visible loaves. Emma notices him, raises her hand and says Buongiorno. The baker immediately turns toward her."), "shot": "medium-wide shot with fixed bakery storefront"},
    {"index": 13, "duration_seconds": 8, "word": "uno due tre", "lyric_cue": "Buongiorno, Emma! Uno, due, tre", "action": action("The baker stops, smiles and clearly replies Buongiorno, Emma, waving with his free hand. Emma counts one, two, three on her fingers and points happily toward herself when the greeting returns."), "shot": "over-the-shoulder from behind Emma"},
    {"index": 14, "duration_seconds": 8, "word": "tutti salutano", "lyric_cue": "Ciao, ciao, chi passa di qua?", "action": action("The postal worker and elderly woman pass through the square again on separate established paths. Emma greets them. Each looks at Emma and returns the greeting before continuing."), "shot": "wide locked shot with clear spatial separation"},
    {"index": 15, "duration_seconds": 8, "word": "splende", "lyric_cue": "Ciao parte, ciao torna: Ciao, Emma!", "action": action("The scooter child and baker are on the opposite side of the square. They notice Emma's wave and respond Ciao, Emma one after the other. Emma smiles toward both."), "shot": "medium-wide foreground-background composition"},
    {"index": 16, "duration_seconds": 8, "word": "piano piano", "lyric_cue": "C’è chi dice ciao forte, chi lo dice piano", "action": action("The shy child with the same yellow backpack walks past slowly while looking slightly downward. Emma offers a small gentle wave. The child looks at Emma but does not answer immediately."), "shot": "calm medium-long shot with respectful distance"},
    {"index": 17, "duration_seconds": 8, "word": "senza fretta", "lyric_cue": "Emma aspetta senza fretta", "action": action("Emma stays relaxed and smiling without moving closer. After a short pause, the shy child smiles, lifts one hand and softly says Ciao. Emma answers with a small nod and warm smile."), "shot": "gentle child-eye-level two-shot"},
    {"index": 18, "duration_seconds": 8, "word": "tutta la città", "lyric_cue": "Ciao, ciao, tutta la città", "action": action("All previously introduced characters naturally cross the square along their established paths: postal worker, elderly woman with dog, scooter child, baker and shy child. Emma stands at the centre and waves. Everyone notices her and waves back while continuing to move."), "shot": "large cinematic wide shot with slow semicircular move"},
    {"index": 19, "duration_seconds": 8, "word": "a presto", "lyric_cue": "Ciao anche a te! A presto, amici!", "action": action("Emma looks directly into camera and says Ciao anche a te. The camera slowly pulls back to reveal all recurring characters behind her. Together they wave once and answer Ciao, Emma. Emma finishes with A presto and the final composition holds briefly."), "shot": "medium close-up smoothly pulling back to final wide"},
]


def scene_signature(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ("index", "duration_seconds", "word", "lyric_cue", "action", "shot")
    return [{field: scene.get(field) for field in fields} for scene in scenes]


def upsert_episode() -> dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as db:
        episode = db.scalar(select(Episode).where(Episode.working_slug == WORKING_SLUG))
        created = episode is None
        values = {
            "title": TITLE,
            "age_min_months": 9,
            "age_max_months": 36,
            "theme": THEME,
            "hook": HOOK,
            "target_words": TARGET_WORDS,
            "featured_characters": FEATURED_CHARACTERS,
            "duration_seconds": 160,
            "bpm": 120,
            "music_direction": MUSIC_DIRECTION,
            "visual_pacing": "medium",
            "language": "it",
        }
        concept = {
            "emma_look_id": EMMA_LOOK_ID,
            "editorial_generation": {
                "format": "storia_musicale",
                "archetype": "domanda_risposta",
                "concept": "Emma saluta chi passa e ogni persona ricambia il saluto",
                "progression": [
                    "Emma scopre il meccanismo del saluto che ritorna",
                    "Emma mostra come guardare, sorridere, salutare e aspettare",
                    "Nuovi passanti rispondono in modi diversi",
                    "Emma rispetta il tempo di un bambino timido",
                    "La piazza intera ricambia il saluto finale",
                ],
            },
            "visual_consistency": {
                "single_world": True,
                "single_emma_look": EMMA_LOOK_ID,
                "camera_policy": "stable child-eye-level shots; no montage; no rapid cuts",
                "recurring_cast_locked": True,
            },
        }
        if episode is None:
            episode = Episode(working_slug=WORKING_SLUG, concept_json=concept, **values)
            db.add(episode)
            db.commit()
            db.refresh(episode)
        else:
            for field, value in values.items():
                setattr(episode, field, value)
            episode.concept_json = concept
            db.commit()

        service = PipelineService(db, settings)
        active = service.active_job(episode)
        if active is not None:
            raise RuntimeError(f"Refusing editorial update while job {active.id} is {active.status.value}")

        lyrics_changed = (
            (episode.lyrics_text or "").strip() != LYRICS
            or not service.has_valid_asset(episode, AssetKind.LYRICS)
        )
        if lyrics_changed:
            service.update_lyrics_draft(episode, LYRICS)
            episode = db.scalar(select(Episode).where(Episode.working_slug == WORKING_SLUG))
            service = PipelineService(db, settings)
        if not service.content_is_approved(episode, "lyrics"):
            service.approve_content(episode, "lyrics")

        storyboard_changed = (
            scene_signature(episode.storyboard_json or []) != scene_signature(SCENES)
            or not service.has_valid_asset(episode, AssetKind.STORYBOARD)
        )
        if storyboard_changed:
            service.update_storyboard_draft(episode, SCENES)
            episode = db.scalar(select(Episode).where(Episode.working_slug == WORKING_SLUG))
            service = PipelineService(db, settings)
        if not service.content_is_approved(episode, "storyboard"):
            service.approve_content(episode, "storyboard")

        return {
            "episode_id": episode.id,
            "working_slug": episode.working_slug,
            "created": created,
            "duration_seconds": episode.duration_seconds,
            "scene_count": len(episode.storyboard_json or []),
            "lyrics_approved": service.content_is_approved(episode, "lyrics"),
            "storyboard_approved": service.content_is_approved(episode, "storyboard"),
            "status": episode.status.value,
        }


def main() -> None:
    print(json.dumps(upsert_episode(), ensure_ascii=False))


if __name__ == "__main__":
    main()
