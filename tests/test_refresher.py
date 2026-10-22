"""Data source refresher: envelope extraction, validation, status recording."""

import json

import httpx
import pytest

from epidash import refresher
from epidash.store import Store


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _seed(store: Store, kind="url_json", url="https://x.test/data"):
    d = store.create_dashboard("D")
    chart = store.add_chart(
        d["id"], "line", title="t",
        data={"columns": ["m", "v"], "rows": [["Jan", 0]]},
    )
    source = store.set_data_source(chart["id"], kind, {"url": url})
    return chart, source


class TestRefreshSource:
    async def test_url_json_success_updates_chart(self, store):
        chart, _ = await _seed(store)
        payload = {"columns": ["m", "v"], "rows": [["Fev", 42]]}

        async def handler(request):
            return httpx.Response(200, json=payload)

        out = await refresher.refresh_source(store, chart["id"], client=_client(handler))
        assert out["status"] == "ok"
        assert store.get_chart(chart["id"])["data"]["rows"] == [["Fev", 42]]
        src = store.get_data_source(chart["id"])
        assert src["last_status"] == "ok" and src["last_error"] is None

    async def test_url_json_with_nested_path(self, store):
        chart, _ = await _seed(store)
        envelope = {"columns": ["m", "v"], "rows": [["Mar", 7]]}

        async def handler(request):
            return httpx.Response(200, json={"result": {"week": envelope}})

        store.set_data_source(
            chart["id"], "url_json", {"url": "https://x.test/data", "path": "result.week"}
        )
        out = await refresher.refresh_source(store, chart["id"], client=_client(handler))
        assert out["status"] == "ok"
        assert store.get_chart(chart["id"])["data"]["rows"] == [["Mar", 7]]

    async def test_http_error_recorded_data_kept(self, store):
        chart, _ = await _seed(store)

        async def handler(request):
            return httpx.Response(500, text="boom")

        out = await refresher.refresh_source(store, chart["id"], client=_client(handler))
        assert out["status"] == "error" and "500" in out["error"]
        src = store.get_data_source(chart["id"])
        assert src["last_status"] == "error"
        # chart data untouched
        assert store.get_chart(chart["id"])["data"]["rows"] == [["Jan", 0]]

    async def test_invalid_envelope_rejected(self, store):
        chart, _ = await _seed(store)

        async def handler(request):
            return httpx.Response(200, json={"unexpected": "shape"})

        out = await refresher.refresh_source(store, chart["id"], client=_client(handler))
        assert out["status"] == "error" and "columns" in out["error"]

    async def test_url_csv_builds_envelope_with_coercion(self, store):
        d = store.create_dashboard("D")
        chart = store.add_chart(d["id"], "table", title="t")
        store.set_data_source(chart["id"], "url_csv", {"url": "https://x.test/f.csv"})
        csv_body = "month,cases\nJan,1200\nFev,1850.5\nMar,done\n"

        async def handler(request):
            return httpx.Response(200, text=csv_body)

        out = await refresher.refresh_source(store, chart["id"], client=_client(handler))
        assert out["status"] == "ok"
        rows = store.get_chart(chart["id"])["data"]["rows"]
        assert rows == [["Jan", 1200], ["Fev", 1850.5], ["Mar", "done"]]

    async def test_url_csv_rejected_for_kpi(self, store):
        d = store.create_dashboard("D")
        chart = store.add_chart(d["id"], "kpi", title="t", data={"value": 1})
        store.set_data_source(chart["id"], "url_csv", {"url": "https://x.test/f.csv"})

        async def handler(request):
            return httpx.Response(200, text="a,b\n1,2\n")

        out = await refresher.refresh_source(store, chart["id"], client=_client(handler))
        assert out["status"] == "error" and "url_csv" in out["error"]

    async def test_refresh_source_without_source(self, store):
        out = await refresher.refresh_source(store, 999)
        assert out["status"] == "error"


class TestRefreshDue:
    async def test_only_due_sources_refreshed(self, store):
        due_chart, _ = await _seed(store, url="https://x.test/due")
        # a fresh source whose interval has not elapsed
        other = store.add_chart(store.create_dashboard("E")["id"], "line", title="o")
        store.set_data_source(other["id"], "url_json", {"url": "https://x.test/fresh"}, 3600)
        store.record_data_source_refresh(other["id"], "ok")

        seen = []

        async def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"columns": ["m", "v"], "rows": [["Jan", 1]]})

        await refresher.refresh_due(store, client=_client(handler))
        assert seen == ["https://x.test/due"]
