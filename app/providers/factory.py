from __future__ import annotations

from ..config import Settings
from .base import MusicProvider, VideoProvider
from .elevenlabs import ElevenLabsMusicProvider
from .mock import MockMusicProvider, MockVideoProvider
from .text_safe_veo import TextSafeVeoProvider


def get_music_provider(settings: Settings) -> MusicProvider:
    if settings.provider_mode == "mock":
        return MockMusicProvider()
    return ElevenLabsMusicProvider(
        api_key=settings.elevenlabs_api_key or "",
        model_id=settings.elevenlabs_music_model,
        output_format=settings.elevenlabs_output_format,
    )


def get_video_provider(settings: Settings) -> VideoProvider:
    if settings.provider_mode == "mock":
        return MockVideoProvider()
    return TextSafeVeoProvider(
        project=settings.google_cloud_project or "",
        location=settings.google_cloud_location,
        model=settings.veo_model,
        output_gcs_uri=settings.veo_output_gcs_uri,
        credentials_file=settings.google_application_credentials,
        backend=getattr(settings, "veo_backend", "vertex"),
        gemini_api_key=getattr(settings, "gemini_api_key", None),
    )
