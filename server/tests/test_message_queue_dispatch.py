"""Dispatcher behaviour: one message per finished turn, in order, never lost.

The queue only helps if something drains it correctly. These cases pin the two
rules that matter: a finished turn releases exactly the next message, and a send
that fails does not silently discard the user's message.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_server.services.message_queue import (
    QUEUE_STATUS_FAILED,
    QUEUE_STATUS_QUEUED,
    QUEUE_STATUS_SENDING,
    MessageQueueService,
)
from agent_server.services.message_queue_dispatch import (
    MessageQueueDispatcher,
    QueueDispatchBusy,
)
from tests.test_message_queue import FakeQueueStore


class RecordingSender:
    """Records dispatched messages and can be told to fail or report busy."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail_with = fail_with

    async def send_queued_message(self, session_id, item, *, user_id):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((session_id, item))


def _dispatcher(sender: RecordingSender) -> tuple[MessageQueueDispatcher, MessageQueueService]:
    queue = MessageQueueService(FakeQueueStore())
    return MessageQueueDispatcher(queue, sender), queue


async def _enqueue(queue: MessageQueueService, text: str, session: str = "s1") -> None:
    await queue.enqueue(session_id=session, user_id="u1", content=text)


def test_dispatches_the_head_message() -> None:
    async def run() -> None:
        sender = RecordingSender()
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "first")
        assert await dispatcher.dispatch_next("s1") is True
        assert [item["content"] for _, item in sender.sent] == ["first"]

    asyncio.run(run())


def test_successful_dispatch_removes_the_item_from_the_queue() -> None:
    async def run() -> None:
        sender = RecordingSender()
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "first")
        await dispatcher.dispatch_next("s1")
        assert await queue.list("s1") == []

    asyncio.run(run())


def test_only_one_message_is_sent_per_finished_turn() -> None:
    async def run() -> None:
        sender = RecordingSender()
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "first")
        await _enqueue(queue, "second")
        await dispatcher.dispatch_next("s1")
        # Sending starts a new turn; the next item must wait for it to finish.
        assert [item["content"] for _, item in sender.sent] == ["first"]
        remaining = await queue.list("s1")
        assert [item.content for item in remaining] == ["second"]

    asyncio.run(run())


def test_successive_turns_advance_the_queue_in_order() -> None:
    async def run() -> None:
        sender = RecordingSender()
        dispatcher, queue = _dispatcher(sender)
        for text in ("one", "two", "three"):
            await _enqueue(queue, text)
        for _ in range(3):
            await dispatcher.dispatch_next("s1")
        assert [item["content"] for _, item in sender.sent] == ["one", "two", "three"]

    asyncio.run(run())


def test_empty_queue_dispatches_nothing() -> None:
    async def run() -> None:
        sender = RecordingSender()
        dispatcher, _queue = _dispatcher(sender)
        assert await dispatcher.dispatch_next("s1") is False
        assert sender.sent == []

    asyncio.run(run())


def test_a_busy_runtime_returns_the_item_to_the_queue() -> None:
    async def run() -> None:
        sender = RecordingSender(fail_with=QueueDispatchBusy())
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "first")
        assert await dispatcher.dispatch_next("s1") is False
        stored = (await queue.list("s1"))[0]
        # Nothing was sent, so the message must still be pending, not failed.
        assert stored.status == QUEUE_STATUS_QUEUED

    asyncio.run(run())


def test_a_requeued_item_is_dispatched_by_the_next_turn() -> None:
    async def run() -> None:
        sender = RecordingSender(fail_with=QueueDispatchBusy())
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "first")
        await dispatcher.dispatch_next("s1")
        sender.fail_with = None
        assert await dispatcher.dispatch_next("s1") is True
        assert [item["content"] for _, item in sender.sent] == ["first"]

    asyncio.run(run())


def test_a_failed_send_is_recorded_not_dropped() -> None:
    async def run() -> None:
        sender = RecordingSender(fail_with=RuntimeError("connector offline"))
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "first")
        assert await dispatcher.dispatch_next("s1") is False
        stored = (await queue.list("s1"))[0]
        assert stored.status == QUEUE_STATUS_FAILED
        assert stored.error_code == "queue_dispatch_failed"
        assert "connector offline" in (stored.error_message or "")

    asyncio.run(run())


def test_a_failed_item_does_not_block_the_next_one() -> None:
    async def run() -> None:
        sender = RecordingSender(fail_with=RuntimeError("boom"))
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "first")
        await _enqueue(queue, "second")
        await dispatcher.dispatch_next("s1")
        sender.fail_with = None
        await dispatcher.dispatch_next("s1")
        # The failed item is retired; the queue continues with what follows.
        assert [item["content"] for _, item in sender.sent] == ["second"]

    asyncio.run(run())


def test_queues_for_other_sessions_are_untouched() -> None:
    async def run() -> None:
        sender = RecordingSender()
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "s1 message", session="s1")
        await _enqueue(queue, "s2 message", session="s2")
        await dispatcher.dispatch_next("s1")
        assert [item["content"] for _, item in sender.sent] == ["s1 message"]
        assert [item.content for item in await queue.list("s2")] == ["s2 message"]

    asyncio.run(run())


def test_concurrent_drains_send_each_item_once() -> None:
    async def run() -> None:
        sender = RecordingSender()
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "only")
        results = await asyncio.gather(
            dispatcher.dispatch_next("s1"),
            dispatcher.dispatch_next("s1"),
        )
        assert sum(1 for sent in results if sent) == 1
        assert len(sender.sent) == 1

    asyncio.run(run())


def test_dispatch_leaves_no_item_stuck_in_flight() -> None:
    async def run() -> None:
        sender = RecordingSender()
        dispatcher, queue = _dispatcher(sender)
        await _enqueue(queue, "first")
        await dispatcher.dispatch_next("s1")
        assert all(item.status != QUEUE_STATUS_SENDING for item in await queue.list("s1"))

    asyncio.run(run())


class _IdleRuntimeStore:
    """Just enough store to resolve a runtime back to its sessions."""

    def __init__(self, rows: list[tuple[str, str, str | None, str]]) -> None:
        # (connector_id, session_id, runtime_id, runtime)
        self._rows = rows
        self.listed: list[str] = []

    async def list_sessions_for_connector(self, connector_id: str):
        self.listed.append(connector_id)
        return [
            _FakeSession(session_id, runtime_id, runtime)
            for cid, session_id, runtime_id, runtime in self._rows
            if cid == connector_id
        ]


class _FakeSession:
    def __init__(self, session_id: str, runtime_id: str | None, runtime: str) -> None:
        self.id = session_id
        self.runtimeId = runtime_id
        self.runtime = runtime


def test_an_idle_runtime_resolves_to_the_sessions_it_owns() -> None:
    """The queue dispatch hangs off a runtime going idle.

    The `runtime.statusChanged` notification produces no ingest effect, so the
    status never reaches the bucket the dispatch reads and a queued message
    could sit in the panel forever. The trigger is resolved from the runtime
    instead, which is what this pins.
    """

    async def run():
        from agent_server.services.connector_ingest import ConnectorIngestService

        service = ConnectorIngestService.__new__(ConnectorIngestService)
        service._store = _IdleRuntimeStore([
            ("conn_1", "sess_a", "rti_a", "codex"),
            ("conn_1", "sess_b", "rti_b", "dsh"),
            ("conn_1", "sess_c", None, "codex"),
            ("conn_2", "sess_d", "rti_a", "codex"),
        ])
        found = await service._sessions_for_idle_runtimes({("conn_1", "rti_a")})
        # Only the session bound to that instance, on that connector.
        assert found == ["sess_a"]
        assert service._store.listed == ["conn_1"]

    asyncio.run(run())


def test_a_session_without_an_instance_id_matches_its_runtime_type() -> None:
    """A runtime with one instance is addressed by type, as the sender does."""

    async def run():
        from agent_server.services.connector_ingest import ConnectorIngestService

        service = ConnectorIngestService.__new__(ConnectorIngestService)
        service._store = _IdleRuntimeStore([("conn_1", "sess_a", None, "codex")])
        found = await service._sessions_for_idle_runtimes({("conn_1", "codex")})
        assert found == ["sess_a"]

    asyncio.run(run())


def test_listing_failure_does_not_break_ingest() -> None:
    """A queue problem must never fail the ingest that observed the change."""

    async def run():
        from agent_server.services.connector_ingest import ConnectorIngestService

        class _Broken:
            async def list_sessions_for_connector(self, connector_id: str):
                raise RuntimeError("database is away")

        service = ConnectorIngestService.__new__(ConnectorIngestService)
        service._store = _Broken()
        assert await service._sessions_for_idle_runtimes({("conn_1", "rti_a")}) == []

    asyncio.run(run())
