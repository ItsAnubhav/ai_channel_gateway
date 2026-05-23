from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from channel_gateway.app.agent_client import AgentClient
from channel_gateway.app.channels import (
    email_polling,
    email_smtp,
    microsoft_graph_email,
    telegram,
    whatsapp_twilio,
)
from channel_gateway.app.config import get_settings
from channel_gateway.app.session_store import ChannelSessionStore

logger = logging.getLogger(__name__)

settings = get_settings()
store = ChannelSessionStore(settings.gateway_database_url)
agent_client = AgentClient(settings)
graph_client = microsoft_graph_email.MicrosoftGraphClient(settings)
email_stop_event = asyncio.Event()
graph_stop_event = asyncio.Event()


def _mask_connection_string(url: str) -> str:
    import re
    return re.sub(r'(://)([^:]+):([^@]+)(@)', r'\1\2:***\4', url)


async def initialize_database_with_retry(max_retries: int = 30, base_delay: float = 1.0) -> None:
    masked_url = _mask_connection_string(settings.gateway_database_url)
    for attempt in range(max_retries):
        try:
            await store.initialize()
            logger.info("Database initialized successfully")
            return
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"Failed to initialize database after {max_retries} attempts (URL: {masked_url}): {e}")
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"Database initialization failed (attempt {attempt + 1}/{max_retries}, URL: {masked_url}), retrying in {delay}s: {e}")
            await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await initialize_database_with_retry()
    poller = asyncio.create_task(
        email_polling.run_email_polling(
            settings,
            store,
            agent_client,
            email_smtp.send_email_reply,
            email_stop_event,
        )
    )
    graph_subscription_manager = asyncio.create_task(
        microsoft_graph_email.run_subscription_manager(
            settings,
            store,
            graph_client,
            graph_stop_event,
        )
    )
    yield
    email_stop_event.set()
    graph_stop_event.set()
    await poller
    await graph_subscription_manager


app = FastAPI(title="ADK Channel Gateway", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(telegram.build_router(settings, store, agent_client))
app.include_router(whatsapp_twilio.build_router(settings, store, agent_client))
app.include_router(microsoft_graph_email.build_router(settings, store, agent_client, graph_client))
