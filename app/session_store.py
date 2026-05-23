from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table, delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def _now() -> datetime:
    return datetime.now(UTC)


class ChannelSessionStore:
    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, future=True)
        self._dialect = self._engine.dialect.name
        self._metadata = MetaData()
        self.sessions = Table(
            "channel_threads",
            self._metadata,
            Column("channel", String, primary_key=True),
            Column("external_user_id", String, primary_key=True),
            Column("thread_id", String, primary_key=True),
            Column("agent_session_id", String, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self.messages = Table(
            "channel_messages",
            self._metadata,
            Column("channel", String, primary_key=True),
            Column("external_message_id", String, primary_key=True),
            Column("status", String, nullable=False),
            Column("error_text", String, nullable=False, default=""),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self.state = Table(
            "gateway_state",
            self._metadata,
            Column("key", String, primary_key=True),
            Column("value", String, nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )

    async def initialize(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(self._metadata.create_all)

    async def get_session_id(
        self,
        *,
        channel: str,
        external_user_id: str,
        thread_id: str,
    ) -> str | None:
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(self.sessions.c.agent_session_id).where(
                        self.sessions.c.channel == channel,
                        self.sessions.c.external_user_id == external_user_id,
                        self.sessions.c.thread_id == thread_id,
                    )
                )
            ).first()
        return str(row[0]) if row else None

    async def upsert_session_id(
        self,
        *,
        channel: str,
        external_user_id: str,
        thread_id: str,
        agent_session_id: str,
    ) -> None:
        now = _now()
        statement = (
            self._insert(self.sessions)
            .values(
                channel=channel,
                external_user_id=external_user_id,
                thread_id=thread_id,
                agent_session_id=agent_session_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["channel", "external_user_id", "thread_id"],
                set_={"agent_session_id": agent_session_id, "updated_at": now},
            )
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def reset_session(
        self,
        *,
        channel: str,
        external_user_id: str,
        thread_id: str,
    ) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(self.sessions).where(
                    self.sessions.c.channel == channel,
                    self.sessions.c.external_user_id == external_user_id,
                    self.sessions.c.thread_id == thread_id,
                )
            )

    async def mark_message_started(self, *, channel: str, external_message_id: str) -> bool:
        now = _now()
        statement = (
            self._insert(self.messages)
            .values(
                channel=channel,
                external_message_id=external_message_id,
                status="processing",
                error_text="",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["channel", "external_message_id"])
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
        return result.rowcount == 1

    async def mark_message_done(
        self,
        *,
        channel: str,
        external_message_id: str,
        status: str = "done",
        error_text: str = "",
    ) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(self.messages)
                .where(
                    self.messages.c.channel == channel,
                    self.messages.c.external_message_id == external_message_id,
                )
                .values(status=status, error_text=error_text, updated_at=_now())
            )

    async def get_value(self, key: str) -> str | None:
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(select(self.state.c.value).where(self.state.c.key == key))
            ).first()
        return str(row[0]) if row else None

    async def set_value(self, key: str, value: str) -> None:
        now = _now()
        statement = (
            self._insert(self.state)
            .values(key=key, value=value, updated_at=now)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value": value, "updated_at": now},
            )
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    def _insert(self, table: Table):
        if self._dialect == "postgresql":
            return postgres_insert(table)
        return sqlite_insert(table)
