from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from urllib.parse import unquote, urlsplit


SESSION_COOKIE_NAME = "nuvibu_session"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60


class LoginAttemptLimiter:
    """Small process-local rolling-window limiter for failed login attempts."""

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_failures = max_failures
        self.window_seconds = float(window_seconds)
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _timestamp(now: float | None) -> float:
        return time.monotonic() if now is None else float(now)

    def _active_failures(self, key: str, now: float) -> deque[float] | None:
        failures = self._failures.get(key)
        if failures is None:
            return None
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
            return None
        return failures

    def check(self, key: str, *, now: float | None = None) -> bool:
        """Return whether another login attempt is currently allowed."""

        timestamp = self._timestamp(now)
        with self._lock:
            failures = self._active_failures(key, timestamp)
            return failures is None or len(failures) < self.max_failures

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Alias for ``check`` for call sites that read as policy decisions."""

        return self.check(key, now=now)

    def record_failure(self, key: str, *, now: float | None = None) -> bool:
        """Record a failure and return whether another attempt remains allowed."""

        timestamp = self._timestamp(now)
        with self._lock:
            failures = self._active_failures(key, timestamp)
            if failures is None:
                failures = deque()
                self._failures[key] = failures
            if len(failures) < self.max_failures:
                failures.append(timestamp)
            return len(failures) < self.max_failures

    def reset(self, key: str) -> None:
        """Forget failures after a successful login."""

        with self._lock:
            self._failures.pop(key, None)


def _signature(
    *,
    secret_key: str,
    username: str,
    password: str,
    issued_at: int,
) -> str:
    material = f"v1\0{username}\0{password}\0{issued_at}".encode("utf-8")
    return hmac.new(
        secret_key.encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()


def create_session_token(
    *,
    secret_key: str,
    username: str,
    password: str,
    now: int | None = None,
) -> str:
    issued_at = int(time.time() if now is None else now)
    signature = _signature(
        secret_key=secret_key,
        username=username,
        password=password,
        issued_at=issued_at,
    )
    return f"{issued_at}.{signature}"


def validate_session_token(
    token: str | None,
    *,
    secret_key: str,
    username: str,
    password: str,
    now: int | None = None,
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> bool:
    if not token:
        return False
    try:
        issued_raw, supplied_signature = token.split(".", 1)
        issued_at = int(issued_raw)
    except (TypeError, ValueError):
        return False

    current_time = int(time.time() if now is None else now)
    if issued_at > current_time + 60:
        return False
    if current_time - issued_at > max_age_seconds:
        return False

    expected_signature = _signature(
        secret_key=secret_key,
        username=username,
        password=password,
        issued_at=issued_at,
    )
    return secrets.compare_digest(supplied_signature, expected_signature)


def safe_next_path(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    decoded = unquote(value)
    if decoded.startswith("//"):
        return "/"
    if "\\" in value or "\\" in decoded:
        return "/"
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return "/"
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value
