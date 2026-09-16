"""Contract test: the queue service must match the real repository's interface.

The service's unit tests use an in-memory double that accepts ``**values``, so a
call with a keyword the real repository does not accept passes every unit test
and then fails in production with a TypeError. This test drives the real
repository against a real database, which is the only way to catch that class of
mismatch.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from agent_server.infra.repositories.message_queue import MessageQueueRepository
from agent_server.services.message_queue import MessageQueueError, MessageQueueService

DATABASE_URL = os.environ.get("AGENT_SERVER_TEST_DB_URL") or os.environ.get(
    "AGENT_SERVER_DB_URL"
)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not str(DATABASE_URL).startswith("postgresql"),
    reason="needs AGENT_SERVER_DB_URL pointing at PostgreSQL",
)


def _engine():
    return create_async_engine(str(DATABASE_URL))


async def _seed_session(engine, session_id: str, user_id: str) -> None:
    """Insert the parent rows the queue's foreign keys require."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, password_hash, role, disabled, created_at, updated_at, display_name) "
                "VALUES (:id, 'x', 'admin', 0, 'now', 'now', 'test') ON CONFLICT (id) DO NOTHING"
            ),
            {"id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO connectors (id, user_id, name, status, token_hash, token_prefix, "
                "connector_kind, created_at, updated_at, revoked) "
                "VALUES (:id, :uid, 'test', 'online', 'h', 'p', 'cli', 'now', 'now', 0) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": f"conn_{session_id}", "uid": user_id},
        )
        # Only the columns the queue's foreign keys and NOT NULL constraints need.
        await conn.execute(
            text(
                "INSERT INTO sessions (id, connector_id, runtime, runtime_id, status, seq, updated_seq, "
                "takeover, created_at, updated_at) "
                "VALUES (:id, :cid, 'dsh', 'rt_test', 'idle', 0, 0, 1, 'now', 'now') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": session_id, "cid": f"conn_{session_id}"},
        )


async def _cleanup(engine, session_id: str, user_id: str) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM session_message_queue WHERE session_id = :s"), {"s": session_id})
        await conn.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": session_id})
        await conn.execute(text("DELETE FROM connectors WHERE id = :c"), {"c": f"conn_{session_id}"})
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def test_enqueue_against_the_real_repository() -> None:
    """The exact call the server makes must be accepted by the real repository."""

    async def run() -> None:
        suffix = uuid.uuid4().hex[:12]
        session_id = f"sess_qtest_{suffix}"
        user_id = f"usr_qtest_{suffix}"
        engine = _engine()
        try:
            await _seed_session(engine, session_id, user_id)
            service = MessageQueueService(MessageQueueRepository(engine))

            # enqueue passes created_at and updated_at; the real append must accept both.
            item = await service.enqueue(
                session_id=session_id,
                user_id=user_id,
                content="hello queue",
            )
            assert item.id
            assert item.status == "queued"
            assert item.created_at and item.updated_at

            listed = await service.list(session_id)
            assert [entry.content for entry in listed] == ["hello queue"]

            updated = await service.update(session_id=session_id, item_id=item.id, content="edited")
            assert updated.content == "edited"
            assert updated.updated_seq == item.updated_seq + 1

            # A second message can still be edited and removed while pending.
            second = await service.enqueue(
                session_id=session_id,
                user_id=user_id,
                content="second",
            )
            removed = await service.remove(session_id=session_id, item_id=second.id)
            assert removed.id == second.id

            claimed = await service.claim_next(session_id)
            assert claimed is not None and claimed.status == "sending"

            await service.mark_sent(claimed.id)
            assert (await service.claim_next(session_id)) is None

            # A dispatched item is owned by the runtime: it must not be removable.
            with pytest.raises(MessageQueueError):
                await service.remove(session_id=session_id, item_id=item.id)
            assert [entry.id for entry in await service.list(session_id)] == [item.id]
        finally:
            await _cleanup(engine, session_id, user_id)
            await engine.dispose()

    asyncio.run(run())


def test_claim_next_is_exclusive_against_the_real_repository() -> None:
    """The status guard must live in the UPDATE, not in a read-then-write."""

    async def run() -> None:
        suffix = uuid.uuid4().hex[:12]
        session_id = f"sess_qtest_{suffix}"
        user_id = f"usr_qtest_{suffix}"
        engine = _engine()
        try:
            await _seed_session(engine, session_id, user_id)
            service = MessageQueueService(MessageQueueRepository(engine))
            await service.enqueue(session_id=session_id, user_id=user_id, content="only")

            results = await asyncio.gather(
                service.claim_next(session_id),
                service.claim_next(session_id),
                service.claim_next(session_id),
            )
            claimed = [item for item in results if item is not None]
            assert len(claimed) == 1, "one queued row must be claimed exactly once"
        finally:
            await _cleanup(engine, session_id, user_id)
            await engine.dispose()

    asyncio.run(run())
