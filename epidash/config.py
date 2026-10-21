"""Environment-based configuration.

Every setting has a sane local default so the service runs out of the box
(``uv run uvicorn epidash.main:app``); production values are injected via
environment variables (see docker-compose.yml / the Ansible role).
"""

import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value is not None else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class Settings:
    """Runtime configuration, read once from the environment."""

    #: SQLite database file (WAL mode).
    db_path: str = field(default_factory=lambda: _env_str("EPIDASH_DB_PATH", "data/epidash.db"))

    #: Public base URL, used to build share links (published dashboard URLs).
    base_url: str = field(default_factory=lambda: _env_str("EPIDASH_BASE_URL", "http://localhost:8000"))

    #: Path the MCP streamable-HTTP endpoint is served under.
    mcp_path: str = field(default_factory=lambda: _env_str("EPIDASH_MCP_PATH", "/mcp"))

    #: Server name advertised during the MCP handshake.
    server_name: str = field(default_factory=lambda: _env_str("EPIDASH_SERVER_NAME", "epidash"))

    #: Hosts allowed by the MCP DNS-rebinding protection (Host header values).
    #: The SDK compares the full Host header (port included) and accepts
    #: ``host:*`` wildcard-port patterns.
    allowed_hosts: list[str] = field(
        default_factory=lambda: [
            h.strip()
            for h in _env_str("EPIDASH_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*").split(",")
            if h.strip()
        ]
    )

    #: Keep DNS-rebinding protection on in production; tests may disable it.
    dns_rebinding_protection: bool = field(
        default_factory=lambda: _env_bool("EPIDASH_DNS_REBINDING_PROTECTION", True)
    )

    #: Optional pre-shared ``dk_`` control key (seeded at startup, used by
    #: infra automation so EpidBot and epidash get the same secret).
    bootstrap_key: str = field(default_factory=lambda: _env_str("EPIDASH_BOOTSTRAP_KEY"))

    #: EpidBot base URL (e.g. https://api.epidbot.kwar-ai.com.br) enabling the
    #: tweak flow. Empty disables it.
    epidbot_base_url: str = field(default_factory=lambda: _env_str("EPIDBOT_BASE_URL"))

    #: Shared secret sent as ``X-Internal-Request`` (EpidBot's
    #: INTERNAL_API_SECRET) or a regular ``ek_`` API key sent as Bearer.
    epidbot_internal_secret: str = field(
        default_factory=lambda: _env_str("EPIDBOT_INTERNAL_SECRET")
    )

    #: Tweak submissions allowed per dashboard slug per hour (LLM cost guard).
    tweak_rate_limit_per_hour: int = field(
        default_factory=lambda: _env_int("EPIDASH_TWEAK_RATE_LIMIT_PER_HOUR", 20)
    )

    #: Viewer questions allowed per dashboard slug per hour (LLM cost guard).
    ask_rate_limit_per_hour: int = field(
        default_factory=lambda: _env_int("EPIDASH_ASK_RATE_LIMIT_PER_HOUR", 30)
    )

    #: How long to wait for EpidBot to finish a tweak (seconds).
    tweak_timeout_s: int = field(default_factory=lambda: _env_int("EPIDASH_TWEAK_TIMEOUT_S", 300))

    @property
    def epidbot_configured(self) -> bool:
        """True when the tweak flow can reach EpidBot."""
        return bool(self.epidbot_base_url) and bool(self.epidbot_internal_secret)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton (tests use ``set_settings``)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings | None) -> None:
    """Replace the settings singleton (mainly for tests)."""
    global _settings
    _settings = settings
