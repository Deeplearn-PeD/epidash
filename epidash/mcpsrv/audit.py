"""Best-effort audit trail for MCP-triggered mutations."""

from typing import Any, Optional

from epidash.logging import get_logger
from epidash.mcpsrv.auth import get_current_key
from epidash.store import Store

logger = get_logger(__name__)

_store: Store | None = None


def set_store(store: Store) -> None:
    global _store
    _store = store


def log_action(
    action: str,
    target_type: Optional[str] = None,
    target_id: Any = None,
    params: Optional[dict] = None,
    result: str = "ok",
    error: Optional[str] = None,
) -> None:
    """Insert one audit row attributed to the in-flight key; never raises."""
    try:
        key_meta = get_current_key()
    except RuntimeError:
        key_meta = {}
    store = _store
    if store is None:
        logger.error("Audit store not configured; dropping audit row for %s", action)
        return
    store.log_audit(
        key_id=(key_meta or {}).get("key_id"),
        action=action,
        target_type=target_type,
        target_id=target_id,
        params=params,
        result=result,
        error=error,
    )
