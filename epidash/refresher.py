"""Background refresher for chart data sources.

Charts attached to a data source (url_json / url_csv) periodically re-fetch
their data envelope. Successful fetches replace the chart's data and bump
the dashboard's updated_at so open viewers pick up the change on their next
poll; failures are recorded on the source and never touch chart data.

The loop runs inside the app lifespan (see main.py) and scans for due
sources every SCAN_INTERVAL_S seconds.
"""

import asyncio
import csv
import io
import json
from typing import Any

import httpx

from epidash.logging import get_logger
from epidash.store import Store, validate_chart_data

logger = get_logger(__name__)

SCAN_INTERVAL_S = 30
FETCH_TIMEOUT_S = 20

_stop: asyncio.Event | None = None
_task: asyncio.Task | None = None


def start(store: Store) -> None:
    """Start the background refresh loop (no-op if already running)."""
    global _stop, _task
    if _task is not None and not _task.done():
        return
    _stop = asyncio.Event()
    _task = asyncio.create_task(_loop(store))
    logger.info("Data source refresher started")


async def stop() -> None:
    """Signal the loop to stop and wait for the current scan to finish."""
    global _task
    if _stop is not None:
        _stop.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
    _task = None
    logger.info("Data source refresher stopped")


async def _loop(store: Store) -> None:
    stop = _stop
    assert stop is not None
    while not stop.is_set():
        try:
            await refresh_due(store)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the loop must survive bugs
            logger.error("Data source scan failed: %s", e)
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


async def refresh_due(store: Store, client: httpx.AsyncClient | None = None) -> None:
    """Refresh every source whose interval has elapsed."""
    for source in store.due_data_sources():
        try:
            result = await refresh_source(store, source["chart_id"], client=client)
            logger.info(
                "Refreshed data source for chart %s: %s",
                source["chart_id"],
                result["status"],
            )
        except Exception as e:  # noqa: BLE001 — one bad source must not block others
            logger.error(
                "Data source refresh crashed for chart %s: %s", source["chart_id"], e
            )


async def refresh_source(
    store: Store, chart_id: int, client: httpx.AsyncClient | None = None
) -> dict:
    """Fetch a source once and, on success, replace the chart's data.

    Returns the recorded outcome {status, error, rows?}; never raises for
    data problems (those are recorded as the source's error status).
    """
    source = store.get_data_source(chart_id)
    chart = store.get_chart(chart_id)
    if source is None or chart is None:
        return {"status": "error", "error": "chart or data source not found"}

    try:
        data = await _fetch_envelope(source, chart["kind"], client)
        validate_chart_data(chart["kind"], data)
    except Exception as e:  # noqa: BLE001 — recorded, not raised
        store.record_data_source_refresh(chart_id, "error", error=str(e))
        return {"status": "error", "error": str(e)}

    store.update_chart_data(chart_id, data)
    store.record_data_source_refresh(chart_id, "ok")
    return {"status": "ok"}


async def _fetch_envelope(
    source: dict, chart_kind: str, client: httpx.AsyncClient | None
) -> dict[str, Any]:
    kind, spec = source["kind"], source["spec"]
    url = spec.get("url", "")
    if not url:
        raise ValueError("data source spec has no 'url'")

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=True)
    try:
        response = await client.get(url)
        response.raise_for_status()
        body = response.text
    finally:
        if own_client:
            await client.aclose()

    if kind == "url_json":
        data = json.loads(body)
        path = spec.get("path")
        if path:
            for key in path.split("."):
                data = data[key]  # KeyError/TypeError -> recorded as error
        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object (chart envelope)")
        return data

    # url_csv: header row + rows; numeric cells converted when possible
    rows = list(csv.reader(io.StringIO(body)))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        raise ValueError("CSV payload needs a header row and at least one data row")
    columns = [c.strip() for c in rows[0]]
    parsed = [[_coerce(cell) for cell in r] for r in rows[1:]]
    data = {"columns": columns, "rows": parsed}
    if chart_kind in ("kpi", "choropleth"):
        raise ValueError(
            f"url_csv sources cannot feed '{chart_kind}' charts"
        )
    return data


def _coerce(cell: str) -> Any:
    cell = cell.strip()
    try:
        return int(cell)
    except ValueError:
        pass
    try:
        return float(cell)
    except ValueError:
        return cell
