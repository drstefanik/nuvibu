from __future__ import annotations

import base64
from dataclasses import asdict
import secrets
import shutil
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db, init_db
from .models import Asset, AssetKind, Episode, EpisodeStatus, Job, JobStatus, MetricSnapshot, PublishRecord
from .providers.youtube import YouTubeClient
from .schemas import EpisodeCreate, MetricCreate, PipelineRequest
from .services.analytics import sync_youtube_metrics
from .services.growth import calculate_growth_score, latest_metric
from .services.pipeline import PipelineService, slugify

settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    if settings.admin_username and settings.admin_password and request.url.path not in {"/health"}:
        auth = request.headers.get("Authorization", "")
        valid = False
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                valid = secrets.compare_digest(username, settings.admin_username) and secrets.compare_digest(password, settings.admin_password)
            except Exception:
                valid = False
        if not valid:
            return JSONResponse(
                {"detail": "Authentication required"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Nuvibù Studio"'},
            )
    return await call_next(request)


@app.on_event("startup")
def startup() -> None:
    init_db()


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
def health() -> dict:
    return {"status": "ok", "provider_mode": settings.provider_mode, "app": settings.app_name}


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
    target_words: str = Form(""), featured_characters: str = Form("Nuvibù"),
    age_min_months: int = Form(6), age_max_months: int = Form(24),
    duration_seconds: int = Form(75), bpm: int = Form(92),
    visual_pacing: str = Form("gentle"), language: str = Form("it"),
    db: Session = Depends(get_db),
):
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
    episode = Episode(working_slug=candidate, **payload.model_dump())
    db.add(episode)
    db.commit()
    db.refresh(episode)
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/api/episodes", status_code=201)
def create_episode_api(payload: EpisodeCreate, db: Session = Depends(get_db)):
    base_slug = slugify(payload.title)
    candidate = base_slug
    number = 2
    while db.scalar(select(Episode.id).where(Episode.working_slug == candidate)):
        candidate = f"{base_slug}-{number}"
        number += 1
    episode = Episode(working_slug=candidate, **payload.model_dump())
    db.add(episode)
    db.commit()
    db.refresh(episode)
    return {"id": episode.id, "status": episode.status.value, "url": f"/episodes/{episode.id}"}


@app.get("/episodes/{episode_id}", response_class=HTMLResponse)
def episode_detail(request: Request, episode_id: str, db: Session = Depends(get_db)):
    episode = get_episode_or_404(db, episode_id)
    return templates.TemplateResponse(
        request, "episode_detail.html",
        {"episode": episode, "assets_by_kind": grouped_assets(episode), "settings": settings, "growth": calculate_growth_score(latest_metric(episode))},
    )


@app.post("/episodes/{episode_id}/run")
def run_pipeline_form(episode_id: str, through_step: str = Form("qc"), queued: bool = Form(False), db: Session = Depends(get_db)):
    payload = PipelineRequest(through_step=through_step)
    episode = get_episode_or_404(db, episode_id)
    service = PipelineService(db, settings)
    if queued:
        service.enqueue(episode, payload.through_step)
    else:
        try:
            service.run_through(episode, payload.through_step)
        except Exception as exc:
            episode.status = EpisodeStatus.FAILED
            db.commit()
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/api/episodes/{episode_id}/run")
def run_pipeline_api(episode_id: str, payload: PipelineRequest, db: Session = Depends(get_db)):
    episode = get_episode_or_404(db, episode_id)
    PipelineService(db, settings).run_through(episode, payload.through_step)
    return {"id": episode.id, "status": episode.status.value, "qc": episode.qc_json}


@app.post("/episodes/{episode_id}/reference")
def upload_character_reference(episode_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    episode = get_episode_or_404(db, episode_id)
    if file.content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(status_code=400, detail="Upload a PNG, JPEG or WebP image")
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "reference.png").suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        temp_path = Path(tmp.name)
    try:
        PipelineService(db, settings).save_character_reference(episode, temp_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/episodes/{episode_id}/approve")
def approve_episode(episode_id: str, db: Session = Depends(get_db)):
    episode = get_episode_or_404(db, episode_id)
    if not episode.qc_json.get("passed"):
        raise HTTPException(status_code=400, detail="Automatic QC must pass before approval")
    episode.status = EpisodeStatus.APPROVED
    db.commit()
    return RedirectResponse(f"/episodes/{episode.id}", status_code=303)


@app.post("/episodes/{episode_id}/youtube")
def upload_to_youtube(episode_id: str, db: Session = Depends(get_db)):
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
def serve_asset(asset_id: str, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None or not Path(asset.path).exists():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(asset.path, media_type=asset.mime_type, filename=Path(asset.path).name)


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
