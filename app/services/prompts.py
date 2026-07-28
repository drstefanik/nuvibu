from __future__ import annotations

import math
from typing import Any

from ..models import Episode


ITALIAN_ACTIONS = [
    "batti le manine",
    "fai ciao con la manina",
    "salta su e giù",
    "gira piano e sorridi",
]


def generate_lyrics(episode: Episode) -> str:
    """Create a simple original draft lyric.

    This deterministic fallback is useful for mock mode and editorial drafts. In live mode the
    music provider still receives the full episode brief and the approved lyric can be edited
    before generation.
    """
    words = [w.strip() for w in episode.target_words if w.strip()] or ["cucù", "ciao", "nuvola", "stella"]
    words = words[:4]

    if episode.language == "en":
        refrain = (
            "Sing with Nuvibù, one, two, three,\n"
            "clap your hands and dance with me.\n"
            "Sing with Nuvibù, here we go,\n"
            "hello, hello, hello!"
        )
        verses = []
        for index, word in enumerate(words):
            action = ["clap your hands", "wave hello", "jump up high", "turn around"][index % 4]
            verses.append(
                f"Look, look, what can you see?\n"
                f"A happy {word} dancing with me.\n"
                f"{action.capitalize()}, nice and slow,\n"
                f"{word}, {word}, here we go!"
            )
        sections = [f"[Chorus]\n{refrain}"]
        for index, verse in enumerate(verses, start=1):
            sections.extend([f"[Verse {index}]\n{verse}", f"[Chorus]\n{refrain}"])
        return "\n\n".join(sections)

    refrain = (
        "Canta con Nuvibù, uno, due e tre,\n"
        "batti le manine insieme a me.\n"
        "Canta con Nuvibù, eccoci qua,\n"
        "ciao ciao, che felicità!"
    )
    verses: list[str] = []
    for index, word in enumerate(words):
        action = ITALIAN_ACTIONS[index % len(ITALIAN_ACTIONS)]
        verses.append(
            f"Guarda, guarda, chi arriverà?\n"
            f"{word.capitalize()} sorride ed eccolo qua.\n"
            f"{action.capitalize()}, uno, due e poi,\n"
            f"{word.capitalize()}, {word}, balla insieme a noi!"
        )
    sections = [f"[Ritornello]\n{refrain}"]
    for index, verse in enumerate(verses, start=1):
        sections.extend([f"[Strofa {index}]\n{verse}", f"[Ritornello]\n{refrain}"])
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


def _scene_prompt(episode: Episode, scene_index: int, word: str, action: str) -> str:
    characters = ", ".join(episode.featured_characters or ["Nuvibù"])
    return (
        "Original premium preschool 3D animation, 16:9, rich commercial YouTube quality. "
        f"Characters: {characters}. Main action: {action}. Feature the concept '{word}'. "
        "Keep Nuvibù identical to the approved character reference: fluffy white cloud body, swirl tuft, "
        "large glossy blue-violet eyes, rosy cheeks, joyful mouth, lavender feet, tiny rounded hands and rainbow chest emblem. "
        "Use plush materials, cinematic soft lighting, vivid saturated colors, expressive faces, depth, sparkles and a detailed "
        "but organized environment. Create one unmistakable focal action, strong foreground/background separation and lively motion "
        "synchronized to a children's song. No empty minimalist scene, no generic flat clip-art look, no text, no logos, no extra limbs, "
        "no malformed hands, no frightening expression, no rapid strobe, no flashing, no camera shake. "
        f"Scene continuity marker {scene_index}."
    )


def generate_storyboard(episode: Episode) -> list[dict[str, Any]]:
    words = [w.strip() for w in episode.target_words if w.strip()] or ["nuvola", "stella", "pulcino", "arcobaleno"]
    target_scene_duration = {"gentle": 8, "medium": 6, "energetic": 5}.get(episode.visual_pacing, 6)
    count = max(2, math.ceil(episode.duration_seconds / target_scene_duration))
    base_duration, remainder = divmod(episode.duration_seconds, count)
    actions = [
        "Nuvibù bursts out from behind a colorful cloud and greets the viewer",
        "the featured character jumps into a matching color splash on the beat",
        "Nuvibù claps while the friends perform one simple dance move together",
        "a magical rainbow transformation changes the featured object into one bold color",
        "the characters move toward camera, laugh, then land in a clear group pose",
        "a playful reveal opens and a new friend appears with confetti and bubbles",
        "the cast repeats the chorus choreography with a new background surprise",
        "Nuvibù leads a final celebratory pose beneath a bright rainbow",
    ]
    scenes: list[dict[str, Any]] = []
    elapsed = 0
    for index in range(count):
        duration = base_duration + (1 if index < remainder else 0)
        if duration <= 0:
            break
        word = words[index % len(words)]
        action = actions[index % len(actions)]
        scenes.append(
            {
                "index": index,
                "start_seconds": elapsed,
                "duration_seconds": duration,
                "word": word,
                "action": action,
                "prompt": _scene_prompt(episode, index, word, action),
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
