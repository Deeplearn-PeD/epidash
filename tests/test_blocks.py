"""Owner text-block endpoints: markdown blocks with LaTeX between charts."""

import pytest
from fastapi.testclient import TestClient

from epidash.config import Settings, set_settings
from epidash.main import create_app
from epidash.routes_auth import make_session_cookie
from epidash.store import MARKDOWN_MAX_CHARS

TEST_SECRET = "unit-test-session-secret"


def make_client(settings: Settings, store) -> TestClient:
    app = create_app(store, run_mcp_lifespan=False)
    with TestClient(app) as c:
        return c


@pytest.fixture()
def oidc_settings(store, settings):
    s = Settings(
        db_path=settings.db_path,
        base_url=settings.base_url,
        dns_rebinding_protection=False,
        epidbot_base_url="",
        epidbot_internal_secret="",
        oidc_issuer="https://auth.test/realms/master",
        oidc_client_id="epidash",
        oidc_client_secret="client-secret",
        session_secret=TEST_SECRET,
    )
    set_settings(s)
    yield s
    set_settings(None)


@pytest.fixture()
def oidc_client(oidc_settings, store):
    return make_client(oidc_settings, store)


@pytest.fixture()
def owner(store):
    return store.upsert_viewer(sub="owner-1", name="alice", email="alice@x.test")


def cookie_for(viewer_id) -> dict:
    return {"epidash_session": make_session_cookie(viewer_id, TEST_SECRET)}


@pytest.fixture()
def dashboard(store):
    """Published dashboard with two charts, owned by viewer owner-1."""
    d = store.create_dashboard(
        "Dengue", description="cases", slug="dengue",
        created_by="alice", owner_sub="owner-1",
    )
    store.add_chart(d["id"], "kpi", title="cases", data={"value": 1},
                    position={"order": 0})
    store.add_chart(d["id"], "kpi", title="deaths", data={"value": 2},
                    position={"order": 1})
    store.set_dashboard_status(d["id"], "published")
    return d


def ordered_slugs(store, dashboard) -> list[str]:
    charts = sorted(
        store.list_charts(dashboard["id"]),
        key=lambda c: c["position"].get("order", c["id"]),
    )
    return [c["title"] or f"md:{c['id']}" for c in charts]


class TestCanEditFlag:
    """The detail payload tells the UI whether the viewer may edit."""

    def test_owner_by_sub(self, oidc_client, dashboard, owner):
        r = oidc_client.get("/api/dashboards/dengue", cookies=cookie_for(owner["id"]))
        assert r.status_code == 200
        assert r.json()["can_edit"] is True

    def test_owner_by_created_by_name(self, oidc_client, store):
        d = store.create_dashboard("T", slug="byname", created_by="bob")
        store.set_dashboard_status(d["id"], "published")
        bob = store.upsert_viewer(sub="bob-1", name="bob", email="bob@x.test")
        r = oidc_client.get("/api/dashboards/byname", cookies=cookie_for(bob["id"]))
        assert r.json()["can_edit"] is True

    def test_owner_by_created_by_email(self, oidc_client, store):
        d = store.create_dashboard("T", slug="byemail", created_by="carol@x.test")
        store.set_dashboard_status(d["id"], "published")
        carol = store.upsert_viewer(sub="carol-1", name="c", email="carol@x.test")
        r = oidc_client.get("/api/dashboards/byemail", cookies=cookie_for(carol["id"]))
        assert r.json()["can_edit"] is True

    def test_stranger_not_owner(self, oidc_client, store, dashboard):
        stranger = store.upsert_viewer(sub="s-1", name="mallory", email="m@x.test")
        r = oidc_client.get("/api/dashboards/dengue", cookies=cookie_for(stranger["id"]))
        assert r.json()["can_edit"] is False

    def test_anonymous_not_owner(self, oidc_client, dashboard):
        assert oidc_client.get("/api/dashboards/dengue").json()["can_edit"] is False


class TestBlockCreate:
    def test_anonymous_401(self, client, dashboard):
        r = client.post("/api/dashboards/dengue/blocks", json={"text": "hi"})
        assert r.status_code == 401

    def test_non_owner_403(self, oidc_client, store, dashboard):
        stranger = store.upsert_viewer(sub="s-1", name="mallory", email="m@x.test")
        r = oidc_client.post(
            "/api/dashboards/dengue/blocks",
            json={"text": "hi"}, cookies=cookie_for(stranger["id"]),
        )
        assert r.status_code == 403

    def test_create_at_end_and_renumber(self, oidc_client, store, dashboard, owner):
        r = oidc_client.post(
            "/api/dashboards/dengue/blocks",
            json={"text": "Notes with $e=mc^2$"},
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 201
        body = r.json()
        assert body["kind"] == "markdown"
        assert body["data"]["text"] == "Notes with $e=mc^2$"
        assert body["position"]["width"] == "full"
        assert ordered_slugs(store, dashboard)[2].startswith("md:")
        charts = store.list_charts(dashboard["id"])
        assert sorted(c["position"]["order"] for c in charts) == [0, 1, 2]

    def test_create_at_top(self, oidc_client, store, dashboard, owner):
        r = oidc_client.post(
            "/api/dashboards/dengue/blocks",
            json={"text": "intro", "top": True}, cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 201
        assert ordered_slugs(store, dashboard) == ["md:%d" % r.json()["id"], "cases", "deaths"]

    def test_create_after_chart(self, oidc_client, store, dashboard, owner):
        anchor = next(
            c for c in store.list_charts(dashboard["id"]) if c["title"] == "cases"
        )
        r = oidc_client.post(
            "/api/dashboards/dengue/blocks",
            json={"text": "mid", "after": anchor["id"]},
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 201
        assert ordered_slugs(store, dashboard)[1].startswith("md:")
        charts = store.list_charts(dashboard["id"])
        assert sorted(c["position"]["order"] for c in charts) == [0, 1, 2]

    def test_create_after_unknown_chart_404(self, oidc_client, dashboard, owner):
        r = oidc_client.post(
            "/api/dashboards/dengue/blocks",
            json={"text": "x", "after": 99999}, cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 404

    def test_create_on_draft_404(self, oidc_client, store, owner):
        store.create_dashboard("Secret", slug="secret")
        r = oidc_client.post(
            "/api/dashboards/secret/blocks", json={"text": "x"},
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 404

    def test_blank_text_422(self, oidc_client, dashboard, owner):
        r = oidc_client.post(
            "/api/dashboards/dengue/blocks", json={"text": "   "},
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 422

    def test_oversized_text_422(self, oidc_client, dashboard, owner):
        r = oidc_client.post(
            "/api/dashboards/dengue/blocks", json={"text": "x" * (MARKDOWN_MAX_CHARS + 1)},
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 422

    def test_restricted_owner_allowed(self, oidc_client, store, owner):
        d = store.create_dashboard("R", slug="restr", owner_sub="owner-1")
        store.set_dashboard_status(d["id"], "published")
        store.set_dashboard_visibility(d["id"], "restricted")
        r = oidc_client.post(
            "/api/dashboards/restr/blocks", json={"text": "ok"},
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 201


class TestBlockUpdateAndDelete:
    def _create_block(self, oidc_client, owner, text="block") -> dict:
        r = oidc_client.post(
            "/api/dashboards/dengue/blocks", json={"text": text},
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 201
        return r.json()

    def test_patch_text(self, oidc_client, store, dashboard, owner):
        block = self._create_block(oidc_client, owner)
        r = oidc_client.patch(
            f"/api/dashboards/dengue/blocks/{block['id']}",
            json={"text": "updated $\\alpha$"}, cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 200
        assert r.json()["data"]["text"] == "updated $\\alpha$"
        stored = store.get_chart(block["id"])
        assert stored["data"]["text"] == "updated $\\alpha$"

    def test_patch_move_to_top(self, oidc_client, store, dashboard, owner):
        block = self._create_block(oidc_client, owner)
        r = oidc_client.patch(
            f"/api/dashboards/dengue/blocks/{block['id']}",
            json={"top": True}, cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 200
        assert ordered_slugs(store, dashboard)[0].startswith("md:")
        charts = store.list_charts(dashboard["id"])
        assert sorted(c["position"]["order"] for c in charts) == [0, 1, 2]

    def test_patch_move_after_chart(self, oidc_client, store, dashboard, owner):
        block = self._create_block(oidc_client, owner)
        anchor = next(
            c for c in store.list_charts(dashboard["id"]) if c["title"] == "deaths"
        )
        r = oidc_client.patch(
            f"/api/dashboards/dengue/blocks/{block['id']}",
            json={"after": anchor["id"]}, cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 200
        assert ordered_slugs(store, dashboard)[2].startswith("md:")

    def test_patch_non_markdown_404(self, oidc_client, store, dashboard, owner):
        kpi = next(
            c for c in store.list_charts(dashboard["id"]) if c["kind"] == "kpi"
        )
        r = oidc_client.patch(
            f"/api/dashboards/dengue/blocks/{kpi['id']}",
            json={"text": "nope"}, cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 404

    def test_delete_block(self, oidc_client, store, dashboard, owner):
        block = self._create_block(oidc_client, owner)
        r = oidc_client.delete(
            f"/api/dashboards/dengue/blocks/{block['id']}",
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 200
        assert r.json() == {"slug": "dengue", "deleted": block["id"]}
        assert len(store.list_charts(dashboard["id"])) == 2
        # deleting again is a 404
        r = oidc_client.delete(
            f"/api/dashboards/dengue/blocks/{block['id']}",
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 404

    def test_delete_non_markdown_404(self, oidc_client, store, dashboard, owner):
        kpi = next(
            c for c in store.list_charts(dashboard["id"]) if c["kind"] == "kpi"
        )
        r = oidc_client.delete(
            f"/api/dashboards/dengue/blocks/{kpi['id']}",
            cookies=cookie_for(owner["id"]),
        )
        assert r.status_code == 404
