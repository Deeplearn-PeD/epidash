"""E2E fixtures: a real uvicorn server + a headless chromium page.

Run with:  uv run pytest tests_e2e -m e2e -v
Requires:  uv sync --group e2e && uv run playwright install chromium
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url():
    """Boots a real EpiDash server against a throwaway DB and seeds data."""
    port = _free_port()
    db_dir = tempfile.mkdtemp(prefix="epidash-e2e-")
    env = {
        **os.environ,
        "EPIDASH_DB_PATH": os.path.join(db_dir, "e2e.db"),
        "EPIDASH_BASE_URL": f"http://localhost:{port}",
        "EPIDASH_DNS_REBINDING_PROTECTION": "false",
        "EPIDBOT_BASE_URL": "",
        "EPIDBOT_INTERNAL_SECRET": "",
    }
    log_file = open(os.path.join(db_dir, "uvicorn.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "epidash.main:app", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    base = f"http://localhost:{port}"
    deadline = time.monotonic() + 30
    import urllib.request

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError("EpiDash server did not become healthy")

    # seed: one published dashboard (line + kpi + table), one draft
    sys.path.insert(0, str(REPO_ROOT))
    from epidash.store import Store

    store = Store(os.path.join(db_dir, "e2e.db"))
    d = store.create_dashboard(
        "Dengue E2E", description="e2e fixture dashboard",
        slug="e2e-dengue", created_by="EpidBot",
    )
    store.add_chart(
        d["id"], "line", title="Cases", position={"order": 1, "width": "half"},
        data={"columns": ["month", "cases"], "rows": [["Jan", 10], ["Fev", 20]]},
    )
    store.add_chart(
        d["id"], "kpi", title="Total", position={"order": 2, "width": "half"},
        data={"value": 30, "label": "cases", "delta": "+5%"},
    )
    store.add_chart(
        d["id"], "table", title="States", position={"order": 3},
        data={"columns": ["state", "cases"], "rows": [["SP", 15], ["RJ", 15]]},
    )
    store.add_chart(
        d["id"], "markdown", title="Methodology", position={"order": 4, "width": "half"},
        data={"text": "## Methodology\nData refreshed **weekly** from `INFO-DENGUE`."},
    )
    store.set_dashboard_status(d["id"], "published")
    draft = store.create_dashboard("Hidden Draft", slug="e2e-draft")
    assert store.get_dashboard(draft["id"])

    yield base
    proc.terminate()
    proc.wait(timeout=15)
    log_file.close()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def fresh_page(browser):
    """A fresh browser context per test: isolated storage/cookies and a
    deterministic en-US locale (pt-BR is exercised explicitly via ?lang=)."""
    context = browser.new_context(
        viewport={"width": 1280, "height": 900}, locale="en-US"
    )
    page = context.new_page()
    yield page
    context.close()
