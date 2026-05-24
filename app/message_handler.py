from __future__ import annotations

from collections.abc import Awaitable, Callable

from channel_gateway.app.agent_client import AgentClient
from channel_gateway.app.channel_identity import resolve_channel_identity
from channel_gateway.app.config import Settings
from channel_gateway.app.formatters import format_channel_response
from channel_gateway.app.schemas import ChannelInboundMessage, ChatRequest
from channel_gateway.app.session_store import ChannelSessionStore

ChannelSender = Callable[[Settings, ChannelInboundMessage, str], Awaitable[None]]


async def handle_inbound_message(
    inbound: ChannelInboundMessage,
    settings: Settings,
    store: ChannelSessionStore,
    agent_client: AgentClient,
    sender: ChannelSender,
    *,
    reset_commands: set[str] | None = None,
) -> None:
    identity = resolve_channel_identity(inbound)
    if not await store.mark_message_started(
        channel=identity.channel,
        external_message_id=inbound.external_message_id,
    ):
        return

    commands = {"/new", "new chat"} if reset_commands is None else reset_commands
    try:
        if inbound.text.strip().lower() in commands:
            await store.reset_session(
                channel=identity.channel,
                external_user_id=identity.external_user_id,
                thread_id=identity.thread_id,
            )
            await sender(settings, inbound, "Started a new chat.")
            await store.mark_message_done(
                channel=identity.channel,
                external_message_id=inbound.external_message_id,
            )
            return

        session_id = await store.get_session_id(
            channel=identity.channel,
            external_user_id=identity.external_user_id,
            thread_id=identity.thread_id,
        )
        response = await agent_client.run_turn(
            ChatRequest(
                message=inbound.text,
                user_id=identity.agent_user_id,
                session_id=session_id,
                agent=settings.default_agent,
                context={"channel": inbound.channel, "metadata": inbound.metadata},
            )
        )
        await store.upsert_session_id(
            channel=identity.channel,
            external_user_id=identity.external_user_id,
            thread_id=identity.thread_id,
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
            channel=identity.channel,
            external_message_id=inbound.external_message_id,
        )
    except Exception as exc:
        await store.mark_message_done(
            channel=identity.channel,
            external_message_id=inbound.external_message_id,
            status="failed",
            error_text=str(exc),
        )
        raise
