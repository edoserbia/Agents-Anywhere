from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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

# How long a producer may wait for room in the queue before the notification is
# dropped. The queue is drained by HTTP uploads that share the link with
# everything else, so a large history upload can leave it full for minutes. A
# producer that waits that long is not "backpressure", it is a stall: the
# callers below include Runtime RPC handlers, and one of them is `session.state`
# — the read the Server performs before it will accept a message. Blocking there
# is what surfaced to users as "runtime did not report its state in time".
DEFAULT_ENQUEUE_TIMEOUT_SECONDS = 2.0

TIMELINE_SYNC = "timeline.sync"

# Notifications that describe a session's *current* value rather than an event:
# the newest one supersedes every older one, so only the newest is worth
# carrying. They are held aside from the bounded queue entirely, which is what
# keeps a busy runtime's event storm from filling the queue with values nobody
# will ever read — and, before this, from stalling the RPC path behind them.
COALESCE_LATEST_METHODS = frozenset(
    {"session.state.updated", "session.source.updated"}
)

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
        self._notify_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self._inflight: list[dict[str, Any]] = []
        self._retry_delay = 1.0
        self._closed: Exception | None = None
        self._post_lock = asyncio.Lock()
        self._available = asyncio.Event()
        self._posting = False
        self._dropped = 0
        self._latest: dict[str, dict[str, Any]] = {}

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
            or bool(self._latest)
            or not self._notify_queue.empty()
        )

    def close(self, error: Exception | None = None) -> None:
        self._closed = error or RuntimeError("connector ingest is closed")
        self._latest.clear()
        # Wake blocked producers. They check _closed again after admission.
        while not self._notify_queue.empty():
            self._notify_queue.get_nowait()


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
        if key is not None:
            self._latest[key] = {"method": method, "params": params}
            self._available.set()
            return
        entry = {"method": method, "params": params}
        try:
            await asyncio.wait_for(
                self._notify_queue.put(entry),
                timeout=self._enqueue_timeout_seconds(),
            )
        except TimeoutError:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 50 == 0:
                logger.warning(
                    "connector ingest queue is full; dropped notification method={} dropped_total={} pending={}",
                    method,
                    self._dropped,
                    self._notify_queue.qsize(),
                )
            return
        self._available.set()
        if self._closed is not None:
            self.close(self._closed)
            raise self._closed

    async def ingest_notifications(self, notifications: list[dict[str, Any]]) -> None:
        """Send a batch synchronously, bypassing the flush queue."""
        if not notifications:
            return
        await self.post_batch(list(notifications))

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
                        self._inflight = []
                        if self._notify_queue.empty():
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
            self._inflight = []
            attempt = 0

    def _collect_pending(self) -> int:
        """Move pending notifications into the in-flight batch.

        Returns how many entries were taken from either source, so a caller
        draining a known backlog can still terminate when most of what it pulled
        collapsed into the latest-wins hold.
        """

        if self._inflight:
            return 0
        taken = 0
        while self._latest and len(self._inflight) < FLUSH_MAX:
            _, entry = self._latest.popitem()
            self._inflight.append(entry)
            taken += 1
        while len(self._inflight) < FLUSH_MAX and not self._notify_queue.empty():
            self._inflight.append(self._notify_queue.get_nowait())
            taken += 1
        return taken

    async def post_batch(self, notifications: list[dict[str, Any]]) -> None:
        async with self._post_lock:
            self._posting = True
            try:
                # Direct scanner snapshots cannot overtake an older failed batch.
                # Drain only work already queued at admission so a live stream
                # cannot starve this synchronous snapshot indefinitely.
                remaining = (
                    len(self._inflight)
                    + len(self._latest)
                    + self._notify_queue.qsize()
                )
                while remaining:
                    taken = self._collect_pending()
                    if self._inflight:
                        await self._post_batch(self._inflight)
                    remaining = max(
                        0, remaining - max(taken, len(self._inflight))
                    )
                    self._inflight = []
                await self._post_batch(notifications)
            finally:
                self._posting = False
                if not self.has_pending:
                    self._available.clear()

    async def _post_batch(self, notifications: list[dict[str, Any]]) -> None:
        if not notifications:
            return
        notifications = coalesce_timeline_item_upserts(
            [_wire_notification(entry) for entry in notifications]
        )
        if not notifications:
            return
        access_token = await self._access_token_provider(False)
        client = self._http_client_getter()
        owned = client is None
        if client is None:
            client = self._http_client_factory(60)
        try:
            try:
                response = await self._post_ingest_batch(
                    client, access_token, notifications
                )
            except httpx.RequestError as exc:
                raise ConnectorNetworkError(
                    f"backend ingest request failed: {exc}"
                ) from exc
            if getattr(response, "status_code", None) == 401:
                logger.warning(
                    "connector ingest token rejected; refreshing access token and retrying"
                )
                access_token = await self._access_token_provider(True)
                try:
                    response = await self._post_ingest_batch(
                        client, access_token, notifications
                    )
                except httpx.RequestError as exc:
                    raise ConnectorNetworkError(
                        f"backend ingest retry failed: {exc}"
                    ) from exc
                if getattr(response, "status_code", None) == 401:
                    raise ConnectorAuthenticationError(
                        "connector credential no longer valid"
                    )
            status = getattr(response, "status_code", 200)
            if status in {408, 429} or status >= 500:
                raise ConnectorNetworkError(f"backend ingest temporarily unavailable: HTTP {status}")
            response.raise_for_status()
            _raise_for_rejected_notifications(response)
        finally:
            if owned:
                await client.aclose()

    async def _post_ingest_batch(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        notifications: list[dict[str, Any]],
    ) -> httpx.Response:
        return await client.post(
            api_v2_url(self._server_url, "/connector/ingest"),
            headers={"Authorization": f"Bearer {access_token}"},
            json={"notifications": notifications},
            timeout=self._upload_timeout_seconds(),
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
    elif method not in COALESCE_LATEST_METHODS:
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
            dropped.add(previous)
        latest_index_by_key[key] = index
    if not dropped:
        return notifications
    return [
        notification
        for index, notification in enumerate(notifications)
        if index not in dropped
    ]
