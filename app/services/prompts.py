from __future__ import annotations

import math
import re
from typing import Any

from ..models import Episode


SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")


def lyric_sections(lyrics: str) -> list[tuple[str, list[str]]]:
    """Parse the editorial lyric format without changing a single sung line."""

    sections: list[tuple[str, list[str]]] = []
    current_name = "Canzone"
    current_lines: list[str] = []
    for raw_line in lyrics.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = SECTION_RE.match(line)
        if heading:
            if current_lines:
                sections.append((current_name, current_lines))
            current_name = heading.group(1).strip()
            current_lines = []
            continue
        current_lines.append(line)
    if current_lines:
        sections.append((current_name, current_lines))
    return sections


def _is_rainbow_chicks(episode: Episode) -> bool:
    brief = " ".join(
        [
            episode.title,
            episode.theme,
            episode.hook,
            *episode.featured_characters,
        ]
    ).casefold()
    return (
        episode.language == "it"
        and "color" in brief
        and ("pulcin" in brief or "paperell" in brief)
    )


def _rainbow_chicks_lyrics(duration_seconds: int) -> str:
    intro = (
        "[Intro]\n"
        "Pio pio, chi arriva di là?\n"
        "Sette pulcini, eccoli qua!"
    )
    chorus = (
        "[Ritornello]\n"
        "Salta nella pozza: splash, splash, splash!\n"
        "Ogni piuma cambia in un flash.\n"
        "Rosso, giallo, verde e blu,\n"
        "balla con i pulcini e con Nuvibù!"
    )
    verse_one = (
        "[Strofa 1]\n"
        "Rosso fa un saltino, giallo gira già,\n"
        "verde apre le ali, blu li seguirà.\n"
        "Uno, due, tre, guarda su:\n"
        "quattro nuovi amici con Nuvibù!"
    )
    verse_two = (
        "[Strofa 2]\n"
        "Arancio fa una curva, viola vola in su,\n"
        "rosa ride forte e poi scende giù.\n"
        "Sette piccole ali, tutte a colori,\n"
        "fanno un girotondo tra nuvole e fiori!"
    )
    bridge = (
        "[Ponte]\n"
        "Piano piano, la pioggia va via,\n"
        "spunta nel cielo una scia di magia.\n"
        "Conta i colori, uno dopo l'altro:\n"
        "l'arcobaleno è sempre più alto!"
    )
    finale = (
        "[Ritornello finale]\n"
        "Salta nella pozza: splash, splash, splash!\n"
        "Sette piume brillano in un flash.\n"
        "Rosso, giallo, verde e blu,\n"
        "arcobaleno insieme a Nuvibù!"
    )
    if duration_seconds <= 45:
        return "\n\n".join([intro, chorus, finale])
    if duration_seconds <= 90:
        return "\n\n".join(
            [intro, chorus, verse_one, chorus, verse_two, finale]
        )
    return "\n\n".join(
        [intro, chorus, verse_one, chorus, verse_two, bridge, finale]
    )


def generate_lyrics(episode: Episode) -> str:
    """Create an original, duration-aware editorial draft.

    The draft deliberately uses short concrete lines, a narrative progression
    and limited repetition for very young listeners. Editors can still change
    every word before approval and paid music generation.
    """
    if _is_rainbow_chicks(episode):
        return _rainbow_chicks_lyrics(episode.duration_seconds)

    words = [w.strip() for w in episode.target_words if w.strip()] or [
        "cucù",
        "ciao",
        "nuvola",
        "stella",
    ]
    words = words[:4]

    if episode.language == "en":
        refrain = (
            "Nuvibù, Nuvibù, one, two, three,\n"
            "clap your hands and dance with me.\n"
            "Nuvibù, Nuvibù, turn around,\n"
            "a little surprise is coming now!"
        )
        verses = []
        for index, word in enumerate(words):
            action = ["clap your hands", "wave hello", "jump up high", "turn around"][index % 4]
            verses.append(
                f"Look, look: {word} is here,\n"
                "see it sparkle, bright and clear.\n"
                f"{action.capitalize()}, count to three,\n"
                f"{word}, {word}, sing with me!"
            )
        sections = [f"[Intro]\nHello, hello, come and see!", f"[Chorus]\n{refrain}"]
        verse_limit = 1 if episode.duration_seconds <= 45 else min(2, len(verses))
        for index, verse in enumerate(verses[:verse_limit], start=1):
            sections.append(f"[Verse {index}]\n{verse}")
            if index < verse_limit:
                sections.append(f"[Chorus]\n{refrain}")
        sections.append(f"[Final chorus]\n{refrain}")
        return "\n\n".join(sections)

    refrain = (
        "Nuvibù, Nuvibù, uno, due e tre,\n"
        "batti le manine insieme a me.\n"
        "Nuvibù, Nuvibù, gira un po',\n"
        "una nuova sorpresa arriverà!"
    )
    verses: list[str] = []
    for index, word in enumerate(words):
        action = [
            "batti le manine",
            "fai ciao con la manina",
            "salta su e giù",
            "gira piano e sorridi",
        ][index % 4]
        verses.append(
            f"Guarda bene: {word} è qui,\n"
            "brilla piano, proprio così.\n"
            f"{action.capitalize()}, conta fino a tre,\n"
            f"{word.capitalize()}, {word}, cantalo con me!"
        )
    sections = [
        "[Intro]\nCiao ciao, vieni a vedere!",
        f"[Ritornello]\n{refrain}",
    ]
    verse_limit = 1 if episode.duration_seconds <= 45 else min(2, len(verses))
    for index, verse in enumerate(verses[:verse_limit], start=1):
        sections.append(f"[Strofa {index}]\n{verse}")
        if index < verse_limit:
            sections.append(f"[Ritornello]\n{refrain}")
    sections.append(f"[Ritornello finale]\n{refrain}")
    return "\n\n".join(sections)


def music_prompt(episode: Episode) -> str:
    language = "Italian" if episode.language == "it" else "English"
    energy = "bright, bouncy and danceable" if episode.visual_pacing == "energetic" else "warm, playful and gently danceable"
    return (
        f"Original {language} preschool song for ages {episode.age_min_months}–{episode.age_max_months} months. "
        f"Theme: {episode.theme}. Hook: {episode.hook}. Target words: {', '.join(episode.target_words)}. "
        f"{episode.bpm} BPM, {energy}, major key, immediate hook in the first three seconds, "
        "clear lead vocal, memorable chorus, call-and-response moments, toy percussion, claps, "
        "glockenspiel, warm bass and polished modern children's production. Keep every word intelligible. "
        "No imitation of an existing song, melody, performer or branded character."
    )


def _scene_prompt(
    episode: Episode,
    scene_index: int,
    word: str,
    action: str,
    lyric_cue: str,
    shot: str,
) -> str:
    characters = ", ".join(episode.featured_characters or ["Nuvibù"])
    return (
        "Original premium preschool 3D animation, 16:9, rich commercial YouTube quality. "
        f"Episode story: {episode.hook}. Theme: {episode.theme}. "
        f"Characters on model: {characters}. Sung lyric cue: '{lyric_cue}'. "
        f"Main action: {action}. Feature the concept '{word}'. Shot: {shot}. "
        "Keep Nuvibù identical to the approved character reference: fluffy white cloud body, swirl tuft, "
        "large glossy blue-violet eyes, rosy cheeks, joyful mouth, lavender feet, tiny rounded hands and rainbow chest emblem. "
        "Preserve the exact cast count, colors, scale and wardrobe from the previous scene. "
        "Use plush materials, cinematic soft lighting, vivid saturated colors, expressive faces, depth, sparkles and a detailed "
        "but organized environment. Create one unmistakable focal action, strong foreground/background separation and readable motion "
        "that visually enacts the literal meaning of the lyric cue. End on a clean pose that can cut into the next scene. "
        "No empty minimalist scene, no generic flat clip-art look, no text, no logos, no extra limbs, "
        "no malformed hands, no frightening expression, no rapid strobe, no flashing, no camera shake. "
        f"Scene continuity marker {scene_index}."
    )


def generate_storyboard(episode: Episode) -> list[dict[str, Any]]:
    words = [w.strip() for w in episode.target_words if w.strip()] or [
        "nuvola",
        "stella",
        "sorpresa",
        "arcobaleno",
    ]
    # Subject references on the full Veo model generate eight-second clips.
    # Plan close to that boundary and express pacing inside each shot instead
    # of buying many short clips that are later discarded.
    target_scene_duration = 8
    count = max(2, math.ceil(episode.duration_seconds / target_scene_duration))
    base_duration, remainder = divmod(episode.duration_seconds, count)
    lyrics = lyric_sections(episode.lyrics_text or generate_lyrics(episode))
    sung_lines = [line for _name, lines in lyrics for line in lines]
    actions = [
        "Open immediately: Nuvibù parts two soft clouds and reveals the full cast to camera",
        "Show the story problem clearly, then let the cast notice the first magical clue",
        "The first character performs one simple action on the beat and triggers a transformation",
        "Two characters repeat the action with a new color or object while Nuvibù reacts",
        "The whole cast performs the signature chorus move in a clean semicircle",
        "Push the story forward with a larger transformation that changes the environment",
        "Use a playful call-and-response between Nuvibù and the featured friends",
        "Repeat the signature move with a fresh visual reward and stronger color contrast",
        "Reveal the completed transformation in one wide, readable hero shot",
        "Finish with the cast facing camera, waving and holding the final rainbow pose",
    ]
    shots = [
        "wide establishing shot with a gentle push-in",
        "medium group shot at child eye level",
        "full-body tracking shot",
        "medium two-shot with clear reaction",
        "symmetrical wide dance shot",
        "low wide reveal, then settle",
        "alternating medium close-ups",
        "full-body lateral tracking shot",
        "cinematic wide hero reveal",
        "front-facing wide finale with a slow pull-back",
    ]
    scenes: list[dict[str, Any]] = []
    elapsed = 0
    for index in range(count):
        duration = base_duration + (1 if index < remainder else 0)
        if duration <= 0:
            break
        word = words[index % len(words)]
        narrative_index = min(
            len(actions) - 1,
            round(index * (len(actions) - 1) / max(1, count - 1)),
        )
        action = actions[narrative_index]
        shot = shots[narrative_index]
        cue_index = min(
            len(sung_lines) - 1,
            round(index * (len(sung_lines) - 1) / max(1, count - 1)),
        )
        lyric_cue = sung_lines[cue_index]
        scenes.append(
            {
                "index": index,
                "start_seconds": elapsed,
                "duration_seconds": duration,
                "word": word,
                "lyric_cue": lyric_cue,
                "action": action,
                "shot": shot,
                "prompt": _scene_prompt(
                    episode,
                    index,
                    word,
                    action,
                    lyric_cue,
                    shot,
                ),
            }
        )
        elapsed += duration
    return scenes


def publish_metadata(episode: Episode) -> tuple[str, str, list[str]]:
    title = f"{episode.title} | Canzone per bambini"
    description = (
        f"{episode.hook}. Una canzone originale di Nuvibù con colori, musica e personaggi da cantare insieme.\n\n"
        f"Tema: {episode.theme}\nParole chiave: {', '.join(episode.target_words)}\n\n"
        "Testo, musica, personaggi e animazioni originali creati per il canale Nuvibù."
    )
    tags = [
        "canzoni per bambini",
        "baby dance",
        "cartoni per bambini",
        "impara i colori",
        "animali per bambini",
        "Nuvibù",
        episode.theme,
    ]
    return title[:100], description, tags[:12]
