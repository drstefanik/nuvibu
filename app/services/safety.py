from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..models import AssetKind, Episode
from .lyrics_engine import count_syllables, editorial_audit


@dataclass(slots=True)
class QCResult:
    passed: bool
    score: int
    findings: list[str]
    checks: dict[str, bool]
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "score": self.score,
            "findings": self.findings,
            "checks": self.checks,
            "metrics": self.metrics,
        }


BLOCKED_TERMS = {
    "violenza",
    "sangue",
    "paura",
    "spavento",
    "arma",
    "coltello",
    "pistola",
    "morte",
    "mostro inquietante",
    "flashing",
    "strobe",
    "rapid cuts",
    "scary",
    "blood",
    "weapon",
}

SIMPLE_CONNECTIVES = {
    "e",
    "ma",
    "poi",
    "ora",
    "quando",
    "dove",
    "come",
    "con",
    "senza",
    "dentro",
    "fuori",
    "sopra",
    "sotto",
}

FORMAT_BPM_RANGES: dict[str, tuple[int, int]] = {
    "animali_e_versi": (80, 128),
    "colori_e_trasformazioni": (80, 132),
    "baby_dance": (128, 155),
    "cucu_e_sorpresa": (70, 122),
    "storia_musicale": (70, 128),
    "nanna": (60, 82),
}

NON_SUNG_SECTION_MARKERS = (
    "parlato",
    "spoken",
    "vocoder",
    "break",
    "finale secco",
    "effetti",
    "sound effect",
    "countdown",
)

WORD_RE = re.compile(r"[a-zà-öø-ÿ']+", re.IGNORECASE)
SECTION_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def review_text(text: str) -> list[str]:
    lowered = text.lower()
    findings = [
        f"Termine non ammesso rilevato: {term}"
        for term in sorted(BLOCKED_TERMS)
        if term in lowered
    ]
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("[")
    ]
    if any(len(line.split()) > 11 for line in lines):
        findings.append(
            "Almeno una riga supera 11 parole: semplificare il testo."
        )
    return findings


def _storyboard_matches_lyrics(episode: Episode) -> bool:
    lyrics = episode.lyrics_text or ""
    scenes = episode.storyboard_json or []
    if not scenes:
        return False
    return all(
        str(scene.get("lyric_cue", "")).strip() in lyrics
        and len(str(scene.get("action", "")).split()) >= 4
        for scene in scenes
    )


def _storyboard_has_progression(episode: Episode) -> bool:
    actions = [
        " ".join(
            WORD_RE.findall(str(scene.get("action", "")).casefold())
        )
        for scene in (episode.storyboard_json or [])
    ]
    if len(actions) < 2:
        return bool(actions)
    unique = len(set(actions))
    return unique >= min(3, len(actions))


def _age_clarity(episode: Episode) -> bool:
    words = WORD_RE.findall(episode.lyrics_text or "")
    if not words:
        return False
    long_words = [word for word in words if len(word) >= 12]
    connective_count = sum(
        word.casefold() in SIMPLE_CONNECTIVES for word in words
    )
    return (
        len(long_words) / len(words) <= 0.06
        and connective_count / len(words) <= 0.28
    )


def _format_bpm_range(song_format: str) -> tuple[int, int]:
    return FORMAT_BPM_RANGES.get(song_format, (70, 135))


def _sung_meter_profile(lyrics: str) -> dict:
    """Measure meter section by section, excluding spoken/SFX material.

    Very short calls such as "BUM!", "Sistema acceso!" or the three-word
    English movement cue "left then right" are intentional rhythmic fragments,
    not complete sung verses. Comparing them with full lyrical lines created
    false blocking failures for energetic baby-dance songs.
    """

    current_section = "testo"
    skip_section = False
    sections: dict[str, list[int]] = {}

    for raw_line in lyrics.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        header_match = SECTION_HEADER_RE.match(line)
        if header_match:
            current_section = header_match.group(1).strip()
            plain_header = current_section.casefold()
            skip_section = any(
                marker in plain_header
                for marker in NON_SUNG_SECTION_MARKERS
            )
            continue

        if skip_section:
            continue

        words = WORD_RE.findall(line)
        syllable_count = count_syllables(line)
        if len(words) < 3 or (
            len(words) == 3 and syllable_count < 4
        ):
            continue

        sections.setdefault(current_section, []).append(
            syllable_count
        )

    section_metrics: list[dict] = []
    all_syllables: list[int] = []
    for name, values in sections.items():
        if not values:
            continue
        span = max(values) - min(values)
        section_metrics.append(
            {
                "section": name,
                "syllables": values,
                "span": span,
            }
        )
        all_syllables.extend(values)

    return {
        "syllables": all_syllables,
        "sections": section_metrics,
        "max_span": max(
            (section["span"] for section in section_metrics),
            default=0,
        ),
    }


def _add_check(
    checks: dict[str, bool],
    check_scores: dict[str, int],
    findings: list[str],
    *,
    name: str,
    passed: bool,
    score: int,
    finding: str,
) -> None:
    checks[name] = passed
    check_scores[name] = max(0, min(100, int(score)))
    if not passed:
        findings.append(finding)


def review_episode(
    episode: Episode,
    *,
    recent_episodes: Iterable[Episode] = (),
    catalog_episodes: Iterable[Episode] | None = None,
    include_media: bool = True,
    editorial_snapshot: dict | None = None,
) -> QCResult:
    recent = list(recent_episodes)
    catalog = (
        list(catalog_episodes)
        if catalog_episodes is not None
        else recent
    )
    findings: list[str] = []
    checks: dict[str, bool] = {}
    check_scores: dict[str, int] = {}

    text_findings = review_text(episode.lyrics_text or "")
    findings.extend(text_findings)
    checks["content_terms"] = not text_findings
    check_scores["content_terms"] = 100 if not text_findings else 0

    prompts = [
        str(scene.get("prompt", "")).lower()
        for scene in (episode.storyboard_json or [])
    ]
    prompt_guardrails_ok = bool(prompts) and all(
        "no flashing" in prompt and "no frightening" in prompt
        for prompt in prompts
    )
    _add_check(
        checks,
        check_scores,
        findings,
        name="prompt_guardrails",
        passed=prompt_guardrails_ok,
        score=100 if prompt_guardrails_ok else 0,
        finding="I prompt visivi non includono tutti i guardrail richiesti.",
    )

    age_range_ok = (
        0 <= episode.age_min_months < episode.age_max_months <= 60
    )
    _add_check(
        checks,
        check_scores,
        findings,
        name="age_range",
        passed=age_range_ok,
        score=100 if age_range_ok else 0,
        finding="Fascia d'età non valida.",
    )

    duration_ok = 15 <= episode.duration_seconds <= 180
    _add_check(
        checks,
        check_scores,
        findings,
        name="duration",
        passed=duration_ok,
        score=100 if duration_ok else 0,
        finding="Durata fuori dal limite configurato.",
    )

    audit = editorial_audit(
        episode,
        recent_episodes=recent,
        catalog_episodes=catalog,
    )

    song_format = str(audit["song_format"])
    bpm_min, bpm_max = _format_bpm_range(song_format)
    bpm_ok = bpm_min <= episode.bpm <= bpm_max
    _add_check(
        checks,
        check_scores,
        findings,
        name="format_bpm",
        passed=bpm_ok,
        score=100 if bpm_ok else 25,
        finding=(
            f"Il format richiede un BPM compreso tra "
            f"{bpm_min} e {bpm_max}; valore impostato: {episode.bpm}."
        ),
    )

    scene_durations = [
        int(scene.get("duration_seconds", 0))
        for scene in (episode.storyboard_json or [])
    ]
    scene_pacing_ok = (
        bool(scene_durations) and min(scene_durations) >= 4
    )
    _add_check(
        checks,
        check_scores,
        findings,
        name="scene_pacing",
        passed=scene_pacing_ok,
        score=100 if scene_pacing_ok else 0,
        finding="Scene troppo rapide o storyboard mancante.",
    )

    media_findings: list[str] = []
    if include_media:
        selected = {
            asset.kind
            for asset in episode.assets
            if asset.selected and Path(asset.path).exists()
        }
        for kind, label in [
            (AssetKind.MUSIC, "music"),
            (AssetKind.RENDER, "main_render"),
            (AssetKind.SHORT, "short"),
            (AssetKind.THUMBNAIL, "thumbnail"),
        ]:
            present = kind in selected
            before = len(findings)
            _add_check(
                checks,
                check_scores,
                findings,
                name=label,
                passed=present,
                score=100 if present else 0,
                finding=f"Asset obbligatorio mancante: {label}.",
            )
            media_findings.extend(findings[before:])

    similarity = float(audit["max_similarity"])
    originality_score = max(0, round(100 - similarity * 140))
    originality_ok = similarity < 0.42
    _add_check(
        checks,
        check_scores,
        findings,
        name="catalog_originality",
        passed=originality_ok,
        score=originality_score,
        finding=(
            "Testo troppo simile al catalogo recente: "
            f"{audit['similarity_percent']}% di similarità."
        ),
    )

    rhyme_overlap = int(audit["rhyme_pair_overlap"])
    phrase_reuse_ok = (
        len(audit["reused_phrases"]) <= 1
        and not audit["blocked_phrases"]
        and rhyme_overlap <= 3
        and len(audit["reused_storyboard_actions"]) <= 1
    )
    phrase_score = max(
        0,
        100
        - max(0, len(audit["reused_phrases"]) - 1) * 35
        - len(audit["blocked_phrases"]) * 45
        - max(0, rhyme_overlap - 1) * 8
        - max(
            0,
            len(audit["reused_storyboard_actions"]) - 1,
        )
        * 20,
    )
    phrase_details: list[str] = []
    if len(audit["reused_phrases"]) > 1:
        phrase_details.append(
            "frasi: " + "; ".join(audit["reused_phrases"][:3])
        )
    if audit["blocked_phrases"]:
        phrase_details.append(
            "formule abusate: "
            + ", ".join(audit["blocked_phrases"])
        )
    if rhyme_overlap > 3:
        phrase_details.append(
            f"coppie di rime riprese: {rhyme_overlap}"
        )
    if len(audit["reused_storyboard_actions"]) > 1:
        phrase_details.append(
            "azioni riprese: "
            + "; ".join(audit["reused_storyboard_actions"][:3])
        )
    _add_check(
        checks,
        check_scores,
        findings,
        name="catalog_phrase_reuse",
        passed=phrase_reuse_ok,
        score=phrase_score,
        finding=(
            "Riciclo eccessivo rispetto agli ultimi 10 episodi"
            + (
                ": " + " • ".join(phrase_details)
                if phrase_details
                else "."
            )
        ),
    )

    verb_count = int(audit["unique_verb_count"])
    verb_ok = verb_count >= 3
    _add_check(
        checks,
        check_scores,
        findings,
        name="verb_variety",
        passed=verb_ok,
        score=min(100, verb_count * 25),
        finding=(
            "Azioni troppo poco varie: servono almeno tre verbi visivi "
            f"distinti, rilevati {verb_count}."
        ),
    )

    progression_ok = bool(
        audit["has_progression"]
    ) and _storyboard_has_progression(episode)
    _add_check(
        checks,
        check_scores,
        findings,
        name="narrative_progression",
        passed=progression_ok,
        score=100 if progression_ok else 35,
        finding=(
            "Manca una progressione reale: le strofe o le azioni dello "
            "storyboard ripetono lo stesso evento."
        ),
    )

    storyboard_ok = _storyboard_matches_lyrics(episode)
    _add_check(
        checks,
        check_scores,
        findings,
        name="lyrics_storyboard_coherence",
        passed=storyboard_ok,
        score=100 if storyboard_ok else 0,
        finding=(
            "Testo e storyboard non sono sincronizzati: almeno una scena "
            "non ha un cue valido o un'azione leggibile."
        ),
    )

    meter_profile = _sung_meter_profile(
        episode.lyrics_text or ""
    )
    syllables = list(meter_profile["syllables"])
    meter_span = int(meter_profile["max_span"])
    meter_ok = (
        bool(syllables)
        and all(4 <= value <= 20 for value in syllables)
        and meter_span <= 8
    )
    meter_penalty = (
        sum(value < 4 or value > 20 for value in syllables) * 18
        + sum(
            max(0, int(section["span"]) - 4) * 4
            for section in meter_profile["sections"]
        )
    )
    _add_check(
        checks,
        check_scores,
        findings,
        name="meter",
        passed=meter_ok,
        score=max(0, 100 - meter_penalty),
        finding=(
            "Metrica irregolare nelle sezioni cantate: rivedere "
            "lunghezza e accenti dei versi "
            f"(escursione massima per sezione {meter_span})."
        ),
    )

    refrain_ok = bool(audit["has_refrain"]) and bool(
        audit["specific_refrain"]
    )
    _add_check(
        checks,
        check_scores,
        findings,
        name="refrain_strength",
        passed=refrain_ok,
        score=100 if refrain_ok else 30,
        finding=(
            "Il ritornello è assente o generico: deve nominare un "
            "elemento specifico del personaggio o del tema."
        ),
    )

    clarity_ok = _age_clarity(episode)
    _add_check(
        checks,
        check_scores,
        findings,
        name="age_clarity",
        passed=clarity_ok,
        score=100 if clarity_ok else 45,
        finding=(
            "Lessico o costruzione sintattica poco chiari per "
            "l'età indicata."
        ),
    )

    editorial_weights = {
        "content_terms": 5,
        "prompt_guardrails": 4,
        "age_range": 2,
        "duration": 2,
        "format_bpm": 5,
        "scene_pacing": 4,
        "catalog_originality": 18,
        "catalog_phrase_reuse": 12,
        "verb_variety": 7,
        "narrative_progression": 8,
        "lyrics_storyboard_coherence": 8,
        "meter": 8,
        "refrain_strength": 8,
        "age_clarity": 4,
    }
    media_weights = {
        "music": 3,
        "main_render": 3,
        "short": 2,
        "thumbnail": 2,
    }

    if include_media and isinstance(editorial_snapshot, dict):
        snapshot_checks = editorial_snapshot.get("checks")
        snapshot_metrics = editorial_snapshot.get("metrics")
        snapshot_scores = (
            snapshot_metrics.get("component_scores")
            if isinstance(snapshot_metrics, dict)
            else None
        )
        snapshot_findings = editorial_snapshot.get("findings")
        if (
            isinstance(snapshot_checks, dict)
            and isinstance(snapshot_scores, dict)
            and isinstance(snapshot_findings, list)
            and all(
                name in snapshot_checks
                for name in editorial_weights
            )
            and all(
                name in snapshot_scores
                for name in editorial_weights
            )
        ):
            checks = {
                **{
                    name: bool(snapshot_checks[name])
                    for name in editorial_weights
                },
                **{
                    name: checks[name]
                    for name in media_weights
                },
            }
            check_scores = {
                **{
                    name: int(snapshot_scores[name])
                    for name in editorial_weights
                },
                **{
                    name: check_scores[name]
                    for name in media_weights
                },
            }
            findings = [
                str(finding)
                for finding in snapshot_findings
            ] + media_findings
            snapshot_editorial = snapshot_metrics.get("editorial")
            if isinstance(snapshot_editorial, dict):
                audit = snapshot_editorial

    weights = {
        **editorial_weights,
        **(media_weights if include_media else {}),
    }
    total_weight = sum(weights.values())
    score = round(
        sum(
            check_scores[name] * weight
            for name, weight in weights.items()
        )
        / total_weight
    )
    passed = score >= 85 and not findings and all(checks.values())

    audit = {
        **audit,
        "meter_sections": meter_profile["sections"],
        "meter_span": meter_span,
    }
    metrics = {
        "editorial": audit,
        "component_scores": check_scores,
        "score_threshold": 85,
        "phase": (
            "final" if include_media else "editorial_preflight"
        ),
    }
    return QCResult(
        passed=passed,
        score=score,
        findings=findings,
        checks=checks,
        metrics=metrics,
    )
