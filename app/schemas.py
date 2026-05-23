from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ChannelName = Literal["telegram", "whatsapp", "email"]


class ChannelInboundMessage(BaseModel):
    channel: ChannelName
    external_user_id: str
    external_message_id: str
    text: str
    thread_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChannelOutboundMessage(BaseModel):
    text: str
    reply_to: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str
    user_id: str
    session_id: str | None = None
    agent: str = "root"
    context: dict[str, Any] = Field(default_factory=dict)


class ChatTurnResponse(BaseModel):
    session_id: str
    agent: str
    message: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
