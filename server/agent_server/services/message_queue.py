"""Per-session outgoing message queue.

A session runs one turn at a time. Messages submitted while a turn is running
are accepted into a durable queue instead of being rejected, and are dispatched
in submission order as each turn finishes.

The design follows the DSH session inbox: an ordered list of pending messages
that the runtime drains, with edit and remove available while an item is still
pending. Only pending items can be mutated — once an item has been handed to the
runtime, the runtime owns it and the queue must not rewrite history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from agent_server.core.utc import utc_now

# Statuses a queue item moves through.
QUEUE_STATUS_QUEUED = "queued"
QUEUE_STATUS_SENDING = "sending"
QUEUE_STATUS_SENT = "sent"
QUEUE_STATUS_FAILED = "failed"

# Items still under the queue's control. Only these may be edited or removed:
# once an item is claimed for dispatch the runtime owns it, and rewriting it
# would misreport what actually ran. This mirrors the DSH session inbox, where
# edit and remove apply only while the message is still queued.
PENDING_STATUSES = (QUEUE_STATUS_QUEUED,)


class MessageQueueError(Exception):
    """Raised when a queue mutation cannot be applied."""

    def __init__(self, code: str, message: str, *, item_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.item_id = item_id

    def detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.item_id is not None:
            payload["itemId"] = self.item_id
        return payload


@dataclass(frozen=True)
class QueuedMessage:
    """One pending or dispatched queue entry."""

    id: str
    session_id: str
    user_id: str | None
    position: int
    status: str
    content: str
    attachments: list[dict[str, Any]]
    selections: dict[str, Any]
    client_message_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    updated_seq: int

    @property
    def pending(self) -> bool:
        return self.status in PENDING_STATUSES

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "position": self.position,
            "status": self.status,
            "content": self.content,
            "attachments": self.attachments,
            "selections": self.selections,
            "clientMessageId": self.client_message_id,
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "updatedSeq": self.updated_seq,
        }


def _json_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _json_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def queued_message_from_row(row: dict[str, Any]) -> QueuedMessage:
    return QueuedMessage(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        user_id=row.get("user_id"),
        position=int(row["position"]),
        status=str(row["status"]),
        content=str(row["content"]),
        attachments=_json_list(row.get("attachments_json")),
        selections=_json_dict(row.get("selections_json")),
        client_message_id=row.get("client_message_id"),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        updated_seq=int(row.get("updated_seq") or 0),
    )


class MessageQueueStore(Protocol):
    async def list_for_session(self, session_id: str) -> list[Any]: ...

    async def next_position(self, session_id: str) -> int: ...

    async def append(self, **values: Any) -> dict[str, Any]: ...

    async def get(self, item_id: str) -> dict[str, Any] | None: ...

    async def update_content(self, item_id: str, **values: Any) -> dict[str, Any] | None: ...

    async def set_status(self, item_id: str, **values: Any) -> None: ...

    async def remove(self, item_id: str) -> None: ...

    async def clear_for_session(self, session_id: str) -> None: ...

    async def claim_next(self, session_id: str, **values: Any) -> dict[str, Any] | None: ...


class MessageQueueService:
    """Ordered, durable queue of messages waiting for a turn to finish."""

    def __init__(self, store: MessageQueueStore) -> None:
        self._store = store

    async def list(self, session_id: str) -> list[QueuedMessage]:
        rows = await self._store.list_for_session(session_id)
        return [queued_message_from_row(row) for row in rows]

    async def enqueue(
        self,
        *,
        session_id: str,
        user_id: str | None,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        selections: dict[str, Any] | None = None,
        client_message_id: str | None = None,
    ) -> QueuedMessage:
        if not content.strip() and not attachments:
            raise MessageQueueError(
                "session/queue-empty-message",
                "a queued message needs text or an attachment",
            )
        position = await self._store.next_position(session_id)
        now = utc_now()
        row = await self._store.append(
            item_id=_new_item_id(),
            session_id=session_id,
            user_id=user_id,
            position=position,
            content=content,
            attachments=attachments,
            selections=selections,
            client_message_id=client_message_id,
            created_at=now,
            updated_at=now,
        )
        return queued_message_from_row(row)

    async def update(
        self,
        *,
        session_id: str,
        item_id: str,
        content: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> QueuedMessage:
        """Edit a still-pending item.

        A dispatched item is rejected rather than rewritten: the runtime may
        already have acted on it, so changing it would misrepresent what ran.
        """
        item = await self._require_pending(session_id, item_id)
        if content is None:
            content = item.content
        if not content.strip() and not attachments:
            raise MessageQueueError(
                "session/queue-empty-message",
                "a queued message needs text or an attachment",
                item_id=item_id,
            )
        updated = await self._store.update_content(
            item_id,
            content=content,
            attachments=attachments if attachments is not None else item.attachments,
            updated_at=utc_now(),
            updated_seq=item.updated_seq + 1,
        )
        if updated is None:
            raise MessageQueueError(
                "session/queue-item-not-found",
                "queued item is no longer pending",
                item_id=item_id,
            )
        return queued_message_from_row(updated)

    async def remove(self, *, session_id: str, item_id: str) -> QueuedMessage:
        item = await self._require_pending(session_id, item_id)
        await self._store.remove(item_id)
        return item

    async def clear(self, session_id: str) -> None:
        await self._store.clear_for_session(session_id)

    async def claim_next(self, session_id: str) -> QueuedMessage | None:
        """Take the head item for dispatch, marking it in-flight.

        Claiming is guarded by the item's status inside the store, so a second
        dispatcher racing on the same queue claims nothing.
        """
        row = await self._store.claim_next(session_id, updated_at=utc_now())
        return queued_message_from_row(row) if row is not None else None

    async def mark_sent(self, item_id: str) -> None:
        await self._store.set_status(
            item_id, status=QUEUE_STATUS_SENT, updated_at=utc_now()
        )

    async def mark_failed(self, item_id: str, *, code: str, message: str) -> None:
        await self._store.set_status(
            item_id,
            status=QUEUE_STATUS_FAILED,
            error_code=code,
            error_message=message,
            updated_at=utc_now(),
        )

    async def requeue(self, item_id: str) -> None:
        """Return a claimed item to the queue after a transient dispatch failure."""
        await self._store.set_status(
            item_id,
            status=QUEUE_STATUS_QUEUED,
            error_code=None,
            error_message=None,
            updated_at=utc_now(),
        )

    async def _require_pending(self, session_id: str, item_id: str) -> QueuedMessage:
        row = await self._store.get(item_id)
        if row is None:
            raise MessageQueueError(
                "session/queue-item-not-found",
                "queued item is no longer pending",
                item_id=item_id,
            )
        item = queued_message_from_row(row)
        # Guard against addressing another session's queue by id.
        if item.session_id != session_id or not item.pending:
            raise MessageQueueError(
                "session/queue-item-not-found",
                "queued item is no longer pending",
                item_id=item_id,
            )
        return item


def _new_item_id() -> str:
    import secrets

    return f"queue_{secrets.token_urlsafe(16)}"
