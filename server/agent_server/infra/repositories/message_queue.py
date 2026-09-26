from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_server.infra.db import session_message_queue as queue_t


class MessageQueueRepository:
    """Durable per-session outgoing message queue.

    A session runs one turn at a time; messages submitted while it is busy are
    appended here and dispatched in ``position`` order as each turn finishes.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_for_session(self, session_id: str) -> list[Any]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(queue_t)
                    .where(queue_t.c.session_id == session_id)
                    .order_by(queue_t.c.position, queue_t.c.created_at)
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def next_position(self, session_id: str) -> int:
        async with self._engine.connect() as conn:
            highest = (
                await conn.execute(
                    select(func.max(queue_t.c.position)).where(
                        queue_t.c.session_id == session_id
                    )
                )
            ).scalar()
        return int(highest or 0) + 1

    async def append(
        self,
        *,
        item_id: str,
        session_id: str,
        user_id: str | None,
        position: int,
        content: str,
        attachments: list[dict[str, Any]] | None,
        selections: dict[str, Any] | None,
        client_message_id: str | None,
        created_at: str,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(queue_t).values(
                    id=item_id,
                    session_id=session_id,
                    user_id=user_id,
                    position=position,
                    status="queued",
                    content=content,
                    attachments_json=json.dumps(attachments or [], ensure_ascii=False),
                    selections_json=json.dumps(selections or {}, ensure_ascii=False),
                    client_message_id=client_message_id,
                    created_at=created_at,
                    updated_at=updated_at or created_at,
                    updated_seq=0,
                )
            )
        stored = await self.get(item_id)
        assert stored is not None
        return stored

    async def get(self, item_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(queue_t).where(queue_t.c.id == item_id))
            ).mappings().first()
        return dict(row) if row is not None else None

    async def update_content(
        self,
        item_id: str,
        *,
        content: str,
        attachments: list[dict[str, Any]] | None,
        updated_at: str,
        updated_seq: int,
        status: str = "queued",
    ) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(queue_t)
                .where(
                    queue_t.c.id == item_id,
                    queue_t.c.status.in_(("queued", "failed")),
                )
                .values(
                    content=content,
                    attachments_json=json.dumps(attachments or [], ensure_ascii=False),
                    updated_at=updated_at,
                    updated_seq=updated_seq,
                    status=status,
                    error_code=None,
                    error_message=None,
                )
            )
            if result.rowcount != 1:
                return None
        return await self.get(item_id)

    async def prioritize(
        self,
        session_id: str,
        item_id: str,
        *,
        status: str,
        error_code: str | None,
        error_message: str | None,
        updated_at: str,
    ) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            lowest = (
                await conn.execute(
                    select(func.min(queue_t.c.position)).where(
                        queue_t.c.session_id == session_id
                    )
                )
            ).scalar()
            result = await conn.execute(
                update(queue_t)
                .where(
                    queue_t.c.id == item_id,
                    queue_t.c.session_id == session_id,
                    queue_t.c.status.in_(("queued", "failed")),
                )
                .values(
                    position=int(lowest or 0) - 1,
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                    updated_at=updated_at,
                )
            )
            if result.rowcount != 1:
                return None
        return await self.get(item_id)

    async def set_status(
        self,
        item_id: str,
        *,
        status: str,
        updated_at: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(queue_t)
                .where(queue_t.c.id == item_id)
                .values(
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                    updated_at=updated_at,
                )
            )

    async def remove(self, item_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(delete(queue_t).where(queue_t.c.id == item_id))

    async def remove_pending(self, session_id: str, item_id: str) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(queue_t).where(
                    queue_t.c.id == item_id,
                    queue_t.c.session_id == session_id,
                    queue_t.c.status.in_(("queued", "failed")),
                )
            )
            return result.rowcount == 1

    async def clear_for_session(self, session_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(delete(queue_t).where(queue_t.c.session_id == session_id))

    async def head_queued(self, session_id: str) -> dict[str, Any] | None:
        """The next item to dispatch, or None when the queue is empty."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(queue_t)
                    .where(
                        queue_t.c.session_id == session_id,
                        queue_t.c.status == "queued",
                    )
                    .order_by(queue_t.c.position, queue_t.c.created_at)
                    .limit(1)
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def has_pending(self, session_id: str) -> bool:
        async with self._engine.connect() as conn:
            found = (
                await conn.execute(
                    select(queue_t.c.id)
                    .where(
                        queue_t.c.session_id == session_id,
                        queue_t.c.status.in_(("queued", "sending")),
                    )
                    .limit(1)
                )
            ).first()
        return found is not None

    async def claim_next(self, session_id: str, *, updated_at: str) -> dict[str, Any] | None:
        """Atomically take the head item so two workers cannot dispatch it twice.

        The status guard is part of the UPDATE rather than a read-then-write, so
        a concurrent dispatcher that already claimed the row updates nothing and
        this returns None.
        """
        async with self._engine.begin() as conn:
            candidate = (
                await conn.execute(
                    select(queue_t.c.id)
                    .where(
                        queue_t.c.session_id == session_id,
                        queue_t.c.status == "queued",
                    )
                    .order_by(queue_t.c.position, queue_t.c.created_at)
                    .limit(1)
                )
            ).scalar()
            if candidate is None:
                return None
            result = await conn.execute(
                update(queue_t)
                .where(queue_t.c.id == candidate, queue_t.c.status == "queued")
                .values(status="sending", updated_at=updated_at)
            )
            if result.rowcount != 1:
                return None
        return await self.get(candidate)
