"""Queue semantics: ordering, pending-only mutation, and single-claim dispatch.

The queue exists so a message sent during a running turn is not lost. These
cases pin the rules that make that safe: FIFO order, no rewriting an item the
runtime already took, and no double dispatch when two workers race.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_server.services.message_queue import (
    QUEUE_STATUS_FAILED,
    QUEUE_STATUS_QUEUED,
    QUEUE_STATUS_SENDING,
    QUEUE_STATUS_SENT,
    MessageQueueError,
    MessageQueueService,
)


class FakeQueueStore:
    """In-memory stand-in for MessageQueueRepository."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def list_for_session(self, session_id: str) -> list[dict]:
        return sorted(
            (row for row in self.rows.values() if row["session_id"] == session_id),
            key=lambda row: (row["position"], row["created_at"]),
        )

    async def next_position(self, session_id: str) -> int:
        positions = [
            row["position"] for row in self.rows.values() if row["session_id"] == session_id
        ]
        return (max(positions) if positions else 0) + 1

    async def append(self, **values):
        item_id = values.pop("item_id")
        # The real repository serialises these to JSON text; mirror that so the
        # reader path (`_json_list`/`_json_dict`) is exercised faithfully.
        values["attachments_json"] = json.dumps(values.pop("attachments") or [])
        values["selections_json"] = json.dumps(values.pop("selections") or {})
        row = {
            "id": item_id,
            "status": QUEUE_STATUS_QUEUED,
            "error_code": None,
            "error_message": None,
            "updated_seq": 0,
            **values,
        }
        self.rows[item_id] = row
        return dict(row)

    async def get(self, item_id: str):
        row = self.rows.get(item_id)
        return dict(row) if row else None

    async def update_content(self, item_id: str, **values):
        row = self.rows.get(item_id)
        if row is None:
            return None
        values["attachments_json"] = json.dumps(values.pop("attachments") or [])
        row.update(values)
        return dict(row)

    async def set_status(self, item_id: str, **values) -> None:
        row = self.rows.get(item_id)
        if row is not None:
            row.update(values)

    async def remove(self, item_id: str) -> None:
        self.rows.pop(item_id, None)

    async def clear_for_session(self, session_id: str) -> None:
        for key in [k for k, v in self.rows.items() if v["session_id"] == session_id]:
            self.rows.pop(key, None)

    async def claim_next(self, session_id: str, *, updated_at: str):
        async with self._lock:
            candidates = [
                row
                for row in self.rows.values()
                if row["session_id"] == session_id and row["status"] == QUEUE_STATUS_QUEUED
            ]
            if not candidates:
                return None
            head = min(candidates, key=lambda row: (row["position"], row["created_at"]))
            head["status"] = QUEUE_STATUS_SENDING
            head["updated_at"] = updated_at
            return dict(head)


@pytest.fixture
def service() -> MessageQueueService:
    return MessageQueueService(FakeQueueStore())


async def _enqueue(service: MessageQueueService, text: str, session: str = "s1"):
    return await service.enqueue(
        session_id=session,
        user_id="u1",
        content=text,
    )


def test_enqueue_assigns_increasing_positions(service) -> None:
    async def run() -> None:
        first = await _enqueue(service, "one")
        second = await _enqueue(service, "two")
        third = await _enqueue(service, "three")
        assert [first.position, second.position, third.position] == [1, 2, 3]

    asyncio.run(run())


def test_list_preserves_submission_order(service) -> None:
    async def run() -> None:
        for text in ("one", "two", "three"):
            await _enqueue(service, text)
        assert [item.content for item in await service.list("s1")] == ["one", "two", "three"]

    asyncio.run(run())


def test_queues_are_isolated_per_session(service) -> None:
    async def run() -> None:
        await _enqueue(service, "a", session="s1")
        await _enqueue(service, "b", session="s2")
        assert [item.content for item in await service.list("s1")] == ["a"]
        assert [item.content for item in await service.list("s2")] == ["b"]

    asyncio.run(run())


def test_empty_message_is_rejected(service) -> None:
    async def run() -> None:
        with pytest.raises(MessageQueueError) as caught:
            await _enqueue(service, "   ")
        assert caught.value.code == "session/queue-empty-message"

    asyncio.run(run())


def test_attachments_alone_are_enough(service) -> None:
    async def run() -> None:
        item = await service.enqueue(
            session_id="s1",
            user_id="u1",
            content="",
            attachments=[{"fileId": "f1", "name": "a.png"}],
        )
        assert item.attachments[0]["fileId"] == "f1"

    asyncio.run(run())


def test_edit_replaces_pending_content(service) -> None:
    async def run() -> None:
        item = await _enqueue(service, "original")
        updated = await service.update(session_id="s1", item_id=item.id, content="edited")
        assert updated.content == "edited"
        assert updated.updated_seq == item.updated_seq + 1
        assert (await service.list("s1"))[0].content == "edited"

    asyncio.run(run())


def test_edit_rejects_a_dispatched_item(service) -> None:
    async def run() -> None:
        item = await _enqueue(service, "original")
        claimed = await service.claim_next("s1")
        assert claimed is not None
        # The runtime owns the item now; rewriting it would misreport what ran.
        with pytest.raises(MessageQueueError) as caught:
            await service.update(session_id="s1", item_id=item.id, content="too late")
        assert caught.value.code == "session/queue-item-not-found"

    asyncio.run(run())


def test_edit_to_blank_is_rejected(service) -> None:
    async def run() -> None:
        item = await _enqueue(service, "original")
        with pytest.raises(MessageQueueError) as caught:
            await service.update(session_id="s1", item_id=item.id, content="   ")
        assert caught.value.code == "session/queue-empty-message"

    asyncio.run(run())


def test_remove_drops_the_item(service) -> None:
    async def run() -> None:
        first = await _enqueue(service, "one")
        await _enqueue(service, "two")
        removed = await service.remove(session_id="s1", item_id=first.id)
        assert removed.id == first.id
        assert [item.content for item in await service.list("s1")] == ["two"]

    asyncio.run(run())


def test_remove_rejects_a_dispatched_item(service) -> None:
    async def run() -> None:
        item = await _enqueue(service, "one")
        await service.claim_next("s1")
        with pytest.raises(MessageQueueError) as caught:
            await service.remove(session_id="s1", item_id=item.id)
        assert caught.value.code == "session/queue-item-not-found"

    asyncio.run(run())


def test_an_item_cannot_be_addressed_across_sessions(service) -> None:
    async def run() -> None:
        item = await _enqueue(service, "one", session="s1")
        with pytest.raises(MessageQueueError):
            await service.update(session_id="s2", item_id=item.id, content="hijack")

    asyncio.run(run())


def test_unknown_item_is_reported_as_not_found(service) -> None:
    async def run() -> None:
        with pytest.raises(MessageQueueError) as caught:
            await service.remove(session_id="s1", item_id="missing")
        assert caught.value.code == "session/queue-item-not-found"

    asyncio.run(run())


def test_claim_returns_items_in_fifo_order(service) -> None:
    async def run() -> None:
        for text in ("one", "two"):
            await _enqueue(service, text)
        first = await service.claim_next("s1")
        second = await service.claim_next("s1")
        assert first is not None and first.content == "one"
        assert second is not None and second.content == "two"
        assert await service.claim_next("s1") is None

    asyncio.run(run())


def test_claim_marks_the_item_in_flight(service) -> None:
    async def run() -> None:
        await _enqueue(service, "one")
        claimed = await service.claim_next("s1")
        assert claimed is not None and claimed.status == QUEUE_STATUS_SENDING

    asyncio.run(run())


def test_concurrent_claims_never_return_the_same_item(service) -> None:
    async def run() -> None:
        await _enqueue(service, "only")
        results = await asyncio.gather(
            service.claim_next("s1"),
            service.claim_next("s1"),
            service.claim_next("s1"),
        )
        claimed = [item for item in results if item is not None]
        assert len(claimed) == 1, "one item must be dispatched exactly once"

    asyncio.run(run())


def test_mark_sent_then_queue_is_empty(service) -> None:
    async def run() -> None:
        await _enqueue(service, "one")
        claimed = await service.claim_next("s1")
        assert claimed is not None
        await service.mark_sent(claimed.id)
        assert await service.claim_next("s1") is None
        assert (await service.list("s1"))[0].status == QUEUE_STATUS_SENT

    asyncio.run(run())


def test_requeue_returns_a_claimed_item_to_the_queue(service) -> None:
    async def run() -> None:
        await _enqueue(service, "one")
        claimed = await service.claim_next("s1")
        assert claimed is not None
        await service.requeue(claimed.id)
        again = await service.claim_next("s1")
        assert again is not None and again.content == "one"

    asyncio.run(run())


def test_mark_failed_records_the_reason(service) -> None:
    async def run() -> None:
        await _enqueue(service, "one")
        claimed = await service.claim_next("s1")
        assert claimed is not None
        await service.mark_failed(claimed.id, code="runtime_error", message="boom")
        stored = (await service.list("s1"))[0]
        assert stored.status == QUEUE_STATUS_FAILED
        assert stored.error_code == "runtime_error"
        assert stored.error_message == "boom"

    asyncio.run(run())


def test_failed_items_are_not_redelivered(service) -> None:
    async def run() -> None:
        await _enqueue(service, "one")
        claimed = await service.claim_next("s1")
        assert claimed is not None
        await service.mark_failed(claimed.id, code="runtime_error", message="boom")
        assert await service.claim_next("s1") is None

    asyncio.run(run())


def test_removing_the_head_promotes_the_next_item(service) -> None:
    async def run() -> None:
        first = await _enqueue(service, "one")
        await _enqueue(service, "two")
        await service.remove(session_id="s1", item_id=first.id)
        claimed = await service.claim_next("s1")
        assert claimed is not None and claimed.content == "two"

    asyncio.run(run())


def test_clear_empties_only_that_session(service) -> None:
    async def run() -> None:
        await _enqueue(service, "a", session="s1")
        await _enqueue(service, "b", session="s2")
        await service.clear("s1")
        assert await service.list("s1") == []
        assert len(await service.list("s2")) == 1

    asyncio.run(run())


def test_a_failed_item_can_be_removed(service) -> None:
    """A dispatch failure must not leave an entry nobody can clear.

    Removal used to require the item to be pending, so once a dispatch failed the
    entry stayed in the queue and every attempt to remove it answered "queued
    item is no longer pending". The reader was left with a queue entry that no
    action could resolve.
    """

    async def run() -> None:
        item = await _enqueue(service, "one")
        await service.claim_next("s1")
        await service.mark_failed(
            item.id, code="queue_dispatch_failed", message="runtime did not report"
        )

        removed = await service.remove(session_id="s1", item_id=item.id)
        assert removed.id == item.id

        remaining = await service.list("s1")
        assert remaining == [], "the queue is clear again"

    asyncio.run(run())


def test_a_sending_item_still_cannot_be_removed(service) -> None:
    """The in-flight window stays protected, only the failure case opened up."""

    async def run() -> None:
        item = await _enqueue(service, "one")
        await service.claim_next("s1")  # status becomes "sending"
        with pytest.raises(MessageQueueError) as caught:
            await service.remove(session_id="s1", item_id=item.id)
        assert caught.value.code == "session/queue-item-not-found"

    asyncio.run(run())


def test_a_failed_item_cannot_be_edited(service) -> None:
    """Editing still targets pending items only, since the item may yet resend."""

    async def run() -> None:
        item = await _enqueue(service, "one")
        await service.claim_next("s1")
        await service.mark_failed(item.id, code="x", message="y")
        with pytest.raises(MessageQueueError):
            await service.update(session_id="s1", item_id=item.id, content="new")

    asyncio.run(run())
