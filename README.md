# EpiDash

[![CI](https://github.com/Deeplearn-PeD/epidash/actions/workflows/ci.yml/badge.svg)](https://github.com/Deeplearn-PeD/epidash/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Deeplearn-PeD/epidash/badge/coverage.json)](https://github.com/Deeplearn-PeD/epidash/tree/badge)
[![PyPI](https://img.shields.io/pypi/v/epidash)](https://pypi.org/project/epidash/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A light, MCP-controlled dashboard service. [EpidBot](https://github.com/Deeplearn-PeD/EpiDBot)
(and any MCP client holding a control key) builds, updates, publishes and
unpublishes dashboards through an MCP tool surface; anyone with a dashboard
link views it in a minimal web UI. Viewers can ask EpidBot to tweak a
dashboard from the browser.

**Live instance: [https://dash.kwar-ai.com.br](https://dash.kwar-ai.com.br)**

```
EpidBot agent ──(MCP client, dk_ key)──▶ EpiDash /mcp (FastMCP tools)
                                            │
                       SQLite ◀─────────────┤ (dashboards, charts, keys, audit)
                                            │
Viewer browser ──(public link)──▶ EpiDash / (vanilla JS + Plotly)
      │                                 │
      └── tweak request ──▶ POST /api/dashboards/{slug}/tweak
                                 │  (X-Internal-Request: shared secret)
                                 ▼
                           EpidBot /api/v1/chat ──agent──▶ EpiDash MCP tools
```

## Quick start

```bash
uv sync
uv run uvicorn epidash.main:app --port 8000
```

Then issue a control key and talk to the MCP endpoint:

```bash
uv run python -m epidash.cli add-key "local"
# → dk_...  (shown once)
```

```bash
# The MCP endpoint is stateless: one POST per call, no handshake needed.
curl -s -X POST http://localhost:8000/mcp \
  -H "X-API-Key: dk_..." -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

In an MCP client (e.g. Claude Desktop, Cursor, EpidBot):

```json
{
  "mcpServers": {
    "epidash": {
      "url": "http://localhost:8000/mcp",
      "headers": { "X-API-Key": "dk_..." }
    }
  }
}
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `EPIDASH_DB_PATH` | `data/epidash.db` | SQLite file (WAL) |
| `EPIDASH_BASE_URL` | `http://localhost:8000` | Public base URL for share links |
| `EPIDASH_MCP_PATH` | `/mcp` | Mount path of the MCP endpoint |
| `EPIDASH_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host header allowlist (DNS-rebinding protection) |
| `EPIDASH_BOOTSTRAP_KEY` | — | Pre-shared `dk_` key seeded at startup (infra automation) |
| `EPIDBOT_BASE_URL` | — | EpidBot base URL; enables the tweak flow |
| `EPIDBOT_INTERNAL_SECRET` | — | Shared secret (EpidBot `INTERNAL_API_SECRET`) or `ek_` API key |
| `EPIDASH_TWEAK_RATE_LIMIT_PER_HOUR` | `20` | Per-dashboard viewer tweak budget |
| `EPIDASH_ASK_RATE_LIMIT_PER_HOUR` | `30` | Per-dashboard viewer question budget |
| `EPIDASH_TWEAK_TIMEOUT_S` | `300` | Max wait for an EpidBot tweak turn |
| `EPIDASH_OIDC_ISSUER` | — | Keycloak issuer URL (enables viewer login) |
| `EPIDASH_OIDC_CLIENT_ID` / `_SECRET` | — | OIDC confidential client for EpiDash |
| `EPIDASH_SESSION_SECRET` | — | Long random string; signs viewer session cookies |
| `EPIDASH_PG_DSN_<NAME>` | — | Named Postgres DSN for `postgres` data sources |

## MCP tools

Read-only: `list_dashboards`, `get_dashboard`, `get_view_stats`, `get_audit_log`
(filterable by `action` and `dashboard_id`).

Mutations (audited): `create_dashboard`, `update_dashboard`,
`publish_dashboard` (confirm), `unpublish_dashboard` (confirm),
`delete_dashboard` (confirm), `add_chart`, `update_chart`, `remove_chart`
(confirm), `duplicate_dashboard`, `style_charts`,
`set_dashboard_access` (confirm), `create_share_token` (confirm),
`list_share_tokens`, `revoke_share_token` (confirm).

### Live data sources

Charts can auto-update from remote data instead of holding a static
snapshot. `attach_data_source(chart_id, kind, ..., refresh_interval_s=300)`
attaches a source:

| kind | payload |
|---|---|
| `url_json` | `url` (+ optional dotted `path`) — the payload is the chart's data envelope itself (`{"columns": ..., "rows": ...}`) |
| `url_csv` | `url` — header row + data rows (numeric cells auto-converted); feeds `line`/`bar`/`pie`/`table` charts |
| `postgres` | `source` (a registered DSN name) + `sql` — query result columns/rows become the envelope; DSNs live in `EPIDASH_PG_DSN_<NAME>` env vars, never in the database |

A background refresher fetches due sources every 30s (minimum interval 30s)
and replaces the chart's data on success — viewers pick the change up on
their next poll, and chart cards show an `⇣ auto` badge with refresh
status. Failures never lose existing data; inspect them with
`get_data_source(chart_id)`, force a run with `refresh_data_source`, or
stop with `remove_data_source` (confirm).

### Chart data envelopes

| kind | data |
|---|---|
| `line`, `bar`, `pie`, `table` | `{"columns": ["month", "cases"], "rows": [["Jan", 120], ...]}` |
| `kpi` | `{"value": 1234, "label": "Cases", "unit": "", "delta": "+12%"}` |
| `choropleth` | `{"values": {"SP": 120, "RJ": 90}, "region_level": "uf", "label": "cases"}` |
| `markdown` | `{"text": "## Notes\nNarrative **between** charts"}` — rendered as a narrative block |

`spec` accepts style overrides (`{"color": "#4ade80"}`, `{"y_label": "cases"}`) and
`position` controls ordering/width (`{"order": 1, "width": "half"}`).

## Viewer login & access control

Optionally, viewers can sign in through Keycloak (OIDC). Configure
`EPIDASH_OIDC_ISSUER`, `EPIDASH_OIDC_CLIENT_ID`, `EPIDASH_OIDC_CLIENT_SECRET`
and `EPIDASH_SESSION_SECRET` to enable it — without them, EpiDash stays
fully anonymous.

With login enabled:

- logged-in viewers get **server-side like dedupe** and their questions are
  attributed (`[Asked by name]` reaches EpidBot with the question);
- dashboards gain a **visibility** level, set via the
  `set_dashboard_access` MCP tool: `public` (gallery + link), `unlisted`
  (link only), `restricted` (logged-in viewers or share tokens);
- `create_share_token` mints expiring links (`https://…/?t=st_…#/d/slug`)
  that work even for restricted dashboards; `revoke_share_token` kills them.

The UI is available in English and pt-BR (`?lang=pt-BR`, persisted).

## Viewer tweak flow

A viewer types a request ("show only the Southeast after 2020") into the
dashboard's chat box. EpiDash forwards it as an EpidBot chat turn with the
dashboard context; the EpidBot agent uses its EpiDash MCP tools to update
the charts; the viewer's browser picks up the change on its next poll.

Viewers can also **ask questions** ("why did cases spike in March?") via
the Ask button — the same chat pipeline, but the prompt instructs EpidBot
to answer from the dashboard data without modifying it. No authentication
is required for either; questions are recorded in the `tweaks` table with
`kind='question'` and rate-limited separately from tweaks.

Requires `EPIDBOT_BASE_URL` + `EPIDBOT_INTERNAL_SECRET` (EpidBot must accept
`X-Internal-Request` on its chat endpoint — see the EpidBot-side integration
docs) or an `ek_` API key.

## Deployment

Single container, single volume:

```bash
docker compose up -d --build
```

Production (KwarAI) deploys this via the Ansible `epidash` role behind
nginx at `dash.kwar-ai.com.br` — see the
[infrastructure repo](https://github.com/Deeplearn-PeD/infrastructure).

## Development

```bash
uv sync                  # deps
uv run pytest -v         # tests
uv run ty check epidash/ # type check
```

Browser end-to-end tests (headless chromium):

```bash
uv sync --group e2e
uv run playwright install chromium
uv run pytest tests_e2e -m e2e -v
```

## License

MIT
