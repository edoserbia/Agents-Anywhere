from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import httpx

from connector.logging import logger
from connector.server.auth import ConnectorAuthenticationError
from connector.server.errors import ConnectorNetworkError
from connector.server.urls import api_v2_url

# HTTP ingest owns explicit bulk sync and disconnected WebSocket fallback.
# History-bearing notifications such as `timeline.sync` must use ingest even
# when the WebSocket is connected; they can exceed the backend WebSocket frame
# limit and are not latency-sensitive. Live small notifications still travel
# over the connector WebSocket.
FLUSH_WINDOW_SECONDS = 0.02
FLUSH_MAX = 64

# How many bytes one ingest request may carry. A batch that fails is re-sent
# whole, so an unbounded request turns one bad moment on a slow link into a
# full restart of whatever it held. Bounding it makes every successful request
# durable progress and every retry small.
FLUSH_MAX_BYTES = 256 * 1024

# How long a producer may wait for room in the queue before the notification is
# dropped. The queue is drained by HTTP uploads that share the link with
# everything else, so a large history upload can leave it full for minutes. A
# producer that waits that long is not "backpressure", it is a stall: the
# callers below include Runtime RPC handlers, and one of them is `session.state`
# — the read the Server performs before it will accept a message. Blocking there
# is what surfaced to users as "runtime did not report its state in time".
DEFAULT_ENQUEUE_TIMEOUT_SECONDS = 2.0

# How long one upload may spend establishing its connection. The upload budget
# below is deliberately generous because a whole runtime history can take
# minutes to push up an asymmetric link, but it is applied to every phase — so a
# blackholed route would hold the single POST lock for that whole budget, and
# every other producer waits behind it. Opening a connection is never
# legitimately slow; only the body transfer is.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0

# How many order-bearing notifications may wait for an upload before their
# producer is made to wait instead. The durable lane exists so a runtime feed
# acknowledges its peer on local receipt rather than on a remote round trip; the
# bound is what keeps that from becoming unbounded memory while the link is
# down. Waiting is the honest backpressure here — the producer is a feed that
# has not acknowledged the data yet, so slowing it down loses nothing.
DEFAULT_DURABLE_LIMIT = 8192

# How long a durability barrier may wait before it reports failure. The DSH
# bridge gives a batch 60s before it tears the stream down, so a barrier that
# outlived that would only delay the same recovery.
DEFAULT_DURABLE_FLUSH_TIMEOUT_SECONDS = 45.0

# Bodies below this are not worth compressing: gzip costs CPU and framing bytes,
# and the small live notifications are the ones latency depends on.
GZIP_MIN_BYTES = 4096

# How long the newest revision of a timeline entry is held before it is sent.
# A streaming reply is re-sent whole on every revision — as fast as the runtime
# produces them — so this window is what turns thirty uplink copies of one
# message into a few, at the cost of that much delay before the newest text
# appears. It applies per timeline item, and an item that keeps streaming keeps
# moving to the back of the hold, so nothing else waits behind it.
DEFAULT_TIMELINE_WINDOW_SECONDS = 0.4

# How long a capability answer is trusted before the Server is asked again, so a
# Server upgrade is picked up without restarting every Connector.
INGEST_CAPABILITY_TTL_SECONDS = 600.0

# How long a *failed* capability probe is trusted. Short, so a blip does not
# turn compression off for the whole TTL.
INGEST_CAPABILITY_RETRY_SECONDS = 30.0

# Bodies above this are staged and resumed rather than risked in one request.
# A complete timeline snapshot cannot be split — the Server replaces the whole
# timeline with it — so a transfer interrupted at 90% has nothing to show for
# itself unless the Server kept the bytes.
RESUMABLE_UPLOAD_MIN_BYTES = 512 * 1024

# How much of a staged body travels per request.
RESUMABLE_UPLOAD_CHUNK_BYTES = 256 * 1024

TIMELINE_SYNC = "timeline.sync"

# Notifications that describe a session's *current* value rather than an event:
# the newest one supersedes every older one, so only the newest is worth
# carrying. They are held aside from the bounded queue entirely, which is what
# keeps a busy runtime's event storm from filling the queue with values nobody
# will ever read — and, before this, from stalling the RPC path behind them.
COALESCE_LATEST_METHODS = frozenset(
    {"session.state.updated", "session.source.updated"}
)

# Coalesced methods that carry timeline content rather than a restated value.
# Their relative order is data, so they are never allowed to overtake one
# another; the values above are the opposite and always go first.
TIMELINE_LATEST_METHODS = frozenset({TIMELINE_SYNC, "timeline.itemUpsert"})

# Coalesced timeline methods whose newest revision is worth holding briefly.
# A streaming reply is re-sent whole on every revision, so waiting one window
# collapses many uplink copies into one. A complete snapshot is excluded: it is
# a wholesale replacement, so letting an increment overtake it — which holding it
# would allow — would drop that increment when the snapshot lands.
WINDOWED_TIMELINE_METHODS = frozenset({"timeline.itemUpsert"})

AccessTokenProvider = Callable[[bool], Awaitable[str]]
HttpClientGetter = Callable[[], httpx.AsyncClient | None]
HttpClientFactory = Callable[[httpx.Timeout | float], httpx.AsyncClient]


class ConnectorIngestRejectedError(RuntimeError):
    """The backend accepted the HTTP request but rejected notifications inside it."""


class ConnectorIngestClient:
    def __init__(
        self,
        server_url: str,
        access_token_provider: AccessTokenProvider,
        http_client_getter: HttpClientGetter,
        http_client_factory: HttpClientFactory,
    ) -> None:
        self._server_url = server_url
        self._access_token_provider = access_token_provider
        self._http_client_getter = http_client_getter
        self._http_client_factory = http_client_factory
        self._notify_queue: deque[dict[str, Any]] = deque()
        self._queue_room = asyncio.Event()
        self._queue_room.set()
        self._inflight: list[dict[str, Any]] = []
        self._retry_delay = 1.0
        self._closed: Exception | None = None
        self._post_lock = asyncio.Lock()
        self._available = asyncio.Event()
        self._posting = False
        self._dropped = 0
        # The latest-wins hold is split by ordering domain. A value restates one
        # fact — the newest supersedes every older one — so it never needs to
        # wait and is always collected first. A coalesced timeline entry is part
        # of one ordered stream and is collected strictly by admission order,
        # because acknowledging a batch locally would otherwise let its
        # increments overtake a complete snapshot still queued ahead of them.
        self._latest_value: dict[str, dict[str, Any]] = {}
        self._latest_timeline: dict[str, dict[str, Any]] = {}
        # Every notification carries its admission order.
        self._admission = 0
        # Ordered, lossless lane for order-bearing notifications. Never
        # coalesced and never dropped: the runtime feed that produced them is
        # told they are safe, so only their upload may release that promise.
        self._durable: deque[dict[str, Any]] = deque()
        self._durable_inflight = 0
        self._durable_submitted = 0
        self._durable_resolved = 0
        self._durable_error: Exception | None = None
        # Everything submitted up to here was refused and dropped. A barrier
        # covering any of it must fail, but data submitted after it must not be
        # blocked: one rejected batch used to stop every session on this
        # Connector until the process was restarted.
        self._durable_refused_through = 0
        # Whether the current refusal has already been reported to a caller.
        self._durable_refusal_reported = True
        self._durable_room = asyncio.Event()
        self._durable_room.set()
        self._durable_progress = asyncio.Event()
        # Set when the timeline hold has room again.
        self._timeline_room = asyncio.Event()
        self._timeline_room.set()
        # Resolved on first use from the Server's own advertisement.
        self._encodings: frozenset[str] | None = None
        self._encodings_ttl = INGEST_CAPABILITY_TTL_SECONDS
        self._capabilities: dict[str, Any] = {}
        # Staged transfers by body digest, so a retry of the same batch resumes
        # the transfer it already began instead of opening a new one.
        self._staged_uploads: dict[str, str] = {}
        self._staged_offsets: dict[str, int] = {}
        self._encodings_checked_at = 0.0

    def _admit(
        self,
        method: str,
        params: dict[str, Any],
        *,
        durable: bool = False,
    ) -> dict[str, Any]:
        """Take one notification, with its wire form and exact size.

        The encoded form is kept because it is needed twice — once to decide how
        much may travel in one request, and once to build that request — and a
        large snapshot should not be serialized twice to answer both.

        `durable` marks a notification whose producer has been promised storage,
        so its resolution is what a durability barrier waits for.
        """

        self._admission += 1
        body = json.dumps(
            {"method": method, "params": params}, ensure_ascii=False
        ).encode("utf-8")
        entry: dict[str, Any] = {
            "method": method,
            "params": params,
            "seq": self._admission,
            "body": body,
            "size": len(body),
        }
        if durable:
            entry["durable"] = True
        if method in WINDOWED_TIMELINE_METHODS:
            entry["notBefore"] = time.monotonic() + self._timeline_window_seconds()
        return entry

    @property
    def dropped(self) -> int:
        """Notifications discarded because the queue stayed full.

        Exposed so a deployment can tell "the Server is behind" from "the
        Connector is silent" without reading logs.
        """

        return self._dropped

    @property
    def has_pending(self) -> bool:
        return (
            self._posting
            or bool(self._inflight)
            or bool(self._latest_value)
            or bool(self._latest_timeline)
            or bool(self._durable)
            or bool(self._notify_queue)
        )

    def close(self, error: Exception | None = None) -> None:
        self._closed = error or RuntimeError("connector ingest is closed")
        self._latest_value.clear()
        self._latest_timeline.clear()
        # Wake blocked producers. They check _closed again after admission.
        self._notify_queue.clear()
        self._queue_room.set()
        self._durable_room.set()
        self._durable_progress.set()
        self._timeline_room.set()


    async def enqueue(self, method: str, params: dict[str, Any]) -> None:
        """Admit one notification, never blocking the caller indefinitely.

        A notification that only restates a session's current value is kept in
        the latest-wins hold instead of the queue: it replaces the previous one
        for that session and cannot be lost to a full queue. Everything else is
        queued, and once the queue is full the *newest* one is dropped rather
        than stalling the caller — the caller can be an RPC whose response the
        Server is waiting on, and a dropped timeline item is restored by the
        next snapshot sync.
        """

        if self._closed is not None:
            raise self._closed
        key = _coalesce_key(method, params)
        entry = self._admit(method, params)
        if key is not None:
            # Re-insert so the hold stays ordered by admission: a replacement
            # takes its newest position rather than the superseded one's.
            hold = (
                self._latest_timeline
                if method in TIMELINE_LATEST_METHODS
                else self._latest_value
            )
            if not self._hold_replace(hold, key, entry):
                return
            self._available.set()
            return
        try:
            await asyncio.wait_for(
                self._queue_put(entry),
                timeout=self._enqueue_timeout_seconds(),
            )
        except TimeoutError:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 50 == 0:
                logger.warning(
                    "connector ingest queue is full; dropped notification method={} dropped_total={} pending={}",
                    method,
                    self._dropped,
                    len(self._notify_queue),
                )
            return
        self._available.set()
        if self._closed is not None:
            self.close(self._closed)
            raise self._closed

    async def _queue_put(self, entry: dict[str, Any]) -> None:
        while len(self._notify_queue) >= self._queue_limit():
            self._queue_room.clear()
            if len(self._notify_queue) < self._queue_limit():
                break
            await self._queue_room.wait()
        self._notify_queue.append(entry)

    def _queue_limit(self) -> int:
        import os

        configured = os.environ.get("AA_CONNECTOR_INGEST_QUEUE_LIMIT")
        if configured:
            try:
                return max(1, int(configured))
            except ValueError:
                pass
        return 1024

    async def ingest_notifications(self, notifications: list[dict[str, Any]]) -> None:
        """Send a batch synchronously, bypassing the flush queue."""
        if not notifications:
            return
        await self.post_batch([
            self._admit(entry["method"], entry["params"]) for entry in notifications
        ])

    async def submit_notifications(self, notifications: list[dict[str, Any]]) -> None:
        """Take responsibility for a batch without waiting for the upload.

        The caller is a runtime feed that is about to acknowledge its own peer.
        Waiting for a remote round trip here is what capped the DSH bridge at a
        couple of batches per second against a peer that flushes thirty times a
        second: the peer's event buffer filled, the stream failed, and every
        session was recalibrated in a loop. Accepting the batch locally and
        uploading it from the background flush loop breaks that coupling while
        still keeping strict order — the lane is a FIFO and is drained before
        every other source, so a complete snapshot is never overtaken by
        incremental items that followed it.

        Durability is unchanged in substance: the sequence that a caller
        protects only advances once this data reaches the Server, which is what
        `flush_notifications` is for.
        """

        if not notifications:
            return
        for entry in notifications:
            await self._durable_admit(
                self._admit(entry["method"], entry["params"], durable=True)
            )
        self._available.set()

    async def _durable_admit(self, entry: dict[str, Any]) -> None:
        """Accept one order-bearing notification.

        Timeline entries go to the coalescing hold rather than the queue: a
        streaming reply is re-sent whole on every revision, so holding the
        newest revision of an item for a moment is what keeps a chatty runtime
        from asking for more uplink than the link has. Everything else keeps
        strict FIFO order in the queue, so a complete snapshot is never
        overtaken by the increments that followed it.
        """

        key = _coalesce_key(entry["method"], entry["params"])
        if key is not None and entry["method"] in WINDOWED_TIMELINE_METHODS:
            # One slot per timeline item, so the hold is already small; the
            # bound is what keeps a runtime that keeps inventing item ids from
            # growing it without limit. Waiting, rather than dropping, is the
            # only safe answer for data a producer was told we would store.
            while (
                len(self._latest_timeline) >= self._durable_limit()
                and key not in self._latest_timeline
            ):
                self._raise_if_closed()
                self._timeline_room.clear()
                if (
                    len(self._latest_timeline) < self._durable_limit()
                    or key in self._latest_timeline
                ):
                    break
                await self._timeline_room.wait()
            self._hold_replace(self._latest_timeline, key, entry)
            self._durable_submitted += 1
            return
        while True:
            self._raise_if_closed()
            if len(self._durable) < self._durable_limit():
                break
            self._durable_room.clear()
            if len(self._durable) < self._durable_limit():
                break
            await self._durable_room.wait()
        self._durable.append(entry)
        self._durable_submitted += 1

    def _raise_if_closed(self) -> None:
        if self._closed is not None:
            raise self._closed

    def _raise_if_refused(self, target: int) -> None:
        """Tell the caller about a refusal, once.

        A barrier is cumulative, so every barrier taken after a refusal covers
        the refused range and comparing targets cannot separate them. What
        matters is that the caller is *told*: its contract on a failed
        checkpoint is to resubscribe and re-send a complete snapshot, which
        supersedes whatever was refused. So the refusal is reported to the first
        barrier that observes it and not again — reporting it forever would
        wedge the Connector until a restart, and reporting it never would let a
        checkpoint advance over data the Server never took.
        """

        if self._durable_error is None or self._durable_refusal_reported:
            return
        self._durable_refusal_reported = True
        raise self._durable_error

    async def flush_notifications(
        self, timeout: float | None = None
    ) -> None:
        """Wait until everything submitted so far has reached the Server.

        This is the durability barrier a checkpoint must pass before it is
        allowed to record that its data is safe.
        """

        target = self._durable_submitted
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (
            self._durable_flush_timeout_seconds() if timeout is None else timeout
        )
        # Checked before the loop, not inside it: refused notifications are
        # already counted as resolved, so the watermark alone would let this
        # barrier report success for data the Server never took.
        self._raise_if_closed()
        self._raise_if_refused(target)
        while self._durable_resolved < target:
            self._durable_progress.clear()
            if self._durable_resolved >= target:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    "connector notification durability barrier timed out"
                )
            try:
                await asyncio.wait_for(self._durable_progress.wait(), remaining)
            except TimeoutError as exc:
                self._raise_if_refused(target)
                raise TimeoutError(
                    "connector notification durability barrier timed out"
                ) from exc
        # The range became resolved. Refused notifications count as resolved
        # too, so this is the check that keeps a barrier from reporting success
        # for data the Server never took.
        self._raise_if_refused(target)

    def durable_accounting(self) -> tuple[int, int, int]:
        """(submitted, resolved, outstanding) — the promise bookkeeping.

        `outstanding` is counted from the entries themselves rather than from a
        counter, because that is what catches the interesting mistake: a promise
        marked resolved while its data is still sitting in a lane — or, worse,
        while the entry that replaced it may be evicted. A counter-only check
        balances perfectly in that case and proves nothing.

        This state is written from admission, coalescing, the flush loop's
        success and failure branches, and the barrier; every defect found in it
        so far has been one of those paths losing track of an entry.
        """

        outstanding = sum(
            1 for entry in self._durable if entry.get("durable")
        ) + sum(
            1 for entry in self._latest_timeline.values() if entry.get("durable")
        ) + sum(
            1 for entry in self._inflight if entry.get("durable")
        )
        return self._durable_submitted, self._durable_resolved, outstanding

    def _advance_durable(self) -> None:
        """Record that the in-flight batch reached the Server."""

        if not self._durable_inflight:
            return
        self._durable_resolved += self._durable_inflight
        self._durable_inflight = 0
        self._durable_progress.set()

    def _fail_durable(self, error: Exception, refused: int) -> None:
        """The Server refused data a caller was told we would store.

        Those notifications are gone, so they are counted as resolved — the
        watermark has to be able to move again — while the refusal is recorded
        against the range they covered so no checkpoint can claim they landed.
        """

        if self._durable_error is None:
            self._durable_error = error
        # A new refusal is news again, whichever refusal came before it.
        self._durable_refusal_reported = False
        # Every refusal extends the range, not only the first: a later batch
        # refused on its own must still fail the barrier that covers it, or a
        # checkpoint would advance over data the Server never took.
        self._durable_refused_through = max(self._durable_refused_through, refused)
        self._durable_room.set()
        self._durable_progress.set()
        self._timeline_room.set()

    def _timeline_window_seconds(self) -> float:
        import os

        configured = os.environ.get("AA_CONNECTOR_TIMELINE_WINDOW_SECONDS")
        if configured:
            try:
                return max(0.0, float(configured))
            except ValueError:
                pass
        return DEFAULT_TIMELINE_WINDOW_SECONDS

    def _durable_limit(self) -> int:
        import os

        configured = os.environ.get("AA_CONNECTOR_INGEST_DURABLE_LIMIT")
        if configured:
            try:
                return max(1, int(configured))
            except ValueError:
                pass
        return DEFAULT_DURABLE_LIMIT

    def _durable_flush_timeout_seconds(self) -> float:
        import os

        configured = os.environ.get("AA_CONNECTOR_INGEST_DURABLE_FLUSH_TIMEOUT_SECONDS")
        if configured:
            try:
                return max(0.1, float(configured))
            except ValueError:
                pass
        return DEFAULT_DURABLE_FLUSH_TIMEOUT_SECONDS

    async def flush_loop(self) -> None:
        """Keep a failed batch at the head; cancellation leaves it recoverable.

        Delivery is at least once after an ambiguous HTTP failure. State/item
        replacements retain their protocol versions; no synthetic revision is
        introduced by a retry. Authentication and permanent rejections do not
        enter the transient-error retry loop.
        """
        attempt = 0
        while True:
            await self._available.wait()
            if not self._inflight:
                await asyncio.sleep(FLUSH_WINDOW_SECONDS)
            try:
                async with self._post_lock:
                    self._posting = True
                    try:
                        self._collect_pending()
                        await self._post_batch(self._inflight)
                        self._advance_durable()
                        self._inflight = []
                        # `has_pending` cannot be used here: it counts this
                        # loop's own in-progress post, so it is never false.
                        if (
                            not self._durable
                            and not self._latest_value
                            and not self._latest_timeline
                            and not self._notify_queue
                        ):
                            self._available.clear()
                    finally:
                        self._posting = False
            except ConnectorAuthenticationError as exc:
                self.close(exc)
                raise
            except ConnectorNetworkError as exc:
                attempt += 1
                if attempt == 1 or attempt % 10 == 0:
                    logger.warning("connector ingest deferred notifications={} attempt={} error={}", len(self._inflight), attempt, exc)
                await asyncio.sleep(min(30.0, self._retry_delay * 2 ** min(attempt - 1, 5)))
                continue
            except (ConnectorIngestRejectedError, httpx.HTTPStatusError) as exc:
                logger.warning("connector ingest permanently rejected notifications={} error={}", len(self._inflight), exc)
                # Data a caller was told we would store cannot be silently
                # dropped: fail the durability barrier so the checkpoint that
                # depends on it is never advanced past it.
                refused = self._durable_submitted
                self._durable_resolved += self._durable_inflight
                self._durable_inflight = 0
                self._fail_durable(exc, refused)
            self._inflight = []
            attempt = 0

    def _collect_pending(self) -> int:
        """Move pending notifications into the in-flight batch.

        Values go first: they are what the Server shows as current, and a newer
        one makes an older one worthless, so they never wait behind bulk
        history. The order-bearing lanes are then collected strictly by
        admission order, so a complete snapshot is never overtaken by the
        increments that followed it — the ordering the old synchronous publish
        bought by draining the whole backlog before every call, now without
        making a live batch wait for it.

        Returns how many entries were taken, so a caller draining a known
        backlog can still terminate when most of what it pulled collapsed into
        the latest-wins holds.
        """

        if self._inflight:
            return 0
        taken = 0
        budget = self._flush_bytes()
        while self._latest_value and len(self._inflight) < FLUSH_MAX:
            entry = self._latest_value[next(iter(self._latest_value))]
            if self._inflight and not self._fits(entry, budget):
                break
            budget -= entry["size"]
            self._inflight.append(
                self._latest_value.pop(next(iter(self._latest_value)))
            )
            taken += 1
        while len(self._inflight) < FLUSH_MAX:
            heads: list[tuple[int, str, str | None]] = []
            if self._durable:
                heads.append((self._durable[0]["seq"], "durable", None))
            if self._latest_timeline:
                key, entry = next(iter(self._latest_timeline.items()))
                heads.append((entry["seq"], "latest", key))
            if self._notify_queue:
                heads.append((self._notify_queue[0]["seq"], "queue", None))
            if not heads:
                break
            _, lane, key = min(heads)
            entry = (
                self._durable[0]
                if lane == "durable"
                else self._notify_queue[0]
                if lane == "queue"
                else self._latest_timeline[key]
            )
            if lane == "latest" and not self._ready(entry):
                # Still inside its coalescing window. Nothing may pass it, so
                # the whole flush waits: a later increment overtaking an earlier
                # one would be silently dropped when a wholesale snapshot lands.
                # The wait is bounded by one window, and an item that keeps
                # streaming moves to the back of the hold, so it never blocks
                # what is behind it for longer than that.
                break
            if self._inflight and not self._fits(entry, budget):
                break
            budget -= entry["size"]
            if lane == "durable":
                self._inflight.append(self._durable.popleft())
                if len(self._durable) < self._durable_limit():
                    self._durable_room.set()
            elif lane == "latest":
                assert key is not None
                self._inflight.append(self._latest_timeline.pop(key))
                self._timeline_room.set()
            else:
                self._inflight.append(self._notify_queue.popleft())
                self._queue_room.set()
            if entry.get("durable"):
                self._durable_inflight += 1
            taken += 1
        return taken

    def _evict_oldest_replaceable(self, hold: dict[str, Any]) -> bool:
        """Make room by dropping the oldest entry that nothing was promised.

        Promised data is never the thing dropped: the durable path waits for
        room instead, because dropping it would break the promise a checkpoint
        depends on.
        """

        for existing_key, existing in hold.items():
            if not existing.get("durable"):
                hold.pop(existing_key)
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 50 == 0:
                    logger.warning(
                        "connector ingest hold is full; dropped notification method={} dropped_total={} pending={}",
                        existing.get("method"),
                        self._dropped,
                        len(hold),
                    )
                return True
        return False

    def _hold_replace(
        self, hold: dict[str, Any], key: str, entry: dict[str, Any]
    ) -> bool:
        """Put a newer revision in a hold slot, keeping the slot's deadline.

        Re-inserting moves the entry to the back, so the hold stays ordered by
        when each item was last touched and a busy item never delays a quiet one
        behind it. The deadline, however, belongs to the *first* revision of the
        window: resetting it on every revision would mean a stream that never
        pauses is never sent, which is worse than sending too much — the reader
        would see nothing at all until the reply stopped.
        """

        live = hold.get(key)
        if live is not None:
            if live.get("durable") and not entry.get("durable"):
                # The replacement came from the legacy lane, which promises
                # nothing and may therefore be evicted. Leaving the promise on
                # the old entry while counting it resolved would let eviction
                # drop the only copy of data the barrier had already accepted —
                # so the promise moves to the revision that replaces it, which
                # is a complete replacement for the older one. It becomes an
                # outstanding submission in its own right, otherwise the
                # resolution just counted would let a barrier pass while this
                # entry is still sitting unsent.
                entry["durable"] = True
                self._durable_submitted += 1
            if live.get("durable"):
                # The superseded revision is resolved: the newest one contains
                # it. That can satisfy a parked barrier, so it has to wake it —
                # otherwise the barrier sleeps to its deadline and fails a
                # checkpoint whose condition was already met. Counting it here
                # rather than in the caller also covers the legacy path, which
                # can replace a promised slot and used to leave the watermark
                # permanently ahead of what it had resolved.
                self._durable_resolved += 1
                self._durable_progress.set()
            if live.get("notBefore") is not None:
                entry["notBefore"] = live["notBefore"]
        elif len(hold) >= self._durable_limit():
            # Bounded. The legacy path cannot wait for room without stalling an
            # RPC whose response the Server is waiting on, so it drops instead.
            if not self._evict_oldest_replaceable(hold):
                return False
        hold.pop(key, None)
        hold[key] = entry
        return True

    def _ready(self, entry: dict[str, Any]) -> bool:
        """Whether a held timeline entry has waited out its coalescing window.

        Without this the hold collapses revisions only while the flush loop is
        busy: with the loop running — which is the normal case — each revision
        was collected the moment it arrived, so a stream that produced thirty
        revisions still cost thirty uploads.
        """

        not_before = entry.get("notBefore")
        return not_before is None or time.monotonic() >= not_before

    def _fits(self, entry: dict[str, Any], budget: int) -> bool:
        return entry["size"] <= budget

    def _flush_bytes(self) -> int:
        """How much may travel in one request.

        A failed upload is re-sent whole, so an unbounded request makes one bad
        moment on a slow link cost everything it was carrying. Bounding it means
        a retry only repeats a small piece and every successful request is
        progress the Server keeps. The entry cap still applies, so ordinary
        small notifications batch exactly as before.
        """

        import os

        configured = os.environ.get("AA_CONNECTOR_INGEST_FLUSH_BYTES")
        if configured:
            try:
                return max(1, int(configured))
            except ValueError:
                pass
        return FLUSH_MAX_BYTES

    async def post_batch(self, notifications: list[dict[str, Any]]) -> None:
        async with self._post_lock:
            self._posting = True
            try:
                # Direct scanner snapshots cannot overtake an older failed batch.
                # Drain only work already queued at admission so a live stream
                # cannot starve this synchronous snapshot indefinitely.
                remaining = (
                    len(self._inflight)
                    + len(self._latest_value)
                    + len(self._latest_timeline)
                    + len(self._notify_queue)
                    # The durable lane holds older entries too. Leaving it out
                    # meant a synchronous snapshot posted ahead of a backlog
                    # that arrived before it — and a complete snapshot replaces
                    # the timeline wholesale, so anything it overtook was lost.
                    + len(self._durable)
                )
                while remaining:
                    taken = self._collect_pending()
                    if self._inflight:
                        await self._post_batch(self._inflight)
                        self._advance_durable()
                    progressed = max(taken, len(self._inflight))
                    self._inflight = []
                    if not progressed:
                        # Nothing could be taken: the head is still inside its
                        # coalescing window. Waiting for it is the point — an
                        # older entry must not be overtaken — but it has to be a
                        # real wait. Without this the loop repeated with no
                        # await at all and froze every task in the process for
                        # the rest of the window, which is up to the configured
                        # window and reachable from the publish path.
                        await asyncio.sleep(FLUSH_WINDOW_SECONDS)
                        continue
                    remaining = max(0, remaining - progressed)
                await self._post_batch(notifications)
            finally:
                self._posting = False
                if not self.has_pending:
                    self._available.clear()

    async def _post_batch(self, notifications: list[dict[str, Any]]) -> None:
        if not notifications:
            return
        # The entries carry method/params themselves, so the coalescer works on
        # them directly and each survivor keeps its already-encoded wire form.
        notifications = coalesce_timeline_item_upserts(notifications)
        if not notifications:
            return
        body = _encode_batch(notifications)
        # Kept for the transfer key: `gzip.compress` stamps the current time into
        # its header, so the compressed bytes differ on every attempt. Keying on
        # them meant a retry never recognised the transfer it had already begun
        # and started a new one — the resume path defeated for exactly the large
        # bodies it exists for.
        raw_body = body
        raw_size = len(body)
        encoding = await self._request_encoding(raw_size)
        if encoding is not None:
            body = gzip.compress(body, compresslevel=6, mtime=0)
        wire_size = len(body)
        started_at = time.monotonic()
        access_token = await self._access_token_provider(False)
        client = self._http_client_getter()
        owned = client is None
        if client is None:
            client = self._http_client_factory(60)
        staged = False
        try:
            staged = (
                await self._supports_resumable_uploads()
                and len(body) >= RESUMABLE_UPLOAD_MIN_BYTES
            )
            try:
                if staged:
                    response = await self._post_staged_batch(
                        client, access_token, body, encoding,
                        _body_digest(raw_body),
                    )
                else:
                    response = await self._post_ingest_batch(
                        client, access_token, body, encoding
                    )
            except httpx.RequestError as exc:
                raise ConnectorNetworkError(
                    f"backend ingest request failed: {exc}"
                ) from exc
            if getattr(response, "status_code", None) == 401 and not staged:
                logger.warning(
                    "connector ingest token rejected; refreshing access token and retrying"
                )
                access_token = await self._access_token_provider(True)
                try:
                    response = await self._post_ingest_batch(
                        client, access_token, body, encoding
                    )
                except httpx.RequestError as exc:
                    raise ConnectorNetworkError(
                        f"backend ingest retry failed: {exc}"
                    ) from exc
                if getattr(response, "status_code", None) == 401 and not staged:
                    raise ConnectorAuthenticationError(
                        "connector credential no longer valid"
                    )
            status = getattr(response, "status_code", 200)
            if status in {408, 429} or status >= 500:
                raise ConnectorNetworkError(f"backend ingest temporarily unavailable: HTTP {status}")
            response.raise_for_status()
            _raise_for_rejected_notifications(response)
        except ConnectorNetworkError:
            # Transient: the flush loop retries this same batch, and the staged
            # bytes are exactly what lets that retry continue instead of
            # restarting. Only the Server's time-to-live may drop them.
            raise
        except BaseException:
            # Permanent: nothing will retry this body, so release the staging
            # rather than waiting for the sweep.
            if staged:
                await self._discard_staged(client, access_token, raw_body)
            raise
        else:
            if staged:
                self._forget_staged(_body_digest(raw_body))
        finally:
            if owned:
                await client.aclose()
            self._log_upload(notifications, wire_size, raw_size, encoding, started_at)

    async def _supports_resumable_uploads(self) -> bool:
        import os

        configured = (
            os.environ.get("AA_CONNECTOR_INGEST_RESUMABLE", "auto").strip().lower()
        )
        if configured == "off":
            return False
        if configured == "on":
            return True
        return "resumable" in await self._server_ingest_capabilities()

    async def _post_staged_batch(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        body: bytes,
        encoding: str | None,
        digest: str,
    ) -> httpx.Response:
        """Send a body in resumable pieces, then ask the Server to apply it.

        Every piece is acknowledged with the running total, so a connection that
        drops half way resumes from the last acknowledgement instead of sending
        the whole snapshot again — which on the uplink this was written for is
        the difference between a session that syncs and one that never does.
        """

        async def send(
            request: Callable[[str], Awaitable[httpx.Response]],
        ) -> httpx.Response:
            """One staged request, refreshing the token once if it is refused.

            A staged transfer runs for minutes, so an access token can expire
            inside it. Treating that as permanent would delete every byte already
            sent; retrying resumes from the offset the Server reports.
            """

            nonlocal access_token
            response = await request(access_token)
            if getattr(response, "status_code", None) != 401:
                return response
            logger.warning(
                "connector ingest token rejected during staged upload; refreshing access token"
            )
            access_token = await self._access_token_provider(True)
            response = await request(access_token)
            if getattr(response, "status_code", None) == 401:
                raise ConnectorAuthenticationError(
                    "connector credential no longer valid"
                )
            return response

        headers = {"Authorization": f"Bearer {access_token}"}
        upload_id = self._staged_uploads.get(digest)
        offset = 0
        if upload_id is not None:
            offset = await self._staged_offset(client, send, upload_id)
            if offset < 0:
                # The Server no longer has it — expired, or swept — so start over.
                upload_id = None
        if upload_id is None:
            created = await send(
                lambda token: client.post(
                    api_v2_url(self._server_url, "/connector/ingest/uploads"),
                    headers=(
                        # Named separately from `Content-Encoding`: this request
                        # has no body, and that header describes the one it does
                        # carry — which a decompressing proxy may consume.
                        {**bearer(token), "X-Upload-Encoding": encoding}
                        if encoding is not None
                        else bearer(token)
                    ),
                    timeout=self._upload_timeout(),
                )
            )
            _raise_for_transient_status(created, "create staged upload")
            created.raise_for_status()
            upload_id = created.json()["uploadId"]
            offset = int(created.json().get("receivedBytes", 0))
            self._staged_uploads[digest] = upload_id
        while offset < len(body):
            chunk = body[offset : offset + RESUMABLE_UPLOAD_CHUNK_BYTES]
            response = await send(
                lambda token: client.put(
                    api_v2_url(
                        self._server_url, f"/connector/ingest/uploads/{upload_id}"
                    ),
                    headers={
                        **bearer(token),
                        "X-Upload-Offset": str(offset),
                        "Content-Type": "application/octet-stream",
                    },
                    content=chunk,
                    timeout=self._upload_timeout(),
                )
            )
            if response.status_code == 409:
                # The Server is further along than we thought; continue from
                # what it actually holds rather than re-sending.
                offset = int(response.json()["detail"]["receivedBytes"])
                continue
            _raise_for_transient_status(response, "stage upload piece")
            response.raise_for_status()
            offset = int(response.json()["receivedBytes"])
            self._staged_offsets[upload_id] = offset
        return await send(
            lambda token: client.post(
                api_v2_url(
                    self._server_url, f"/connector/ingest/uploads/{upload_id}/commit"
                ),
                headers=bearer(token),
                timeout=self._upload_timeout(),
            )
        )

    async def _staged_offset(self, client, send, upload_id: str) -> int:
        """How much the Server already holds, or -1 if it holds nothing.

        Goes through `send` like every other staged request. It used to be the
        one call that did not, so a retryable status here was read as permanent:
        the handler deleted the staged bytes and the batch was reported as
        refused, which is the opposite of what resuming is for.
        """

        try:
            response = await send(
                lambda token: client.get(
                    api_v2_url(
                        self._server_url, f"/connector/ingest/uploads/{upload_id}"
                    ),
                    headers=bearer(token),
                    timeout=self._upload_timeout(),
                )
            )
        except httpx.RequestError:
            # Unreachable is not the same as missing; let the upload continue
            # from where this Connector left off and resynchronise on the piece.
            return self._staged_offsets.get(upload_id, 0)
        if response.status_code == 404:
            return -1
        _raise_for_transient_status(response, "read staged upload")
        response.raise_for_status()
        return int(response.json()["receivedBytes"])

    async def _discard_staged(
        self, client: httpx.AsyncClient, access_token: str, body: bytes
    ) -> None:
        digest = _body_digest(body)
        upload_id = self._staged_uploads.pop(digest, None)
        if upload_id is None:
            return
        self._staged_offsets.pop(upload_id, None)
        with suppress(Exception):
            await client.delete(
                api_v2_url(self._server_url, f"/connector/ingest/uploads/{upload_id}"),
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self._upload_timeout(),
            )

    def _forget_staged(self, digest: str) -> None:
        upload_id = self._staged_uploads.pop(digest, None)
        if upload_id is not None:
            self._staged_offsets.pop(upload_id, None)

    def _log_upload(
        self,
        notifications: list[dict[str, Any]],
        wire_size: int,
        raw_size: int,
        encoding: str | None,
        started_at: float,
    ) -> None:
        """Report the throughput the link actually sustains.

        An asymmetric link is the binding constraint on how much history a
        Connector can carry, and every previous diagnosis of this system had to
        infer that from behaviour. One line per slow upload makes it observable:
        bytes on the wire per second, how much compression bought, and how much
        the lanes are still holding behind it.
        """

        elapsed = time.monotonic() - started_at
        if elapsed < 1.0 and raw_size < 128 * 1024:
            return
        logger.info(
            "connector ingest upload notifications={} wire_bytes={} raw_bytes={} "
            "encoding={} elapsed_ms={:.0f} kbps={:.1f} queued={} durable={} batches={}",
            len(notifications),
            wire_size,
            raw_size,
            encoding or "identity",
            elapsed * 1000,
            (wire_size / 1024 / elapsed) if elapsed else 0.0,
            len(self._notify_queue),
            len(self._durable),
            len(self._latest_timeline) + len(self._latest_value),
        )

    async def _post_ingest_batch(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        body: bytes,
        encoding: str | None = None,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        if encoding is not None:
            headers["Content-Encoding"] = encoding
        return await client.post(
            api_v2_url(self._server_url, "/connector/ingest"),
            headers=headers,
            content=body,
            timeout=self._upload_timeout(),
        )

    async def _request_encoding(self, size: int) -> str | None:
        """The encoding to send this body in, or ``None`` for plain JSON.

        Only the Server can say whether it can read a compressed body, so the
        answer comes from its health endpoint rather than from a version
        comparison — a new Connector against an old Server must keep working,
        and vice versa. An unreadable or silent endpoint means plain JSON, which
        every version understands.
        """

        import os

        configured = os.environ.get("AA_CONNECTOR_INGEST_GZIP", "auto").strip().lower()
        if configured == "off":
            return None
        if size < GZIP_MIN_BYTES:
            return None
        if configured == "on":
            return "gzip"
        return "gzip" if "gzip" in await self._server_encodings() else None

    async def _server_ingest_capabilities(self) -> dict[str, Any]:
        await self._server_encodings()
        return self._capabilities

    async def _server_encodings(self) -> frozenset[str]:
        now = time.monotonic()
        if (
            self._encodings is not None
            and now - self._encodings_checked_at < self._encodings_ttl
        ):
            return self._encodings
        client = self._http_client_getter()
        owned = client is None
        if client is None:
            client = self._http_client_factory(10)
        try:
            response = await client.get(
                api_v2_url(self._server_url, "/health"),
                timeout=httpx.Timeout(10.0, connect=self._connect_timeout_seconds()),
            )
            payload = response.json() if response.status_code == 200 else {}
        except Exception:  # noqa: BLE001 - discovery must never break ingest
            # An old Server, an unreachable one, or a client that cannot answer
            # this at all all mean the same thing: plain, whole-body uploads,
            # which every version understands. The failure is cached only
            # briefly, though: caching it for the full TTL meant one unreachable
            # /health disabled compression and resumable uploads for ten
            # minutes, which is the opposite of what a bad link needs.
            payload = {}
            self._encodings_ttl = INGEST_CAPABILITY_RETRY_SECONDS
        else:
            # A success is trusted for the full window again. Leaving the short
            # retry window in place made every later probe run every thirty
            # seconds for the life of the process.
            self._encodings_ttl = INGEST_CAPABILITY_TTL_SECONDS
        finally:
            if owned:
                await client.aclose()
        self._encodings_checked_at = time.monotonic()
        ingest = payload.get("ingest") if isinstance(payload, dict) else None
        self._capabilities = dict(ingest) if isinstance(ingest, dict) else {}
        encodings = self._capabilities.get("encodings")
        self._encodings = frozenset(
            value for value in encodings if isinstance(value, str)
        ) if isinstance(encodings, list) else frozenset()
        return self._encodings

    def _upload_timeout(self) -> httpx.Timeout:
        """One upload's budget, with the connect phase kept short.

        Read and write stay generous so a large history can still finish;
        connect and pool are bounded so a connection that will never come up
        fails fast instead of pinning the shared post lock.
        """

        connect = self._connect_timeout_seconds()
        return httpx.Timeout(
            self._upload_timeout_seconds(), connect=connect, pool=connect
        )

    def _upload_timeout_seconds(self) -> float:
        """Time to allow one ingest upload.

        The previous fixed 60s assumed a link where a whole snapshot fits in a
        minute. On an asymmetric connection it does not: measured here, upload
        runs at ~19 KB/s against 216 KB/s down, so a single megabyte already
        takes ~54s and any larger history times out every attempt. The request is
        not what the bridge waits on — it waits for the batch acknowledgement,
        which this call is upstream of — so a longer upload budget lets a large
        snapshot complete instead of failing deterministically.

        Overridable so a deployment can tighten or extend it.
        """
        import os

        configured = os.environ.get("AA_CONNECTOR_INGEST_TIMEOUT_SECONDS")
        if configured:
            try:
                return max(1.0, float(configured))
            except ValueError:
                pass
        return 900.0

    def _connect_timeout_seconds(self) -> float:
        """How long one upload may spend establishing its connection."""

        import os

        configured = os.environ.get("AA_CONNECTOR_INGEST_CONNECT_TIMEOUT_SECONDS")
        if configured:
            try:
                return max(1.0, float(configured))
            except ValueError:
                pass
        return DEFAULT_CONNECT_TIMEOUT_SECONDS

    def _enqueue_timeout_seconds(self) -> float:
        """How long one producer may wait for room before its notification drops."""

        import os

        configured = os.environ.get("AA_CONNECTOR_INGEST_ENQUEUE_TIMEOUT_SECONDS")
        if configured:
            try:
                return max(0.0, float(configured))
            except ValueError:
                pass
        return DEFAULT_ENQUEUE_TIMEOUT_SECONDS


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _raise_for_transient_status(response: Any, what: str) -> None:
    """Treat a retryable status as a network failure, not a refusal.

    The staged path is used precisely for the bodies that take minutes to send,
    so a proxy answering 502/503/429 partway through must leave the uploaded
    pieces alone and let the retry resume from the Server's offset. Classifying
    it as a refusal would delete the transfer and drop the whole batch.
    """

    status = getattr(response, "status_code", 200)
    if status in {408, 429} or status >= 500:
        raise ConnectorNetworkError(
            f"backend {what} temporarily unavailable: HTTP {status}"
        )


def _body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _encode_batch(notifications: list[dict[str, Any]]) -> bytes:
    """Assemble the request body from each notification's wire form.

    Every entry was serialized when it was admitted, so this is a join rather
    than another pass over a snapshot that can be megabytes.
    """

    def wire(entry: dict[str, Any]) -> bytes:
        body = entry.get("body")
        if body is not None:
            return body
        return json.dumps(
            {"method": entry["method"], "params": entry["params"]},
            ensure_ascii=False,
        ).encode("utf-8")

    joined = b",".join(wire(entry) for entry in notifications)
    return b'{"notifications":[' + joined + b"]}"


def _wire_notification(entry: dict[str, Any]) -> dict[str, Any]:
    """Reduce one queue entry to the notification the Server expects.

    Queue entries carry Connector-local bookkeeping (whether the entry was
    superseded, and which session it coalesces on) that must never reach the
    wire.
    """

    return {"method": entry["method"], "params": entry["params"]}


def _coalesce_key(method: str, params: dict[str, Any]) -> str | None:
    """The latest-wins slot a notification belongs to, or ``None``.

    ``timeline.sync`` is coalesced only when it is *complete*: a complete
    snapshot replaces the session's whole timeline, so the newest one says
    everything the older ones did. An incomplete snapshot upserts, so a newer
    one does not necessarily carry what an older one held, and dropping it would
    lose items. A busy session republishes its entire transcript every second or
    so — megabytes per snapshot against an uplink that carries tens of kilobytes
    per second — which is exactly why carrying only the newest matters.
    """

    if method == TIMELINE_SYNC:
        if params.get("complete") is not True:
            return None
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return None
        return f"{method}:{session_id}"
    if method == "timeline.itemUpsert":
        # A streaming DSH item is a complete replacement for one stable item.
        # Keep the newest revision per item instead of allowing a transcript
        # stream to fill the bounded queue and drop the live tail.
        session_id = params.get("sessionId")
        item = params.get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(session_id, str) or not session_id or not isinstance(item_id, str) or not item_id:
            return None
        return f"{method}:{session_id}:{item_id}"
    if method not in COALESCE_LATEST_METHODS:
        return None
    session_id = params.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None
    return f"{method}:{session_id}"


def _raise_for_rejected_notifications(response: httpx.Response) -> None:
    json_reader = getattr(response, "json", None)
    if not callable(json_reader):
        return
    try:
        payload = json_reader()
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    rejected = payload.get("rejected")
    if not isinstance(rejected, list) or not rejected:
        return
    first = rejected[0] if isinstance(rejected[0], dict) else {}
    method = first.get("method") if isinstance(first.get("method"), str) else "unknown"
    code = (
        first.get("code")
        if isinstance(first.get("code"), str)
        else "notification_rejected"
    )
    message = (
        first.get("message")
        if isinstance(first.get("message"), str)
        else "backend rejected connector notification"
    )
    raise ConnectorIngestRejectedError(
        f"backend ingest rejected {len(rejected)} notification(s); "
        f"first method={method} code={code}: {message}"
    )


def coalesce_timeline_item_upserts(
    notifications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only the newest upsert per timeline item inside one outbound batch."""
    latest_index_by_key: dict[tuple[str, str], int] = {}
    dropped: set[int] = set()
    for index, notification in enumerate(notifications):
        if notification.get("method") != "timeline.itemUpsert":
            continue
        params = notification.get("params")
        if not isinstance(params, dict):
            continue
        session_id = params.get("sessionId")
        item = params.get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(session_id, str) or not isinstance(item_id, str):
            continue
        key = (session_id, item_id)
        previous = latest_index_by_key.get(key)
        if previous is not None:
            previous_item = notifications[previous].get("params", {}).get("item", {})
            previous_seq = previous_item.get("updatedSeq", previous_item.get("updated_seq", 0))
            current_seq = item.get("updatedSeq", item.get("updated_seq", 0))
            # A latest-wins slot can be emitted before an older queued entry.
            # Keep the greatest complete revision, not merely the last position.
            if isinstance(previous_seq, int) and isinstance(current_seq, int) and current_seq < previous_seq:
                dropped.add(index)
                continue
            dropped.add(previous)
        latest_index_by_key[key] = index
    if not dropped:
        return notifications
    return [
        notification
        for index, notification in enumerate(notifications)
        if index not in dropped
    ]
