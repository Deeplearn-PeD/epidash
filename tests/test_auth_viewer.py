"""Viewer identity, visibility gating, share tokens, like dedupe."""

import pytest

from epidash import routes_auth
from epidash.config import Settings, set_settings
from epidash.main import create_app
from epidash.routes_auth import (
    make_session_cookie,
    parse_session_cookie,
)
from epidash.store import Store
from fastapi.testclient import TestClient

TEST_SECRET = "unit-test-session-secret"


def make_client(settings: Settings, store: Store) -> TestClient:
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
    app = create_app(store, run_mcp_lifespan=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def viewer_cookie(store):
    viewer = store.upsert_viewer(sub="user-1", name="fccoelho", email="f@x.test")
    cookie = make_session_cookie(viewer["id"], TEST_SECRET)
    return viewer, cookie


@pytest.fixture()
def published(store):
    d = store.create_dashboard("Pub", slug="pub")
    store.add_chart(d["id"], "kpi", title="t", data={"value": 1})
    store.set_dashboard_status(d["id"], "published")
    return d


class TestSessionCookie:
    def test_round_trip(self):
        cookie = make_session_cookie(42, TEST_SECRET)
        assert parse_session_cookie(cookie, TEST_SECRET) == 42

    def test_wrong_secret_rejected(self):
        cookie = make_session_cookie(42, TEST_SECRET)
        assert parse_session_cookie(cookie, "other") is None

    def test_tampered_payload_rejected(self):
        cookie = make_session_cookie(42, TEST_SECRET)
        bad = cookie.replace("42.", "43.", 1)
        assert parse_session_cookie(bad, TEST_SECRET) is None

    def test_garbage_rejected(self):
        assert parse_session_cookie("not-a-cookie", TEST_SECRET) is None


class TestVisibility:
    """public / unlisted / restricted × anonymous / viewer / share token."""

    def _setup(self, store, visibility):
        d = store.create_dashboard("D", slug=f"d-{visibility}")
        store.set_dashboard_visibility(d["id"], visibility)
        store.set_dashboard_status(d["id"], "published")
        return d

    def test_public_anonymous_ok(self, client, store):
        self._setup(store, "public")
        assert client.get("/api/dashboards/d-public").status_code == 200

    def test_unlisted_reachable_but_hidden_from_gallery(self, client, store):
        self._setup(store, "unlisted")
        assert client.get("/api/dashboards/d-unlisted").status_code == 200
        assert client.get("/api/dashboards").json() == []

    def test_restricted_anonymous_403(self, client, store):
        self._setup(store, "restricted")
        res = client.get("/api/dashboards/d-restricted")
        assert res.status_code == 403
        assert "Login required" in res.json()["detail"]

    def test_restricted_viewer_ok(
        self, oidc_client, oidc_settings, store, viewer_cookie
    ):
        self._setup(store, "restricted")
        _, cookie = viewer_cookie
        res = oidc_client.get(
            "/api/dashboards/d-restricted", cookies={"epidash_session": cookie}
        )
        assert res.status_code == 200

    def test_restricted_share_token_ok(self, client, store):
        d = self._setup(store, "restricted")
        store.create_share_token(
            d["id"], "st_valid",
            expires_at="2099-01-01T00:00:00Z", created_by="epidbot",
        )
        assert client.get("/api/dashboards/d-restricted?token=st_valid").status_code == 200

    def test_restricted_expired_token_403(self, client, store):
        d = self._setup(store, "restricted")
        store.create_share_token(
            d["id"], "st_old", expires_at="2000-01-01T00:00:00Z"
        )
        assert client.get("/api/dashboards/d-restricted?token=st_old").status_code == 403

    def test_gallery_lists_public_only(self, client, store):
        for v in ("public", "unlisted", "restricted"):
            self._setup(store, v)
        slugs = [d["slug"] for d in client.get("/api/dashboards").json()]
        assert slugs == ["d-public"]


class TestViewerLikes:
    """Logged-in likes dedupe server-side; anonymous behave as before."""

    def test_dedupe_for_viewer(
        self, oidc_client, oidc_settings, store, viewer_cookie
    ):
        viewer, cookie = viewer_cookie
        d = store.create_dashboard("D", slug="liked")
        store.set_dashboard_status(d["id"], "published")
        headers = {"epidash_session": cookie}

        r1 = oidc_client.post("/api/dashboards/liked/like", cookies=headers)
        assert r1.json()["likes"] == 1
        # duplicate like is idempotent
        r2 = oidc_client.post("/api/dashboards/liked/like", cookies=headers)
        assert r2.json()["likes"] == 1
        assert oidc_client.get("/api/dashboards/liked", cookies=headers).json()[
            "viewer_liked"
        ] is True

        r3 = oidc_client.delete("/api/dashboards/liked/like", cookies=headers)
        assert r3.json()["likes"] == 0
        # duplicate unlike is idempotent
        r4 = oidc_client.delete("/api/dashboards/liked/like", cookies=headers)
        assert r4.json()["likes"] == 0


class TestAuthEndpoints:
    def test_me_anonymous_no_oidc(self, client):
        res = client.get("/auth/me")
        assert res.json() == {"viewer": None, "login_enabled": False}

    def test_me_anonymous_with_oidc(self, oidc_client):
        res = oidc_client.get("/auth/me")
        assert res.json() == {"viewer": None, "login_enabled": True}

    def test_me_logged_in(self, oidc_client, store, viewer_cookie):
        viewer, cookie = viewer_cookie
        res = oidc_client.get("/auth/me", cookies={"epidash_session": cookie})
        assert res.json()["viewer"]["name"] == "fccoelho"

    def test_login_without_config_503(self, client):
        assert client.get("/auth/login", follow_redirects=False).status_code == 503

    def test_callback_bad_state_400(self, oidc_client):
        res = oidc_client.get(
            "/auth/callback",
            params={"code": "c", "state": "wrong"},
            cookies={"epidash_oauth_state": "expected"},
            follow_redirects=False,
        )
        assert res.status_code == 400


class TestCallbackEpidbotGate:
    """OIDC callback only issues sessions to existing EpidBot users."""

    @pytest.fixture()
    def gate_settings(self, settings):
        s = Settings(
            db_path=settings.db_path,
            base_url=settings.base_url,
            dns_rebinding_protection=False,
            epidbot_base_url="https://epidbot.test",
            epidbot_internal_secret="shared",
            oidc_issuer="https://auth.test/realms/epidbot",
            oidc_client_id="epidash",
            oidc_client_secret="client-secret",
            session_secret=TEST_SECRET,
        )
        set_settings(s)
        yield s
        set_settings(None)

    @pytest.fixture()
    def gate_client(self, gate_settings, store):
        app = create_app(store, run_mcp_lifespan=False)
        with TestClient(app) as c:
            yield c

    @pytest.fixture()
    def mock_oidc(self, monkeypatch):
        async def fake_exchange(code):
            return {
                "sub": "kc-sub-1",
                "email": "f@x.test",
                "preferred_username": "fccoelho",
            }

        monkeypatch.setattr(routes_auth, "exchange_code", fake_exchange)

    def _callback(self, client):
        client.cookies.set("epidash_oauth_state", "st123")
        return client.get(
            "/auth/callback",
            params={"code": "c", "state": "st123"},
            follow_redirects=False,
        )

    def test_validation_on_when_oidc_and_epidbot_configured(
        self, gate_settings
    ):
        assert gate_settings.epidbot_user_validation is True

    def test_validation_off_when_flag_disabled(self, gate_settings):
        gate_settings.require_epidbot_user = False
        assert gate_settings.epidbot_user_validation is False

    def test_validation_off_without_epidbot(self, oidc_settings):
        assert oidc_settings.epidbot_user_validation is False

    def test_epidbot_user_gets_session(
        self, gate_client, gate_settings, store, mock_oidc, monkeypatch
    ):
        async def probe(sub, email):
            return True

        monkeypatch.setattr(routes_auth, "epidbot_user_exists", probe)
        res = self._callback(gate_client)
        assert res.status_code == 307
        viewer = store.get_viewer_by_sub("kc-sub-1")
        assert viewer is not None and viewer["email"] == "f@x.test"

    def test_non_epidbot_user_rejected(
        self, gate_client, store, mock_oidc, monkeypatch
    ):
        async def probe(sub, email):
            return False

        monkeypatch.setattr(routes_auth, "epidbot_user_exists", probe)
        res = self._callback(gate_client)
        assert res.status_code == 403
        assert "Only EpidBot users" in res.json()["detail"]
        assert store.get_viewer_by_sub("kc-sub-1") is None

    def test_epidbot_outage_fails_closed(
        self, gate_client, store, mock_oidc, monkeypatch
    ):
        async def probe(sub, email):
            return False

        monkeypatch.setattr(routes_auth, "epidbot_user_exists", probe)
        assert self._callback(gate_client).status_code == 403

    def test_gate_skipped_when_epidbot_unconfigured(
        self, oidc_client, store, mock_oidc, monkeypatch
    ):
        async def unexpected(sub, email):
            raise AssertionError("probe must not run without EpidBot")

        monkeypatch.setattr(routes_auth, "epidbot_user_exists", unexpected)
        oidc_client.cookies.set("epidash_oauth_state", "st123")
        res = oidc_client.get(
            "/auth/callback",
            params={"code": "c", "state": "st123"},
            follow_redirects=False,
        )
        assert res.status_code == 307


class TestTweakAttribution:
    async def test_question_records_viewer_name(
        self, client, store, viewer_cookie
    ):
        viewer, _ = viewer_cookie
        d = store.create_dashboard("D", slug="att")
        t = store.create_tweak(
            d["id"], "why?", kind="question", viewer_id=viewer["id"]
        )
        res = client.get(f"/api/tweaks/{t['id']}")
        assert res.json()["viewer"] == "fccoelho"

    def test_anonymous_tweak_has_no_viewer(self, client, store):
        d = store.create_dashboard("D", slug="att2")
        t = store.create_tweak(d["id"], "fix", kind="tweak")
        assert client.get(f"/api/tweaks/{t['id']}").json()["viewer"] is None
