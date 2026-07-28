from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Job, JobStatus
from app.services.pipeline import PipelineService


def process_once() -> bool:
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.status == JobStatus.PENDING).order_by(Job.created_at.asc()).limit(1))
        if job is None:
            return False
        PipelineService(db, get_settings()).process_job(job)
        print(f"Processed job {job.id}: {job.status.value}")
        return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    init_db()
    if args.once:
        process_once()
        return
    while True:
        if not process_once():
            time.sleep(2)


if __name__ == "__main__":
    main()
