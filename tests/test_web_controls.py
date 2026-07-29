from __future__ import annotations

from io import BytesIO
from pathlib import Path

import app.main as main_module
from PIL import Image
from tests.test_auth import configured_client


def _login(client) -> None:
    response = client.post(
        "/login",
        data={
            "username": "stefano",
            "password": "a-different-long-password",
            "next": "/",
        },
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _png_bytes(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (64, 64), color).save(output, "PNG")
    return output.getvalue()


def test_reference_pack_upload_requires_and_displays_all_three_slots(
    tmp_path: Path,
    monkeypatch,
):
    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        monkeypatch.setattr(
            main_module.settings,
            "storage_root",
            tmp_path / "storage",
        )
        main_module.settings.ensure_directories()
        created = client.post(
            "/episodes",
            data={
                "title": "Reference pack",
                "theme": "colori",
                "hook": "Emma gioca con sette pulcini",
                "target_words": "rosso, giallo, verde",
                "featured_characters": "Emma, Nuvi la nuvola, pulcini",
                "age_min_months": "6",
                "age_max_months": "24",
                "duration_seconds": "75",
                "bpm": "112",
                "visual_pacing": "gentle",
                "language": "it",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        episode_url = created.headers["location"]

        uploaded = client.post(
            f"{episode_url}/reference",
            files={
                "friends_file": ("friends.png", _png_bytes("red"), "image/png"),
                "world_file": ("world.png", _png_bytes("green"), "image/png"),
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert uploaded.status_code == 303
        detail = client.get(episode_url)
        assert detail.status_code == 200
        assert "3/3 completo" in detail.text
        assert "Reference ufficiale Emma" in detail.text
        assert "Reference Amici dell’episodio" in detail.text
        assert "Reference Mondo dell’episodio" in detail.text


def test_failed_dispatch_can_be_retried_from_episode_page(
    tmp_path: Path,
    monkeypatch,
):
    dispatch_calls: list[str] = []

    def dispatch_once_then_succeed(_settings, job_id: str) -> str:
        dispatch_calls.append(job_id)
        if len(dispatch_calls) == 1:
            raise RuntimeError("temporary Cloud Run API failure")
        return "operations/retry-succeeded"

    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        monkeypatch.setattr(main_module.settings, "app_env", "production")
        monkeypatch.setattr(main_module.settings, "provider_mode", "live")
        monkeypatch.setattr(
            main_module.settings,
            "app_base_url",
            "http://testserver",
        )
        monkeypatch.setattr(
            main_module.settings,
            "cloud_run_dispatch_retry_seconds",
            0,
        )
        monkeypatch.setattr(
            main_module,
            "dispatch_worker_job",
            dispatch_once_then_succeed,
        )

        created = client.post(
            "/episodes",
            data={
                "title": "Pilota sicuro",
                "theme": "saluto",
                "hook": "Nuvibù saluta piano",
                "target_words": "ciao",
                "featured_characters": "Nuvibù",
                "age_min_months": "6",
                "age_max_months": "24",
                "duration_seconds": "24",
                "bpm": "92",
                "visual_pacing": "gentle",
                "language": "it",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        episode_url = created.headers["location"]

        failed = client.post(
            f"{episode_url}/run",
            data={"through_step": "lyrics", "queued": "true"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert failed.status_code == 503
        assert len(dispatch_calls) == 1

        detail = client.get(
            episode_url,
            headers={"accept": "text/html"},
        )
        assert detail.status_code == 200
        assert "temporary Cloud Run API failure" in detail.text
        assert "Riprova avvio worker" in detail.text

        retried = client.post(
            f"{episode_url}/run",
            data={"through_step": "lyrics", "queued": "true"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert retried.status_code == 303
        assert len(dispatch_calls) == 2
        assert dispatch_calls[0] == dispatch_calls[1]


def test_healthy_pending_dispatch_cannot_be_retried_early(
    tmp_path: Path,
    monkeypatch,
):
    dispatch_calls: list[str] = []

    def dispatch_successfully(_settings, job_id: str) -> str:
        dispatch_calls.append(job_id)
        return "operations/started"

    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        monkeypatch.setattr(main_module.settings, "app_env", "production")
        monkeypatch.setattr(main_module.settings, "provider_mode", "live")
        monkeypatch.setattr(
            main_module.settings,
            "app_base_url",
            "http://testserver",
        )
        monkeypatch.setattr(
            main_module.settings,
            "cloud_run_dispatch_retry_seconds",
            180,
        )
        monkeypatch.setattr(
            main_module,
            "dispatch_worker_job",
            dispatch_successfully,
        )

        created = client.post(
            "/episodes",
            data={
                "title": "Pilota in partenza",
                "theme": "saluto",
                "hook": "Nuvibù saluta piano",
                "target_words": "ciao",
                "featured_characters": "Nuvibù",
                "age_min_months": "6",
                "age_max_months": "24",
                "duration_seconds": "24",
                "bpm": "92",
                "visual_pacing": "gentle",
                "language": "it",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        episode_url = created.headers["location"]

        started = client.post(
            f"{episode_url}/run",
            data={"through_step": "lyrics", "queued": "true"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert started.status_code == 303
        assert len(dispatch_calls) == 1

        duplicate = client.post(
            f"{episode_url}/run",
            data={"through_step": "lyrics", "queued": "true"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert duplicate.status_code == 409
        assert "not ready to be retried" in duplicate.json()["detail"]
        assert len(dispatch_calls) == 1


def test_music_ready_page_can_regenerate_only_after_cost_confirmation(
    tmp_path: Path,
    monkeypatch,
):
    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        monkeypatch.setattr(
            main_module.settings,
            "storage_root",
            tmp_path / "storage",
        )
        main_module.settings.ensure_directories()
        created = client.post(
            "/episodes",
            data={
                "title": "Musica da rifare",
                "theme": "colori",
                "hook": "Sette pulcini saltano nella pozza",
                "target_words": "rosso, giallo, verde, blu",
                "featured_characters": "Nuvibù, pulcini",
                "age_min_months": "6",
                "age_max_months": "24",
                "duration_seconds": "15",
                "bpm": "92",
                "visual_pacing": "gentle",
                "language": "it",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        episode_url = created.headers["location"]
        assert client.post(
            f"{episode_url}/run",
            data={"through_step": "lyrics"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        ).status_code == 303
        assert client.post(
            f"{episode_url}/lyrics/approve",
            data={
                "lyrics_text": (
                    "[Intro]\nPio pio, eccoci qua!\n"
                    "[Ritornello]\nSalta e canta con Nuvibù!"
                )
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        ).status_code == 303
        assert client.post(
            f"{episode_url}/run",
            data={"through_step": "storyboard"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        ).status_code == 303
        assert client.post(
            f"{episode_url}/storyboard/approve",
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        ).status_code == 303
        assert client.post(
            f"{episode_url}/run",
            data={"through_step": "music"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        ).status_code == 303

        detail = client.get(episode_url)
        assert detail.status_code == 200
        assert "Rigenera musica" in detail.text
        assert "La versione attuale e il suo costo resteranno nel registro" in detail.text
        assert "<audio" in detail.text

        rejected = client.post(
            f"{episode_url}/music/regenerate",
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert rejected.status_code == 400

        regenerated = client.post(
            f"{episode_url}/music/regenerate",
            data={"confirm_cost": "true"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert regenerated.status_code == 303
        assert len(
            list(
                (tmp_path / "storage" / "assets").glob(
                    "*/music-v1*.mp3"
                )
            )
        ) == 2
