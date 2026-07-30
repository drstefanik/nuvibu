from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .music_direction import (
    DEFAULT_MUSIC_DIRECTION,
    MAX_MUSIC_DIRECTION_LENGTH,
)


class EpisodeCreate(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    theme: str = Field(min_length=2, max_length=100)
    hook: str = Field(min_length=3, max_length=220)
    age_min_months: int = Field(default=6, ge=0, le=48)
    age_max_months: int = Field(default=24, ge=1, le=60)
    target_words: list[str] = Field(default_factory=list, max_length=24)
    featured_characters: list[str] = Field(
        default_factory=lambda: ["Emma", "Nuvi la nuvola"],
        max_length=6,
    )
    duration_seconds: int = Field(default=24, ge=15, le=600)
    bpm: int = Field(default=92, ge=60, le=160)
    music_direction: str = Field(
        default=DEFAULT_MUSIC_DIRECTION,
        min_length=20,
        max_length=MAX_MUSIC_DIRECTION_LENGTH,
    )
    visual_pacing: str = Field(default="gentle", pattern=r"^(gentle|medium|energetic)$")
    language: str = Field(default="it", pattern=r"^(it|en)$")

    @field_validator("music_direction", mode="before")
    @classmethod
    def normalize_music_direction(cls, value):
        return str(value or "").strip()

    @field_validator("target_words", mode="before")
    @classmethod
    def normalize_target_words(cls, value):
        """Keep useful prompt vocabulary without duplicate or blank entries."""

        words = value if isinstance(value, list) else []
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_word in words:
            word = str(raw_word).strip()
            key = word.casefold()
            if not word or key in seen:
                continue
            seen.add(key)
            normalized.append(word)
        return normalized[:24]

    @field_validator("featured_characters", mode="before")
    @classmethod
    def lock_emma_as_protagonist(cls, value):
        """Nuvibù is the brand; Emma is always the first human character."""

        names = value if isinstance(value, list) else []
        supporting: list[str] = []
        for raw_name in names:
            name = str(raw_name).strip()
            if not name or name.casefold() in {"emma", "nuvibù", "nuvibu"}:
                continue
            if name not in supporting:
                supporting.append(name)
        return ["Emma", *supporting[:5]]

    @field_validator("age_max_months")
    @classmethod
    def validate_age_range(cls, value: int, info):
        min_age = info.data.get("age_min_months", 0)
        if value <= min_age:
            raise ValueError("age_max_months must be greater than age_min_months")
        return value


class PipelineRequest(BaseModel):
    through_step: str = Field(default="qc", pattern=r"^(lyrics|music|storyboard|scenes|render|qc)$")
    confirm_cost: bool = False


class MetricCreate(BaseModel):
    views: int = Field(default=0, ge=0)
    watch_minutes: float = Field(default=0, ge=0)
    average_view_duration_seconds: float = Field(default=0, ge=0)
    average_view_percentage: float = Field(default=0, ge=0, le=100)
    impressions: int = Field(default=0, ge=0)
    impressions_ctr: float | None = Field(default=None, ge=0, le=100)
    subscribers_gained: int = Field(default=0, ge=0)
    relative_retention: float | None = Field(default=None, ge=0, le=1)
    retention_curve: list[dict[str, float]] = Field(default_factory=list)
