from __future__ import annotations

from dataclasses import dataclass

from channel_gateway.app.schemas import ChannelInboundMessage, ChannelName


@dataclass(frozen=True)
class ChannelIdentity:
    channel: ChannelName
    external_user_id: str
    thread_id: str
    agent_user_id: str


def resolve_channel_identity(inbound: ChannelInboundMessage) -> ChannelIdentity:
    external_user_id = _normalize_external_user_id(inbound)
    thread_id = _normalize_thread_id(inbound, external_user_id)
    return ChannelIdentity(
        channel=inbound.channel,
        external_user_id=external_user_id,
        thread_id=thread_id,
        agent_user_id=f"{inbound.channel}:{external_user_id}",
    )


def _normalize_external_user_id(inbound: ChannelInboundMessage) -> str:
    if inbound.channel == "telegram":
        return str(inbound.metadata.get("chat_id") or inbound.external_user_id)
    if inbound.channel == "whatsapp":
        return inbound.external_user_id.removeprefix("whatsapp:")
    if inbound.channel == "email":
        return inbound.external_user_id.strip().lower()
    if inbound.channel == "teams":
        tenant_id = str(inbound.metadata.get("tenant_id") or "").strip()
        user_id = str(
            inbound.metadata.get("aad_object_id")
            or inbound.metadata.get("from_id")
            or inbound.external_user_id
        ).strip()
        if tenant_id and not user_id.startswith(f"{tenant_id}:"):
            return f"{tenant_id}:{user_id}"
        return user_id
    return inbound.external_user_id


def _normalize_thread_id(inbound: ChannelInboundMessage, external_user_id: str) -> str:
    if inbound.channel in {"telegram", "whatsapp"}:
        return external_user_id
    if inbound.channel == "teams":
        return str(inbound.metadata.get("conversation_id") or inbound.thread_id or external_user_id)
    return inbound.thread_id or external_user_id
