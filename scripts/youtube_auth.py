from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.providers.youtube import authorize


if __name__ == "__main__":
    settings = get_settings()
    authorize(settings.youtube_client_secrets_file, settings.youtube_token_file)
    print(f"YouTube OAuth token saved to {settings.youtube_token_file}")
