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


class TestStaticCaching:
    """Static assets must revalidate so deploys reach viewers immediately."""

    async def test_static_assets_send_no_cache(self, client):
        for path in ("/", "/app.js", "/style.css"):
            res = client.get(path)
            assert res.status_code == 200
            assert res.headers["cache-control"] == "no-cache"

    async def test_api_has_no_static_cache_header(self, client):
        res = client.get("/api/dashboards")
        assert res.headers.get("cache-control") != "no-cache"


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

    async def test_listing_and_detail_expose_created_at_and_language(self, client, store):
        d = store.create_dashboard(
            "Casos de dengue", description="notificações semanais",
            slug="lang", language="pt-BR",
        )
        store.set_dashboard_status(d["id"], "published")
        listing = client.get("/api/dashboards").json()[0]
        assert listing["language"] == "pt-BR"
        assert listing["created_at"]
        detail = client.get("/api/dashboards/lang").json()
        assert detail["language"] == "pt-BR"
        assert detail["created_at"] == listing["created_at"]

    async def test_language_defaults_to_empty_when_not_set(self, client, published):
        assert client.get("/api/dashboards").json()[0]["language"] == ""


class TestLikes:
    """Viewer like/unlike with persistent counts."""

    async def test_like_increments(self, client, published, store):
        res = client.post("/api/dashboards/dengue/like")
        assert res.status_code == 200
        assert res.json() == {"slug": "dengue", "likes": 1}
        assert store.get_likes(published["id"]) == 1

    async def test_unlike_decrements(self, client, published, store):
        client.post("/api/dashboards/dengue/like")
        client.post("/api/dashboards/dengue/like")
        res = client.delete("/api/dashboards/dengue/like")
        assert res.json()["likes"] == 1
        assert store.get_likes(published["id"]) == 1

    async def test_detail_includes_likes(self, client, published):
        client.post("/api/dashboards/dengue/like")
        assert client.get("/api/dashboards/dengue").json()["likes"] == 1

    async def test_like_draft_404(self, client, store):
        store.create_dashboard("Secret", slug="secret")
        assert client.post("/api/dashboards/secret/like").status_code == 404
        assert client.delete("/api/dashboards/secret/like").status_code == 404

    async def test_like_unknown_404(self, client):
        assert client.post("/api/dashboards/nope/like").status_code == 404


class TestCreatorExposure:
    """The creator recorded via MCP shows up publicly."""

    async def test_detail_and_listing_include_created_by(self, client, store):
        d = store.create_dashboard("Zika Norte", slug="zika", created_by="EpidBot")
        store.set_dashboard_status(d["id"], "published")
        assert client.get("/api/dashboards/zika").json()["created_by"] == "EpidBot"
        assert client.get("/api/dashboards").json()[0]["created_by"] == "EpidBot"


class TestExport:
    """GET /export downloads the full dashboard definition."""

    async def test_export_payload_and_headers(self, client, published):
        res = client.get("/api/dashboards/dengue/export")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/json")
        assert res.headers["content-disposition"] == 'attachment; filename="dengue.json"'
        body = res.json()
        assert body["app"] == "epidash"
        assert body["slug"] == "dengue"
        assert body["title"] == "Dengue Brasil"
        assert body["created_by"] == ""
        kinds = {c["kind"] for c in body["charts"]}
        assert kinds == {"line", "kpi"}
        line = next(c for c in body["charts"] if c["kind"] == "line")
        assert line["data"]["rows"][0] == ["Jan", 100]

    async def test_markdown_kind_served(self, client, store):
        d = store.create_dashboard("Doc", slug="doc")
        store.add_chart(d["id"], "markdown", data={"text": "## About\nAll good"})
        store.set_dashboard_status(d["id"], "published")
        body = client.get("/api/dashboards/doc").json()
        assert body["charts"][0]["kind"] == "markdown"
        assert "About" in body["charts"][0]["data"]["text"]

    async def test_export_draft_404(self, client, store):
        store.create_dashboard("Secret", slug="secret")
        assert client.get("/api/dashboards/secret/export").status_code == 404
