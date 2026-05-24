from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from channel_gateway.app.agent_client import AgentClient
from channel_gateway.app.config import Settings
from channel_gateway.app.message_handler import handle_inbound_message
from channel_gateway.app.schemas import ChannelInboundMessage
from channel_gateway.app.session_store import ChannelSessionStore

logger = logging.getLogger(__name__)
BOT_FRAMEWORK_JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"


class TeamsBotClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._access_token = ""
        self._access_token_expires_at = datetime.fromtimestamp(0, UTC)

    @property
    def configured(self) -> bool:
        return bool(self._settings.teams_bot_app_id and self._settings.teams_bot_app_password)

    async def send_activity(
        self,
        inbound: ChannelInboundMessage,
        *,
        activity_type: str = "message",
        text: str = "",
    ) -> None:
        if not self.configured:
            return

        service_url = str(inbound.metadata.get("service_url") or "").rstrip("/")
        conversation_id = str(inbound.metadata.get("conversation_id") or "")
        activity_id = str(inbound.metadata.get("activity_id") or "")
        if not service_url or not conversation_id:
            logger.warning("Teams reply skipped because serviceUrl or conversation ID is missing")
            return

        token = await self._token()
        payload: dict[str, Any] = {
            "type": activity_type,
            "from": inbound.metadata.get("recipient") or {},
            "recipient": inbound.metadata.get("from") or {},
            "conversation": inbound.metadata.get("conversation") or {"id": conversation_id},
            "replyToId": activity_id,
        }
        if text:
            payload["text"] = text

        encoded_conversation_id = quote(conversation_id, safe="")
        encoded_activity_id = quote(activity_id, safe="")
        path = f"/v3/conversations/{encoded_conversation_id}/activities"
        if activity_type == "message" and activity_id:
            path = f"{path}/{encoded_activity_id}"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{service_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            response.raise_for_status()

    async def _token(self) -> str:
        token_is_fresh = (
            self._access_token
            and self._access_token_expires_at > datetime.now(UTC) + timedelta(minutes=5)
        )
        if token_is_fresh:
            return self._access_token

        tenant_id = self._settings.teams_bot_tenant_id or "botframework.com"
        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                token_url,
                data={
                    "client_id": self._settings.teams_bot_app_id,
                    "client_secret": self._settings.teams_bot_app_password,
                    "grant_type": "client_credentials",
                    "scope": "https://api.botframework.com/.default",
                },
            )
            response.raise_for_status()
            payload = response.json()

        self._access_token = str(payload["access_token"])
        self._access_token_expires_at = datetime.now(UTC) + timedelta(
            seconds=int(payload.get("expires_in", 3600))
        )
        return self._access_token


def build_router(
    settings: Settings,
    store: ChannelSessionStore,
    agent_client: AgentClient,
    teams_client: TeamsBotClient,
) -> APIRouter:
    router = APIRouter(prefix="/webhooks/teams", tags=["teams"])

    @router.post("")
    async def teams_webhook(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        webhook_secret: str | None = Header(default=None, alias="X-Teams-Webhook-Secret"),
    ) -> dict[str, str]:
        if settings.teams_webhook_secret and webhook_secret != settings.teams_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid Teams webhook secret")
        if not await validate_bot_framework_authorization(authorization, settings):
            raise HTTPException(status_code=401, detail="Invalid Teams authorization token")

        inbound = parse_teams_activity(await request.json())
        if inbound is None:
            return {"status": "ignored"}

        asyncio.create_task(
            handle_teams_inbound(inbound, settings, store, agent_client, teams_client)
        )
        return {"status": "accepted"}

    return router


async def handle_teams_inbound(
    inbound: ChannelInboundMessage,
    settings: Settings,
    store: ChannelSessionStore,
    agent_client: AgentClient,
    teams_client: TeamsBotClient,
) -> None:
    async def send_reply(settings: Settings, inbound: ChannelInboundMessage, text: str) -> None:
        await teams_client.send_activity(inbound, text=text)

    try:
        await teams_client.send_activity(inbound, activity_type="typing")
        await handle_inbound_message(
            inbound,
            settings,
            store,
            agent_client,
            send_reply,
            reset_commands={"/new", "new chat"},
        )
    except Exception:
        logger.exception("Teams message failed channel_message_id=%s", inbound.external_message_id)


def parse_teams_activity(payload: dict[str, Any]) -> ChannelInboundMessage | None:
    if payload.get("type") != "message":
        return None

    text = teams_activity_text(payload)
    if not text:
        return None

    from_user = payload.get("from") if isinstance(payload.get("from"), dict) else {}
    conversation = payload.get("conversation")
    conversation = conversation if isinstance(conversation, dict) else {}
    channel_data = payload.get("channelData")
    channel_data = channel_data if isinstance(channel_data, dict) else {}
    tenant = channel_data.get("tenant") if isinstance(channel_data.get("tenant"), dict) else {}

    conversation_id = str(conversation.get("id") or "")
    from_id = str(from_user.get("id") or "")
    aad_object_id = str(from_user.get("aadObjectId") or "")
    tenant_id = str(tenant.get("id") or "")
    teams_user_id = aad_object_id or from_id
    external_user_id = f"{tenant_id}:{teams_user_id}" if tenant_id else teams_user_id
    activity_id = str(payload.get("id") or channel_data.get("clientActivityId") or "")
    if not external_user_id or not conversation_id or not activity_id:
        return None

    return ChannelInboundMessage(
        channel="teams",
        external_user_id=external_user_id,
        external_message_id=activity_id,
        thread_id=conversation_id,
        text=text,
        metadata={
            "activity_id": activity_id,
            "service_url": str(payload.get("serviceUrl") or ""),
            "conversation_id": conversation_id,
            "conversation": conversation,
            "from": from_user,
            "from_id": from_id,
            "aad_object_id": aad_object_id,
            "recipient": payload.get("recipient")
            if isinstance(payload.get("recipient"), dict)
            else {},
            "tenant_id": tenant_id,
            "channel_id": str(payload.get("channelId") or ""),
        },
    )


def teams_activity_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or "")
    text = re.sub(r"(?is)<at>.*?</at>", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    return html.unescape(text).strip()


async def validate_bot_framework_authorization(
    authorization: str | None,
    settings: Settings,
) -> bool:
    if not settings.teams_validate_auth or not settings.teams_bot_app_id:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return False
    return await asyncio.to_thread(_validate_bot_framework_jwt, token, settings.teams_bot_app_id)


def _validate_bot_framework_jwt(token: str, app_id: str) -> bool:
    try:
        from jwt import PyJWKClient, decode
        from jwt.exceptions import PyJWTError
    except ImportError:
        logger.exception("PyJWT is required for Teams Bot Framework token validation")
        return False

    try:
        signing_key = PyJWKClient(BOT_FRAMEWORK_JWKS_URL).get_signing_key_from_jwt(token)
        decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=BOT_FRAMEWORK_ISSUER,
        )
        return True
    except PyJWTError:
        logger.warning("Teams Bot Framework token validation failed", exc_info=True)
        return False
