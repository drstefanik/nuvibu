from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Nuvibù Studio"
    app_env: Literal["development", "test", "production"] = "development"
    runtime_role: Literal["web", "worker"] = "web"
    app_base_url: str = "http://localhost:8000"
    secret_key: str = "development-only-change-me"
    admin_username: str | None = None
    admin_password: str | None = None
    database_url: str = "sqlite:///./nuvibu.db"
    storage_root: Path = Path("./storage")
    storage_backend: Literal["local", "gcs_mount"] = "local"
    provider_mode: Literal["mock", "live"] = "mock"

    elevenlabs_api_key: str | None = None
    elevenlabs_music_model: str = "music_v2"
    elevenlabs_output_format: str = "mp3_48000_192"

    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"
    google_application_credentials: str | None = None
    veo_backend: Literal["gemini", "vertex"] = "vertex"
    gemini_api_key: str | None = None
    veo_model: str = "veo-3.1-generate-001"
    veo_output_gcs_uri: str | None = None
    cloud_run_job_name: str | None = None
    cloud_run_job_location: str = "us-central1"
    cloud_run_dispatch_retry_seconds: int = Field(default=180, ge=30, le=3600)
    job_stale_after_seconds: int = Field(default=7200, ge=900, le=86400)

    youtube_client_secrets_file: Path = Path("./secrets/youtube-client-secret.json")
    youtube_token_file: Path = Path("./secrets/youtube-token.json")
    youtube_enabled: bool = False
    youtube_category_id: str = "10"
    youtube_default_privacy: Literal["private", "unlisted", "public"] = "private"
    youtube_channel_made_for_kids: bool = True

    max_episode_seconds: int = Field(default=180, ge=15, le=600)
    max_music_variants: int = Field(default=1, ge=1, le=8)
    max_scene_retries: int = Field(default=0, ge=0, le=5)
    max_estimated_cost_usd_per_episode: float = Field(default=40.0, ge=1.0, le=1000.0)

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @model_validator(mode="after")
    def validate_veo_model_backend(self) -> "Settings":
        if self.veo_backend == "gemini" and "veo_model" not in self.model_fields_set:
            self.veo_model = "veo-3.1-fast-generate-preview"
        if self.veo_backend == "vertex" and "veo_model" not in self.model_fields_set:
            self.veo_model = "veo-3.1-generate-001"
        if self.veo_backend == "gemini" and not self.veo_model.endswith("-preview"):
            raise ValueError("Gemini Veo models must use a Gemini API *-preview model ID")
        if self.veo_backend == "vertex" and self.veo_model.endswith("-preview"):
            raise ValueError("Vertex Veo models must use a Vertex model ID such as *-001")
        return self

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

    def production_errors(self, *, require_dispatch: bool = True) -> list[str]:
        """Return unsafe or incomplete production settings.

        Production deliberately fails closed: a missing mount, secret, provider,
        or database must never silently fall back to local/mock behaviour.
        """

        if self.app_env != "production":
            return []
        errors: list[str] = []
        if self.runtime_role == "web":
            if not self.admin_username or not self.admin_password:
                errors.append("ADMIN_USERNAME and ADMIN_PASSWORD are required")
            elif len(self.admin_password) < 16:
                errors.append("ADMIN_PASSWORD must contain at least 16 characters")
            if self.secret_key in {
                "development-only-change-me",
                "replace-with-a-long-random-secret",
            } or len(self.secret_key) < 32:
                errors.append("SECRET_KEY must be changed and contain at least 32 characters")
            if not self.app_base_url.startswith("https://"):
                errors.append("APP_BASE_URL must use HTTPS")
        if not self.database_url.startswith("postgresql"):
            errors.append("DATABASE_URL must use PostgreSQL/Neon")
        if self.provider_mode != "live":
            errors.append("PROVIDER_MODE must be live")
        if self.storage_backend != "gcs_mount":
            errors.append("STORAGE_BACKEND must be gcs_mount")
        if not self.storage_root.is_absolute():
            errors.append("STORAGE_ROOT must be an absolute path")
        elif not self.storage_root.is_dir() or not os.path.ismount(self.storage_root):
            errors.append(f"STORAGE_ROOT is not a mounted filesystem: {self.storage_root}")
        if self.runtime_role == "worker":
            if not self.elevenlabs_api_key:
                errors.append("ELEVENLABS_API_KEY is required")
            if self.veo_backend == "gemini":
                if not self.gemini_api_key:
                    errors.append("GEMINI_API_KEY is required for VEO_BACKEND=gemini")
            elif not self.google_cloud_project:
                errors.append("GOOGLE_CLOUD_PROJECT is required for VEO_BACKEND=vertex")
            if self.veo_backend == "vertex" and not self.veo_output_gcs_uri:
                errors.append("VEO_OUTPUT_GCS_URI is required for VEO_BACKEND=vertex")
        if require_dispatch:
            if not self.google_cloud_project:
                errors.append("GOOGLE_CLOUD_PROJECT is required to dispatch the worker")
            if not self.cloud_run_job_name:
                errors.append("CLOUD_RUN_JOB_NAME is required to dispatch the worker")
        return errors

    def validate_production(self, *, require_dispatch: bool = True) -> None:
        errors = self.production_errors(require_dispatch=require_dispatch)
        if errors:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_production(require_dispatch=settings.runtime_role == "web")
    settings.ensure_directories()
    return settings
