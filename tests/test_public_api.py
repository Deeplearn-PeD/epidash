"""Public API: health, published dashboard exposure, view counting."""

import pytest

from epidash.store import Store


@pytest.fixture()
def published(store: Store):
    """A published dashboard with one chart of each simple kind."""
    d = store.create_dashboard("Dengue Brasil", description="weekly cases", slug="dengue")
    store.add_chart(
        d["id"], "line", title="Cases",
        data={"columns": ["month", "cases"], "rows": [["Jan", 100], ["Fev", 150]]},
        position={"order": 1},
    )
    store.add_chart(d["id"], "kpi", title="Total", data={"value": 250, "label": "cases"})
    store.set_dashboard_status(d["id"], "published")
    return d


class TestHealthz:
    async def test_healthz(self, client):
        res = client.get("/healthz")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}


class TestPublishedDashboardExposure:
    """Drafts stay hidden; published dashboards are fully served."""

    async def test_draft_hidden(self, client, store):
        store.create_dashboard("Secret", slug="secret")
        assert client.get("/api/dashboards").json() == []
        assert client.get("/api/dashboards/secret").status_code == 404

    async def test_unknown_slug_404(self, client):
        assert client.get("/api/dashboards/nope").status_code == 404

    async def test_published_listing_and_detail(self, client, published):
        listing = client.get("/api/dashboards").json()
        assert [d["slug"] for d in listing] == ["dengue"]

        detail = client.get("/api/dashboards/dengue")
        assert detail.status_code == 200
        body = detail.json()
        assert body["title"] == "Dengue Brasil"
        assert body["layout"] == {}
        kinds = {c["kind"] for c in body["charts"]}
        assert kinds == {"line", "kpi"}
        line = next(c for c in body["charts"] if c["kind"] == "line")
        assert line["data"]["rows"][0] == ["Jan", 100]
        # internal bookkeeping is not exposed
        assert "status" not in body and "id" not in body

    async def test_view_count_increments(self, client, published, store):
        client.get("/api/dashboards/dengue")
        client.get("/api/dashboards/dengue")
        assert store.get_view_count(published["id"]) == 2

    async def test_unpublish_removes_public_access(self, client, published, store):
        store.set_dashboard_status(published["id"], "draft")
        assert client.get("/api/dashboards/dengue").status_code == 404
