from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import AssetKind, Episode


@dataclass(slots=True)
class QCResult:
    passed: bool
    score: int
    findings: list[str]
    checks: dict[str, bool]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "score": self.score, "findings": self.findings, "checks": self.checks}


BLOCKED_TERMS = {
    "violenza", "sangue", "paura", "spavento", "arma", "coltello", "pistola", "morte", "mostro inquietante",
    "flashing", "strobe", "rapid cuts", "scary", "blood", "weapon",
}


def review_text(text: str) -> list[str]:
    lowered = text.lower()
    findings = [f"Termine non ammesso rilevato: {term}" for term in sorted(BLOCKED_TERMS) if term in lowered]
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("[")]
    if any(len(line.split()) > 11 for line in lines):
        findings.append("Almeno una riga supera 11 parole: semplificare il testo.")
    return findings


def review_episode(episode: Episode) -> QCResult:
    findings: list[str] = []
    checks: dict[str, bool] = {}
    text_findings = review_text(episode.lyrics_text or "")
    findings.extend(text_findings)
    checks["content_terms"] = not text_findings

    prompts = [str(scene.get("prompt", "")).lower() for scene in episode.storyboard_json]
    checks["prompt_guardrails"] = bool(prompts) and all("no flashing" in prompt and "no frightening" in prompt for prompt in prompts)
    if not checks["prompt_guardrails"]:
        findings.append("I prompt visivi non includono tutti i guardrail richiesti.")

    checks["age_range"] = 0 <= episode.age_min_months < episode.age_max_months <= 60
    if not checks["age_range"]:
        findings.append("Fascia d'età non valida.")

    checks["duration"] = 15 <= episode.duration_seconds <= 180
    if not checks["duration"]:
        findings.append("Durata fuori dal limite configurato.")

    checks["bpm"] = 60 <= episode.bpm <= 120
    if not checks["bpm"]:
        findings.append("BPM troppo elevati per il profilo editoriale 0–3.")

    scene_durations = [int(s.get("duration_seconds", 0)) for s in episode.storyboard_json]
    checks["scene_pacing"] = bool(scene_durations) and min(scene_durations) >= 4
    if not checks["scene_pacing"]:
        findings.append("Scene troppo rapide o storyboard mancante.")

    selected = {asset.kind for asset in episode.assets if asset.selected and Path(asset.path).exists()}
    for kind, label in [
        (AssetKind.MUSIC, "music"), (AssetKind.RENDER, "main_render"),
        (AssetKind.SHORT, "short"), (AssetKind.THUMBNAIL, "thumbnail"),
    ]:
        checks[label] = kind in selected
        if not checks[label]:
            findings.append(f"Asset obbligatorio mancante: {label}.")

    score = round(100 * sum(checks.values()) / max(1, len(checks)))
    return QCResult(passed=not findings and all(checks.values()), score=score, findings=findings, checks=checks)
