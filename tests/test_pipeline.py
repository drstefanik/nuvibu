from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Episode, MetricSnapshot
from app.services.growth import calculate_growth_score
from app.services.pipeline import PipelineService, slugify
from app.services.prompts import generate_storyboard
from app.services.safety import review_text


def make_episode(duration: int = 16) -> Episode:
    return Episode(
        title="Cucù con Nuvibù",
        working_slug="cucu-con-nuvibu",
        age_min_months=6,
        age_max_months=24,
        theme="cucù e sorpresa",
        hook="Due oggetti compaiono lentamente",
        target_words=["stella", "luna"],
        featured_characters=["Nuvibù"],
        duration_seconds=duration,
        bpm=90,
        visual_pacing="gentle",
        language="it",
    )


def test_slugify_handles_accents_and_symbols():
    assert slugify("Cucù! È qui?") == "cucu-e-qui"


def test_storyboard_covers_duration_without_fast_scene():
    episode = make_episode(75)
    scenes = generate_storyboard(episode)
    assert sum(scene["duration_seconds"] for scene in scenes) == 75
    assert min(scene["duration_seconds"] for scene in scenes) >= 4
    assert all("no flashing" in scene["prompt"] for scene in scenes)


def test_safety_blocks_risky_terms():
    assert review_text("una scena con rapid cuts")
    assert not review_text("Nuvibù saluta piano")


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
        assert episode.qc_json["score"] == 100
        assert selected["render"].exists()
        assert selected["short"].exists()
        assert selected["thumbnail"].exists()
        assert selected["report"].exists()
        assert selected["render"].stat().st_size > 10_000
