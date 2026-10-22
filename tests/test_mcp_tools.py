"""MCP tools: direct function tests + real protocol round-trip.

The tool functions are exercised directly (FastMCP's @tool decorator
returns the original function) and once through an in-memory MCP client
session to prove the wire-level surface works.
"""

import json
from contextlib import asynccontextmanager

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import (
    create_connected_server_and_client_session as make_session,
)

from epidash.mcpsrv import auth as mcp_auth
from epidash.mcpsrv import server as mcp_server


@asynccontextmanager
async def keyed_context(store):
    """Point the tools at the store and fake an authenticated dk_ key."""
    mcp_server.set_store(store)
    token = mcp_auth._current_key.set(
        {"key_id": 1, "name": "tests", "key_prefix": "dk_tests"}
    )
    try:
        yield
    finally:
        mcp_auth._current_key.reset(token)


@pytest.fixture()
def env(store, settings):
    return store, settings


class TestDashboardWorkflow:
    """create → add charts → publish round trip via MCP tools."""

    async def test_full_workflow(self, env):
        store, settings = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("Dengue RJ", description="weekly")
            assert d["status"] == "draft" and d["slug"]

            chart = await mcp_server.add_chart(
                d["id"], "line", title="cases",
                data={"columns": ["week", "cases"], "rows": [["2024-01", 10], ["2024-02", 15]]},
                position={"order": 1},
            )
            assert chart["dashboard_id"] == d["id"]

            got = await mcp_server.get_dashboard(d["id"])
            assert [c["id"] for c in got["charts"]] == [chart["id"]]

            published = await mcp_server.publish_dashboard(d["id"], confirm=True)
            assert published["status"] == "published"
            assert published["url"] == f"{settings.base_url}/#/d/{d['slug']}"

            stats = await mcp_server.get_view_stats(d["id"])
            assert stats["views"] == 0

    async def test_create_attributes_dashboard_to_key_name(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("Dengue RJ")
        assert d["created_by"] == "tests"

    async def test_create_author_overrides_key_name(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("Dengue RJ", author="fccoelho")
        assert d["created_by"] == "fccoelho"

    async def test_update_dashboard_can_correct_author(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("Dengue RJ")
            assert d["created_by"] == "tests"
            updated = await mcp_server.update_dashboard(d["id"], author="fccoelho")
        assert updated["created_by"] == "fccoelho"
        assert store.get_dashboard(d["id"])["created_by"] == "fccoelho"

    async def test_list_dashboards_and_audit_log(self, env):
        store, _ = env
        async with keyed_context(store):
            await mcp_server.create_dashboard("A", slug="a")
            await mcp_server.create_dashboard("B", slug="b")
            listed = await mcp_server.list_dashboards()
            assert {d["slug"] for d in listed} == {"a", "b"}

            audit = await mcp_server.get_audit_log()
            actions = [row["action"] for row in audit]
            assert actions.count("create_dashboard") == 2
            assert all(row["key_id"] == 1 for row in audit)

    async def test_update_dashboard_partial(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("Old", description="keep me")
            out = await mcp_server.update_dashboard(d["id"], title="New")
            assert out["title"] == "New"
            assert out["description"] == "keep me"

    async def test_missing_dashboard_raises_tool_error(self, env):
        store, _ = env
        async with keyed_context(store):
            with pytest.raises(ToolError, match="not found"):
                await mcp_server.get_dashboard(12345)
            with pytest.raises(ToolError, match="not found"):
                await mcp_server.publish_dashboard(12345, confirm=True)
            with pytest.raises(ToolError, match="not found"):
                await mcp_server.delete_dashboard(12345, confirm=True)

    async def test_delete_dashboard_removes_charts(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("Doomed", slug="doomed")
            chart = await mcp_server.add_chart(
                d["id"], "line",
                data={"columns": ["w", "c"], "rows": [["2024-01", 1]]},
            )
            deleted = await mcp_server.delete_dashboard(d["id"], confirm=True)
            assert deleted["id"] == d["id"]
            assert deleted["slug"] == "doomed"
            assert store.get_dashboard(d["id"]) is None
            assert store.get_chart(chart["id"]) is None
            assert store.list_charts(d["id"]) == []
            # The deletion is audited as 'ok'.
            audit = await mcp_server.get_audit_log()
            row = next(r for r in audit if r["action"] == "delete_dashboard")
            assert row["result"] == "ok"
            assert row["target_id"] == str(d["id"])


class TestConfirmGating:
    """Destructive/outward actions must be refused without confirm=true."""

    async def test_publish_requires_confirm(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("D", slug="c1")
            with pytest.raises(ToolError, match="Refused"):
                await mcp_server.publish_dashboard(d["id"], confirm=False)
            assert store.get_dashboard(d["id"])["status"] == "draft"
            # The refusal is audited as 'refused'.
            audit = await mcp_server.get_audit_log()
            assert audit[0]["action"] == "publish_dashboard"
            assert audit[0]["result"] == "refused"

    async def test_unpublish_requires_confirm(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("D", slug="c2")
            await mcp_server.publish_dashboard(d["id"], confirm=True)
            with pytest.raises(ToolError, match="Refused"):
                await mcp_server.unpublish_dashboard(d["id"], confirm=False)
            assert store.get_dashboard(d["id"])["status"] == "published"
            await mcp_server.unpublish_dashboard(d["id"], confirm=True)
            assert store.get_dashboard(d["id"])["status"] == "draft"

    async def test_remove_chart_requires_confirm(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("D")
            c = await mcp_server.add_chart(d["id"], "kpi", data={"value": 1})
            with pytest.raises(ToolError, match="Refused"):
                await mcp_server.remove_chart(c["id"], confirm=False)
            assert store.get_chart(c["id"]) is not None
            removed = await mcp_server.remove_chart(c["id"], confirm=True)
            assert removed["id"] == c["id"]

    async def test_delete_dashboard_requires_confirm(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await mcp_server.create_dashboard("D", slug="d3l")
            await mcp_server.add_chart(d["id"], "kpi", data={"value": 1})
            with pytest.raises(ToolError, match="Refused"):
                await mcp_server.delete_dashboard(d["id"], confirm=False)
            assert store.get_dashboard(d["id"]) is not None
            await mcp_server.delete_dashboard(d["id"], confirm=True)
            assert store.get_dashboard(d["id"]) is None


class TestChartValidation:
    """Per-kind data envelopes are validated before persistence."""

    async def _dashboard(self, store):
        return await mcp_server.create_dashboard("D")

    async def test_line_envelope_ok(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await self._dashboard(store)
            chart = await mcp_server.add_chart(
                d["id"], "line", data={"columns": ["x", "y"], "rows": [["a", 1]]}
            )
            assert chart["id"] > 0

    async def test_missing_columns_rejected(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await self._dashboard(store)
            with pytest.raises(ToolError, match="columns"):
                await mcp_server.add_chart(d["id"], "bar", data={"rows": [[1]]})

    async def test_ragged_rows_rejected(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await self._dashboard(store)
            with pytest.raises(ToolError, match="row 0"):
                await mcp_server.add_chart(
                    d["id"], "pie",
                    data={"columns": ["a", "b"], "rows": [["only-one"]]},
                )

    async def test_kpi_requires_value(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await self._dashboard(store)
            with pytest.raises(ToolError, match="value"):
                await mcp_server.add_chart(d["id"], "kpi", data={"label": "no value"})
            ok = await mcp_server.add_chart(d["id"], "kpi", data={"value": 42})
            assert ok["data"]["value"] == 42

    async def test_choropleth_requires_uf_values(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await self._dashboard(store)
            with pytest.raises(ToolError, match="values"):
                await mcp_server.add_chart(d["id"], "choropleth", data={})
            with pytest.raises(ToolError, match="UF codes"):
                await mcp_server.add_chart(
                    d["id"], "choropleth", data={"values": {"BRA": 1}}
                )
            ok = await mcp_server.add_chart(
                d["id"], "choropleth", data={"values": {"SP": 10, "RJ": 5}}
            )
            assert ok["data"]["values"]["SP"] == 10

    async def test_update_chart_validates_against_new_kind(self, env):
        store, _ = env
        async with keyed_context(store):
            d = await self._dashboard(store)
            c = await mcp_server.add_chart(d["id"], "kpi", data={"value": 1})
            with pytest.raises(ToolError, match="columns"):
                await mcp_server.update_chart(c["id"], kind="line", data={"value": 2})
            # valid against the *existing* kind when kind is unchanged
            out = await mcp_server.update_chart(c["id"], data={"value": 3})
            assert out["data"]["value"] == 3

    async def test_update_missing_chart(self, env):
        store, _ = env
        async with keyed_context(store):
            with pytest.raises(ToolError, match="not found"):
                await mcp_server.update_chart(999, title="x")


class TestProtocolRoundTrip:
    """Full MCP protocol: initialize, list tools, call a tool."""

    async def test_list_and_call_over_the_wire(self, store):
        mcp_server.set_store(store)
        async with make_session(mcp_server.mcp) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {
                "create_dashboard", "update_dashboard", "publish_dashboard",
                "unpublish_dashboard", "add_chart", "update_chart",
                "remove_chart", "list_dashboards", "get_dashboard",
                "get_view_stats", "get_audit_log",
            } <= names

            result = await session.call_tool(
                "create_dashboard", {"title": "Wire", "slug": "wire"}
            )
            assert not result.isError
            payload = json.loads(result.content[0].text)
            assert payload["slug"] == "wire"

            result = await session.call_tool(
                "add_chart",
                {"dashboard_id": payload["id"], "kind": "bar",
                 "data": {"columns": ["a"], "rows": [[1]]}},
            )
            assert not result.isError

    async def test_tool_error_over_the_wire(self, store):
        mcp_server.set_store(store)
        async with make_session(mcp_server.mcp) as session:
            await session.initialize()
            result = await session.call_tool(
                "add_chart",
                {"dashboard_id": 999, "kind": "line",
                 "data": {"columns": ["a"], "rows": [[1]]}},
            )
            assert result.isError
