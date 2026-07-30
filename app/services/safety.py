from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..models import AssetKind, Episode
from .lyrics_engine import editorial_audit


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
WORD_RE = re.compile(r"[a-zà-öø-ÿ']+", re.IGNORECASE)


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
        for scene in episode.storyboard_json
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
    connective_count = sum(word.casefold() in SIMPLE_CONNECTIVES for word in words)
    return (
        len(long_words) / len(words) <= 0.06
        and connective_count / len(words) <= 0.28
    )


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
) -> QCResult:
    recent = list(recent_episodes)
    catalog = list(catalog_episodes) if catalog_episodes is not None else recent
    findings: list[str] = []
    checks: dict[str, bool] = {}
    check_scores: dict[str, int] = {}

    text_findings = review_text(episode.lyrics_text or "")
    findings.extend(text_findings)
    checks["content_terms"] = not text_findings
    check_scores["content_terms"] = 100 if not text_findings else 0

    prompts = [
        str(scene.get("prompt", "")).lower()
        for scene in episode.storyboard_json
    ]
    _add_check(
        checks,
        check_scores,
        findings,
        name="prompt_guardrails",
        passed=bool(prompts)
        and all(
            "no flashing" in prompt and "no frightening" in prompt
            for prompt in prompts
        ),
        score=100
        if prompts
        and all(
            "no flashing" in prompt and "no frightening" in prompt
            for prompt in prompts
        )
        else 0,
        finding="I prompt visivi non includono tutti i guardrail richiesti.",
    )

    age_range_ok = 0 <= episode.age_min_months < episode.age_max_months <= 60
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
    is_bedtime = audit["song_format"] == "nanna"
    bpm_ok = (
        60 <= episode.bpm <= 82
        if is_bedtime
        else 70 <= episode.bpm <= 125
    )
    _add_check(
        checks,
        check_scores,
        findings,
        name="format_bpm",
        passed=bpm_ok,
        score=100 if bpm_ok else 25,
        finding=(
            "Il format Nanna richiede un BPM compreso tra 60 e 82."
            if is_bedtime
            else "BPM non coerente con il format editoriale."
        ),
    )

    scene_durations = [
        int(scene.get("duration_seconds", 0))
        for scene in episode.storyboard_json
    ]
    scene_pacing_ok = bool(scene_durations) and min(scene_durations) >= 4
    _add_check(
        checks,
        check_scores,
        findings,
        name="scene_pacing",
        passed=scene_pacing_ok,
        score=100 if scene_pacing_ok else 0,
        finding="Scene troppo rapide o storyboard mancante.",
    )

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
        _add_check(
            checks,
            check_scores,
            findings,
            name=label,
            passed=present,
            score=100 if present else 0,
            finding=f"Asset obbligatorio mancante: {label}.",
        )

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

    phrase_reuse_ok = (
        len(audit["reused_phrases"]) <= 1
        and not audit["blocked_phrases"]
        and int(audit["rhyme_pair_overlap"]) <= 1
        and len(audit["reused_storyboard_actions"]) <= 1
    )
    phrase_score = max(
        0,
        100
        - max(0, len(audit["reused_phrases"]) - 1) * 35
        - len(audit["blocked_phrases"]) * 45
        - max(0, int(audit["rhyme_pair_overlap"]) - 1) * 15
        - max(0, len(audit["reused_storyboard_actions"]) - 1) * 20,
    )
    phrase_details: list[str] = []
    if audit["reused_phrases"]:
        phrase_details.append(
            "frasi: " + "; ".join(audit["reused_phrases"][:3])
        )
    if audit["blocked_phrases"]:
        phrase_details.append(
            "formule abusate: " + ", ".join(audit["blocked_phrases"])
        )
    if int(audit["rhyme_pair_overlap"]) > 1:
        phrase_details.append(
            f"coppie di rime riprese: {audit['rhyme_pair_overlap']}"
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
            + (": " + " • ".join(phrase_details) if phrase_details else ".")
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

    progression_ok = bool(audit["has_progression"]) and _storyboard_has_progression(
        episode
    )
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
            "Testo e storyboard non sono sincronizzati: almeno una scena non "
            "ha un cue valido o un'azione leggibile."
        ),
    )

    syllables = list(audit["syllables"])
    meter_span = int(audit["meter_span"])
    meter_ok = (
        bool(syllables)
        and all(4 <= value <= 18 for value in syllables)
        and meter_span <= 12
    )
    meter_penalty = (
        sum(value < 4 or value > 18 for value in syllables) * 18
        + max(0, meter_span - 8) * 4
    )
    _add_check(
        checks,
        check_scores,
        findings,
        name="meter",
        passed=meter_ok,
        score=max(0, 100 - meter_penalty),
        finding=(
            "Metrica irregolare: rivedere lunghezza e accenti dei versi "
            f"(escursione sillabica {meter_span})."
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
            "Il ritornello è assente o generico: deve nominare un elemento "
            "specifico del personaggio o del tema."
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
        finding="Lessico o costruzione sintattica poco chiari per l'età indicata.",
    )

    weights = {
        "content_terms": 5,
        "prompt_guardrails": 4,
        "age_range": 2,
        "duration": 2,
        "format_bpm": 5,
        "scene_pacing": 4,
        "music": 3,
        "main_render": 3,
        "short": 2,
        "thumbnail": 2,
        "catalog_originality": 18,
        "catalog_phrase_reuse": 12,
        "verb_variety": 7,
        "narrative_progression": 8,
        "lyrics_storyboard_coherence": 8,
        "meter": 8,
        "refrain_strength": 8,
        "age_clarity": 4,
    }
    total_weight = sum(weights.values())
    score = round(
        sum(check_scores[name] * weight for name, weight in weights.items())
        / total_weight
    )
    passed = score >= 85 and not findings and all(checks.values())
    metrics = {
        "editorial": audit,
        "component_scores": check_scores,
        "score_threshold": 85,
    }
    return QCResult(
        passed=passed,
        score=score,
        findings=findings,
        checks=checks,
        metrics=metrics,
    )
