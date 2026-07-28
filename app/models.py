from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class EpisodeStatus(str, enum.Enum):
    DRAFT = "draft"
    LYRICS_READY = "lyrics_ready"
    MUSIC_READY = "music_ready"
    STORYBOARD_READY = "storyboard_ready"
    SCENES_READY = "scenes_ready"
    RENDER_READY = "render_ready"
    QC_REVIEW = "qc_review"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssetKind(str, enum.Enum):
    LYRICS = "lyrics"
    MUSIC = "music"
    STORYBOARD = "storyboard"
    CHARACTER_REFERENCE = "character_reference"
    VIDEO_SCENE = "video_scene"
    THUMBNAIL = "thumbnail"
    SUBTITLES = "subtitles"
    RENDER = "render"
    SHORT = "short"
    REPORT = "report"


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    working_slug: Mapped[str] = mapped_column(String(180), nullable=False, unique=True, index=True)
    age_min_months: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    age_max_months: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    theme: Mapped[str] = mapped_column(String(100), nullable=False)
    hook: Mapped[str] = mapped_column(String(220), nullable=False)
    target_words: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    featured_characters: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=75, nullable=False)
    bpm: Mapped[int] = mapped_column(Integer, default=92, nullable=False)
    visual_pacing: Mapped[str] = mapped_column(String(32), default="gentle", nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="it", nullable=False)
    status: Mapped[EpisodeStatus] = mapped_column(
        Enum(EpisodeStatus, native_enum=False), default=EpisodeStatus.DRAFT, nullable=False, index=True
    )
    concept_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    lyrics_text: Mapped[str | None] = mapped_column(Text)
    storyboard_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    qc_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    publish_title: Mapped[str | None] = mapped_column(String(220))
    publish_description: Mapped[str | None] = mapped_column(Text)
    publish_tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    assets: Mapped[list["Asset"]] = relationship(back_populates="episode", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="episode", cascade="all, delete-orphan")
    publish_records: Mapped[list["PublishRecord"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )
    metrics: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    kind: Mapped[AssetKind] = mapped_column(Enum(AssetKind, native_enum=False), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(80), default="local", nullable=False)
    variant: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    episode: Mapped[Episode] = relationship(back_populates="assets")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    job_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.PENDING, nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    episode: Mapped[Episode] = relationship(back_populates="jobs")


class PublishRecord(Base):
    __tablename__ = "publish_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(32), default="youtube", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    privacy_status: Mapped[str] = mapped_column(String(20), default="private", nullable=False)
    made_for_kids: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    episode: Mapped[Episode] = relationship(back_populates="publish_records")


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watch_minutes: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    average_view_duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    average_view_percentage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    impressions_ctr: Mapped[float | None] = mapped_column(Float)
    subscribers_gained: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    relative_retention: Mapped[float | None] = mapped_column(Float)
    retention_curve_json: Mapped[list[dict[str, float]]] = mapped_column(JSON, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)

    episode: Mapped[Episode] = relationship(back_populates="metrics")
