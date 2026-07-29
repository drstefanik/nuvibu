from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Episode
from app.services.pipeline import PipelineService


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the offline technical pilot")
    parser.add_argument("--render", action="store_true", help="Run the complete mock pipeline")
    args = parser.parse_args()
    settings = get_settings()
    if args.render and settings.provider_mode != "mock":
        raise SystemExit("seed_demo --render is intentionally restricted to PROVIDER_MODE=mock")
    init_db()
    with SessionLocal() as db:
        episode = db.scalar(select(Episode).where(Episode.working_slug == "pulcini-arcobaleno-pilota"))
        if episode is None:
            episode = Episode(
                title="Emma e i Pulcini Arcobaleno – Pilota",
                working_slug="pulcini-arcobaleno-pilota",
                age_min_months=9,
                age_max_months=48,
                theme="colori e trasformazioni",
                hook="Emma guida sette pulcini tra pozze magiche che cambiano colore",
                target_words=["rosso", "giallo", "blu", "verde", "rosa", "viola", "bianco"],
                featured_characters=[
                    "Emma",
                    "Nuvi la nuvola",
                    "Pulcini Arcobaleno",
                ],
                duration_seconds=32,
                bpm=112,
                visual_pacing="energetic",
                language="it",
            )
            db.add(episode)
            db.commit()
            db.refresh(episode)
        if args.render:
            PipelineService(db, settings).run_through(episode, "qc")
        print(episode.id)
        for asset in episode.assets:
            if asset.selected:
                print(f"{asset.kind.value}: {asset.path}")
        if episode.qc_json:
            print(f"QC: {episode.qc_json.get('score')}/100 passed={episode.qc_json.get('passed')}")


if __name__ == "__main__":
    main()
