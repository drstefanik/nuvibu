from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import app.main as main_module
from app.auth import (
    LoginAttemptLimiter,
    SESSION_COOKIE_NAME,
    create_session_token,
    safe_next_path,
    validate_session_token,
)
from app.database import Base, get_db


@contextmanager
def configured_client(
    tmp_path: Path,
    monkeypatch,
) -> Iterator[TestClient]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with Session() as db:
            yield db

    monkeypatch.setattr(main_module.settings, "app_env", "test")
    monkeypatch.setattr(main_module.settings, "admin_username", "stefano")
    monkeypatch.setattr(
        main_module.settings,
        "admin_password",
        "a-different-long-password",
    )
    monkeypatch.setattr(
        main_module.settings,
        "secret_key",
        "a-production-grade-session-signing-secret",
    )
    monkeypatch.setattr(main_module, "init_db", lambda: None)
    main_module.app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(main_module.app) as client:
            yield client
    finally:
        main_module.app.dependency_overrides.clear()
        engine.dispose()


def test_browser_login_uses_secure_cookie_session(tmp_path: Path, monkeypatch):
    with configured_client(tmp_path, monkeypatch) as client:
        protected = client.get(
            "/",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert protected.status_code == 303
        assert protected.headers["location"] == "/login?next=%2F"

        page = client.get("/login")
        assert page.status_code == 200
        assert "Accedi allo studio" in page.text

        invalid = client.post(
            "/login",
            data={
                "username": "stefano",
                "password": "wrong-password",
                "next": "/",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert invalid.status_code == 401
        assert "Credenziali non valide" in invalid.text

        authenticated = client.post(
            "/login",
            data={
                "username": "stefano",
                "password": "a-different-long-password",
                "next": "/",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert authenticated.status_code == 303
        assert authenticated.headers["location"] == "/"
        assert SESSION_COOKIE_NAME in authenticated.cookies
        assert "HttpOnly" in authenticated.headers["set-cookie"]
        assert "SameSite=lax" in authenticated.headers["set-cookie"]

        dashboard = client.get("/", headers={"accept": "text/html"})
        assert dashboard.status_code == 200
        assert "Crea il pilota" in dashboard.text

        logged_out = client.post(
            "/logout",
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert logged_out.status_code == 303
        assert logged_out.headers["location"] == "/login"

        denied_after_logout = client.get(
            "/",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        assert denied_after_logout.status_code == 303
        assert denied_after_logout.headers["location"] == "/login?next=%2F"


def test_api_requests_still_receive_json_401(tmp_path: Path, monkeypatch):
    with configured_client(tmp_path, monkeypatch) as client:
        response = client.get(
            "/api/growth",
            headers={"accept": "application/json"},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Authentication required"}


def test_production_login_cookie_is_secure(tmp_path: Path, monkeypatch):
    with configured_client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(main_module.settings, "app_env", "production")
        monkeypatch.setattr(
            main_module.settings,
            "app_base_url",
            "https://testserver",
        )
        response = client.post(
            "https://testserver/login",
            data={
                "username": "stefano",
                "password": "a-different-long-password",
                "next": "/",
            },
            headers={"origin": "https://testserver"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "Secure" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=lax" in response.headers["set-cookie"]


def test_session_token_expires_and_rotates_with_password():
    token = create_session_token(
        secret_key="session-secret",
        username="admin",
        password="first-password",
        now=1_000,
    )
    assert validate_session_token(
        token,
        secret_key="session-secret",
        username="admin",
        password="first-password",
        now=1_100,
    )
    assert not validate_session_token(
        token,
        secret_key="session-secret",
        username="admin",
        password="rotated-password",
        now=1_100,
    )
    assert not validate_session_token(
        token,
        secret_key="session-secret",
        username="admin",
        password="first-password",
        now=1_000 + 43_201,
    )
    issued_at, signature = token.split(".", 1)
    tampered = f"{issued_at}.{signature[:-1]}{'0' if signature[-1] != '0' else '1'}"
    assert not validate_session_token(
        tampered,
        secret_key="session-secret",
        username="admin",
        password="first-password",
        now=1_100,
    )


def test_next_path_rejects_external_redirects():
    assert safe_next_path("/episodes/123?tab=qc") == "/episodes/123?tab=qc"
    assert safe_next_path("https://attacker.example") == "/"
    assert safe_next_path("//attacker.example") == "/"
    assert safe_next_path("/\r\nLocation: https://attacker.example") == "/"
    assert safe_next_path("/\\attacker.example") == "/"
    assert safe_next_path("/%5cattacker.example") == "/"
    assert safe_next_path("/episodes/\x00hidden") == "/"
    assert safe_next_path("/episodes/\x1fhidden") == "/"
    assert safe_next_path("/episodes/\x7fhidden") == "/"
    assert safe_next_path("/episodes/%00hidden") == "/"


def test_login_attempt_limiter_blocks_five_failures_for_five_minutes():
    limiter = LoginAttemptLimiter()

    for attempt in range(4):
        assert limiter.check("login-key", now=float(attempt))
        assert limiter.record_failure("login-key", now=float(attempt))

    assert limiter.allow("login-key", now=4.0)
    assert not limiter.record_failure("login-key", now=4.0)
    assert not limiter.check("login-key", now=299.0)

    assert limiter.check("login-key", now=300.0)
    limiter.record_failure("login-key", now=300.0)
    limiter.reset("login-key")
    assert limiter.check("login-key", now=300.0)


def test_login_attempt_limiter_keeps_keys_independent():
    limiter = LoginAttemptLimiter(max_failures=1, window_seconds=300)

    assert not limiter.record_failure("first", now=10.0)
    assert not limiter.check("first", now=10.0)
    assert limiter.check("second", now=10.0)


def test_production_login_rate_limit_uses_cloud_run_client_ip(
    tmp_path: Path,
    monkeypatch,
):
    first_client = "xff:203.0.113.10"
    second_client = "xff:198.51.100.20"
    main_module.LOGIN_LIMITER.reset(first_client)
    main_module.LOGIN_LIMITER.reset(second_client)

    with configured_client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(main_module.settings, "app_env", "production")
        monkeypatch.setattr(
            main_module.settings,
            "app_base_url",
            "https://testserver",
        )
        wrong_login = {
            "username": "stefano",
            "password": "wrong-password",
            "next": "/",
        }

        responses = [
            client.post(
                "https://testserver/login",
                data=wrong_login,
                headers={
                    "origin": "https://testserver",
                    # The left value is client-supplied and must be ignored.
                    "x-forwarded-for": (
                        f"192.0.2.{attempt + 1}, "
                        "203.0.113.10, 35.191.0.1"
                    ),
                },
                follow_redirects=False,
            )
            for attempt in range(5)
        ]

        assert [response.status_code for response in responses] == [
            401,
            401,
            401,
            401,
            429,
        ]
        assert responses[-1].headers["retry-after"] == "300"

        independent = client.post(
            "https://testserver/login",
            data=wrong_login,
            headers={
                "origin": "https://testserver",
                "x-forwarded-for": (
                    "192.0.2.200, 198.51.100.20, 35.191.0.1"
                ),
            },
            follow_redirects=False,
        )
        assert independent.status_code == 401

        authenticated = client.post(
            "https://testserver/login",
            data={
                "username": "stefano",
                "password": "a-different-long-password",
                "next": "/",
            },
            headers={
                "origin": "https://testserver",
                "x-forwarded-for": (
                    "192.0.2.201, 198.51.100.20, 35.191.0.1"
                ),
            },
            follow_redirects=False,
        )
        assert authenticated.status_code == 303

    main_module.LOGIN_LIMITER.reset(first_client)
    main_module.LOGIN_LIMITER.reset(second_client)


def test_numeric_alias_redirects_to_canonical_console(monkeypatch):
    monkeypatch.setattr(main_module.settings, "app_env", "production")
    monkeypatch.setattr(
        main_module.settings,
        "app_base_url",
        "https://nuvibu-web-va66lw5csa-uc.a.run.app",
    )
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/episodes/new",
            "raw_path": b"/episodes/new",
            "query_string": b"from=alias",
            "headers": [
                (
                    b"host",
                    b"nuvibu-web-168551345173.us-central1.run.app",
                )
            ],
            "server": (
                "nuvibu-web-168551345173.us-central1.run.app",
                443,
            ),
            "client": ("127.0.0.1", 1234),
        }
    )

    assert main_module.canonical_console_url(request) == (
        "https://nuvibu-web-va66lw5csa-uc.a.run.app"
        "/episodes/new?from=alias"
    )
