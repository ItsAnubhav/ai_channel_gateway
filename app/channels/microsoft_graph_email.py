from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from channel_gateway.app.agent_client import AgentClient
from channel_gateway.app.config import Settings
from channel_gateway.app.formatters import format_channel_response
from channel_gateway.app.schemas import ChannelInboundMessage, ChatRequest
from channel_gateway.app.session_store import ChannelSessionStore

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SUBSCRIPTION_ID_KEY = "microsoft_graph_email_subscription_id"
SUBSCRIPTION_EXPIRY_KEY = "microsoft_graph_email_subscription_expiry"


class MicrosoftGraphClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._access_token: str = ""
        self._access_token_expires_at = datetime.fromtimestamp(0, UTC)

    @property
    def configured(self) -> bool:
        return bool(
            self._settings.microsoft_tenant_id
            and self._settings.microsoft_client_id
            and self._settings.microsoft_client_secret
            and self._settings.microsoft_mailbox
        )

    async def ensure_subscription(self, store: ChannelSessionStore) -> None:
        if not self.configured:
            return

        subscription_id = await store.get_value(SUBSCRIPTION_ID_KEY)
        expires_at = _parse_datetime(await store.get_value(SUBSCRIPTION_EXPIRY_KEY))
        if subscription_id and expires_at and expires_at - datetime.now(UTC) > timedelta(hours=24):
            return

        if subscription_id:
            try:
                subscription = await self.renew_subscription(subscription_id)
            except httpx.HTTPStatusError:
                logger.exception("Microsoft Graph subscription renewal failed; creating a new one")
                subscription = await self.create_subscription()
        else:
            subscription = await self.create_subscription()

        await store.set_value(SUBSCRIPTION_ID_KEY, str(subscription["id"]))
        await store.set_value(SUBSCRIPTION_EXPIRY_KEY, str(subscription["expirationDateTime"]))

    async def create_subscription(self) -> dict[str, Any]:
        notification_url = _notification_url(self._settings)
        expires_at = datetime.now(UTC) + timedelta(days=6)
        body = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "lifecycleNotificationUrl": notification_url,
            "resource": (
                f"/users/{self._settings.microsoft_mailbox}"
                "/mailFolders('Inbox')/messages"
            ),
            "expirationDateTime": expires_at.isoformat().replace("+00:00", "Z"),
            "clientState": self._settings.microsoft_webhook_client_state,
        }
        return await self._request("POST", "/subscriptions", json=body)

    async def renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        expires_at = datetime.now(UTC) + timedelta(days=6)
        return await self._request(
            "PATCH",
            f"/subscriptions/{subscription_id}",
            json={"expirationDateTime": expires_at.isoformat().replace("+00:00", "Z")},
        )

    async def get_message(self, resource: str | None, message_id: str | None) -> dict[str, Any]:
        if resource:
            path = _resource_path(resource)
            if path:
                return await self._request(
                    "GET",
                    f"/{path}",
                    params={
                        "$select": (
                            "id,subject,conversationId,body,bodyPreview,from,"
                            "sender,internetMessageId,createdDateTime"
                        )
                    },
                )
        if not message_id:
            raise ValueError("Microsoft Graph notification did not include a message resource or id")
        encoded_mailbox = quote(self._settings.microsoft_mailbox, safe="")
        encoded_message_id = quote(message_id, safe="")
        return await self._request(
            "GET",
            f"/users/{encoded_mailbox}/messages/{encoded_message_id}",
            params={
                "$select": (
                    "id,subject,conversationId,body,bodyPreview,from,"
                    "sender,internetMessageId,createdDateTime"
                )
            },
        )

    async def send_reply(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
    ) -> None:
        encoded_mailbox = quote(self._settings.microsoft_mailbox, safe="")
        await self._request(
            "POST",
            f"/users/{encoded_mailbox}/sendMail",
            json={
                "message": {
                    "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}",
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": to_address}}],
                },
                "saveToSentItems": True,
            },
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        token = await self._token()
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(
                method,
                f"{GRAPH_ROOT}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    async def _token(self) -> str:
        if self._access_token and self._access_token_expires_at > datetime.now(UTC) + timedelta(minutes=5):
            return self._access_token

        token_url = (
            "https://login.microsoftonline.com/"
            f"{self._settings.microsoft_tenant_id}/oauth2/v2.0/token"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                token_url,
                data={
                    "client_id": self._settings.microsoft_client_id,
                    "client_secret": self._settings.microsoft_client_secret,
                    "grant_type": "client_credentials",
                    "scope": "https://graph.microsoft.com/.default",
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
    graph_client: MicrosoftGraphClient,
) -> APIRouter:
    router = APIRouter(prefix="/webhooks/microsoft/email", tags=["microsoft-email"])

    @router.post("", response_model=None)
    async def microsoft_email_webhook(
        request: Request,
        validation_token: str | None = Query(default=None, alias="validationToken"),
    ) -> Response | dict[str, str]:
        if validation_token is not None:
            return Response(content=validation_token, media_type="text/plain")

        payload = await request.json()
        notifications = payload.get("value")
        if not isinstance(notifications, list):
            return {"status": "ignored"}

        for notification in notifications:
            await handle_notification(notification, settings, store, agent_client, graph_client)
        return {"status": "ok"}

    return router


async def run_subscription_manager(
    settings: Settings,
    store: ChannelSessionStore,
    graph_client: MicrosoftGraphClient,
    stop_event: asyncio.Event,
) -> None:
    if not graph_client.configured:
        return

    while not stop_event.is_set():
        try:
            await graph_client.ensure_subscription(store)
        except Exception:
            logger.exception("Microsoft Graph subscription setup/renewal failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.microsoft_subscription_renewal_seconds,
            )
        except TimeoutError:
            continue


async def handle_notification(
    notification: dict[str, Any],
    settings: Settings,
    store: ChannelSessionStore,
    agent_client: AgentClient,
    graph_client: MicrosoftGraphClient,
) -> None:
    if not validate_graph_notification_client_state(
        notification,
        settings.microsoft_webhook_client_state,
    ):
        raise HTTPException(status_code=401, detail="Invalid Microsoft Graph clientState")

    if notification.get("lifecycleEvent"):
        await graph_client.ensure_subscription(store)
        return

    message = await graph_client.get_message(
        resource=notification.get("resource") if isinstance(notification.get("resource"), str) else None,
        message_id=_message_id_from_notification(notification),
    )
    inbound = graph_message_to_inbound(message, settings)
    if inbound.external_user_id.lower() == settings.microsoft_mailbox.lower():
        return
    if not await store.mark_message_started(
        channel=inbound.channel,
        external_message_id=inbound.external_message_id,
    ):
        return

    thread_id = inbound.thread_id or inbound.external_user_id
    try:
        session_id = await store.get_session_id(
            channel=inbound.channel,
            external_user_id=inbound.external_user_id,
            thread_id=thread_id,
        )
        response = await agent_client.run_turn(
            ChatRequest(
                message=inbound.text,
                user_id=f"{inbound.channel}:{inbound.external_user_id}",
                session_id=session_id,
                agent=settings.default_agent,
                context={"channel": inbound.channel, "metadata": inbound.metadata},
            )
        )
        await store.upsert_session_id(
            channel=inbound.channel,
            external_user_id=inbound.external_user_id,
            thread_id=thread_id,
            agent_session_id=response.session_id,
        )
        reply_text = format_channel_response(
            message=response.message,
            artifacts=response.artifacts,
            result_limit=settings.channel_result_limit,
            public_app_url=settings.public_app_url,
        )
        await graph_client.send_reply(
            to_address=inbound.external_user_id,
            subject=str(inbound.metadata.get("subject") or "Travel assistant"),
            body=reply_text,
        )
        await store.mark_message_done(
            channel=inbound.channel,
            external_message_id=inbound.external_message_id,
        )
    except Exception as exc:
        await store.mark_message_done(
            channel=inbound.channel,
            external_message_id=inbound.external_message_id,
            status="failed",
            error_text=str(exc),
        )
        raise


def graph_message_to_inbound(message: dict[str, Any], settings: Settings) -> ChannelInboundMessage:
    sender = _email_address(message.get("from")) or _email_address(message.get("sender"))
    message_id = str(message.get("internetMessageId") or message.get("id") or "")
    subject = str(message.get("subject") or "")
    text = graph_message_text(message)
    return ChannelInboundMessage(
        channel="email",
        external_user_id=sender,
        external_message_id=message_id,
        thread_id=str(message.get("conversationId") or sender),
        text=text,
        metadata={
            "subject": subject,
            "message_id": message_id,
            "graph_message_id": str(message.get("id") or ""),
            "mailbox": settings.microsoft_mailbox,
            "source": "microsoft_graph",
        },
    )


def graph_message_text(message: dict[str, Any]) -> str:
    body = message.get("body")
    if isinstance(body, dict):
        content = str(body.get("content") or "")
        if str(body.get("contentType") or "").lower() == "html":
            return _html_to_text(content)
        if content.strip():
            return content.strip()
    return str(message.get("bodyPreview") or "").strip()


def validate_graph_notification_client_state(
    notification: dict[str, Any],
    expected_client_state: str,
) -> bool:
    if not expected_client_state:
        return True
    return notification.get("clientState") == expected_client_state


def _email_address(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    email_address = value.get("emailAddress")
    if not isinstance(email_address, dict):
        return ""
    return str(email_address.get("address") or "")


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", "", value)
    return html.unescape(value).strip()


def _notification_url(settings: Settings) -> str:
    return f"{settings.public_gateway_url.rstrip('/')}/webhooks/microsoft/email"


def _resource_path(resource: str) -> str:
    parsed = urlparse(resource)
    path = parsed.path if parsed.scheme else resource
    path = path.lstrip("/")
    if path.startswith("v1.0/"):
        path = path.removeprefix("v1.0/")
    return path


def _message_id_from_notification(notification: dict[str, Any]) -> str | None:
    resource_data = notification.get("resourceData")
    if isinstance(resource_data, dict):
        message_id = resource_data.get("id")
        if isinstance(message_id, str) and message_id:
            return message_id
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
