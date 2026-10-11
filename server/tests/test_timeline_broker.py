from __future__ import annotations

import asyncio
import contextlib
import json

from agent_server.infra.timeline_broker import TimelineBroker


def test_dashboard_events_are_debounced() -> None:
    async def exercise() -> None:
        broker = TimelineBroker(dashboard_debounce_seconds=0.01)
        queue = await broker.register_dashboard("user1")

        await broker.publish_dashboard("user1", {"reason": "first", "serverTime": "t1"})
        await broker.publish_dashboard("user1", {"reason": "second", "serverTime": "t2"})

        message = await asyncio.wait_for(queue.get(), timeout=1)
        payload = json.loads(message)
        assert payload["type"] == "dashboard.changed"
        assert payload["serverTime"] == "t2"
        assert queue.empty()

    asyncio.run(exercise())


class _HealthyPubSub:
    """A subscription that works."""

    def __init__(self, events: list) -> None:
        self._events = events
        self.patterns: tuple[str, ...] = ()

    async def psubscribe(self, *patterns: str) -> None:
        self.patterns = patterns

    async def listen(self):
        for event in self._events:
            yield event

    async def aclose(self) -> None:
        return None


class _DistributedCoordinator:
    """Just enough coordinator for the listener: a client and a channel name."""

    prefix = "aa"

    def __init__(self, *, pubsub) -> None:
        self._pubsub = pubsub

    @property
    def distributed(self) -> bool:
        return True

    @property
    def client(self):
        return self

    def pubsub(self):
        # What a reconnect gets: a fresh, working subscription.
        return self._pubsub

    def channel(self, *parts: str) -> str:
        return ":".join((self.prefix, "channel", *(str(part) for part in parts)))


class _FlakyPubSub:
    """A subscription that dies once, the way a dropped Redis connection does."""

    def __init__(self, events_after_recovery: list) -> None:
        self._events = events_after_recovery
        self._dropped = False
        self.patterns: tuple[str, ...] = ()

    async def psubscribe(self, *patterns: str) -> None:
        self.patterns = patterns

    async def listen(self):
        if not self._dropped:
            self._dropped = True
            # A connection lost mid-stream: the iterator raises, exactly as the
            # redis client does, and the listener has to come back from it.
            raise ConnectionError(
                "Error UNKNOWN while writing to socket. Connection lost."
            )
        for event in self._events:
            yield event

    async def aclose(self) -> None:
        return None


def test_the_listener_resubscribes_after_redis_drops_the_connection() -> None:
    """A subscription that dies with its connection freezes every client.

    Seen in production: Redis dropped the pubsub connection, the listener ended,
    and from then on publishers wrote into a channel nobody read. Clients kept
    the snapshot they had loaded and never saw another change until the process
    was restarted. The listener has to outlive its connection.
    """

    async def run():
        broker = TimelineBroker(
            listen_retry_seconds=0.01,
            listen_retry_max_seconds=0.02,
        )
        event = {
            "type": "pmessage",
            "channel": "aa:channel:timeline:sess_1",
            "data": '{"type":"timeline.items"}',
        }
        recovered = _HealthyPubSub([event])
        broker._coordinator = _DistributedCoordinator(pubsub=recovered)
        first = _FlakyPubSub([event])
        broker._pubsub = first
        queue: asyncio.Queue = asyncio.Queue()
        broker._subs["sess_1"] = {queue}

        task = asyncio.create_task(broker._listen())
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if not queue.empty():
                    break
            assert not queue.empty(), "the listener never recovered the subscription"
            assert broker._pubsub is not None
            assert broker._pubsub is not first, "it kept using the dead connection"
            assert broker._pubsub.patterns == (
                "aa:channel:timeline:*",
                "aa:channel:dashboard:*",
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_the_listener_stops_when_it_is_cancelled() -> None:
    """Recovery must not turn shutdown into a spin."""

    async def run():
        broker = TimelineBroker(listen_retry_seconds=0.01)
        broker._coordinator = _DistributedCoordinator(pubsub=_HealthyPubSub([]))
        broker._pubsub = _FlakyPubSub([])
        task = asyncio.create_task(broker._listen())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.done()

    asyncio.run(run())
