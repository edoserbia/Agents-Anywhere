"""Dispatch queued messages as each turn finishes.

The queue would be useless if nothing drained it. The server learns that a turn
ended from the connector's runtime-status notification, so that observation is
the trigger: when a session settles into a state that can accept work, the next
queued message is handed to the runtime.

Only one item is dispatched per turn. Sending starts a new turn, so the item
after it must wait for that turn to finish — which produces exactly the
"queue advances as the current task completes" behaviour the feature promises.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from loguru import logger

from agent_server.services.message_queue import MessageQueueService


# A session can accept a new turn in these states. Anything else means the
# runtime is still working, and dispatching now would collide with it.
DISPATCHABLE_STATUSES = frozenset({"idle", "error"})


class QueueDispatchBusy(Exception):
    """The runtime started working again before dispatch reached it."""


class QueueDispatcherPort(Protocol):
    async def send_queued_message(
        self,
        session_id: str,
        item: dict[str, Any],
        *,
        user_id: str | None,
    ) -> None: ...


class MessageQueueDispatcher:
    """Drains a session's queue one item per finished turn."""

    def __init__(
        self,
        queue: MessageQueueService,
        sender: QueueDispatcherPort,
    ) -> None:
        self._queue = queue
        self._sender = sender
        # One drain per session at a time: two overlapping drains could claim
        # and send the same head item twice.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def dispatch_next(self, session_id: str) -> bool:
        """Send the head queued message, if any.

        The item carries the id of the user who queued it, so ownership is read
        from the row rather than passed in — the two cannot disagree.

        Returns whether a message was sent. The item is marked sent only after
        the runtime accepted it, so a failure never silently drops the user's
        message.
        """
        async with self._lock_for(session_id):
            item = await self._queue.claim_next(session_id)
            if item is None:
                return False
            try:
                await self._sender.send_queued_message(
                    session_id,
                    item.to_payload(),
                    user_id=item.user_id,
                )
            except QueueDispatchBusy:
                # The runtime picked up other work first. Put the item back at
                # the head so ordering is preserved for the next completion.
                await self._queue.requeue(item.id)
                return False
            except Exception as error:  # noqa: BLE001 - dispatch must not break ingest
                logger.warning(
                    "queued message dispatch failed session_id={} item_id={} error={}",
                    session_id,
                    item.id,
                    error,
                )
                await self._queue.mark_failed(
                    item.id,
                    code="queue_dispatch_failed",
                    message=str(error) or "could not dispatch queued message",
                )
                return False
            await self._queue.mark_sent(item.id)
            return True


def build_queue_dispatcher(app_state: Any, store: Any, rpc: Any) -> MessageQueueDispatcher:
    """Build a dispatcher that sends through the ordinary session send path.

    Imported lazily to keep this module free of a cycle: the run service imports
    the dispatcher's busy signal, so the dispatcher cannot import it at module
    scope.
    """
    from agent_server.services.session_run import SessionRunService

    queue = MessageQueueService(store.message_queue)
    sender = SessionRunService(store, rpc, app_state.device_runtime_service, queue)
    return MessageQueueDispatcher(queue, sender)
