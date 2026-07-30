from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.media import is_valid_video
from app.models import AssetKind, Episode, MetricSnapshot
from app.schemas import EpisodeCreate
from app.services.growth import calculate_growth_score
from app.services.lyrics_engine import generate_song
from app.services.pipeline import PipelineService, slugify
from app.services.prompts import generate_lyrics, generate_storyboard, lyric_sections
from app.services.safety import review_text


def make_episode(duration: int = 16) -> Episode:
    return Episode(
        title="Cucù con Emma",
        working_slug="cucu-con-emma",
        age_min_months=6,
        age_max_months=24,
        theme="cucù e sorpresa",
        hook="Due oggetti compaiono lentamente",
        target_words=["stella", "luna"],
        featured_characters=["Emma", "Nuvi la nuvola"],
        duration_seconds=duration,
        bpm=90,
        visual_pacing="gentle",
        language="it",
    )


def test_slugify_handles_accents_and_symbols():
    assert slugify("Cucù! È qui?") == "cucu-e-qui"


def test_episode_brief_always_locks_emma_in_first_position():
    payload = EpisodeCreate(
        title="I pulcini colorati",
        theme="colori",
        hook="Sette pulcini scoprono una pozza",
        featured_characters=["Nuvibù", "Pulcini", "Emma"],
    )

    assert payload.featured_characters == ["Emma", "Pulcini"]


def test_storyboard_covers_duration_without_fast_scene():
    episode = make_episode(75)
    episode.lyrics_text = generate_lyrics(episode)
    scenes = generate_storyboard(episode)
    assert sum(scene["duration_seconds"] for scene in scenes) == 75
    assert min(scene["duration_seconds"] for scene in scenes) >= 4
    assert all("no flashing" in scene["prompt"] for scene in scenes)
    assert all(scene["lyric_cue"] for scene in scenes)
    assert all(scene["lyric_cue"] in episode.lyrics_text for scene in scenes)
    assert "Episode story:" in scenes[0]["prompt"]
    assert all("Emma is the recurring main character" in scene["prompt"] for scene in scenes)
    assert all("Nuvibù is the name of the platform" in scene["prompt"] for scene in scenes)


def test_rainbow_chicks_use_the_color_format_and_four_distinct_concepts():
    episode = make_episode(75)
    episode.title = "Pulcini Arcobaleno"
    episode.theme = "colori e trasformazioni"
    episode.hook = "Sette pulcini saltano in pozze magiche e cambiano colore"
    episode.target_words = ["arcobaleno"]
    episode.featured_characters = ["Emma", "Nuvi la nuvola", "Pulcini Arcobaleno"]
    episode.bpm = 112

    generation = generate_song(episode)
    lyrics = generation.lyrics
    sections = lyric_sections(lyrics)

    assert generation.song_format == "colori_e_trasformazioni"
    assert len(generation.candidates) == 4
    assert len({candidate.archetype for candidate in generation.candidates}) == 4
    assert len({candidate.lyrics for candidate in generation.candidates}) == 4
    assert len(sections) == 6
    assert "Pulcini Arcobaleno" in lyrics
    assert "trasforma" in lyrics or "cambiato" in lyrics
    assert "Guarda bene" not in lyrics
    assert "proprio così" not in lyrics
    assert all(lines for _name, lines in sections)


def test_safety_blocks_risky_terms():
    assert review_text("una scena con rapid cuts")
    assert not review_text("Emma saluta piano")


def test_growth_score_is_cautious_with_small_sample():
    metric = MetricSnapshot(
        episode_id="00000000-0000-0000-0000-000000000000",
        views=100,
        average_view_percentage=80,
        impressions=1500,
        impressions_ctr=6,
        subscribers_gained=2,
        relative_retention=0.7,
    )
    result = calculate_growth_score(metric)
    assert result.score > 50
    assert result.confidence == "bassa"
    assert "campione insufficiente" in result.recommendation


def test_complete_mock_pipeline_creates_all_outputs(tmp_path: Path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{db_path}",
        storage_root=tmp_path / "storage",
        provider_mode="mock",
        max_music_variants=2,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        db.refresh(episode)
        PipelineService(db, settings).run_through(episode, "qc")
        db.refresh(episode)
        selected = {asset.kind.value: Path(asset.path) for asset in episode.assets if asset.selected}
        assert episode.qc_json["passed"] is True
        assert episode.qc_json["score"] >= 95
        assert selected["render"].exists()
        assert selected["short"].exists()
        assert selected["thumbnail"].exists()
        assert selected["report"].exists()
        assert selected["render"].stat().st_size > 10_000
        thumbnail = next(
            asset
            for asset in episode.assets
            if asset.kind == AssetKind.THUMBNAIL and asset.selected
        )
        assert thumbnail.provider == "episode-frame-thumbnail-v2"
        assert thumbnail.metadata_json["thumbnail_source"] == (
            "final_render_frame"
        )
        assert thumbnail.metadata_json["preview_label"] is False


def test_render_rebuild_reuses_paid_sources_and_repairs_final_media(
    tmp_path: Path,
):
    db_path = tmp_path / "rebuild.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{db_path}",
        storage_root=tmp_path / "storage",
        provider_mode="mock",
        max_music_variants=1,
    )
    settings.ensure_directories()
    with Session() as db:
        episode = make_episode(16)
        db.add(episode)
        db.commit()
        service = PipelineService(db, settings)
        service.run_through(episode, "qc")
        source_assets_before = {
            asset.id
            for asset in episode.assets
            if asset.kind in {
                AssetKind.MUSIC,
                AssetKind.VIDEO_SCENE,
            }
        }
        cost_before = episode.actual_cost_usd
        original_render = service.selected_valid_asset(
            episode,
            AssetKind.RENDER,
        )
        assert original_render is not None
        Path(original_render.path).write_bytes(b"broken" * 1024)

        repair_job = service.enqueue(
            episode,
            "qc",
            estimated_incremental_cost=0.0,
            rebuild_render=True,
        )
        assert repair_job.payload_json["rebuild_render"] is True
        assert repair_job.payload_json["budget_reserved_usd"] == 0.0
        service.process_job(repair_job)
        db.refresh(episode)

        assert repair_job.status.value == "succeeded"
        assert repair_job.result_json["derived_media_rebuild"] is True
        rebuilt_render = service.selected_valid_asset(
            episode,
            AssetKind.RENDER,
        )
        rebuilt_thumbnail = service.selected_valid_asset(
            episode,
            AssetKind.THUMBNAIL,
        )
        assert rebuilt_render is not None
        assert is_valid_video(Path(rebuilt_render.path))
        assert rebuilt_thumbnail is not None
        assert rebuilt_thumbnail.provider == "episode-frame-thumbnail-v2"
        assert rebuilt_thumbnail.metadata_json["preview_label"] is False
        assert {
            asset.id
            for asset in episode.assets
            if asset.kind in {
                AssetKind.MUSIC,
                AssetKind.VIDEO_SCENE,
            }
        } == source_assets_before
        assert episode.actual_cost_usd == cost_before
        assert episode.qc_json["passed"] is True
