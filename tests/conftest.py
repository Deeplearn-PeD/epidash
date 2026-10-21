"""Shared fixtures: isolated settings/store per test, TestClient app."""

import os
import tempfile

# Ensure the module-level `epidash.main.app` import (created before any
# fixture runs) never touches the repo working directory.
os.environ.setdefault(
    "EPIDASH_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="epidash-import-"), "test.db"),
)

import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from epidash.config import Settings, set_settings  # noqa: E402
from epidash.main import create_app  # noqa: E402
from epidash.store import Store  # noqa: E402


@pytest.fixture()
def settings(tmp_path):
    """Per-test settings pointing at a throwaway SQLite file."""
    s = Settings(
        db_path=str(tmp_path / "test.db"),
        base_url="http://testserver",
        dns_rebinding_protection=False,
        epidbot_base_url="",
        epidbot_internal_secret="",
    )
    set_settings(s)
    yield s
    set_settings(None)


@pytest.fixture()
def store(settings):
    return Store(settings.db_path)


@pytest.fixture()
def client(settings, store):
    app = create_app(store, run_mcp_lifespan=False)
    with TestClient(app) as c:
        yield c


def wait_for_tweak(client, tweak_id: str, timeout_s: float = 5.0) -> dict:
    """Poll a tweak until it reaches a terminal state (TestClient is sync)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        tweak = client.get(f"/api/tweaks/{tweak_id}").json()
        if tweak["status"] in ("completed", "failed"):
            return tweak
        time.sleep(0.02)
    raise AssertionError("tweak did not reach a terminal state in time")
