"""
The EpiDash MCP server: FastMCP instance and tool definitions.

This is the surface EpidBot (or any MCP client holding a ``dk_`` control
key) uses to build and publish dashboards. Conventions mirror the EpidBot
admin MCP server:

- every mutating tool writes an audit row (ok/refused/error);
- destructive or outward-visible actions require ``confirm=true``;
- tool errors are ``ToolError`` with actionable messages;
- ``functools.wraps`` is mandatory in the audit decorator — FastMCP
  derives the tool name and schema from the wrapped function.
"""

import datetime
import functools
import inspect
import secrets
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from epidash.config import get_settings
from epidash.logging import get_logger
from epidash.mcpsrv import audit
from epidash.mcpsrv.auth import get_current_key
from epidash.store import Store, CHART_KINDS, validate_chart_data

logger = get_logger(__name__)

_store: Store | None = None


def set_store(store: Store) -> None:
    """Point the tools at the process store (called at app startup)."""
    global _store
    _store = store
    audit.set_store(store)


def _get_store() -> Store:
    if _store is None:
        raise RuntimeError("Store not initialised; app startup did not run")
    return _store


_cfg = get_settings()
mcp = FastMCP(
    _cfg.server_name,
    streamable_http_path=_cfg.mcp_path,
    # Stateless mode: every POST is self-contained (no initialize/session
    # handshake). The right choice for a machine-to-machine control surface
    # — clients (EpidBot) stay as simple stateless JSON-RPC callers and
    # there is no session state to leak between callers.
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=_cfg.dns_rebinding_protection,
        allowed_hosts=_cfg.allowed_hosts,
    ),
)


# ── helpers ──────────────────────────────────────────────────────────────


def _require_confirm(confirm: bool, action: str, consequence: str) -> None:
    if not confirm:
        raise ToolError(
            f"Refused: {action} was NOT executed — this action {consequence}. "
            "Re-run with confirm=true to proceed."
        )


def _audit(
    action: str,
    result: str = "ok",
    target_type: Optional[str] = None,
    target_id: Any = None,
    params: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Best-effort audit row attributed to the in-flight control key."""
    try:
        key_meta = get_current_key()
    except RuntimeError:
        key_meta = {}
    try:
        _get_store().log_audit(
            key_id=(key_meta or {}).get("key_id"),
            action=action,
            target_type=target_type,
            target_id=target_id,
            params=params,
            result=result,
            error=error,
        )
    except Exception as e:  # noqa: BLE001 — audit must never break the tool
        logger.error("EpiDash audit write failed for %s: %s", action, e)


def _audited(
    action: str,
    target_type: Optional[str] = None,
    target_id_arg: Optional[str] = None,
):
    """Decorator for mutating tools: audit ok/refused/error around the fn."""

    def _resolve_target_id(fn, args, kwargs):
        if not target_id_arg:
            return None
        try:
            bound = inspect.signature(fn).bind(*args, **kwargs)
            return bound.arguments.get(target_id_arg)
        except (TypeError, ValueError):
            return None

    def deco(fn):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*a, **kw):
                tid = _resolve_target_id(fn, a, kw)
                try:
                    out = await fn(*a, **kw)
                except ToolError as e:
                    _audit(action, result="refused", target_type=target_type,
                           target_id=tid, error=str(e))
                    raise
                except Exception as e:
                    _audit(action, result="error", target_type=target_type,
                           target_id=tid, error=str(e))
                    raise
                _audit(action, target_type=target_type, target_id=tid)
                return out
            return awrapper

        @functools.wraps(fn)
        def swrapper(*a, **kw):
            tid = _resolve_target_id(fn, a, kw)
            try:
                out = fn(*a, **kw)
            except ToolError as e:
                _audit(action, result="refused", target_type=target_type,
                       target_id=tid, error=str(e))
                raise
            except Exception as e:
                _audit(action, result="error", target_type=target_type,
                       target_id=tid, error=str(e))
                raise
            _audit(action, target_type=target_type, target_id=tid)
            return out
        return swrapper
    return deco


def _validate_chart_data(kind: str, data: dict) -> None:
    """Validate the per-kind data envelope; raise ToolError when malformed."""
    try:
        validate_chart_data(kind, data)
    except ValueError as e:
        raise ToolError(str(e)) from e


# ── read-only tools ──────────────────────────────────────────────────────


@mcp.tool()
async def list_dashboards(include_unpublished: bool = True) -> list:
    """All dashboards (id, slug, title, description, status, timestamps).
    Published dashboards are visible at {base_url}/#/d/{slug}."""
    return _get_store().list_dashboards(include_unpublished=include_unpublished)


@mcp.tool()
async def get_dashboard(dashboard_id: int) -> dict:
    """One dashboard including all of its charts (full data, spec, position)."""
    dashboard = _get_store().get_dashboard(dashboard_id)
    if dashboard is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    dashboard["charts"] = _get_store().list_charts(dashboard_id)
    return dashboard


@mcp.tool()
async def get_view_stats(dashboard_id: int) -> dict:
    """View count and last-viewed timestamp for one dashboard."""
    store = _get_store()
    dashboard = store.get_dashboard(dashboard_id)
    if dashboard is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    return {
        "dashboard_id": dashboard_id,
        "views": store.get_view_count(dashboard_id),
    }


@mcp.tool()
async def get_audit_log(
    limit: int = 50,
    action: Optional[str] = None,
    dashboard_id: Optional[int] = None,
) -> list:
    """Recent audit entries for mutations performed through this MCP
    server (newest first). Optional filters: by action name (e.g.
    'publish_dashboard') and/or by target dashboard."""
    return _get_store().list_audit(
        limit=min(max(limit, 1), 500), action=action, dashboard_id=dashboard_id
    )


# ── mutating tools: dashboards ───────────────────────────────────────────


@mcp.tool()
@_audited("create_dashboard", target_type="dashboard", target_id_arg="slug")
async def create_dashboard(
    title: str,
    description: str = "",
    slug: Optional[str] = None,
    layout: Optional[dict] = None,
    author: Optional[str] = None,
    language: Optional[str] = None,
) -> dict:
    """Create a dashboard in draft status. Returns its id and public slug.
    slug: optional URL-safe identifier (random when omitted). layout:
    free-form options dict (e.g. {"theme": "dark"}). author: display name
    of the person who requested the dashboard — pass the requesting
    user's name/handle when known (shown publicly as the creator);
    defaults to the control key's name. language: language the dashboard
    content is written in (e.g. "pt-BR", "en") — shown as a flag in the
    gallery."""
    try:
        try:
            creator = author or get_current_key().get("name", "")
        except RuntimeError:
            creator = author or ""
        return _get_store().create_dashboard(
            title=title, description=description, slug=slug, layout=layout,
            created_by=creator, language=language or "",
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


@mcp.tool()
@_audited("update_dashboard", target_type="dashboard", target_id_arg="dashboard_id")
async def update_dashboard(
    dashboard_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    layout: Optional[dict] = None,
    author: Optional[str] = None,
    language: Optional[str] = None,
) -> dict:
    """Update a dashboard's title/description/layout (only provided fields
    change). author: replace the public creator name (e.g. to correct a
    dashboard attributed to a shared key). language: language the dashboard
    content is written in (e.g. "pt-BR", "en") — shown as a flag in the
    gallery."""
    dashboard = _get_store().update_dashboard(
        dashboard_id, title=title, description=description, layout=layout,
        created_by=author, language=language,
    )
    if dashboard is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    return dashboard


@mcp.tool()
@_audited("publish_dashboard", target_type="dashboard", target_id_arg="dashboard_id")
async def publish_dashboard(dashboard_id: int, confirm: bool = False) -> dict:
    """Publish a dashboard: it becomes publicly visible to anyone with the
    link {base_url}/#/d/{slug}. Re-run with confirm=true to proceed."""
    _require_confirm(
        confirm, f"publish_dashboard({dashboard_id})",
        "makes this dashboard publicly accessible via its link",
    )
    store = _get_store()
    dashboard = store.set_dashboard_status(dashboard_id, "published")
    if dashboard is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    settings = get_settings()
    return {
        **dashboard,
        "url": f"{settings.base_url.rstrip('/')}/#/d/{dashboard['slug']}",
    }


@mcp.tool()
@_audited("unpublish_dashboard", target_type="dashboard", target_id_arg="dashboard_id")
async def unpublish_dashboard(dashboard_id: int, confirm: bool = False) -> dict:
    """Unpublish a dashboard: the public link stops working (charts and
    history are kept). Re-run with confirm=true to proceed."""
    _require_confirm(
        confirm, f"unpublish_dashboard({dashboard_id})",
        "removes public access to this dashboard",
    )
    dashboard = _get_store().set_dashboard_status(dashboard_id, "draft")
    if dashboard is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    return dashboard


@mcp.tool()
@_audited("delete_dashboard", target_type="dashboard", target_id_arg="dashboard_id")
async def delete_dashboard(dashboard_id: int, confirm: bool = False) -> dict:
    """Permanently delete a dashboard and all of its charts. Re-run with
    confirm=true to proceed."""
    _require_confirm(
        confirm, f"delete_dashboard({dashboard_id})",
        "deletes this dashboard and all its charts (cannot be undone)",
    )
    dashboard = _get_store().delete_dashboard(dashboard_id)
    if dashboard is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    return dashboard


# ── mutating tools: charts ───────────────────────────────────────────────


@mcp.tool()
@_audited("add_chart", target_type="chart")
async def add_chart(
    dashboard_id: int,
    kind: Literal["line", "bar", "pie", "kpi", "table", "choropleth", "markdown"],
    title: str = "",
    data: Optional[dict] = None,
    spec: Optional[dict] = None,
    position: Optional[dict] = None,
) -> dict:
    """Add a chart to a dashboard. kind: line|bar|pie|kpi|table|choropleth|markdown.

    data envelopes:
    - line/bar/pie/table: {"columns": ["month", "cases"],
      "rows": [["Jan", 120], ...]}
    - kpi: {"value": 1234, "label": "Cases", "unit": "", "delta": "+12%"}
    - choropleth: {"values": {"SP": 120, "RJ": 90}, "region_level": "uf",
      "label": "cases"} (UF codes)
    - markdown: {"text": "## Notes\nSome **narrative** between charts"}
      (rendered as a narrative block; markdown supported)
    spec: optional style overrides (Plotly trace/layout fragments, e.g.
    {"color": "#4ade80"} or {"y_label": "cases"}). position: ordering/
    width hints, e.g. {"order": 1, "width": "half"}."""
    data = data or {}
    _validate_chart_data(kind, data)
    try:
        return _get_store().add_chart(
            dashboard_id=dashboard_id, kind=kind, title=title,
            data=data, spec=spec, position=position,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


@mcp.tool()
@_audited("update_chart", target_type="chart", target_id_arg="chart_id")
async def update_chart(
    chart_id: int,
    kind: Optional[Literal["line", "bar", "pie", "kpi", "table", "choropleth", "markdown"]] = None,
    title: Optional[str] = None,
    data: Optional[dict] = None,
    spec: Optional[dict] = None,
    position: Optional[dict] = None,
) -> dict:
    """Update a chart (only provided fields change). The data envelope must
    match the (new) kind — see add_chart for the per-kind shapes."""
    if kind is not None and kind not in CHART_KINDS:
        raise ToolError(f"invalid chart kind '{kind}'")
    if data is not None:
        effective_kind = kind
        if effective_kind is None:
            existing = _get_store().get_chart(chart_id)
            if existing is None:
                raise ToolError(f"chart {chart_id} not found")
            effective_kind = existing["kind"]
        _validate_chart_data(effective_kind, data)
    chart = _get_store().update_chart(
        chart_id, kind=kind, title=title, data=data, spec=spec, position=position
    )
    if chart is None:
        raise ToolError(f"chart {chart_id} not found")
    return chart


@mcp.tool()
@_audited("remove_chart", target_type="chart", target_id_arg="chart_id")
async def remove_chart(chart_id: int, confirm: bool = False) -> dict:
    """Remove a chart from its dashboard. Re-run with confirm=true to
    proceed."""
    _require_confirm(
        confirm, f"remove_chart({chart_id})",
        "deletes this chart (cannot be undone)",
    )
    chart = _get_store().remove_chart(chart_id)
    if chart is None:
        raise ToolError(f"chart {chart_id} not found")
    return chart


# ── mutating tools: data sources ─────────────────────────────────────────


@mcp.tool()
@_audited("attach_data_source", target_type="chart", target_id_arg="chart_id")
async def attach_data_source(
    chart_id: int,
    kind: Literal["url_json", "url_csv", "postgres"],
    url: Optional[str] = None,
    refresh_interval_s: int = 300,
    path: Optional[str] = None,
    source: Optional[str] = None,
    sql: Optional[str] = None,
) -> dict:
    """Make a chart auto-update from remote data. kind:
    url_json (payload is the chart's data envelope; optional dotted `path`
    extracts a nested object), url_csv (header row + rows; feeds
    line/bar/pie/table), or postgres (server-side query — pass `source`:
    the registered DSN name, and `sql`: the query; DSNs live in the
    EPIDASH_PG_DSN_<NAME> environment variables of this server, never in
    the database). refresh_interval_s: minimum 30. The source is refreshed
    immediately on attach; failures are recorded on the source (see
    get_data_source) and never lose the chart's existing data."""
    if kind == "postgres":
        spec = {"source": source or "", "sql": sql or ""}
    else:
        if not url:
            raise ToolError(f"kind '{kind}' needs a url")
        spec = {"url": url, **({"path": path} if path else {})}
    try:
        registered = get_settings().pg_sources
        if kind == "postgres" and (source or "") not in registered:
            raise ToolError(
                f"unknown postgres source '{source}'; registered sources: "
                f"{sorted(registered) or 'none'}"
            )
        attached = _get_store().set_data_source(
            chart_id, kind, spec, refresh_interval_s=refresh_interval_s
        )
    except ValueError as e:
        raise ToolError(str(e)) from e
    from epidash import refresher

    outcome = await refresher.refresh_source(_get_store(), chart_id)
    return {**attached, "last_refresh": outcome}


@mcp.tool()
@_audited("remove_data_source", target_type="chart", target_id_arg="chart_id")
async def remove_data_source(chart_id: int, confirm: bool = False) -> dict:
    """Detach a chart's data source: the chart keeps its current data but
    stops auto-updating. Re-run with confirm=true to proceed."""
    _require_confirm(
        confirm, f"remove_data_source({chart_id})",
        "stops this chart from auto-updating (data is kept)",
    )
    if _get_store().delete_data_source(chart_id):
        return {"chart_id": chart_id, "removed": True}
    raise ToolError(f"chart {chart_id} has no data source")


@mcp.tool()
async def get_data_source(chart_id: int) -> dict:
    """Auto-refresh configuration and health of one chart's data source
    (status of the last refresh, error, interval, timestamps)."""
    source = _get_store().get_data_source(chart_id)
    if source is None:
        raise ToolError(f"chart {chart_id} has no data source")
    return source


@mcp.tool()
@_audited("refresh_data_source", target_type="chart", target_id_arg="chart_id")
async def refresh_data_source(chart_id: int) -> dict:
    """Force an immediate refresh of a chart's data source and report the
    outcome without waiting for the scheduled interval."""
    from epidash import refresher

    return await refresher.refresh_source(_get_store(), chart_id)


# ── mutating tools: authoring power tools ────────────────────────────────


@mcp.tool()
@_audited("duplicate_dashboard", target_type="dashboard", target_id_arg="dashboard_id")
async def duplicate_dashboard(
    dashboard_id: int,
    new_title: Optional[str] = None,
    new_slug: Optional[str] = None,
    author: Optional[str] = None,
) -> dict:
    """Copy a dashboard — as a new draft — including all charts (data,
    spec, position) and their data sources. Useful to iterate on a variant
    without touching the published original. new_title/new_slug/author
    override the copies' title/slug/creator (defaults: '<title> (copy)',
    random slug, original creator)."""
    try:
        copy = _get_store().duplicate_dashboard(
            dashboard_id, new_title=new_title, new_slug=new_slug,
            created_by=author,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e
    if copy is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    settings = get_settings()
    return {
        **copy,
        "url": f"{settings.base_url.rstrip('/')}/#/d/{copy['slug']}",
    }


@mcp.tool()
@_audited("style_charts", target_type="dashboard", target_id_arg="dashboard_id")
async def style_charts(dashboard_id: int, spec: dict) -> dict:
    """Apply a style override to every chart of a dashboard at once, e.g.
    {"color": "#4ade80"} or {"y_label": "casos"}. The fragment is merged
    into each chart's existing spec (per-chart overrides win for keys set
    afterwards via update_chart)."""
    try:
        count = _get_store().style_charts(dashboard_id, spec)
    except ValueError as e:
        raise ToolError(str(e)) from e
    return {"dashboard_id": dashboard_id, "charts_styled": count, "spec": spec}


# ── mutating tools: access control ───────────────────────────────────────


@mcp.tool()
@_audited("set_dashboard_access", target_type="dashboard", target_id_arg="dashboard_id")
async def set_dashboard_access(
    dashboard_id: int,
    visibility: Literal["public", "unlisted", "restricted"],
    confirm: bool = False,
) -> dict:
    """Control who can reach a published dashboard: public (gallery +
    link), unlisted (link only — hidden from the gallery), restricted
    (logged-in viewers or valid share tokens only). Re-run with
    confirm=true to proceed."""
    _require_confirm(
        confirm, f"set_dashboard_access({dashboard_id}, {visibility})",
        f"changes who can reach this dashboard to '{visibility}'",
    )
    try:
        dashboard = _get_store().set_dashboard_visibility(dashboard_id, visibility)
    except ValueError as e:
        raise ToolError(str(e)) from e
    if dashboard is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    return dashboard


@mcp.tool()
@_audited("create_share_token", target_type="dashboard", target_id_arg="dashboard_id")
async def create_share_token(
    dashboard_id: int,
    expires_in_days: int = 7,
    confirm: bool = False,
) -> dict:
    """Create a share link that reaches even restricted dashboards.
    expires_in_days: token lifetime (1-365). Re-run with confirm=true to
    proceed — anyone holding the link can view the dashboard."""
    _require_confirm(
        confirm, f"create_share_token({dashboard_id})",
        "creates a link that grants view access to this dashboard",
    )
    store = _get_store()
    if store.get_dashboard(dashboard_id) is None:
        raise ToolError(f"dashboard {dashboard_id} not found")
    expires_in_days = min(max(expires_in_days, 1), 365)
    token = "st_" + secrets.token_urlsafe(18)
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=expires_in_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = store.create_share_token(
        dashboard_id, token, expires_at,
        created_by=(get_current_key().get("name", "") or ""),
    )
    settings = get_settings()
    return {
        **row,
        "url": f"{settings.base_url.rstrip('/')}/?t={token}#/d/",
    }


@mcp.tool()
async def list_share_tokens(dashboard_id: int) -> list:
    """Share tokens created for one dashboard (newest first)."""
    return _get_store().list_share_tokens(dashboard_id)


@mcp.tool()
@_audited("revoke_share_token", target_type="dashboard")
async def revoke_share_token(token: str, confirm: bool = False) -> dict:
    """Revoke a share token: links that used it stop working. Re-run with
    confirm=true to proceed."""
    _require_confirm(
        confirm, f"revoke_share_token({token[:8]}...)",
        "invalidates this share link permanently",
    )
    if _get_store().revoke_share_token(token):
        return {"token": token, "revoked": True}
    raise ToolError("share token not found")
