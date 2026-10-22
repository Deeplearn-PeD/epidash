"""Client for EpidBot's chat API — powers the viewer "tweak"/"ask" flows.

Authentication uses the internal-service pattern already established by
EpidBot's file-upload endpoint: the shared ``INTERNAL_API_SECRET`` sent as
an ``X-Internal-Request`` header. An ``ek_`` API key sent as a Bearer
token is supported as a fallback for local experiments.

Flow: submit the turn to the service chat route (POST /api/v1/chat/service
— the plain /chat route requires user credentials), then poll
GET /api/v1/chat/service/{job_id} until the agent finishes. The agent is
expected to mutate the dashboard through its EpiDash MCP tools during a
tweak turn; question turns are answered without touching the dashboard.
"""

import asyncio
from typing import Any, Optional

import httpx

from epidash.config import get_settings
from epidash.logging import get_logger

logger = get_logger(__name__)

POLL_INTERVAL_S = 2.0


class EpidBotError(RuntimeError):
    """A tweak round-trip against EpidBot failed."""


def _compact_charts(charts: list[dict]) -> list[dict]:
    return [
        {
            "id": c["id"],
            "kind": c["kind"],
            "title": c["title"],
            "data": c["data"],
            "spec": c["spec"],
            "position": c["position"],
        }
        for c in charts
    ]


def build_tweak_message(dashboard: dict, charts: list[dict], request: str) -> str:
    """Prompt sent to the EpidBot agent for one tweak turn."""
    compact = _compact_charts(charts)
    return (
        f"A viewer of the EpiDash dashboard '{dashboard.get('title', '')}' "
        f"(dashboard_id={dashboard['id']}, slug={dashboard['slug']}) requests "
        f"changes to it.\n\n"
        f"Current charts (JSON):\n{compact}\n\n"
        f"Viewer request: {request}\n\n"
        "Use your EpiDash MCP tools to apply the requested changes to this "
        "dashboard (update_chart / add_chart / remove_chart as needed). Keep "
        "the existing visual style unless asked otherwise, keep data "
        "envelopes consistent with each chart kind, and reply with a one or "
        "two sentence summary of what you changed."
    )


def build_question_message(dashboard: dict, charts: list[dict], question: str) -> str:
    """Prompt sent to the EpidBot agent when a viewer asks a question.

    Same chat pipeline as tweaks, but the agent must answer from the
    dashboard data and leave the dashboard untouched.
    """
    compact = _compact_charts(charts)
    return (
        f"A viewer of the EpiDash dashboard '{dashboard.get('title', '')}' "
        f"(dashboard_id={dashboard['id']}, slug={dashboard['slug']}) is asking "
        f"a QUESTION about it.\n\n"
        f"Current charts (JSON):\n{compact}\n\n"
        f"Viewer question: {question}\n\n"
        "Answer the question based on the dashboard's data. This is a "
        "read-only turn: do NOT use your EpiDash MCP tools to modify the "
        "dashboard. Reply with a concise, self-contained answer (a short "
        "paragraph at most); cite the specific numbers or charts you used."
    )


class EpidBotClient:
    """Submits tweak turns to EpidBot and waits for completion."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        internal_secret: Optional[str] = None,
        timeout_s: Optional[int] = None,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.epidbot_base_url).rstrip("/")
        self.internal_secret = internal_secret or settings.epidbot_internal_secret
        self.timeout_s = timeout_s or settings.tweak_timeout_s

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.internal_secret.startswith("ek_"):
            headers["Authorization"] = f"Bearer {self.internal_secret}"
        else:
            headers["X-Internal-Request"] = self.internal_secret
        return headers

    async def _submit(self, client: httpx.AsyncClient, message: str) -> tuple[str, Optional[int]]:
        response = await client.post(
            f"{self.base_url}/api/v1/chat/service",
            json={"message": message, "locale": "en"},
            headers=self._headers(),
            timeout=30,
        )
        if response.status_code != 200:
            raise EpidBotError(
                f"EpidBot chat submit failed: HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        payload = response.json()
        return payload["job_id"], payload.get("session_id")

    async def _poll(
        self, client: httpx.AsyncClient, job_id: str
    ) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + self.timeout_s
        while True:
            response = await client.get(
                f"{self.base_url}/api/v1/chat/service/{job_id}",
                headers=self._headers(),
                timeout=15,
            )
            if response.status_code != 200:
                raise EpidBotError(
                    f"EpidBot chat poll failed: HTTP {response.status_code}"
                )
            payload = response.json()
            status = payload.get("status")
            if status == "completed":
                return payload
            if status == "failed":
                raise EpidBotError(
                    f"EpidBot chat job failed: {payload.get('error') or 'unknown error'}"
                )
            if asyncio.get_event_loop().time() > deadline:
                raise EpidBotError(
                    f"EpidBot chat job {job_id} timed out after {self.timeout_s}s"
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    async def run_tweak(self, message: str) -> dict[str, Any]:
        """Submit the tweak turn and wait for the agent to finish."""
        async with httpx.AsyncClient() as client:
            job_id, _session_id = await self._submit(client, message)
            logger.info("EpiDash tweak submitted to EpidBot (job_id=%s)", job_id)
            result = await self._poll(client, job_id)
        return result
