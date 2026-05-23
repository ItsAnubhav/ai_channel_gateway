from __future__ import annotations

import logging
import time

import httpx

from channel_gateway.app.config import Settings
from channel_gateway.app.schemas import ChatRequest, ChatTurnResponse


logger = logging.getLogger(__name__)


class AgentClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run_turn(self, request: ChatRequest) -> ChatTurnResponse:
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self._settings.agent_api_timeout_seconds) as client:
            try:
                response = await client.post(
                    f"{self._settings.agent_api_base_url.rstrip('/')}/chat/turn",
                    json=request.model_dump(exclude_none=True),
                )
                response.raise_for_status()
                logger.info(
                    "Agent API turn completed user_id=%s session_id=%s status=%s elapsed_s=%.3f",
                    request.user_id,
                    request.session_id,
                    getattr(response, "status_code", "unknown"),
                    time.perf_counter() - started,
                )
                return ChatTurnResponse.model_validate(response.json())
            except Exception:
                logger.exception(
                    "Agent API turn failed user_id=%s session_id=%s elapsed_s=%.3f",
                    request.user_id,
                    request.session_id,
                    time.perf_counter() - started,
                )
                raise
