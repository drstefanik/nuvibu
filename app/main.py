from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from ipaddress import ip_address
import secrets
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .auth import (
    LoginAttemptLimiter,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    safe_next_path,
    validate_session_token,
)
from .config import get_settings
from .database import get_db, init_db
from .emma_looks import (
    EMMA_LOOKS,
    NEW_EPISODE_DEFAULT_LOOK_ID,
)
from .models import Asset, AssetKind, Episode, EpisodeStatus, Job, JobStatus, MetricSnapshot, PublishRecord
from .providers.youtube import YouTubeClient
from .reference_presets import (
    REFERENCE_PRESET_ROLES,
    REFERENCE_PRESETS,
    ReferencePresetCatalogError,
    get_reference_preset,
    recommend_reference_preset_id,
)
from .schemas import EpisodeCreate, MetricCreate, PipelineRequest
from .services.analytics import sync_youtube_metrics
from .services.growth import calculate_growth_score, latest_metric
from .services.pipeline import (
    ActiveJobError,
    PipelineService,
    REFERENCE_ROLE_LABELS,
    REFERENCE_ROLE_ORDER,
    ReferenceChangeConflictError,
    slugify,
)
from .services.worker_dispatch import dispatch_worker_job

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.app_env == "production":
        settings.validate_production(require_dispatch=True)
    else:
        init_db()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
PUBLIC_PATHS = {"/health", "/healthz", "/readyz", "/login"}
LOGIN_LIMITER = LoginAttemptLimiter(max_failures=5, window_seconds=300)


def valid_console_session(request: Request) -> bool:
    if not settings.admin_username or not settings.admin_password:
        return False
    return validate_session_token(
        request.cookies.get(SESSION_COOKIE_NAME),
        secret_key=settings.secret_key,
        username=settings.admin_username,
        password=settings.admin_password,
    )


def login_client_key(request: Request) -> str:
    """Use the verified client address in Google's appended XFF suffix."""

    if settings.app_env == "production":
        forwarded = [
            value.strip()
            for value in request.headers.get("x-forwarded-for", "").split(",")
            if value.strip()
        ]
        # Google external load balancing appends client IP then forwarding-rule
        # IP. Anything farther left is supplied by the caller and untrusted.
        candidate = forwarded[-2] if len(forwarded) >= 2 else None
        if candidate is not None:
            try:
                return f"xff:{ip_address(candidate).compressed}"
            except ValueError:
                # Never walk farther left into client-supplied XFF values.
                pass
    peer = request.client.host if request.client else "unknown"
    return f"peer:{peer}"


def secure_response(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; media-src 'self'; "
        "style-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'",
    )
    if settings.app_env == "production":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


def canonical_console_url(request: Request) -> str | None:
    if settings.app_env != "production" or request.method not in {"GET", "HEAD"}:
        return None
    if request.url.path in {"/health", "/healthz", "/readyz"}:
        return None
    canonical = urlsplit(settings.app_base_url)
    if canonical.hostname == "placeholder.invalid":
        return None
    if not canonical.netloc or request.url.netloc == canonical.netloc:
        return None
    target = f"{settings.app_base_url.rstrip('/')}{request.url.path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return target


def allowed_browser_origins(request: Request) -> set[str]:
    """Return browser-visible origins accepted for unsafe requests.

    Cloud Run terminates TLS before forwarding the request to Uvicorn, so the
    ASGI request can report ``http`` even though the browser used ``https``.
    Keep the comparison host-bound while accepting that production proxy
    boundary.
    """

    allowed = {
        str(request.base_url).rstrip("/"),
        settings.app_base_url.rstrip("/"),
    }
    if settings.app_env == "production" and request.url.netloc:
        allowed.add(f"https://{request.url.netloc}")
    return allowed


@app.middleware("http")
async def console_security(request: Request, call_next):
    canonical_url = canonical_console_url(request)
    if canonical_url:
        return secure_response(RedirectResponse(canonical_url, status_code=308))

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site == "cross-site":
            return secure_response(
                JSONResponse(
                    {"detail": "Cross-site request rejected"},
                    status_code=403,
                )
            )
        browser_proves_same_origin = fetch_site == "same-origin"
        origin = request.headers.get("origin")
        allowed_origins = allowed_browser_origins(request)
        if (
            origin
            and not browser_proves_same_origin
            and origin.rstrip("/") not in allowed_origins
        ):
            return secure_response(
                JSONResponse({"detail": "Origin rejected"}, status_code=403)
            )
        referer = request.headers.get("referer")
        if not browser_proves_same_origin and not origin and referer:
            parsed = urlsplit(referer)
            referer_origin = f"{parsed.scheme}://{parsed.netloc}"
            if referer_origin.rstrip("/") not in allowed_origins:
                return secure_response(
                    JSONResponse({"detail": "Referer rejected"}, status_code=403)
                )
        content_type = request.headers.get("content-type", "").lower()
        is_browser_form = content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        )
        if (
            is_browser_form
            and not browser_proves_same_origin
            and not origin
            and not referer
        ):
            return secure_response(
                JSONResponse(
                    {"detail": "Same-origin form proof required"},
                    status_code=403,
                )
            )
    is_public = (
        request.url.path in PUBLIC_PATHS
        or request.url.path.startswith("/static/")
    )
    if (
        settings.admin_username
        and settings.admin_password
        and not is_public
        and not valid_console_session(request)
    ):
        if request.method in {"GET", "HEAD"} and "text/html" in request.headers.get(
            "accept", ""
        ).lower():
            next_path = request.url.path
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            return secure_response(
                RedirectResponse(
                    f"/login?next={quote(next_path, safe='')}",
                    status_code=303,
                )
            )
        else:
            return secure_response(
                JSONResponse(
                    {"detail": "Authentication required"},
                    status_code=401,
                )
            )
    response = await call_next(request)
    if not is_public:
        response.headers.setdefault("Cache-Control", "no-store")
    return secure_response(response)


def get_episode_or_404(db: Session, episode_id: str) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


def grouped_assets(episode: Episode) -> dict[str, list[Asset]]:
    grouped: dict[str, list[Asset]] = {}
    for asset in episode.assets:
        grouped.setdefault(asset.kind.value, []).append(asset)
    return grouped


@app.get("/health")
@app.get("/healthz")
def health() -> dict:
    return {
        "status": "ok",
        "provider_mode": settings.provider_mode,
        "app": settings.app_name,
        "reference_presets": [
            preset.id for preset in REFERENCE_PRESETS
        ],
    }


@app.get("/readyz")
def readiness(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    if settings.app_env == "production":
        errors = settings.production_errors(require_dispatch=True)
        if errors:
            raise HTTPException(status_code=503, detail="; ".join(errors))
        if db.scalar(text("SELECT to_regclass('public.episodes')")) is None:
            raise HTTPException(status_code=503, detail="Database migration is missing")
    elif not settings.storage_root.exists() or not settings.storage_root.is_dir():
        raise HTTPException(status_code=503, detail="Storage is unavailable")
    return {"status": "ready", "database": "ok", "storage": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    destination = safe_next_path(next)
    if valid_console_session(request):
        return RedirectResponse(destination, status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "next_path": destination,
            "settings": settings,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    destination = safe_next_path(next)
    client_key = login_client_key(request)
    if not LOGIN_LIMITER.check(client_key):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": (
                    "Troppi tentativi non riusciti. "
                    "Attendi cinque minuti prima di riprovare."
                ),
                "next_path": destination,
                "settings": settings,
            },
            status_code=429,
            headers={
                "Cache-Control": "no-store",
                "Retry-After": "300",
            },
        )
    valid = bool(
        settings.admin_username
        and settings.admin_password
        and secrets.compare_digest(username, settings.admin_username)
        and secrets.compare_digest(password, settings.admin_password)
    )
    if valid:
        LOGIN_LIMITER.reset(client_key)
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            create_session_token(
                secret_key=settings.secret_key,
                username=settings.admin_username,
                password=settings.admin_password,
            ),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=settings.app_env == "production",
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    another_attempt_allowed = LOGIN_LIMITER.record_failure(client_key)
    if not another_attempt_allowed:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": (
                    "Troppi tentativi non riusciti. "
                    "Attendi cinque minuti prima di riprovare."
                ),
                "next_path": destination,
                "settings": settings,
            },
            status_code=429,
            headers={
                "Cache-Control": "no-store",
                "Retry-After": "300",
            },
        )
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "Credenziali non valide. Riprova.",
            "next_path": destination,
            "settings": settings,
        },
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=settings.app_env == "production",
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def worker_dispatch_due(
    job: Job,
    *,
    now: datetime | None = None,
    retry_after_seconds: int | None = None,
) -> bool:
    if job.status != JobStatus.PENDING:
        return False
    dispatched_at_raw = job.result_json.get("cloud_run_dispatched_at")
    if not isinstance(dispatched_at_raw, str):
        return True
    try:
        dispatched_at = datetime.fromisoformat(dispatched_at_raw)
    except ValueError:
        return True
    if dispatched_at.tzinfo is None:
        dispatched_at = dispatched_at.replace(tzinfo=timezone.utc)
    current_time = now or datetime.now(timezone.utc)
    retry_after = (
        retry_after_seconds
        if retry_after_seconds is not None
        else settings.cloud_run_dispatch_retry_seconds
    )
    return (
        current_time - dispatched_at
    ).total_seconds() >= retry_after


def validate_production_stage(
    service: PipelineService,
    episode: Episode,
    through_step: str,
    *,
    confirm_cost: bool,
    replace_existing_music: bool = False,
) -> float:
    active_job = service.active_job(episode)
    if active_job is not None:
        active_step = str(
            active_job.payload_json.get("through_step", "qc")
        )
        if active_step != through_step:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Worker job {active_job.id} is already "
                    f"{active_job.status.value} for phase {active_step}"
                ),
            )
        retryable = (
            active_job.status == JobStatus.PENDING
            and worker_dispatch_due(active_job)
        ) or service.job_is_stale(active_job)
        if not retryable:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Worker job {active_job.id} is already "
                    f"{active_job.status.value} for phase {active_step}; "
                    "it is not ready to be retried"
                ),
            )
        if through_step in {"music", "scenes", "render", "qc"} and not confirm_cost:
            raise HTTPException(
                status_code=400,
                detail="Explicit cost confirmation is required to resume paid work",
            )
        return service.estimate_job_incremental_cost(
            episode,
            through_step,
        )

    has_lyrics = bool(
        episode.lyrics_text
        and service.has_valid_asset(episode, AssetKind.LYRICS)
    )
    has_storyboard = bool(
        episode.storyboard_json
        and service.has_valid_asset(episode, AssetKind.STORYBOARD)
    )
    has_music = service.has_valid_asset(episode, AssetKind.MUSIC)
    has_reference = service.reference_pack_complete(episode)
    has_qc = service.has_valid_asset(episode, AssetKind.REPORT)

    if through_step == "lyrics":
        if has_lyrics:
            raise HTTPException(
                status_code=409,
                detail="Lyrics already exist; review, edit and approve them",
            )
        return 0.0

    if through_step == "storyboard":
        if not has_lyrics or not service.content_is_approved(
            episode,
            "lyrics",
        ):
            raise HTTPException(
                status_code=409,
                detail="Approve the current lyrics before creating the storyboard",
            )
        if has_storyboard:
            raise HTTPException(
                status_code=409,
                detail="Storyboard already exists; review and approve it",
            )
        return 0.0

    if through_step == "music":
        if not has_storyboard or not service.content_is_approved(
            episode,
            "storyboard",
        ):
            raise HTTPException(
                status_code=409,
                detail="Approve the current storyboard before generating music",
            )
        if has_music:
            if not replace_existing_music:
                raise HTTPException(
                    status_code=409,
                    detail="Music already exists",
                )
            try:
                service.validate_music_regeneration(episode)
            except ReferenceChangeConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not confirm_cost:
            raise HTTPException(
                status_code=400,
                detail="Explicit music cost confirmation is required",
            )
        incremental_cost = (
            service.estimate_music_regeneration_cost(episode)
            if replace_existing_music
            else service.estimate_music_cost(episode)
        )
    elif through_step == "qc":
        if not has_storyboard or not service.content_is_approved(
            episode,
            "storyboard",
        ):
            raise HTTPException(
                status_code=409,
                detail="Approve the current storyboard before rendering",
            )
        if not has_music:
            raise HTTPException(
                status_code=409,
                detail="Generate and review the music before rendering",
            )
        if not has_reference:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Upload the complete approved reference pack "
                    "(official Emma, episode friends and world) before rendering"
                ),
            )
        if has_qc:
            raise HTTPException(
                status_code=409,
                detail="QC already exists for this episode",
            )
        if not confirm_cost:
            raise HTTPException(
                status_code=400,
                detail="Explicit render cost confirmation is required",
            )
        incremental_cost = service.estimate_remaining_cost(episode)
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Production accepts only the controlled phases: "
                "lyrics, storyboard, music and qc"
            ),
        )

    try:
        service.assert_budget(
            episode,
            additional_cost=(
                incremental_cost
                if replace_existing_music and has_music
                else 0.0
            ),
        )
        service.assert_daily_budget(incremental_cost)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return incremental_cost


def validate_render_rebuild_stage(
    service: PipelineService,
    episode: Episode,
) -> float:
    active_job = service.active_job(episode)
    if active_job is not None:
        if not (active_job.payload_json or {}).get("rebuild_render"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Worker job {active_job.id} is already "
                    f"{active_job.status.value} for another production phase"
                ),
            )
        retryable = (
            active_job.status == JobStatus.PENDING
            and worker_dispatch_due(active_job)
        ) or service.job_is_stale(active_job)
        if not retryable:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Render rebuild job {active_job.id} is already "
                    f"{active_job.status.value}; it is not ready to be retried"
                ),
            )
        return 0.0
    try:
        service.validate_render_rebuild(episode)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return 0.0


def enqueue_and_dispatch(
    db: Session,
    episode: Episode,
    through_step: str,
    *,
    confirm_cost: bool = False,
    replace_existing_music: bool = False,
    rebuild_render: bool = False,
) -> Job:
    service = PipelineService(db, settings)
    estimated_incremental_cost: float | None = None
    if settings.app_env == "production":
        estimated_incremental_cost = (
            validate_render_rebuild_stage(service, episode)
            if rebuild_render
            else validate_production_stage(
                service,
                episode,
                through_step,
                confirm_cost=confirm_cost,
                replace_existing_music=replace_existing_music,
            )
        )
        db.commit()
    try:
        job = service.enqueue(
            episode,
            through_step,
            estimated_incremental_cost=estimated_incremental_cost,
            replace_existing_music=replace_existing_music,
            rebuild_render=rebuild_render,
        )
    except ActiveJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if (
        settings.app_env == "production"
        and worker_dispatch_due(job)
    ):
        dispatched_at = datetime.now(timezone.utc).isoformat()
        job.result_json = {
            **job.result_json,
            # Persist before the external call. If its outcome is ambiguous,
            # a retry waits before starting another Cloud Run execution.
            "cloud_run_dispatched_at": dispatched_at,
        }
        db.commit()
        try:
            operation_name = dispatch_worker_job(settings, job.id)
        except Exception as exc:
            job.error_text = f"Worker dispatch failed: {exc}"
            db.commit()
            raise HTTPException(
                status_code=503,
                detail=f"Job queued but the worker could not be started: {exc}",
            ) from exc
        history_value = job.result_json.get("cloud_run_operation_history", [])
        prior_operations = list(history_value) if isinstance(history_value, list) else []
        prior_operation = job.result_json.get("cloud_run_operation")
        if prior_operation and prior_operation not in prior_operations:
            prior_operations.append(prior_operation)
        job.result_json = {
            **job.result_json,
            "cloud_run_operation": operation_name,
            "cloud_run_operation_history": prior_operations,
            "cloud_run_dispatched_at": dispatched_at,
        }
        job.error_text = None
        db.commit()
    return job


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    episodes = db.scalars(select(Episode).order_by(Episode.created_at.desc())).all()
    stats = {
        "episodes": len(episodes),
        "ready": sum(e.status in {EpisodeStatus.QC_REVIEW, EpisodeStatus.APPROVED, EpisodeStatus.PUBLISHED} for e in episodes),
        "published": sum(e.status == EpisodeStatus.PUBLISHED for e in episodes),
        "views": db.scalar(select(func.coalesce(func.sum(MetricSnapshot.views), 0))) or 0,
    }
    return templates.TemplateResponse(request, "dashboard.html", {"episodes": episodes, "stats": stats, "settings": settings})


@app.get("/episodes/new", response_class=HTMLResponse)
def new_episode(request: Request):
    return templates.TemplateResponse(request, "episode_form.html", {"settings": settings})


@app.post("/episodes")
def create_episode_form(
    title: str = Form(...), theme: str = Form(...), hook: str = Form(...),
    target_words: str = Form(""),
    featured_characters: str = Form("Emma, Nuvi la nuvola"),
    age_min_months: int = Form(6), age_max_months: int = Form(24),
    duration_seconds: int = Form(24), bpm: int = Form(92),
    visual_pacing: str = Form("gentle"), language: str = Form("it"),
    db: Session = Depends(get_db),
):
    if duration_seconds > settings.max_episode_seconds:
        raise HTTPException(
            status_code=400,
            detail=f"Duration exceeds MAX_EPISODE_SECONDS={settings.max_episode_seconds}",
        )
    payload = EpisodeCreate(
        title=title, theme=theme, hook=hook,
        target_words=[x.strip() for x in target_words.split(",") if x.strip()],
        featured_characters=[x.strip() for x in featured_characters.split(",") if x.strip()],
        age_min_months=age_min_months, age_max_months=age_max_months,
        duration_seconds=duration_seconds, bpm=bpm, visual_pacing=visual_pacing, language=language,
    )
    base_slug = slugify(payload.title)
    candidate = base_slug
    number = 2
    while db.scalar(select(Episode.id).where(Episode.working_slug == candidate)):
        candidate = f"{base_slug}-{number}"
        number += 1
    episode = Episode(
        working_slug=candidate,
        concept_json={
            "emma_look_id": NEW_EPISODE_DEFAULT_LOOK_ID,
        },
        **payload.model_dump(),
    )
    db.add(episode)
    db.commit()
    db.refresh(episode)
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/api/episodes", status_code=201)
def create_episode_api(payload: EpisodeCreate, db: Session = Depends(get_db)):
    if payload.duration_seconds > settings.max_episode_seconds:
        raise HTTPException(
            status_code=400,
            detail=f"Duration exceeds MAX_EPISODE_SECONDS={settings.max_episode_seconds}",
        )
    base_slug = slugify(payload.title)
    candidate = base_slug
    number = 2
    while db.scalar(select(Episode.id).where(Episode.working_slug == candidate)):
        candidate = f"{base_slug}-{number}"
        number += 1
    episode = Episode(
        working_slug=candidate,
        concept_json={
            "emma_look_id": NEW_EPISODE_DEFAULT_LOOK_ID,
        },
        **payload.model_dump(),
    )
    db.add(episode)
    db.commit()
    db.refresh(episode)
    return {"id": episode.id, "status": episode.status.value, "url": f"/episodes/{episode.id}"}


@app.get("/episodes/{episode_id}", response_class=HTMLResponse)
def episode_detail(
    request: Request,
    episode_id: str,
    reference_error: str | None = None,
    db: Session = Depends(get_db),
):
    episode = get_episode_or_404(db, episode_id)
    service = PipelineService(db, settings)
    estimated_cost = service.estimate_cost(episode)
    active_job = service.active_job(episode)
    active_job_retryable = bool(
        active_job
        and (
            (
                active_job.status == JobStatus.PENDING
                and worker_dispatch_due(active_job)
            )
            or service.job_is_stale(active_job)
        )
    )
    jobs = db.scalars(
        select(Job)
        .where(Job.episode_id == episode.id)
        .order_by(Job.created_at.desc())
        .limit(5)
    ).all()
    selected_music = service.selected_valid_asset(
        episode,
        AssetKind.MUSIC,
    )
    main_render = service.selected_display_asset(
        episode,
        AssetKind.RENDER,
    )
    short_render = service.selected_display_asset(
        episode,
        AssetKind.SHORT,
    )
    thumbnail = service.selected_display_asset(
        episode,
        AssetKind.THUMBNAIL,
    )
    has_derived_media_rows = any(
        asset.kind
        in {
            AssetKind.RENDER,
            AssetKind.SHORT,
            AssetKind.THUMBNAIL,
            AssetKind.REPORT,
        }
        for asset in episode.assets
    )
    thumbnail_is_episode_frame = bool(
        thumbnail
        and (thumbnail.metadata_json or {}).get("thumbnail_source")
        == "final_render_frame"
        and not (thumbnail.metadata_json or {}).get("preview_label")
    )
    render_rebuild_recommended = has_derived_media_rows and (
        main_render is None
        or short_render is None
        or thumbnail is None
        or not thumbnail_is_episode_frame
    )
    reference_assets = {
        service.explicit_reference_role(asset): asset
        for asset in service.reference_pack_assets(episode)
    }
    reference_preset_ids = [
        (reference_assets[role].metadata_json or {}).get(
            "reference_preset_id"
        )
        for role in ("friends", "world")
        if role in reference_assets
    ]
    selected_reference_preset_id = (
        str(reference_preset_ids[0])
        if len(reference_preset_ids) == 2
        and reference_preset_ids[0] is not None
        and reference_preset_ids[0] == reference_preset_ids[1]
        else None
    )
    legacy_reference = service.legacy_reference_asset(episode)
    selected_emma_look_id = service.selected_emma_look_id(episode)
    selected_emma_look = service.selected_emma_look(episode)
    recommended_reference_preset_id = recommend_reference_preset_id(
        (
            episode.title,
            episode.theme,
            episode.hook,
            episode.target_words,
            episode.featured_characters,
            episode.lyrics_text,
            episode.storyboard_json,
        )
    )
    return templates.TemplateResponse(
        request, "episode_detail.html",
        {
            "episode": episode,
            "main_render": main_render,
            "short_render": short_render,
            "thumbnail": thumbnail,
            "render_rebuild_available": (
                service.render_rebuild_available(episode)
            ),
            "render_rebuild_recommended": (
                render_rebuild_recommended
            ),
            "jobs": jobs,
            "estimated_cost": estimated_cost,
            "music_estimated_cost": service.estimate_music_cost(episode),
            "music_regeneration_cost": (
                service.estimate_music_regeneration_cost(episode)
            ),
            "remaining_estimated_cost": (
                service.estimate_remaining_cost_for_display(episode)
            ),
            "active_job": active_job,
            "active_job_retryable": active_job_retryable,
            "has_lyrics": bool(
                episode.lyrics_text
                and service.has_valid_asset(episode, AssetKind.LYRICS)
            ),
            "lyrics_approved": service.content_is_approved(
                episode,
                "lyrics",
            ),
            "has_storyboard": bool(
                episode.storyboard_json
                and service.has_valid_asset(episode, AssetKind.STORYBOARD)
            ),
            "storyboard_approved": service.content_is_approved(
                episode,
                "storyboard",
            ),
            "has_music": service.has_valid_asset(episode, AssetKind.MUSIC),
            "selected_music": selected_music,
            "can_regenerate_music": service.can_regenerate_music(episode),
            "has_reference": service.reference_pack_complete(episode),
            "reference_pack_mutable": service.reference_pack_mutable(episode),
            "reference_assets": reference_assets,
            "legacy_reference": legacy_reference,
            "reference_role_order": REFERENCE_ROLE_ORDER,
            "reference_role_labels": REFERENCE_ROLE_LABELS,
            "emma_looks": EMMA_LOOKS,
            "selected_emma_look_id": selected_emma_look_id,
            "selected_emma_look": selected_emma_look,
            "reference_presets": REFERENCE_PRESETS,
            "selected_reference_preset_id": (
                selected_reference_preset_id
            ),
            "reference_error": (
                reference_error[:400] if reference_error else None
            ),
            "recommended_reference_preset_id": (
                recommended_reference_preset_id
            ),
            "has_qc": service.has_valid_asset(
                episode,
                AssetKind.REPORT,
            ),
            "settings": settings,
            "growth": calculate_growth_score(latest_metric(episode)),
        },
    )


@app.post("/episodes/{episode_id}/run")
def run_pipeline_form(
    episode_id: str,
    through_step: str = Form("qc"),
    queued: bool = Form(False),
    confirm_cost: bool = Form(False),
    db: Session = Depends(get_db),
):
    payload = PipelineRequest(
        through_step=through_step,
        confirm_cost=confirm_cost,
    )
    episode = get_episode_or_404(db, episode_id)
    if queued or settings.app_env == "production":
        enqueue_and_dispatch(
            db,
            episode,
            payload.through_step,
            confirm_cost=payload.confirm_cost,
        )
    else:
        try:
            PipelineService(db, settings).run_through(episode, payload.through_step)
        except Exception as exc:
            episode.status = EpisodeStatus.FAILED
            db.commit()
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/episodes/{episode_id}/music/regenerate")
def regenerate_music_form(
    episode_id: str,
    confirm_cost: bool = Form(False),
    db: Session = Depends(get_db),
):
    if not confirm_cost:
        raise HTTPException(
            status_code=400,
            detail="Explicit music regeneration cost confirmation is required",
        )
    episode = get_episode_or_404(db, episode_id)
    service = PipelineService(db, settings)
    try:
        if settings.app_env == "production":
            enqueue_and_dispatch(
                db,
                episode,
                "music",
                confirm_cost=True,
                replace_existing_music=True,
            )
        else:
            service.prepare_music_regeneration(episode)
            service.run_through(episode, "music")
    except ActiveJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReferenceChangeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/episodes/{episode_id}/render/rebuild")
def rebuild_render_form(
    episode_id: str,
    db: Session = Depends(get_db),
):
    episode = get_episode_or_404(db, episode_id)
    if settings.app_env == "production":
        enqueue_and_dispatch(
            db,
            episode,
            "qc",
            rebuild_render=True,
        )
    else:
        PipelineService(db, settings).rebuild_render_and_qc(episode)
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/api/episodes/{episode_id}/run")
def run_pipeline_api(episode_id: str, payload: PipelineRequest, db: Session = Depends(get_db)):
    episode = get_episode_or_404(db, episode_id)
    if settings.app_env == "production":
        job = enqueue_and_dispatch(
            db,
            episode,
            payload.through_step,
            confirm_cost=payload.confirm_cost,
        )
        return JSONResponse(
            {
                "id": episode.id,
                "job_id": job.id,
                "job_status": job.status.value,
                "queued": True,
            },
            status_code=202,
        )
    PipelineService(db, settings).run_through(episode, payload.through_step)
    return {"id": episode.id, "status": episode.status.value, "qc": episode.qc_json}


@app.post("/episodes/{episode_id}/lyrics")
def update_lyrics(
    episode_id: str,
    lyrics_text: str = Form(...),
    db: Session = Depends(get_db),
):
    episode = get_episode_or_404(db, episode_id)
    cleaned = lyrics_text.strip()
    if not 20 <= len(cleaned) <= 4000:
        raise HTTPException(
            status_code=400,
            detail="Lyrics must contain between 20 and 4000 characters",
        )
    try:
        PipelineService(db, settings).update_lyrics_draft(episode, cleaned)
    except (ActiveJobError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/episodes/{episode.id}#lyrics-review",
        status_code=303,
    )


@app.post("/episodes/{episode_id}/lyrics/approve")
def approve_lyrics(
    episode_id: str,
    lyrics_text: str = Form(...),
    db: Session = Depends(get_db),
):
    episode = get_episode_or_404(db, episode_id)
    cleaned = lyrics_text.strip()
    if not 20 <= len(cleaned) <= 4000:
        raise HTTPException(
            status_code=400,
            detail="Lyrics must contain between 20 and 4000 characters",
        )
    try:
        service = PipelineService(db, settings)
        service.update_lyrics_draft(episode, cleaned)
        service.approve_content(episode, "lyrics")
    except (ActiveJobError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/episodes/{episode.id}#storyboard-review",
        status_code=303,
    )


@app.post("/episodes/{episode_id}/storyboard/approve")
def approve_storyboard(
    episode_id: str,
    db: Session = Depends(get_db),
):
    episode = get_episode_or_404(db, episode_id)
    try:
        PipelineService(db, settings).approve_content(
            episode,
            "storyboard",
        )
    except (ActiveJobError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/episodes/{episode.id}",
        status_code=303,
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "id": job.id,
        "episode_id": job.episode_id,
        "status": job.status.value,
        "attempt": job.attempt,
        "error": job.error_text,
        "result": job.result_json,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


@app.post("/episodes/{episode_id}/emma-look")
def select_emma_look(
    episode_id: str,
    emma_look_id: str = Form(...),
    db: Session = Depends(get_db),
):
    episode = get_episode_or_404(db, episode_id)
    if emma_look_id not in {look.id for look in EMMA_LOOKS}:
        raise HTTPException(
            status_code=400,
            detail="Invalid Emma look",
        )
    try:
        PipelineService(db, settings).set_emma_look(
            episode,
            emma_look_id,
        )
    except ReferenceChangeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/episodes/{episode.id}#character-reference",
        status_code=303,
    )


def reference_form_redirect(
    episode_id: str,
    *,
    error: str | None = None,
) -> RedirectResponse:
    target = f"/episodes/{episode_id}"
    if error:
        target = f"{target}?reference_error={quote(error, safe='')}"
    return RedirectResponse(
        f"{target}#character-reference",
        status_code=303,
    )


@app.get("/reference-presets/{preset_id}/{role}")
def reference_preset_image(preset_id: str, role: str):
    try:
        preset = get_reference_preset(preset_id)
        if role not in REFERENCE_PRESET_ROLES:
            raise ValueError(f"Unknown reference preset role: {role!r}")
        path = preset.validated_path_for(role)
    except (ValueError, ReferencePresetCatalogError) as exc:
        raise HTTPException(
            status_code=404,
            detail="Reference preset image not found",
        ) from exc
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.post("/episodes/{episode_id}/reference-preset")
def use_reference_preset(
    episode_id: str,
    reference_preset_id: str = Form(...),
    emma_look_id: str = Form(""),
    db: Session = Depends(get_db),
):
    episode = get_episode_or_404(db, episode_id)
    service = PipelineService(db, settings)
    try:
        preset = get_reference_preset(reference_preset_id)
    except ValueError:
        return reference_form_redirect(
            episode.id,
            error="Il reference pack selezionato non è valido.",
        )
    selected_emma_look_id = (
        emma_look_id or service.selected_emma_look_id(episode)
    )
    if selected_emma_look_id not in {look.id for look in EMMA_LOOKS}:
        return reference_form_redirect(
            episode.id,
            error="Il look di Emma selezionato non è valido.",
        )
    try:
        sources = {
            "emma": service.selected_emma_look(episode).reference_path,
            **preset.sources,
        }
        service.save_reference_pack(
            episode,
            sources,
            emma_look_id=selected_emma_look_id,
            source_metadata={
                "friends": {
                    "reference_preset_id": preset.id,
                    "source_sha256": preset.friends_sha256,
                },
                "world": {
                    "reference_preset_id": preset.id,
                    "source_sha256": preset.world_sha256,
                },
            },
        )
    except ReferencePresetCatalogError:
        return reference_form_redirect(
            episode.id,
            error=(
                "Il reference pack precaricato non è disponibile. "
                "Ridistribuisci la versione verificata."
            ),
        )
    except ReferenceChangeConflictError as exc:
        return reference_form_redirect(
            episode.id,
            error=str(exc),
        )
    except (OSError, ValueError) as exc:
        return reference_form_redirect(
            episode.id,
            error=f"Reference pack non valido: {exc}",
        )
    return reference_form_redirect(
        episode.id,
    )


@app.post("/episodes/{episode_id}/reference")
def upload_reference_pack(
    episode_id: str,
    emma_look_id: str = Form(""),
    existing_role: str = Form(""),
    friends_file: UploadFile | None = File(None),
    world_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    episode = get_episode_or_404(db, episode_id)
    service = PipelineService(db, settings)
    uploads = {
        "friends": friends_file,
        "world": world_file,
    }
    allowed_types = {"image/png", "image/jpeg", "image/webp"}
    temporary_paths: dict[str, Path] = {}
    try:
        selected_emma_look_id = (
            emma_look_id or service.selected_emma_look_id(episode)
        )
        if selected_emma_look_id not in {
            look.id for look in EMMA_LOOKS
        }:
            raise HTTPException(
                status_code=400,
                detail="Invalid Emma look",
            )
        sources = {
            service.explicit_reference_role(asset): Path(asset.path)
            for asset in service.reference_pack_assets(episode)
        }
        # Emma always comes from the bundled allowlist; the service replaces
        # this placeholder with the exact selected catalog bytes.
        sources["emma"] = service.selected_emma_look(
            episode,
        ).reference_path
        legacy_reference = service.legacy_reference_asset(episode)
        if legacy_reference is not None:
            if existing_role not in {"friends", "world"}:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Assign the previously uploaded reference to "
                        "the episode friends or world"
                    ),
                )
            sources[existing_role] = Path(legacy_reference.path)

        for role, upload in uploads.items():
            if upload is None or not upload.filename:
                continue
            if upload.content_type not in allowed_types:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{REFERENCE_ROLE_LABELS[role]}: "
                        "upload a PNG, JPEG or WebP image"
                    ),
                )
            suffix = Path(upload.filename or "reference.png").suffix or ".png"
            with tempfile.NamedTemporaryFile(
                suffix=suffix,
                delete=False,
            ) as temporary:
                temporary_paths[role] = Path(temporary.name)
                total = 0
                while chunk := upload.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 20 * 1024 * 1024:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"{REFERENCE_ROLE_LABELS[role]} exceeds "
                                "the 20 MB Veo input limit"
                            ),
                        )
                    temporary.write(chunk)
            sources[role] = temporary_paths[role]

        missing_roles = [
            role
            for role in REFERENCE_ROLE_ORDER
            if role not in sources
        ]
        if missing_roles:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reference pack incomplete: missing "
                    + ", ".join(
                        REFERENCE_ROLE_LABELS[role]
                        for role in missing_roles
                    )
                ),
            )
        try:
            service.save_reference_pack(
                episode,
                sources,
                emma_look_id=selected_emma_look_id,
            )
        except ReferenceChangeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid reference pack: {exc}",
            ) from exc
    except HTTPException as exc:
        return reference_form_redirect(
            episode.id,
            error=str(exc.detail),
        )
    finally:
        for temporary_path in temporary_paths.values():
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return reference_form_redirect(episode.id)


@app.post("/episodes/{episode_id}/approve")
def approve_episode(episode_id: str, db: Session = Depends(get_db)):
    episode = get_episode_or_404(db, episode_id)
    service = PipelineService(db, settings)
    if settings.app_env == "production" and (
        not service.content_is_approved(episode, "lyrics")
        or not service.content_is_approved(episode, "storyboard")
    ):
        raise HTTPException(
            status_code=409,
            detail="Lyrics and storyboard approvals are required",
        )
    if not episode.qc_json.get("passed"):
        raise HTTPException(status_code=400, detail="Automatic QC must pass before approval")
    final_assets = {
        kind: service.selected_valid_asset(episode, kind)
        for kind in (
            AssetKind.RENDER,
            AssetKind.SHORT,
            AssetKind.THUMBNAIL,
        )
    }
    if any(asset is None for asset in final_assets.values()):
        raise HTTPException(
            status_code=409,
            detail=(
                "Main video, Short and thumbnail must all pass media "
                "integrity validation before approval"
            ),
        )
    thumbnail = final_assets[AssetKind.THUMBNAIL]
    if (
        (thumbnail.metadata_json or {}).get("thumbnail_source")
        != "final_render_frame"
        or (thumbnail.metadata_json or {}).get("preview_label")
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Rebuild the final package before approval: concept previews "
                "cannot be used as production thumbnails"
            ),
        )
    episode.status = EpisodeStatus.APPROVED
    db.commit()
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/episodes/{episode_id}/youtube")
def upload_to_youtube(episode_id: str, db: Session = Depends(get_db)):
    if not settings.youtube_enabled:
        raise HTTPException(status_code=503, detail="YouTube integration is not enabled")
    episode = get_episode_or_404(db, episode_id)
    if episode.status != EpisodeStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Approve the episode before upload")
    render = next((a for a in episode.assets if a.kind == AssetKind.RENDER and a.selected and Path(a.path).exists()), None)
    thumbnail = next((a for a in episode.assets if a.kind == AssetKind.THUMBNAIL and a.selected and Path(a.path).exists()), None)
    if render is None:
        raise HTTPException(status_code=400, detail="Main render missing")
    client = YouTubeClient(
        client_secrets_file=settings.youtube_client_secrets_file,
        token_file=settings.youtube_token_file,
        category_id=settings.youtube_category_id,
    )
    try:
        response = client.upload_video(
            video_path=Path(render.path), thumbnail_path=Path(thumbnail.path) if thumbnail else None,
            title=episode.publish_title or episode.title,
            description=episode.publish_description or "",
            tags=episode.publish_tags,
            privacy_status="private", made_for_kids=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    record = PublishRecord(
        episode_id=episode.id, platform="youtube", external_id=response.get("id"),
        privacy_status="private", made_for_kids=True, response_json=response,
    )
    db.add(record)
    episode.status = EpisodeStatus.SCHEDULED
    db.commit()
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.get("/assets/{asset_id}")
def serve_asset(
    asset_id: str,
    download: bool = False,
    db: Session = Depends(get_db),
):
    asset = db.get(Asset, asset_id)
    if asset is None or not Path(asset.path).exists():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(
        asset.path,
        media_type=asset.mime_type,
        filename=Path(asset.path).name,
        content_disposition_type=(
            "attachment" if download else "inline"
        ),
    )


@app.post("/api/episodes/{episode_id}/metrics")
def add_metrics(episode_id: str, payload: MetricCreate, db: Session = Depends(get_db)):
    get_episode_or_404(db, episode_id)
    metric = MetricSnapshot(
        episode_id=episode_id,
        views=payload.views, watch_minutes=payload.watch_minutes,
        average_view_duration_seconds=payload.average_view_duration_seconds,
        average_view_percentage=payload.average_view_percentage,
        impressions=payload.impressions, impressions_ctr=payload.impressions_ctr,
        subscribers_gained=payload.subscribers_gained, relative_retention=payload.relative_retention,
        retention_curve_json=payload.retention_curve, source="manual",
    )
    db.add(metric)
    db.commit()
    return {"metric_id": metric.id, "growth": asdict(calculate_growth_score(metric))}


@app.post("/episodes/{episode_id}/analytics/sync")
def sync_analytics(episode_id: str, db: Session = Depends(get_db)):
    if not settings.youtube_enabled:
        raise HTTPException(status_code=503, detail="YouTube integration is not enabled")
    episode = get_episode_or_404(db, episode_id)
    try:
        sync_youtube_metrics(db, settings, episode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.get("/growth", response_class=HTMLResponse)
def growth_lab(request: Request, db: Session = Depends(get_db)):
    episodes = db.scalars(select(Episode).order_by(Episode.created_at.desc())).all()
    rows = [{"episode": e, "metric": latest_metric(e), "growth": calculate_growth_score(latest_metric(e))} for e in episodes]
    rows.sort(key=lambda row: row["growth"].score, reverse=True)
    return templates.TemplateResponse(request, "growth.html", {"rows": rows, "settings": settings})


@app.get("/api/growth")
def growth_api(db: Session = Depends(get_db)):
    episodes = db.scalars(select(Episode).order_by(Episode.created_at.desc())).all()
    return [
        {"episode_id": e.id, "title": e.title, "score": calculate_growth_score(latest_metric(e)).score,
         "confidence": calculate_growth_score(latest_metric(e)).confidence,
         "recommendation": calculate_growth_score(latest_metric(e)).recommendation}
        for e in episodes
    ]
