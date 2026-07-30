from __future__ import annotations

import html
import re
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


def _create_episode(client, title: str = "Look di Emma") -> str:
    created = client.post(
        "/episodes",
        data={
            "title": title,
            "theme": "colori",
            "hook": "Emma scopre un arcobaleno",
            "target_words": "rosso, giallo, verde",
            "featured_characters": "Emma, Nuvi la nuvola",
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
    assert created.status_code == 303
    return created.headers["location"]


def _emma_look_buttons(html: str) -> list[str]:
    return re.findall(
        r'<button\s+class="emma-look-choice.*?</button>',
        html,
        flags=re.DOTALL,
    )


def test_music_direction_field_is_prefilled_and_persists(
    tmp_path: Path,
    monkeypatch,
):
    direction = (
        "Turbo electro-pop con basso elastico. Voce femminile adulta e robot "
        "maschile con vocoder; nessun coro infantile continuo."
    )
    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        form = client.get("/episodes/new")

        assert form.status_code == 200
        assert "Direzione musicale e vocale" in form.text
        assert 'name="music_direction"' in form.text
        assert form.text.index('name="bpm"') < form.text.index(
            'name="music_direction"'
        )
        assert "Voce principale femminile adulta" in form.text

        created = client.post(
            "/episodes",
            data={
                "title": "Robot turbo",
                "theme": "baby dance",
                "hook": "Emma insegue un robot ballerino",
                "target_words": "robot, turbo",
                "featured_characters": "Emma, Robot Bumbo",
                "age_min_months": "9",
                "age_max_months": "36",
                "duration_seconds": "75",
                "bpm": "128",
                "music_direction": direction,
                "visual_pacing": "energetic",
                "language": "it",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert created.status_code == 303
        detail = client.get(created.headers["location"])
        assert detail.status_code == 200
        assert direction in detail.text


def test_episode_page_has_exactly_ten_clickable_emma_looks(
    tmp_path: Path,
    monkeypatch,
):
    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        episode_url = _create_episode(client)

        detail = client.get(episode_url)

        assert detail.status_code == 200
        buttons = _emma_look_buttons(detail.text)
        assert len(main_module.EMMA_LOOKS) == 10
        assert len(buttons) == 10
        assert all(" disabled" not in button for button in buttons)
        assert detail.text.count('class="emma-look-form"') == 1
        assert {look.id for look in main_module.EMMA_LOOKS} == {
            re.search(r'data-emma-look-id="([^"]+)"', button).group(1)
            for button in buttons
        }
        assert "Scegli il look di Emma" in detail.text
        assert "type=\"file\" name=\"emma_file\"" not in detail.text
        default_button = next(
            button
            for button in buttons
            if (
                f'data-emma-look-id="'
                f'{main_module.NEW_EPISODE_DEFAULT_LOOK_ID}"'
            ) in button
        )
        assert 'aria-pressed="true"' in default_button


def test_selected_emma_look_persists_and_is_used_by_reference_form(
    tmp_path: Path,
    monkeypatch,
):
    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        episode_url = _create_episode(client)
        selected = next(
            look
            for look in main_module.EMMA_LOOKS
            if look.id != main_module.NEW_EPISODE_DEFAULT_LOOK_ID
        )

        changed = client.post(
            f"{episode_url}/emma-look",
            data={"emma_look_id": selected.id},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert changed.status_code == 303
        assert changed.headers["location"] == (
            f"{episode_url}#character-reference"
        )
        detail = client.get(episode_url)
        selected_button = next(
            button
            for button in _emma_look_buttons(detail.text)
            if f'data-emma-look-id="{selected.id}"' in button
        )
        assert 'aria-pressed="true"' in selected_button
        assert "emma-look-choice-selected" in selected_button
        assert "Selezionato" in selected_button
        assert selected.thumbnail_url in detail.text

        persisted = client.get(episode_url)
        persisted_button = next(
            button
            for button in _emma_look_buttons(persisted.text)
            if f'data-emma-look-id="{selected.id}"' in button
        )
        assert 'aria-pressed="true"' in persisted_button

        original_has_valid_asset = (
            main_module.PipelineService.has_valid_asset
        )
        original_content_is_approved = (
            main_module.PipelineService.content_is_approved
        )
        monkeypatch.setattr(
            main_module.PipelineService,
            "has_valid_asset",
            lambda service, episode, kind: (
                True
                if kind == main_module.AssetKind.MUSIC
                else original_has_valid_asset(service, episode, kind)
            ),
        )
        monkeypatch.setattr(
            main_module.PipelineService,
            "content_is_approved",
            lambda service, episode, content_kind: (
                True
                if content_kind == "storyboard"
                else original_content_is_approved(
                    service,
                    episode,
                    content_kind,
                )
            ),
        )
        editable_detail = client.get(episode_url)
        assert (
            f'<input type="hidden" name="emma_look_id" '
            f'value="{selected.id}">'
        ) in editable_detail.text


def test_invalid_emma_look_id_is_rejected(
    tmp_path: Path,
    monkeypatch,
):
    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        episode_url = _create_episode(client)

        rejected = client.post(
            f"{episode_url}/emma-look",
            data={"emma_look_id": "not-in-the-official-catalog"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "Invalid Emma look"
        detail = client.get(episode_url)
        default_button = next(
            button
            for button in _emma_look_buttons(detail.text)
            if (
                f'data-emma-look-id="'
                f'{main_module.NEW_EPISODE_DEFAULT_LOOK_ID}"'
            ) in button
        )
        assert 'aria-pressed="true"' in default_button


def test_emma_look_controls_are_disabled_and_post_conflicts_when_locked(
    tmp_path: Path,
    monkeypatch,
):
    def reject_locked_change(_service, _episode, _look_id):
        raise main_module.ReferenceChangeConflictError(
            "Emma look is locked",
        )

    with configured_client(tmp_path, monkeypatch) as client:
        _login(client)
        episode_url = _create_episode(client)
        monkeypatch.setattr(
            main_module.PipelineService,
            "reference_pack_mutable",
            lambda _service, _episode: False,
        )
        monkeypatch.setattr(
            main_module.PipelineService,
            "set_emma_look",
            reject_locked_change,
        )

        detail = client.get(episode_url)
        buttons = _emma_look_buttons(detail.text)
        assert len(buttons) == 10
        assert all(" disabled" in button for button in buttons)
        assert all('aria-disabled="true"' in button for button in buttons)
        assert "Il look è bloccato" in detail.text

        rejected = client.post(
            f"{episode_url}/emma-look",
            data={"emma_look_id": main_module.EMMA_LOOKS[1].id},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "Emma look is locked"


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


def test_bundled_reference_preset_is_recommended_and_applied_server_side(
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
                "title": "Palloncini colorati",
                "theme": "nanna",
                "hook": "Emma sogna tra arcobaleni e nuvolette",
                "target_words": "arcobaleno, nanna, nuvolette, culla",
                "featured_characters": "Emma, Nuvi la nuvola",
                "age_min_months": "6",
                "age_max_months": "24",
                "duration_seconds": "75",
                "bpm": "92",
                "visual_pacing": "gentle",
                "language": "it",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        episode_url = created.headers["location"]
        original_has_valid_asset = (
            main_module.PipelineService.has_valid_asset
        )
        monkeypatch.setattr(
            main_module.PipelineService,
            "has_valid_asset",
            lambda service, episode, kind: (
                True
                if kind == main_module.AssetKind.MUSIC
                else original_has_valid_asset(service, episode, kind)
            ),
        )
        monkeypatch.setattr(
            main_module.PipelineService,
            "content_is_approved",
            lambda _service, _episode, content_kind: (
                content_kind == "storyboard"
            ),
        )

        detail = client.get(episode_url)

        assert detail.status_code == 200
        assert detail.text.count('class="reference-preset-card') == 2
        assert "Reference pack precaricati" in detail.text
        assert "Nanna arcobaleno" in detail.text
        assert "reference-preset-recommended" in detail.text
        nanna_preset = next(
            preset
            for preset in main_module.REFERENCE_PRESETS
            if preset.id == "nanna-arcobaleno-v1"
        )
        assert nanna_preset.image_url("friends") in detail.text
        assert nanna_preset.image_url("world") in detail.text

        preview = client.get(nanna_preset.image_url("world"))
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"
        with Image.open(BytesIO(preview.content)) as image:
            image.load()
            assert (image.mode, image.size) == ("RGB", (1280, 720))

        applied = client.post(
            f"{episode_url}/reference-preset",
            data={
                "emma_look_id": main_module.NEW_EPISODE_DEFAULT_LOOK_ID,
                "reference_preset_id": nanna_preset.id,
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert applied.status_code == 303
        assert applied.headers["location"] == (
            f"{episode_url}#character-reference"
        )
        saved = sorted(
            (tmp_path / "storage" / "uploads").glob(
                "*/reference-*.png"
            )
        )
        assert len(saved) == 3
        for path in saved:
            with Image.open(path) as image:
                image.load()
                assert (image.mode, image.size) == ("RGB", (1280, 720))
        selected_detail = client.get(episode_url)
        assert "reference-preset-selected" in selected_detail.text
        assert ">in uso</span>" in selected_detail.text


def test_truncated_upload_names_the_broken_role_and_leaves_no_partial_pack(
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
        episode_url = _create_episode(client)
        broken_world = _png_bytes("green")[:64]

        rejected = client.post(
            f"{episode_url}/reference",
            files={
                "friends_file": (
                    "friends.png",
                    _png_bytes("red"),
                    "image/png",
                ),
                "world_file": (
                    "world.png",
                    broken_world,
                    "image/png",
                ),
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert rejected.status_code == 303
        error_page = client.get(rejected.headers["location"])
        assert error_page.status_code == 200
        assert (
            "Invalid reference pack: Mondo dell’episodio: "
            "il file è incompleto o danneggiato"
        ) in error_page.text
        assert 'role="alert"' in error_page.text
        assert not list(
            (tmp_path / "storage" / "uploads").glob(
                "*/reference-*.png"
            )
        )


def test_truncated_replacement_preserves_the_existing_complete_pack(
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
        episode_url = _create_episode(client)
        first = client.post(
            f"{episode_url}/reference",
            files={
                "friends_file": (
                    "friends.png",
                    _png_bytes("red"),
                    "image/png",
                ),
                "world_file": (
                    "world.png",
                    _png_bytes("green"),
                    "image/png",
                ),
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert first.status_code == 303
        stored_before = {
            path: path.read_bytes()
            for path in (tmp_path / "storage" / "uploads").glob(
                "*/reference-*.png"
            )
        }
        assert len(stored_before) == 3

        rejected = client.post(
            f"{episode_url}/reference",
            files={
                "friends_file": (
                    "friends-new.png",
                    _png_bytes("blue"),
                    "image/png",
                ),
                "world_file": (
                    "world-broken.png",
                    _png_bytes("yellow")[:64],
                    "image/png",
                ),
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert rejected.status_code == 303
        error_page = client.get(rejected.headers["location"])
        assert "Mondo dell’episodio" in error_page.text
        stored_after = {
            path: path.read_bytes()
            for path in (tmp_path / "storage" / "uploads").glob(
                "*/reference-*.png"
            )
        }
        assert stored_after == stored_before
        detail = client.get(episode_url)
        assert "3/3 completo" in detail.text


def test_upload_rejects_a_mislabelled_or_oversized_image(
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
        episode_url = _create_episode(client)
        gif = BytesIO()
        Image.new("RGB", (64, 64), "red").save(gif, "GIF")

        wrong_format = client.post(
            f"{episode_url}/reference",
            files={
                "friends_file": (
                    "friends.png",
                    gif.getvalue(),
                    "image/png",
                ),
                "world_file": (
                    "world.png",
                    _png_bytes("green"),
                    "image/png",
                ),
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert wrong_format.status_code == 303
        wrong_format_page = client.get(
            wrong_format.headers["location"]
        )
        assert (
            "Invalid reference pack: Amici dell’episodio: "
            "usa un’immagine PNG, JPEG o WebP"
        ) in wrong_format_page.text

        oversized = BytesIO()
        Image.new("RGB", (8193, 1), "blue").save(oversized, "PNG")
        too_large = client.post(
            f"{episode_url}/reference",
            files={
                "friends_file": (
                    "friends.png",
                    oversized.getvalue(),
                    "image/png",
                ),
                "world_file": (
                    "world.png",
                    _png_bytes("green"),
                    "image/png",
                ),
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

        assert too_large.status_code == 303
        too_large_page = client.get(too_large.headers["location"])
        assert (
            "Invalid reference pack: Amici dell’episodio: "
            "le dimensioni dell’immagine sono eccessive"
        ) in too_large_page.text


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
        lyrics_page = client.get(episode_url)
        lyrics_match = re.search(
            r'<textarea[^>]+name="lyrics_text"[^>]*>(.*?)</textarea>',
            lyrics_page.text,
            flags=re.DOTALL,
        )
        assert lyrics_match is not None
        generated_lyrics = html.unescape(lyrics_match.group(1)).strip()
        assert client.post(
            f"{episode_url}/lyrics/approve",
            data={"lyrics_text": generated_lyrics},
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


def test_final_media_is_inline_and_can_be_rebuilt_without_provider_cost(
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
        episode_url = _create_episode(
            client,
            title="Pappì fa confusione",
        )
        produced = client.post(
            f"{episode_url}/run",
            data={"through_step": "qc"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert produced.status_code == 303

        detail = client.get(episode_url)
        assert detail.status_code == 200
        assert "CONCEPT PREVIEW" not in detail.text
        assert "VIDEO PRINCIPALE UNITO" in detail.text
        assert "Scarica video completo" in detail.text
        assert "Ricostruisci video e 4 copertine • $0" in detail.text
        assert "Scegli fra 4 fotogrammi reali" in detail.text
        assert detail.text.count('class="thumbnail-choice') >= 4
        match = re.search(
            r'<video controls playsinline preload="metadata"[^>]*>\s*'
            r'<source src="([^"]+)" type="video/mp4">',
            detail.text,
        )
        assert match is not None
        asset_url = match.group(1)

        inline = client.get(asset_url)
        assert inline.status_code == 200
        assert inline.headers["content-disposition"].startswith("inline;")
        assert inline.headers["accept-ranges"] == "bytes"
        ranged = client.get(
            asset_url,
            headers={"range": "bytes=0-1023"},
        )
        assert ranged.status_code == 206
        assert ranged.headers["content-range"].startswith("bytes 0-1023/")
        download = client.get(f"{asset_url}?download=true")
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith(
            "attachment;"
        )
        assert "content-length" not in download.headers

        main_path = next(
            path
            for path in (tmp_path / "storage" / "renders").rglob("*.mp4")
            if not path.name.endswith("-short.mp4")
        )
        with main_path.open("ab") as media_file:
            media_file.truncate(
                main_module.MEDIA_RANGE_RESPONSE_BYTES
                + main_module.MEDIA_STREAM_CHUNK_BYTES
            )
        open_ended_range = client.get(
            asset_url,
            headers={"range": "bytes=0-"},
        )
        assert open_ended_range.status_code == 206
        assert len(open_ended_range.content) == (
            main_module.MEDIA_RANGE_RESPONSE_BYTES
        )
        assert open_ended_range.headers["content-length"] == str(
            main_module.MEDIA_RANGE_RESPONSE_BYTES
        )
        assert open_ended_range.headers["content-range"].startswith(
            "bytes 0-8388607/"
        )
        choices = re.findall(
            rf'action="({re.escape(episode_url)}/thumbnail/'
            r'[^"]+/select)"',
            detail.text,
        )
        assert len(choices) == 4
        changed = client.post(
            choices[-1],
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert changed.status_code == 303
        selected_page = client.get(episode_url)
        selected_card = re.search(
            rf'<form class="thumbnail-choice '
            rf'thumbnail-choice-selected" action="{re.escape(choices[-1])}"',
            selected_page.text,
        )
        assert selected_card is not None
