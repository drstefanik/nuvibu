from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np


MUSIC_ANALYSIS_SAMPLE_RATE = 24_000
MUSIC_MIN_LOW_BAND_ENERGY_RATIO = 0.001
BROWSER_VIDEO_CODECS = {"h264", "avc1"}


def _music_arrangement_metrics_from_samples(
    samples: np.ndarray,
    *,
    sample_rate: int = MUSIC_ANALYSIS_SAMPLE_RATE,
) -> dict:
    """Measure whether a track has the low-frequency bed of an arrangement.

    This is deliberately conservative: it does not score artistic quality.
    It only catches the production failure seen in the pilot, where almost all
    energy was concentrated in the vocal range and the promised bass/drums
    were effectively absent.
    """

    mono = np.asarray(samples, dtype=np.float32).reshape(-1)
    minimum_samples = sample_rate * 3
    if mono.size < minimum_samples:
        return {
            "passed": False,
            "reason": "audio_too_short_for_arrangement_analysis",
            "low_band_energy_ratio": 0.0,
            "minimum_low_band_energy_ratio": MUSIC_MIN_LOW_BAND_ENERGY_RATIO,
        }

    frame_size = 8192
    usable = mono[: mono.size // frame_size * frame_size]
    frames = usable.reshape(-1, frame_size)
    frame_energy = np.mean(frames * frames, axis=1)
    active_floor = max(float(frame_energy.mean()) * 0.01, 1e-9)
    active_frames = frames[frame_energy >= active_floor]
    if active_frames.size == 0:
        return {
            "passed": False,
            "reason": "audio_has_no_active_music_frames",
            "low_band_energy_ratio": 0.0,
            "minimum_low_band_energy_ratio": MUSIC_MIN_LOW_BAND_ENERGY_RATIO,
        }

    window = np.hanning(frame_size).astype(np.float32)
    power = np.abs(np.fft.rfft(active_frames * window, axis=1)) ** 2
    average_power = power.mean(axis=0)
    frequencies = np.fft.rfftfreq(frame_size, 1 / sample_rate)
    audible = (frequencies >= 45) & (frequencies < sample_rate / 2)
    low_band = (frequencies >= 45) & (frequencies < 180)
    total_energy = float(average_power[audible].sum())
    low_band_energy = float(average_power[low_band].sum())
    ratio = low_band_energy / total_energy if total_energy > 0 else 0.0
    passed = ratio >= MUSIC_MIN_LOW_BAND_ENERGY_RATIO
    return {
        "passed": passed,
        "reason": (
            "instrumental_low_end_present"
            if passed
            else "instrumental_backing_too_sparse_or_voice_only"
        ),
        "low_band_energy_ratio": round(ratio, 8),
        "minimum_low_band_energy_ratio": MUSIC_MIN_LOW_BAND_ENERGY_RATIO,
        "sample_rate": sample_rate,
        "active_frames": int(active_frames.shape[0]),
    }


def music_arrangement_quality(
    path: Path,
    *,
    decode_timeout_seconds: int = 180,
) -> dict:
    """Decode a music asset and reject a voice-only/a-cappella production."""

    try:
        if not path.is_file() or path.stat().st_size <= 1024:
            raise ValueError("music file is missing or empty")
        decode = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                str(MUSIC_ANALYSIS_SAMPLE_RATE),
                "-f",
                "f32le",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=decode_timeout_seconds,
        )
        if decode.returncode != 0:
            raise ValueError(
                decode.stderr.decode("utf-8", errors="replace")[:500]
                or "FFmpeg could not decode the music asset"
            )
        samples = np.frombuffer(decode.stdout, dtype="<f4")
        return _music_arrangement_metrics_from_samples(samples)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {
            "passed": False,
            "reason": "music_arrangement_analysis_failed",
            "detail": str(exc)[:500],
            "low_band_energy_ratio": 0.0,
            "minimum_low_band_energy_ratio": MUSIC_MIN_LOW_BAND_ENERGY_RATIO,
        }


def video_stream_info(
    path: Path,
    *,
    probe_timeout_seconds: int = 30,
) -> dict | None:
    """Return normalized metadata for the first real video stream."""

    try:
        if not path.is_file() or path.stat().st_size <= 1024:
            return None
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
            timeout=probe_timeout_seconds,
        )
        if probe.returncode != 0:
            return None
        payload = json.loads(probe.stdout)
        streams = payload.get("streams")
        if not isinstance(streams, list) or not streams:
            return None
        stream = streams[0]
        if (
            stream.get("codec_type") != "video"
            or not stream.get("codec_name")
            or int(stream.get("width") or 0) <= 0
            or int(stream.get("height") or 0) <= 0
        ):
            return None
        durations = (
            stream.get("duration"),
            (payload.get("format") or {}).get("duration"),
        )
        duration = next(
            (
                float(value)
                for value in durations
                if _positive_float(value)
            ),
            0.0,
        )
        if duration <= 0:
            return None
        return {
            "codec_name": str(stream["codec_name"]).casefold(),
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "duration_seconds": duration,
            "format_name": str(
                (payload.get("format") or {}).get("format_name") or ""
            ),
        }
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return None


def is_streamable_video(
    path: Path,
    *,
    minimum_duration_seconds: float = 0.0,
    browser_compatible: bool = False,
    probe_timeout_seconds: int = 30,
) -> bool:
    """Return whether a browser can discover a usable video and duration."""

    info = video_stream_info(
        path,
        probe_timeout_seconds=probe_timeout_seconds,
    )
    if info is None:
        return False
    if (
        minimum_duration_seconds > 0
        and float(info["duration_seconds"]) + 0.05
        < minimum_duration_seconds
    ):
        return False
    if (
        browser_compatible
        and str(info["codec_name"]) not in BROWSER_VIDEO_CODECS
    ):
        return False
    return True


def is_valid_video(
    path: Path,
    *,
    minimum_duration_seconds: float = 0.0,
    browser_compatible: bool = False,
    decode_timeout_seconds: int = 180,
) -> bool:
    """Return whether a file is complete and fully decodable."""

    try:
        if not is_streamable_video(
            path,
            minimum_duration_seconds=minimum_duration_seconds,
            browser_compatible=browser_compatible,
        ):
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
    except (OSError, subprocess.TimeoutExpired):
        return False


def _positive_float(value: object) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False
