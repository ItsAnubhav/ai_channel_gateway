from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from channel_gateway.app.agent_client import AgentClient
from channel_gateway.app.config import Settings
from channel_gateway.app.formatters import format_channel_response
from channel_gateway.app.schemas import ChannelInboundMessage, ChatRequest
from channel_gateway.app.session_store import ChannelSessionStore


logger = logging.getLogger(__name__)


def build_router(settings: Settings, store: ChannelSessionStore, agent_client: AgentClient) -> APIRouter:
    router = APIRouter(prefix="/webhooks/telegram", tags=["telegram"])

    @router.post("")
    async def telegram_webhook(
        request: Request,
        secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> dict[str, str]:
        if settings.telegram_webhook_secret and secret_token != settings.telegram_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

        inbound = parse_telegram_update(await request.json())
        if inbound is None:
            return {"status": "ignored"}

        asyncio.create_task(
            handle_telegram_inbound_with_typing(inbound, settings, store, agent_client)
        )
        return {"status": "accepted"}

    return router


async def handle_telegram_inbound_with_typing(
    inbound: ChannelInboundMessage,
    settings: Settings,
    store: ChannelSessionStore,
    agent_client: AgentClient,
) -> None:
    started = time.perf_counter()
    typing_task = asyncio.create_task(send_telegram_typing_until_cancelled(settings, inbound))
    try:
        await handle_inbound_message(inbound, settings, store, agent_client, send_telegram_message)
        logger.info(
            "Telegram message processed channel_message_id=%s elapsed_s=%.3f",
            inbound.external_message_id,
            time.perf_counter() - started,
        )
    except Exception:
        logger.exception(
            "Telegram message failed channel_message_id=%s elapsed_s=%.3f",
            inbound.external_message_id,
            time.perf_counter() - started,
        )
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task


def parse_telegram_update(payload: dict[str, Any]) -> ChannelInboundMessage | None:
    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    chat = message.get("chat")
    if not isinstance(text, str) or not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return None
    external_user_id = str(chat_id)
    return ChannelInboundMessage(
        channel="telegram",
        external_user_id=external_user_id,
        external_message_id=str(message_id),
        thread_id=external_user_id,
        text=text,
        metadata={"chat_id": external_user_id},
    )


async def handle_inbound_message(
    inbound: ChannelInboundMessage,
    settings: Settings,
    store: ChannelSessionStore,
    agent_client: AgentClient,
    sender,
) -> None:
    if not await store.mark_message_started(
        channel=inbound.channel,
        external_message_id=inbound.external_message_id,
    ):
        return

    thread_id = inbound.thread_id or inbound.external_user_id
    try:
        if inbound.text.strip() == "/new":
            await store.reset_session(
                channel=inbound.channel,
                external_user_id=inbound.external_user_id,
                thread_id=thread_id,
            )
            await sender(settings, inbound, "Started a new chat.")
            await store.mark_message_done(
                channel=inbound.channel,
                external_message_id=inbound.external_message_id,
            )
            return

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
        text = format_channel_response(
            message=response.message,
            artifacts=response.artifacts,
            result_limit=settings.channel_result_limit,
            public_app_url=settings.public_app_url,
        )
        await sender(settings, inbound, text)
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


async def send_telegram_message(settings: Settings, inbound: ChannelInboundMessage, text: str) -> None:
    if not settings.telegram_bot_token:
        return
    chat_id = inbound.metadata.get("chat_id") or inbound.external_user_id
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        response.raise_for_status()


async def send_telegram_typing_until_cancelled(
    settings: Settings,
    inbound: ChannelInboundMessage,
) -> None:
    if not settings.telegram_bot_token:
        return
    chat_id = inbound.metadata.get("chat_id") or inbound.external_user_id
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendChatAction",
                    json={"chat_id": chat_id, "action": "typing"},
                )
                response.raise_for_status()
            except Exception:
                logger.warning("Failed to send Telegram typing action", exc_info=True)
            await asyncio.sleep(4)
