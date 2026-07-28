from __future__ import annotations

import json
import subprocess
from pathlib import Path


def is_valid_video(path: Path, *, decode_timeout_seconds: int = 180) -> bool:
    """Return whether a file is a complete, decodable video.

    `ffprobe` verifies the container and stream metadata, while the full FFmpeg
    decode catches truncated media whose MP4 header is still readable.
    """

    try:
        if not path.is_file() or path.stat().st_size <= 1024:
            return False
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,duration:"
                "format=duration,format_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode != 0:
            return False
        payload = json.loads(probe.stdout)
        streams = payload.get("streams")
        if not isinstance(streams, list) or not streams:
            return False
        stream = streams[0]
        if (
            stream.get("codec_type") != "video"
            or not stream.get("codec_name")
            or int(stream.get("width") or 0) <= 0
            or int(stream.get("height") or 0) <= 0
        ):
            return False
        durations = (
            stream.get("duration"),
            (payload.get("format") or {}).get("duration"),
        )
        if not any(_positive_float(value) for value in durations):
            return False

        decode = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=decode_timeout_seconds,
        )
        return decode.returncode == 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return False


def _positive_float(value: object) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False
