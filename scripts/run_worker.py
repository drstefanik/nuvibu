from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Job, JobStatus
from app.services.pipeline import PipelineService


def job_claim_query(requested_job_id: str | None):
    query = select(Job).where(Job.status == JobStatus.PENDING)
    if requested_job_id:
        # A Cloud Run execution targets one exact database job. Wait for a
        # short reservation transaction instead of skipping the locked row and
        # wasting the execution.
        return query.where(Job.id == requested_job_id).with_for_update().limit(1)
    return (
        query.order_by(Job.created_at.asc(), Job.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )


def process_once() -> bool:
    requested_job_id = os.getenv("NUVIBU_JOB_ID")
    with SessionLocal() as db:
        with db.begin():
            job = db.scalar(job_claim_query(requested_job_id))
            if job is None:
                if requested_job_id:
                    requested = db.get(Job, requested_job_id)
                    if requested is None:
                        raise RuntimeError(f"Requested job {requested_job_id} does not exist")
                    if requested.status == JobStatus.SUCCEEDED:
                        return False
                    raise RuntimeError(
                        f"Requested job {requested_job_id} is {requested.status.value}, not pending"
                    )
                return False
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            job.attempt += 1
        PipelineService(db, get_settings()).process_job(job, already_claimed=True)
        print(f"Processed job {job.id}: {job.status.value}")
        return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    settings.validate_production(require_dispatch=False)
    if settings.app_env != "production":
        init_db()
    if args.once:
        process_once()
        return
    while True:
        if not process_once():
            time.sleep(2)


if __name__ == "__main__":
    main()
