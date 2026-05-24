from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from channel_gateway.app.agent_client import AgentClient
from channel_gateway.app.config import Settings
from channel_gateway.app.message_handler import handle_inbound_message
from channel_gateway.app.schemas import ChannelInboundMessage
from channel_gateway.app.session_store import ChannelSessionStore


def build_router(settings: Settings, store: ChannelSessionStore, agent_client: AgentClient) -> APIRouter:
    router = APIRouter(prefix="/webhooks/whatsapp/twilio", tags=["whatsapp"])

    @router.post("")
    async def whatsapp_webhook(
        request: Request,
        twilio_signature: str | None = Header(default=None, alias="X-Twilio-Signature"),
    ) -> dict[str, str]:
        body = await request.body()
        form = {key: values[0] for key, values in parse_qs(body.decode()).items()}
        if settings.twilio_auth_token and not validate_twilio_signature(
            str(request.url),
            form,
            twilio_signature or "",
            settings.twilio_auth_token,
        ):
            raise HTTPException(status_code=401, detail="Invalid Twilio signature")

        inbound = parse_twilio_form(form)
        try:
            await handle_inbound_message(
                inbound,
                settings,
                store,
                agent_client,
                send_twilio_whatsapp_message,
            )
        except Exception:
            raise
        return {"status": "ok"}

    return router


def parse_twilio_form(form: dict[str, str]) -> ChannelInboundMessage:
    from_number = form.get("From", "")
    body = form.get("Body", "")
    message_sid = form.get("MessageSid") or form.get("SmsMessageSid") or ""
    normalized_user = from_number.removeprefix("whatsapp:")
    return ChannelInboundMessage(
        channel="whatsapp",
        external_user_id=normalized_user,
        external_message_id=message_sid,
        thread_id=normalized_user,
        text=body,
        metadata={"from": from_number, "to": form.get("To", ""), "message_sid": message_sid},
    )


def validate_twilio_signature(
    url: str,
    params: dict[str, str],
    signature: str,
    auth_token: str,
) -> bool:
    signed = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), signed.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


async def send_twilio_whatsapp_message(
    settings: Settings,
    inbound: ChannelInboundMessage,
    text: str,
) -> None:
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        return
    to_number = inbound.metadata.get("from") or f"whatsapp:{inbound.external_user_id}"
    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Messages.json"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            data={"From": settings.twilio_whatsapp_from, "To": to_number, "Body": text},
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
        )
        response.raise_for_status()
