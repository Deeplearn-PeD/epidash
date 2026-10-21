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

import functools
import inspect
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from epidash.config import get_settings
from epidash.logging import get_logger
from epidash.mcpsrv import audit
from epidash.mcpsrv.auth import get_current_key
from epidash.store import Store, CHART_KINDS

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
    if not isinstance(data, dict):
        raise ToolError("data must be a JSON object")

    if kind in ("line", "bar", "pie", "table"):
        columns = data.get("columns")
        rows = data.get("rows")
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise ToolError(
                f"'{kind}' data needs 'columns': a list of column-name strings"
            )
        if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
            raise ToolError(f"'{kind}' data needs 'rows': a list of row arrays")
        for i, row in enumerate(rows):
            if len(row) != len(columns):
                raise ToolError(
                    f"row {i} has {len(row)} values but there are "
                    f"{len(columns)} columns"
                )
    elif kind == "kpi":
        if "value" not in data:
            raise ToolError("'kpi' data needs a 'value' (number or string)")
    elif kind == "choropleth":
        values = data.get("values")
        if not isinstance(values, dict) or not values:
            raise ToolError(
                "'choropleth' data needs 'values': a non-empty object mapping "
                "UF code (e.g. 'SP', 'RJ') to a number"
            )
        for code, value in values.items():
            if not isinstance(code, str) or len(code) != 2 or not isinstance(value, (int, float)):
                raise ToolError(
                    "'choropleth' values must map two-letter UF codes to numbers"
                )


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
async def get_audit_log(limit: int = 50) -> list:
    """Recent audit entries for mutations performed through this MCP
    server (newest first)."""
    return _get_store().list_audit(limit=min(max(limit, 1), 500))


# ── mutating tools: dashboards ───────────────────────────────────────────


@mcp.tool()
@_audited("create_dashboard", target_type="dashboard", target_id_arg="slug")
async def create_dashboard(
    title: str,
    description: str = "",
    slug: Optional[str] = None,
    layout: Optional[dict] = None,
) -> dict:
    """Create a dashboard in draft status. Returns its id and public slug.
    slug: optional URL-safe identifier (random when omitted). layout:
    free-form options dict (e.g. {"theme": "dark"})."""
    try:
        return _get_store().create_dashboard(
            title=title, description=description, slug=slug, layout=layout
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
) -> dict:
    """Update a dashboard's title/description/layout (only provided fields
    change)."""
    dashboard = _get_store().update_dashboard(
        dashboard_id, title=title, description=description, layout=layout
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


# ── mutating tools: charts ───────────────────────────────────────────────


@mcp.tool()
@_audited("add_chart", target_type="chart")
async def add_chart(
    dashboard_id: int,
    kind: Literal["line", "bar", "pie", "kpi", "table", "choropleth"],
    title: str = "",
    data: Optional[dict] = None,
    spec: Optional[dict] = None,
    position: Optional[dict] = None,
) -> dict:
    """Add a chart to a dashboard. kind: line|bar|pie|kpi|table|choropleth.

    data envelopes:
    - line/bar/pie/table: {"columns": ["month", "cases"],
      "rows": [["Jan", 120], ...]}
    - kpi: {"value": 1234, "label": "Cases", "unit": "", "delta": "+12%"}
    - choropleth: {"values": {"SP": 120, "RJ": 90}, "region_level": "uf",
      "label": "cases"} (UF codes)
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
    kind: Optional[Literal["line", "bar", "pie", "kpi", "table", "choropleth"]] = None,
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
