"""Tweak flow: submit → EpidBot turn → dashboard updated → poll completes."""

import pytest

from epidash.api import routes_public
from epidash.config import Settings, set_settings
from epidash.epidbot_client import EpidBotError, build_tweak_message

from conftest import wait_for_tweak


@pytest.fixture()
def published_with_epidbot(store, settings, monkeypatch):
    """Published dashboard + EpidBot integration configured."""
    s = Settings(
        db_path=settings.db_path,
        base_url=settings.base_url,
        dns_rebinding_protection=False,
        epidbot_base_url="https://api.epidbot.test",
        epidbot_internal_secret="topsecret",
    )
    set_settings(s)
    # Re-apply the configured settings on the app's client-side singletons.
    routes_public._epidbot = None  # rebuilt lazily by tests that patch it
    d = store.create_dashboard("Chikungunya", description="br", slug="chik")
    store.add_chart(
        d["id"], "line", title="cases",
        data={"columns": ["month", "cases"], "rows": [["Jan", 10]]},
    )
    store.set_dashboard_status(d["id"], "published")
    return d


class FakeEpidBot:
    """Stands in for the EpidBot round-trip; can simulate agent mutations."""

    def __init__(self, store=None, response="Updated the chart.", error=None):
        self.store = store
        self.response = response
        self.error = error
        self.messages = []

    async def run_tweak(self, message):
        self.messages.append(message)
        if self.error:
            raise EpidBotError(self.error)
        if self.store is not None:
            # Simulate the EpidBot agent updating the dashboard via MCP.
            charts = self.store.list_charts(
                self.store.get_dashboard_by_slug("chik")["id"]
            )
            self.store.update_chart(
                charts[0]["id"],
                data={"columns": ["month", "cases"], "rows": [["Jan", 10], ["Fev", 99]]},
            )
        return {"content": self.response, "status": "completed"}


class TestTweakSubmission:
    async def test_requires_configuration(self, client, store, settings):
        store.create_dashboard("D", slug="d1")
        store.set_dashboard_status(
            store.get_dashboard_by_slug("d1")["id"], "published"
        )
        res = client.post("/api/dashboards/d1/tweak", json={"request": "make it red"})
        assert res.status_code == 503

    async def test_unknown_or_draft_dashboard_404(self, client, store, settings):
        store.create_dashboard("Draft", slug="draft1")
        assert client.post(
            "/api/dashboards/nothing/tweak", json={"request": "hello there"}
        ).status_code == 404
        assert client.post(
            "/api/dashboards/draft1/tweak", json={"request": "hello there"}
        ).status_code == 404

    async def test_validation(self, client, published_with_epidbot, monkeypatch):
        monkeypatch.setattr(routes_public, "_epidbot", FakeEpidBot())
        res = client.post("/api/dashboards/chik/tweak", json={"request": "no"})
        assert res.status_code == 422

    async def test_successful_tweak_updates_dashboard(self, client, published_with_epidbot, store, monkeypatch):
        fake = FakeEpidBot(store=store, response="Added Fev data point.")
        monkeypatch.setattr(routes_public, "_epidbot", fake)

        res = client.post(
            "/api/dashboards/chik/tweak", json={"request": "add February data"}
        )
        assert res.status_code == 202
        tweak_id = res.json()["tweak_id"]

        tweak = wait_for_tweak(client, tweak_id)
        assert tweak["status"] == "completed"
        assert tweak["response"] == "Added Fev data point."

        # The simulated agent's MCP mutation is visible publicly.
        detail = client.get("/api/dashboards/chik").json()
        assert detail["charts"][0]["data"]["rows"] == [["Jan", 10], ["Fev", 99]]

        # The prompt sent to EpidBot carries dashboard context + request.
        message = fake.messages[0]
        assert f"dashboard_id={published_with_epidbot['id']}" in message
        assert "add February data" in message
        assert "Current charts" in message

    async def test_failed_epidbot_round_trip_marks_tweak_failed(self, client, published_with_epidbot, monkeypatch):
        fake = FakeEpidBot(error="EpidBot chat job failed: boom")
        monkeypatch.setattr(routes_public, "_epidbot", fake)

        res = client.post(
            "/api/dashboards/chik/tweak", json={"request": "please change everything"}
        )
        tweak = wait_for_tweak(client, res.json()["tweak_id"])
        assert tweak["status"] == "failed"
        assert "boom" in tweak["error"]

    async def test_tweak_endpoint_rate_limit(self, client, published_with_epidbot, monkeypatch):
        monkeypatch.setattr(routes_public, "_epidbot", FakeEpidBot())
        routes_public._tweak_windows.clear()
        # The default budget (20/hour) is enforced per slug.
        for i in range(20):
            res = client.post(
                "/api/dashboards/chik/tweak", json={"request": f"tweak number {i}"}
            )
            assert res.status_code == 202
        res = client.post("/api/dashboards/chik/tweak", json={"request": "one too many"})
        assert res.status_code == 429
        routes_public._tweak_windows.clear()

    async def test_tweak_unknown_id_404(self, client):
        assert client.get("/api/tweaks/does-not-exist").status_code == 404


class TestTweakMessage:
    async def test_build_tweak_message_contents(self, store):
        d = store.create_dashboard("Zika", slug="zika")
        store.add_chart(
            d["id"], "choropleth", title="map",
            data={"values": {"SP": 1, "RJ": 2}, "region_level": "uf"},
        )
        charts = store.list_charts(d["id"])
        msg = build_tweak_message(d, charts, "only show the Southeast")
        assert "dashboard_id=" in msg
        assert "slug=zika" in msg
        assert "only show the Southeast" in msg
        assert "choropleth" in msg
        assert "MCP tools" in msg
