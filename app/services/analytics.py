from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Episode, MetricSnapshot, PublishRecord
from ..providers.youtube import YouTubeClient


def sync_youtube_metrics(db: Session, settings: Settings, episode: Episode) -> MetricSnapshot:
    record = next((r for r in reversed(episode.publish_records) if r.platform == "youtube" and r.external_id), None)
    if record is None:
        raise RuntimeError("The episode has no YouTube video ID")
    client = YouTubeClient(
        client_secrets_file=settings.youtube_client_secrets_file,
        token_file=settings.youtube_token_file,
        category_id=settings.youtube_category_id,
    )
    values = client.channel_metrics(record.external_id)
    metric = MetricSnapshot(
        episode_id=episode.id,
        views=values["views"],
        watch_minutes=values["watch_minutes"],
        average_view_duration_seconds=values["average_view_duration_seconds"],
        average_view_percentage=values["average_view_percentage"],
        subscribers_gained=values["subscribers_gained"],
        relative_retention=values["relative_retention"],
        retention_curve_json=values["retention_curve"],
        source="youtube_analytics",
    )
    db.add(metric)
    db.commit()
    db.refresh(metric)
    return metric
