from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any


def _imports():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError("Install the Google API dependencies from requirements.txt") from exc
    return Request, Credentials, InstalledAppFlow, build, MediaFileUpload


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def authorize(client_secrets_file: Path, token_file: Path) -> None:
    Request, Credentials, InstalledAppFlow, build, MediaFileUpload = _imports()
    del Request, Credentials, build, MediaFileUpload
    if not client_secrets_file.exists():
        raise FileNotFoundError(f"Missing OAuth client secrets file: {client_secrets_file}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")


def load_credentials(client_secrets_file: Path, token_file: Path):
    Request, Credentials, InstalledAppFlow, build, MediaFileUpload = _imports()
    del InstalledAppFlow, build, MediaFileUpload
    if not token_file.exists():
        raise RuntimeError("YouTube OAuth token missing. Run: python scripts/youtube_auth.py")
    credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_file.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        raise RuntimeError("YouTube credentials are invalid; repeat OAuth authorization")
    return credentials


class YouTubeClient:
    def __init__(self, *, client_secrets_file: Path, token_file: Path, category_id: str = "10"):
        self.client_secrets_file = client_secrets_file
        self.token_file = token_file
        self.category_id = category_id

    def _services(self):
        Request, Credentials, InstalledAppFlow, build, MediaFileUpload = _imports()
        del Request, Credentials, InstalledAppFlow
        credentials = load_credentials(self.client_secrets_file, self.token_file)
        return build("youtube", "v3", credentials=credentials), build("youtubeAnalytics", "v2", credentials=credentials), MediaFileUpload

    def upload_video(
        self,
        *,
        video_path: Path,
        thumbnail_path: Path | None,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str = "private",
        made_for_kids: bool = True,
    ) -> dict[str, Any]:
        youtube, analytics, MediaFileUpload = self._services()
        del analytics
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:30],
                "categoryId": self.category_id,
                "defaultLanguage": "it",
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": made_for_kids,
                "embeddable": True,
            },
        }
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4"),
        )
        response = None
        while response is None:
            _status, response = request.next_chunk()
        video_id = response["id"]
        if thumbnail_path and thumbnail_path.exists():
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png")).execute()
        return response

    def channel_metrics(self, video_id: str, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        youtube, analytics, MediaFileUpload = self._services()
        del youtube, MediaFileUpload
        end_date = end_date or date.today().isoformat()
        start_date = start_date or (date.today() - timedelta(days=365)).isoformat()
        summary = analytics.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
            filters=f"video=={video_id}",
        ).execute()
        values = (summary.get("rows") or [[0, 0, 0, 0, 0]])[0]
        retention = analytics.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="audienceWatchRatio,relativeRetentionPerformance",
            dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}",
            sort="elapsedVideoTimeRatio",
        ).execute()
        curve = [
            {"elapsed_ratio": float(row[0]), "audience_watch_ratio": float(row[1]), "relative_retention": float(row[2])}
            for row in retention.get("rows", [])
        ]
        relative_values = [point["relative_retention"] for point in curve]
        relative_average = sum(relative_values) / len(relative_values) if relative_values else None
        return {
            "views": int(values[0] or 0),
            "watch_minutes": float(values[1] or 0),
            "average_view_duration_seconds": float(values[2] or 0),
            "average_view_percentage": float(values[3] or 0),
            "subscribers_gained": int(values[4] or 0),
            "relative_retention": relative_average,
            "retention_curve": curve,
        }
