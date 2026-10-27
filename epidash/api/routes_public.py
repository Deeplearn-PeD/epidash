"""Public API routes.

- GET /api/dashboards              — published dashboards (listing only)
- GET /api/dashboards/{slug}       — one published dashboard with charts
- POST /api/dashboards/{slug}/like — like a published dashboard
- DELETE /api/dashboards/{slug}/like — undo a like
- GET  /api/dashboards/{slug}/export — download the dashboard as JSON
- POST /api/dashboards/{slug}/tweak — forward a viewer request to EpidBot
- POST /api/dashboards/{slug}/ask  — ask EpidBot a question about a dashboard
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
from epidash.epidbot_client import (
    EpidBotClient,
    EpidBotError,
    build_question_message,
    build_tweak_message,
)
from epidash.logging import get_logger
from epidash.routes_auth import get_viewer_from_request
from epidash.store import MARKDOWN_MAX_CHARS, Store, validate_chart_data

logger = get_logger(__name__)

router = APIRouter(prefix="/api", include_in_schema=True)

_store: Store | None = None
_epidbot: EpidBotClient | None = None

# Sliding-window rate limiter: slug -> list of submission timestamps.
_tweak_windows: dict[str, list[float]] = {}
_ask_windows: dict[str, list[float]] = {}
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


def _get_published(
    store: Store,
    slug: str,
    viewer: dict | None = None,
    token: str | None = None,
) -> dict:
    """Published dashboard by slug with visibility enforced.

    public: anyone; unlisted: anyone with the link (hidden from the
    gallery); restricted: logged-in viewers or a valid share token.
    Drafts are MCP-only (404).
    """
    dashboard = store.get_dashboard_by_slug(slug)
    if dashboard is None or dashboard["status"] != "published":
        raise HTTPException(status_code=404, detail="Dashboard not found")
    if dashboard["visibility"] == "restricted":
        if viewer is None and not (
            token and store.share_token_valid(token, dashboard["id"])
        ):
            raise HTTPException(
                status_code=403, detail="Login required to view this dashboard"
            )
    return dashboard


def _public_dashboard(dashboard: dict) -> dict:
    """Shape of a dashboard exposed publicly (no internal status detail)."""
    return {
        "slug": dashboard["slug"],
        "title": dashboard["title"],
        "description": dashboard["description"],
        "created_by": dashboard["created_by"],
        "language": dashboard.get("language", ""),
        "layout": dashboard["layout"],
        "created_at": dashboard["created_at"],
        "updated_at": dashboard["updated_at"],
        "published_at": dashboard["published_at"],
    }


@router.get("/dashboards")
async def list_published_dashboards(request: Request) -> list[dict]:
    store = get_store()
    # the gallery lists public dashboards only; unlisted/restricted stay
    # reachable via direct link / share tokens
    return [
        {
            "slug": d["slug"],
            "title": d["title"],
            "description": d["description"],
            "created_by": d["created_by"],
            "language": d.get("language", ""),
            "likes": store.get_likes(d["id"]),
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
        }
        for d in store.list_dashboards(include_unpublished=False)
        if d["visibility"] == "public"
    ]


@router.get("/dashboards/{slug}")
async def get_published_dashboard(slug: str, request: Request) -> dict:
    store = get_store()
    viewer = get_viewer_from_request(request)
    dashboard = _get_published(
        store, slug, viewer=viewer, token=request.query_params.get("token")
    )
    try:
        store.record_view(dashboard["id"])
    except Exception as e:  # noqa: BLE001 — view counting is best effort
        logger.debug("View count failed for %s: %s", slug, e)
    charts = store.list_charts(dashboard["id"])
    sources = {s["chart_id"]: s for s in store.list_data_sources(dashboard["id"])}
    for chart in charts:
        source = sources.get(chart["id"])
        if source:
            chart["source"] = {
                "status": source["last_status"],
                "refresh_interval_s": source["refresh_interval_s"],
                "last_refreshed_at": source["last_refreshed_at"],
            }
    liked = viewer is not None and store.has_viewer_like(viewer["id"], dashboard["id"])
    return {
        **_public_dashboard(dashboard),
        "likes": store.get_likes(dashboard["id"]),
        "viewer_liked": liked,
        "can_edit": _is_owner(dashboard, viewer),
        "charts": charts,
        "tweaks_enabled": get_settings().epidbot_configured,
    }


@router.post("/dashboards/{slug}/like")
async def like_dashboard(slug: str, request: Request) -> dict:
    store = get_store()
    viewer = get_viewer_from_request(request)
    dashboard = _get_published(
        store, slug, viewer=viewer, token=request.query_params.get("token")
    )
    if _rate_limited(slug, _like_windows, LIKE_RATE_LIMIT_PER_HOUR):
        raise HTTPException(status_code=429, detail="Too many like actions; try again later")
    if viewer is not None and not store.add_viewer_like(viewer["id"], dashboard["id"]):
        # already liked by this viewer — count unchanged, idempotent
        return {"slug": slug, "likes": store.get_likes(dashboard["id"])}
    return {"slug": slug, "likes": store.record_like(dashboard["id"])}


@router.delete("/dashboards/{slug}/like")
async def unlike_dashboard(slug: str, request: Request) -> dict:
    store = get_store()
    viewer = get_viewer_from_request(request)
    dashboard = _get_published(
        store, slug, viewer=viewer, token=request.query_params.get("token")
    )
    if _rate_limited(slug, _like_windows, LIKE_RATE_LIMIT_PER_HOUR):
        raise HTTPException(status_code=429, detail="Too many like actions; try again later")
    if viewer is not None and not store.remove_viewer_like(viewer["id"], dashboard["id"]):
        return {"slug": slug, "likes": store.get_likes(dashboard["id"])}
    return {"slug": slug, "likes": store.remove_like(dashboard["id"])}


@router.get("/dashboards/{slug}/export")
async def export_dashboard(slug: str, request: Request) -> JSONResponse:
    """Download the full dashboard definition (re-importable via MCP)."""
    store = get_store()
    viewer = get_viewer_from_request(request)
    dashboard = _get_published(
        store, slug, viewer=viewer, token=request.query_params.get("token")
    )
    payload = {
        "app": "epidash",
        "slug": dashboard["slug"],
        "title": dashboard["title"],
        "description": dashboard["description"],
        "created_by": dashboard["created_by"],
        "language": dashboard.get("language", ""),
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
    viewer = get_viewer_from_request(request)
    dashboard = _get_published(
        store, slug, viewer=viewer, token=request.query_params.get("token")
    )
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

    tweak = store.create_tweak(
        dashboard["id"], body.request, kind="tweak",
        viewer_id=viewer["id"] if viewer else None,
    )
    asyncio.create_task(_process_tweak(tweak["id"], dashboard, body.request))
    return TweakSubmitted(tweak_id=tweak["id"], status=tweak["status"])


@router.post("/dashboards/{slug}/ask", status_code=202)
async def ask_about_dashboard(
    slug: str, body: TweakRequest, request: Request
) -> TweakSubmitted:
    """Viewer question about a published dashboard — an EpidBot chat turn
    that answers from the dashboard data without modifying it."""
    store = get_store()
    viewer = get_viewer_from_request(request)
    dashboard = _get_published(
        store, slug, viewer=viewer, token=request.query_params.get("token")
    )
    settings = get_settings()
    if not settings.epidbot_configured:
        raise HTTPException(
            status_code=503,
            detail="EpidBot integration is not configured on this server",
        )
    if _rate_limited(slug, _ask_windows, settings.ask_rate_limit_per_hour):
        raise HTTPException(status_code=429, detail="Too many questions; try again later")

    tweak = store.create_tweak(
        dashboard["id"], body.request, kind="question",
        viewer_id=viewer["id"] if viewer else None,
    )
    requester = viewer["name"] if viewer else None
    asyncio.create_task(_process_question(tweak["id"], dashboard, body.request, requester))
    return TweakSubmitted(tweak_id=tweak["id"], status=tweak["status"])


async def _process_epidbot_turn(tweak_id: str, message: str, action: str) -> None:
    """Background task: run one EpidBot chat turn and record the outcome."""
    store = get_store()
    assert _epidbot is not None
    store.update_tweak(tweak_id, status="processing")
    try:
        result = await _epidbot.run_tweak(message)
        content = result.get("content") or ""
        store.update_tweak(tweak_id, status="completed", response=content)
    except EpidBotError as e:
        logger.warning("%s %s failed: %s", action, tweak_id, e)
        store.update_tweak(tweak_id, status="failed", error=str(e))
    except Exception as e:  # noqa: BLE001 — never crash the background task
        logger.error("%s %s crashed: %s", action, tweak_id, e)
        store.update_tweak(tweak_id, status="failed", error=f"internal error: {e}")


async def _process_tweak(tweak_id: str, dashboard: dict, request_text: str) -> None:
    """Background task: send the tweak turn to EpidBot and record the result."""
    message = build_tweak_message(
        dashboard, get_store().list_charts(dashboard["id"]), request_text
    )
    await _process_epidbot_turn(tweak_id, message, "Tweak")


async def _process_question(
    tweak_id: str, dashboard: dict, question: str, requester: str | None = None
) -> None:
    """Background task: send the question turn to EpidBot (read-only)."""
    message = build_question_message(
        dashboard, get_store().list_charts(dashboard["id"]), question,
        requester=requester,
    )
    await _process_epidbot_turn(tweak_id, message, "Question")


def _is_owner(dashboard: dict, viewer: dict | None) -> bool:
    """True when the signed-in viewer may edit this dashboard's text blocks.

    Matched by the dashboard's stored owner identity (owner_sub), or — for
    dashboards created before ownership was tracked — by the public creator
    name matching the viewer's name or email."""
    if viewer is None:
        return False
    owner_sub = (dashboard.get("owner_sub") or "").strip()
    if owner_sub and viewer.get("sub") == owner_sub:
        return True
    created_by = (dashboard.get("created_by") or "").strip().casefold()
    if not created_by:
        return False
    return created_by in {
        (viewer.get("name") or "").strip().casefold(),
        (viewer.get("email") or "").strip().casefold(),
    }


def _display_key(chart: dict) -> int:
    """Display order of a chart (mirrors the viewer's grid sort)."""
    order = (chart.get("position") or {}).get("order")
    return order if isinstance(order, (int, float)) else chart["id"]


def _owner_dashboard(slug: str, request: Request) -> dict:
    """Published dashboard whose text blocks the requester may edit."""
    store = get_store()
    viewer = get_viewer_from_request(request)
    dashboard = _get_published(
        store, slug, viewer=viewer, token=request.query_params.get("token")
    )
    if viewer is None:
        raise HTTPException(status_code=401, detail="Login required to edit")
    if not _is_owner(dashboard, viewer):
        raise HTTPException(
            status_code=403, detail="Only the owner can edit this dashboard"
        )
    return dashboard


class BlockCreate(BaseModel):
    """New markdown text block. Placement: `top`, or right after chart
    `after`, or at the end when neither is given."""

    text: str = Field(min_length=1, max_length=MARKDOWN_MAX_CHARS)
    after: Optional[int] = None
    top: bool = False


class BlockUpdate(BaseModel):
    """Change a markdown block's text and/or move it."""

    text: Optional[str] = Field(default=None, min_length=1, max_length=MARKDOWN_MAX_CHARS)
    after: Optional[int] = None
    top: bool = False


@router.post("/dashboards/{slug}/blocks", status_code=201)
async def create_block(slug: str, body: BlockCreate, request: Request) -> dict:
    """Add a markdown text block between the dashboard's visualizations."""
    store = get_store()
    dashboard = _owner_dashboard(slug, request)
    try:
        validate_chart_data("markdown", {"text": body.text})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    ordered = [
        c["id"] for c in sorted(
            store.list_charts(dashboard["id"]), key=_display_key
        )
    ]
    if body.top:
        index = 0
    elif body.after is not None:
        if body.after not in ordered:
            raise HTTPException(status_code=404, detail="Anchor chart not found")
        index = ordered.index(body.after) + 1
    else:
        index = len(ordered)
    chart = store.add_chart(
        dashboard["id"], "markdown", data={"text": body.text},
        position={"order": index, "width": "full"},
    )
    ordered.insert(index, chart["id"])
    store.renumber_charts(dashboard["id"], ordered)
    return store.get_chart(chart["id"])


@router.patch("/dashboards/{slug}/blocks/{chart_id}")
async def update_block(slug: str, chart_id: int, body: BlockUpdate, request: Request) -> dict:
    """Edit a markdown text block's text and/or move it."""
    store = get_store()
    dashboard = _owner_dashboard(slug, request)
    chart = store.get_chart(chart_id)
    if (
        chart is None
        or chart["dashboard_id"] != dashboard["id"]
        or chart["kind"] != "markdown"
    ):
        raise HTTPException(status_code=404, detail="Text block not found")
    if body.text is not None:
        try:
            validate_chart_data("markdown", {"text": body.text})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        store.update_chart(chart_id, data={"text": body.text})

    if body.top or body.after is not None:
        ordered = [
            c["id"] for c in sorted(
                store.list_charts(dashboard["id"]), key=_display_key
            )
            if c["id"] != chart_id
        ]
        if body.top:
            index = 0
        else:
            if body.after not in ordered:
                raise HTTPException(status_code=404, detail="Anchor chart not found")
            index = ordered.index(body.after) + 1
        ordered.insert(index, chart_id)
        store.renumber_charts(dashboard["id"], ordered)
    else:
        # text-only change: still let viewers' polling pick it up
        store.update_dashboard(dashboard["id"])
    return store.get_chart(chart_id)


@router.delete("/dashboards/{slug}/blocks/{chart_id}")
async def delete_block(slug: str, chart_id: int, request: Request) -> dict:
    """Remove a markdown text block."""
    store = get_store()
    dashboard = _owner_dashboard(slug, request)
    chart = store.get_chart(chart_id)
    if (
        chart is None
        or chart["dashboard_id"] != dashboard["id"]
        or chart["kind"] != "markdown"
    ):
        raise HTTPException(status_code=404, detail="Text block not found")
    store.remove_chart(chart_id)
    return {"slug": slug, "deleted": chart_id}


@router.get("/tweaks/{tweak_id}")
async def get_tweak(tweak_id: str) -> dict:
    store = get_store()
    tweak = store.get_tweak(tweak_id)
    if tweak is None:
        raise HTTPException(status_code=404, detail="Tweak not found")
    viewer = (
        store.get_viewer(tweak["viewer_id"])
        if tweak.get("viewer_id")
        else None
    )
    return {
        "tweak_id": tweak["id"],
        "dashboard_id": tweak["dashboard_id"],
        "kind": tweak.get("kind", "tweak"),
        "viewer": viewer["name"] if viewer else None,
        "status": tweak["status"],
        "response": tweak["response"],
        "error": tweak["error"],
        "created_at": tweak["created_at"],
        "updated_at": tweak["updated_at"],
    }
