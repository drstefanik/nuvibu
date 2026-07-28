from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Nuvibù Studio"
    app_env: Literal["development", "test", "production"] = "development"
    app_base_url: str = "http://localhost:8000"
    secret_key: str = "development-only-change-me"
    admin_username: str | None = None
    admin_password: str | None = None
    database_url: str = "sqlite:///./nuvibu.db"
    storage_root: Path = Path("./storage")
    provider_mode: Literal["mock", "live"] = "mock"

    elevenlabs_api_key: str | None = None
    elevenlabs_music_model: str = "music_v2"
    elevenlabs_output_format: str = "mp3_48000_192"

    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"
    google_application_credentials: str | None = None
    veo_model: str = "veo-3.1-lite-generate-001"
    veo_output_gcs_uri: str | None = None

    youtube_client_secrets_file: Path = Path("./secrets/youtube-client-secret.json")
    youtube_token_file: Path = Path("./secrets/youtube-token.json")
    youtube_category_id: str = "10"
    youtube_default_privacy: Literal["private", "unlisted", "public"] = "private"
    youtube_channel_made_for_kids: bool = True

    max_episode_seconds: int = Field(default=180, ge=15, le=600)
    max_music_variants: int = Field(default=4, ge=1, le=8)
    max_scene_retries: int = Field(default=2, ge=0, le=5)
    max_estimated_cost_usd_per_episode: float = Field(default=40.0, ge=1.0, le=1000.0)

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @property
    def asset_dir(self) -> Path:
        return self.storage_root / "assets"

    @property
    def render_dir(self) -> Path:
        return self.storage_root / "renders"

    @property
    def upload_dir(self) -> Path:
        return self.storage_root / "uploads"

    def ensure_directories(self) -> None:
        for path in (self.asset_dir, self.render_dir, self.upload_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
