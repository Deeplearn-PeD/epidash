"""MCP auth middleware: dk_ key enforcement on the MCP endpoint.

A stub inner ASGI app isolates the middleware: a request that passes auth
gets 200 from the stub; everything else must be rejected with 401 before
reaching it. The real streamable app is used for the rejection paths.
"""

import httpx
import pytest

from epidash.mcpsrv.auth import McpAuthMiddleware
from epidash.mcpsrv import server as mcp_server


async def _stub_app(scope, receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.fixture()
def app_with_stub(store):
    mcp_server.set_store(store)
    return McpAuthMiddleware(_stub_app, store=store)


@pytest.fixture()
def app_with_real(store):
    mcp_server.set_store(store)
    return McpAuthMiddleware(mcp_server.mcp.streamable_http_app(), store=store)


async def _post(app, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers=headers or {},
        )


class TestMcpAuthMiddleware:
    """Only valid dk_ control keys get through to the wrapped app."""

    async def test_missing_key_rejected_401(self, app_with_stub):
        res = await _post(app_with_stub)
        assert res.status_code == 401
        assert "X-API-Key" in res.json()["error"]

    async def test_invalid_key_rejected_401(self, app_with_stub, store):
        store.add_key("dk_realkey", name="k")
        res = await _post(app_with_stub, {"X-API-Key": "dk_fakekey"})
        assert res.status_code == 401
        assert "Invalid or revoked" in res.json()["error"]

    async def test_non_dk_key_rejected_401(self, app_with_stub, store):
        store.add_key("dk_realkey", name="k")
        res = await _post(app_with_stub, {"X-API-Key": "ek_userkey"})
        assert res.status_code == 401

    async def test_valid_key_reaches_inner_app(self, app_with_stub, store):
        store.add_key("dk_goodkey", name="k")
        res = await _post(app_with_stub, {"X-API-Key": "dk_goodkey"})
        assert res.status_code == 200
        assert res.text == "ok"

    async def test_bearer_header_supported(self, app_with_stub, store):
        store.add_key("dk_bearerkey", name="k")
        res = await _post(app_with_stub, {"Authorization": "Bearer dk_bearerkey"})
        assert res.status_code == 200
        assert res.text == "ok"

    async def test_revoked_key_rejected(self, app_with_stub, store):
        meta = store.add_key("dk_gone", name="k")
        store.revoke_key(meta["id"])
        res = await _post(app_with_stub, {"X-API-Key": "dk_gone"})
        assert res.status_code == 401

    async def test_rejection_happens_before_real_mcp_app(self, app_with_real):
        """On the real streamable app, bad keys are 401 without needing a
        running session manager (middleware short-circuits first)."""
        res = await _post(app_with_real)
        assert res.status_code == 401
