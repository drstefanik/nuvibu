from __future__ import annotations

from urllib.parse import quote

from ..config import Settings


def dispatch_worker_job(settings: Settings, job_id: str) -> str:
    """Start one Cloud Run Job execution and return its operation name."""

    if not settings.google_cloud_project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required to dispatch the production worker")
    if not settings.cloud_run_job_name:
        raise RuntimeError("CLOUD_RUN_JOB_NAME is required to dispatch the production worker")

    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as exc:
        raise RuntimeError("Install google-auth to dispatch the Cloud Run worker") from exc

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = AuthorizedSession(credentials)
    project = quote(settings.google_cloud_project, safe="")
    location = quote(settings.cloud_run_job_location, safe="")
    job_name = quote(settings.cloud_run_job_name, safe="")
    url = (
        f"https://run.googleapis.com/v2/projects/{project}/locations/{location}/"
        f"jobs/{job_name}:run"
    )
    response = session.post(
        url,
        json={
            "overrides": {
                "taskCount": 1,
                "containerOverrides": [
                    {
                        "env": [
                            {"name": "NUVIBU_JOB_ID", "value": job_id},
                        ]
                    }
                ],
            }
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    operation_name = payload.get("name")
    if not operation_name:
        raise RuntimeError(f"Cloud Run did not return an operation name: {payload}")
    return str(operation_name)
