"""Public API routes.

- GET /api/dashboards              — published dashboards (listing only)
- GET /api/dashboards/{slug}       — one published dashboard with charts
- POST /api/dashboards/{slug}/like — like a published dashboard
- DELETE /api/dashboards/{slug}/like — undo a like
- GET  /api/dashboards/{slug}/export — download the dashboard as JSON
- POST /api/dashboards/{slug}/tweak — forward a viewer request to EpidBot
- GET  /api/tweaks/{tweak_id}      — poll a tweak's status

(/healthz lives on the root app, see main.py.)

Draft dashboards are never exposed here; control goes through MCP only.
"""

import asyncio
import datetime as _dt
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from epidash.config import get_settings
from epidash.epidbot_client import EpidBotClient, EpidBotError, build_tweak_message
from epidash.logging import get_logger
from epidash.store import Store

logger = get_logger(__name__)

router = APIRouter(prefix="/api", include_in_schema=True)

_store: Store | None = None
_epidbot: EpidBotClient | None = None

# Sliding-window rate limiter: slug -> list of submission timestamps.
_tweak_windows: dict[str, list[float]] = {}
_like_windows: dict[str, list[float]] = {}

# Like actions per dashboard per hour (likes + unlikes combined).
LIKE_RATE_LIMIT_PER_HOUR = 60


def set_store(store: Store) -> None:
    global _store, _epidbot
    _store = store
    _epidbot = EpidBotClient()


def get_store() -> Store:
    if _store is None:
        raise RuntimeError("Store not initialised; app startup did not run")
    return _store


class TweakRequest(BaseModel):
    """Viewer request to change a dashboard."""

    request: str = Field(min_length=3, max_length=2000)


def _rate_limited(slug: str, windows: dict[str, list[float]], limit: int) -> bool:
    """True when this slug exceeded its hourly budget for `windows`."""
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    window = [t for t in windows.get(slug, []) if now - t < 3600]
    if len(window) >= limit:
        windows[slug] = window
        return True
    window.append(now)
    windows[slug] = window
    return False


def _tweak_rate_limited(slug: str) -> bool:
    return _rate_limited(slug, _tweak_windows, get_settings().tweak_rate_limit_per_hour)


def _get_published(store: Store, slug: str) -> dict:
    """Published dashboard by slug or 404 (drafts are MCP-only)."""
    dashboard = store.get_dashboard_by_slug(slug)
    if dashboard is None or dashboard["status"] != "published":
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return dashboard


def _public_dashboard(dashboard: dict) -> dict:
    """Shape of a dashboard exposed publicly (no internal status detail)."""
    return {
        "slug": dashboard["slug"],
        "title": dashboard["title"],
        "description": dashboard["description"],
        "created_by": dashboard["created_by"],
        "layout": dashboard["layout"],
        "updated_at": dashboard["updated_at"],
        "published_at": dashboard["published_at"],
    }


@router.get("/dashboards")
async def list_published_dashboards() -> list[dict]:
    store = get_store()
    return [
        {
            "slug": d["slug"],
            "title": d["title"],
            "description": d["description"],
            "created_by": d["created_by"],
            "likes": store.get_likes(d["id"]),
            "updated_at": d["updated_at"],
        }
        for d in store.list_dashboards(include_unpublished=False)
    ]


@router.get("/dashboards/{slug}")
async def get_published_dashboard(slug: str) -> dict:
    store = get_store()
    dashboard = _get_published(store, slug)
    try:
        store.record_view(dashboard["id"])
    except Exception as e:  # noqa: BLE001 — view counting is best effort
        logger.debug("View count failed for %s: %s", slug, e)
    return {
        **_public_dashboard(dashboard),
        "likes": store.get_likes(dashboard["id"]),
        "charts": store.list_charts(dashboard["id"]),
        "tweaks_enabled": get_settings().epidbot_configured,
    }


@router.post("/dashboards/{slug}/like")
async def like_dashboard(slug: str) -> dict:
    store = get_store()
    dashboard = _get_published(store, slug)
    if _rate_limited(slug, _like_windows, LIKE_RATE_LIMIT_PER_HOUR):
        raise HTTPException(status_code=429, detail="Too many like actions; try again later")
    return {"slug": slug, "likes": store.record_like(dashboard["id"])}


@router.delete("/dashboards/{slug}/like")
async def unlike_dashboard(slug: str) -> dict:
    store = get_store()
    dashboard = _get_published(store, slug)
    if _rate_limited(slug, _like_windows, LIKE_RATE_LIMIT_PER_HOUR):
        raise HTTPException(status_code=429, detail="Too many like actions; try again later")
    return {"slug": slug, "likes": store.remove_like(dashboard["id"])}


@router.get("/dashboards/{slug}/export")
async def export_dashboard(slug: str) -> JSONResponse:
    """Download the full dashboard definition (re-importable via MCP)."""
    store = get_store()
    dashboard = _get_published(store, slug)
    payload = {
        "app": "epidash",
        "slug": dashboard["slug"],
        "title": dashboard["title"],
        "description": dashboard["description"],
        "created_by": dashboard["created_by"],
        "layout": dashboard["layout"],
        "published_at": dashboard["published_at"],
        "updated_at": dashboard["updated_at"],
        "likes": store.get_likes(dashboard["id"]),
        "charts": [
            {
                "kind": c["kind"],
                "title": c["title"],
                "data": c["data"],
                "spec": c["spec"],
                "position": c["position"],
            }
            for c in store.list_charts(dashboard["id"])
        ],
    }
    return JSONResponse(
        payload,
        headers={
            "Content-Disposition": f'attachment; filename="{slug}.json"',
        },
    )


class TweakSubmitted(BaseModel):
    tweak_id: str
    status: str = "pending"


@router.post("/dashboards/{slug}/tweak", status_code=202)
async def submit_tweak(slug: str, body: TweakRequest, request: Request) -> TweakSubmitted:
    store = get_store()
    dashboard = _get_published(store, slug)
    settings = get_settings()
    if not settings.epidbot_configured:
        raise HTTPException(
            status_code=503,
            detail="EpidBot integration is not configured on this server",
        )
    if _tweak_rate_limited(slug):
        raise HTTPException(
            status_code=429, detail="Too many tweak requests; try again later"
        )

    tweak = store.create_tweak(dashboard["id"], body.request)
    asyncio.create_task(_process_tweak(tweak["id"], dashboard, body.request))
    return TweakSubmitted(tweak_id=tweak["id"], status=tweak["status"])


async def _process_tweak(tweak_id: str, dashboard: dict, request_text: str) -> None:
    """Background task: send the tweak turn to EpidBot and record the result."""
    store = get_store()
    assert _epidbot is not None
    store.update_tweak(tweak_id, status="processing")
    message = build_tweak_message(dashboard, store.list_charts(dashboard["id"]), request_text)
    try:
        result = await _epidbot.run_tweak(message)
        content = result.get("content") or ""
        store.update_tweak(tweak_id, status="completed", response=content)
    except EpidBotError as e:
        logger.warning("Tweak %s failed: %s", tweak_id, e)
        store.update_tweak(tweak_id, status="failed", error=str(e))
    except Exception as e:  # noqa: BLE001 — never crash the background task
        logger.error("Tweak %s crashed: %s", tweak_id, e)
        store.update_tweak(tweak_id, status="failed", error=f"internal error: {e}")


@router.get("/tweaks/{tweak_id}")
async def get_tweak(tweak_id: str) -> dict:
    tweak = get_store().get_tweak(tweak_id)
    if tweak is None:
        raise HTTPException(status_code=404, detail="Tweak not found")
    return {
        "tweak_id": tweak["id"],
        "dashboard_id": tweak["dashboard_id"],
        "status": tweak["status"],
        "response": tweak["response"],
        "error": tweak["error"],
        "created_at": tweak["created_at"],
        "updated_at": tweak["updated_at"],
    }
