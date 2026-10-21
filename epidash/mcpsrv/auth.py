"""Authentication for the EpiDash MCP server.

A pure-ASGI middleware verifies a ``dk_`` control key on every request
*before* the MCP handshake and stores the key metadata in a contextvar so
tool functions can attribute audit rows. Mirrors the pattern used by the
EpidBot admin MCP server.
"""

import asyncio
import json
from contextvars import ContextVar
from typing import Optional

from epidash.logging import get_logger
from epidash.store import Store

logger = get_logger(__name__)

_current_key: ContextVar[Optional[dict]] = ContextVar("epidash_current_key", default=None)

_store: Store | None = None


def set_store(store: Store) -> None:
    """Point the auth layer at the process store (called at app startup)."""
    global _store
    _store = store


def get_current_key() -> dict:
    """Key metadata for the in-flight MCP request."""
    meta = _current_key.get()
    if meta is None:
        raise RuntimeError("No authenticated key on the MCP request context")
    return meta


def _extract_key(headers: dict[str, str]) -> Optional[str]:
    raw = headers.get("x-api-key")
    if raw:
        return raw.strip()
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
        if candidate.startswith("dk_"):
            return candidate
    return None


async def _send_rejection(send, status_code: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class McpAuthMiddleware:
    """Reject any request without a valid ``dk_`` control key."""

    def __init__(self, app, store: Store | None = None):
        self.app = app
        self._store = store

    def _verify(self, raw_key: str) -> Optional[dict]:
        store = self._store or _store
        if store is None:
            return None
        return store.verify_key(raw_key)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        raw_key = _extract_key(headers)
        if not raw_key:
            await _send_rejection(
                send, 401, "Missing API key. Send 'X-API-Key: dk_...' with a control key."
            )
            return

        matched = await asyncio.to_thread(self._verify, raw_key)
        if matched is None:
            logger.warning("EpiDash MCP auth failed for key prefix %s...", raw_key[:6])
            await _send_rejection(send, 401, "Invalid or revoked control key.")
            return

        token = _current_key.set(matched)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_key.reset(token)
