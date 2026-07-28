from __future__ import annotations

from dataclasses import dataclass

from ..models import Episode, MetricSnapshot


@dataclass(slots=True)
class GrowthScore:
    score: float
    confidence: str
    recommendation: str
    components: dict[str, float]


def calculate_growth_score(metric: MetricSnapshot | None) -> GrowthScore:
    if metric is None or metric.views <= 0:
        return GrowthScore(0.0, "nessun dato", "Pubblicare e raccogliere un campione reale.", {})
    avp = min(1.0, metric.average_view_percentage / 100)
    relative = metric.relative_retention if metric.relative_retention is not None else 0.5
    ctr = min(1.0, (metric.impressions_ctr or 0.0) / 10)
    subs_per_1k = min(1.0, (metric.subscribers_gained / metric.views * 1000) / 12)
    score = 100 * (0.42 * avp + 0.33 * relative + 0.15 * ctr + 0.10 * subs_per_1k)
    confidence = "bassa" if metric.views < 500 else "media" if metric.views < 5000 else "alta"
    if metric.views < 500:
        recommendation = "Non scalare ancora: campione insufficiente. Mantieni il format per altri episodi comparabili."
    elif avp < 0.55:
        recommendation = "Ridurre l'introduzione e portare il primo reveal entro i primi 3 secondi."
    elif relative < 0.55:
        recommendation = "Semplificare la parte centrale e ripetere prima il ritornello."
    elif ctr < 0.45 and metric.impressions > 1000:
        recommendation = "Testare una nuova thumbnail mantenendo invariato il video."
    else:
        recommendation = "Format promettente: produrre due variazioni controllate, non copie identiche."
    return GrowthScore(
        round(score, 1), confidence, recommendation,
        {"average_view_percentage": round(avp*100,1), "relative_retention": round(relative*100,1), "ctr_normalized": round(ctr*100,1), "subs_per_1k_normalized": round(subs_per_1k*100,1)},
    )


def latest_metric(episode: Episode) -> MetricSnapshot | None:
    return max(episode.metrics, key=lambda m: m.captured_at) if episode.metrics else None
