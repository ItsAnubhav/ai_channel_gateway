from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from email.message import Message
from email.utils import parseaddr

from channel_gateway.app.agent_client import AgentClient
from channel_gateway.app.config import Settings
from channel_gateway.app.formatters import format_channel_response
from channel_gateway.app.schemas import ChannelInboundMessage, ChatRequest
from channel_gateway.app.session_store import ChannelSessionStore

logger = logging.getLogger(__name__)


async def run_email_polling(
    settings: Settings,
    store: ChannelSessionStore,
    agent_client: AgentClient,
    reply_sender,
    stop_event: asyncio.Event,
) -> None:
    if not settings.email_imap_host or not settings.email_imap_user:
        return

    while not stop_event.is_set():
        try:
            messages = await asyncio.to_thread(_fetch_unseen_messages, settings)
            for inbound, original in messages:
                await _handle_email_message(inbound, original, settings, store, agent_client, reply_sender)
        except Exception:
            logger.exception("Email polling failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.email_poll_interval_seconds)
        except TimeoutError:
            continue


def _fetch_unseen_messages(settings: Settings) -> list[tuple[ChannelInboundMessage, Message]]:
    messages: list[tuple[ChannelInboundMessage, Message]] = []
    with imaplib.IMAP4_SSL(settings.email_imap_host, settings.email_imap_port) as mailbox:
        mailbox.login(settings.email_imap_user, settings.email_imap_password)
        mailbox.select(settings.email_imap_folder)
        _, data = mailbox.search(None, "UNSEEN")
        for uid in data[0].split():
            _, fetched = mailbox.fetch(uid, "(RFC822)")
            raw = fetched[0][1]
            message = email.message_from_bytes(raw)
            messages.append((parse_email_message(message, uid.decode()), message))
    return messages


async def _handle_email_message(
    inbound: ChannelInboundMessage,
    original: Message,
    settings: Settings,
    store: ChannelSessionStore,
    agent_client: AgentClient,
    reply_sender,
) -> None:
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
        text = format_channel_response(
            message=response.message,
            artifacts=response.artifacts,
            result_limit=settings.channel_result_limit,
            public_app_url=settings.public_app_url,
        )
        reply_sender(
            settings,
            to_address=inbound.external_user_id,
            subject=str(original.get("Subject") or "Travel assistant"),
            body=text,
            in_reply_to=str(original.get("Message-ID") or ""),
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


def parse_email_message(message: Message, fallback_uid: str) -> ChannelInboundMessage:
    _, from_address = parseaddr(str(message.get("From") or ""))
    message_id = str(message.get("Message-ID") or fallback_uid)
    references = str(message.get("References") or message.get("In-Reply-To") or message_id)
    return ChannelInboundMessage(
        channel="email",
        external_user_id=from_address,
        external_message_id=message_id,
        thread_id=references,
        text=extract_plain_text(message),
        metadata={"subject": str(message.get("Subject") or ""), "message_id": message_id},
    )


def extract_plain_text(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition") or ""
            ).lower():
                return _decode_part(part)
        return ""
    return _decode_part(message)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace").strip()
