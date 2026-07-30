from __future__ import annotations

import math
import re
from typing import Any

from ..models import Episode
from .lyrics_engine import FORMAT_LABELS, generate_song, resolve_song_format


SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
EMMA_VISUAL_GUARD = (
    "Emma is the recurring main character and must be clearly visible for the "
    "entire scene, leading the primary action. Keep her identical to the "
    "approved Emma reference: nine-month-old baby proportions, large "
    "grey-green eyes, round rosy cheeks, warm light skin, thick dark "
    "chestnut-brown hair with one high playful ponytail tied in pastel pink, "
    "with the exact face, hair silhouette and proportions shown in reference "
    "image 1. Preserve exactly the single approved outfit shown in reference "
    "image 1 for the whole episode; never mix garments or colors from another "
    "Emma look. Nuvibù is the name of the "
    "platform and channel, not a character. The small plush white cloud is "
    "Emma's secondary friend and must never replace, obscure or visually "
    "dominate her. "
)


def emma_visual_guard(outfit_prompt: str) -> str:
    """Bind Emma's immutable identity to one catalog outfit for an episode."""

    return (
        EMMA_VISUAL_GUARD
        + "Locked wardrobe for this episode: "
        + outfit_prompt.strip()
        + " The locked wardrobe description and reference image 1 supersede "
        "any older wardrobe wording that may remain in a saved storyboard. "
    )


def featured_characters(episode: Episode) -> list[str]:
    """Return the on-screen cast with Emma locked in first position."""

    supporting: list[str] = []
    for raw_name in episode.featured_characters or []:
        name = str(raw_name).strip()
        if not name:
            continue
        if name.casefold() in {"emma", "nuvibù", "nuvibu"}:
            continue
        if name not in supporting:
            supporting.append(name)
    return ["Emma", *supporting]


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


def generate_lyrics(episode: Episode) -> str:
    """Create a catalog-aware draft when no database memory is available.

    The production pipeline calls the same engine with the previous catalog.
    Keeping this wrapper memory-free preserves the pure helper used by tests
    and local tooling.
    """

    return generate_song(episode).lyrics


def music_prompt(episode: Episode) -> str:
    language = "Italian" if episode.language == "it" else "English"
    generation = (episode.concept_json or {}).get(
        "editorial_generation",
        {},
    )
    song_format = str(
        generation.get("format") or resolve_song_format(episode)
    )
    archetype = str(generation.get("archetype") or "micro-story")
    if song_format == "nanna":
        energy = (
            "soft, slow and reassuring lullaby; sparse warm arrangement, "
            "no dance groove, no sudden dynamic changes"
        )
    elif song_format == "baby_dance":
        energy = "bright, physical and strongly danceable with clear movement cues"
    elif episode.visual_pacing == "energetic":
        energy = "bright, bouncy and danceable"
    else:
        energy = "warm, playful and gently danceable"
    return (
        f"Original {language} preschool song for ages {episode.age_min_months}–{episode.age_max_months} months. "
        f"Theme: {episode.theme}. Hook: {episode.hook}. Target words: {', '.join(episode.target_words)}. "
        f"Editorial format: {FORMAT_LABELS.get(song_format, song_format)}. Narrative archetype: {archetype}. "
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
    characters = ", ".join(featured_characters(episode))
    return (
        "Original premium preschool 3D animation, 16:9, rich commercial YouTube quality. "
        f"Episode story: {episode.hook}. Theme: {episode.theme}. "
        f"Characters on model: {characters}. Sung lyric cue: '{lyric_cue}'. "
        f"Main action: {action}. Feature the concept '{word}'. Shot: {shot}. "
        f"{EMMA_VISUAL_GUARD}"
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
    default_actions = [
        "Open immediately on Emma's face as she parts two soft clouds and reveals her friends to camera",
        "Emma shows the story problem clearly, then notices the first magical clue with her friends",
        "Emma performs one simple action on the beat and triggers a transformation",
        "Two friends repeat Emma's action with a new color or object while she reacts",
        "Emma leads the whole cast in the signature chorus move in a clean semicircle",
        "Emma pushes the story forward with a larger transformation that changes the environment",
        "Use a playful call-and-response between Emma and the featured friends",
        "Emma repeats the signature move with a fresh visual reward and stronger color contrast",
        "Reveal Emma and the completed transformation in one wide, readable hero shot",
        "Finish with Emma centred, her friends waving and everyone holding the final rainbow pose",
    ]
    generation = (episode.concept_json or {}).get(
        "editorial_generation",
        {},
    )
    saved_progression = generation.get("progression", [])
    progression = [
        str(action).strip()
        for action in saved_progression
        if str(action).strip()
    ]
    actions = progression or default_actions
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
        shot_index = min(
            len(shots) - 1,
            round(index * (len(shots) - 1) / max(1, count - 1)),
        )
        action = actions[narrative_index]
        shot = shots[shot_index]
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
        f"{episode.hook}. Una canzone originale di Nuvibù – Emma & Friends, con colori, musica e personaggi da cantare insieme.\n\n"
        f"Tema: {episode.theme}\nParole chiave: {', '.join(episode.target_words)}\n\n"
        "Testo, musica, personaggi e animazioni originali creati per il canale Nuvibù. Emma è la protagonista di ogni avventura."
    )
    tags = [
        "canzoni per bambini",
        "baby dance",
        "cartoni per bambini",
        "impara i colori",
        "animali per bambini",
        "Nuvibù",
        "Emma and Friends",
        "Emma e i suoi amici",
        episode.theme,
    ]
    return title[:100], description, tags[:12]
