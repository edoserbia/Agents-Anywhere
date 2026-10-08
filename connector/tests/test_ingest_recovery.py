from __future__ import annotations

import asyncio

import httpx
import pytest

from connector.runtime_protocol import SessionState
from connector.server.auth import ConnectorAuthenticationError
from connector.server.ingest import ConnectorIngestClient
from connector.server.rpc import ConnectorRpcChannel
from connector.server.runtime_host import ConnectorRuntimeHost
from connector.server.runtime_session_rpc import read_session_state


def test_network_failure_and_server_503_keep_fifo_for_recovery():
    async def run():
        attempts = []
        delivered = asyncio.Event()

        async def token(force):
            return "token"

        async def transport(request):
            import json

            batch = json.loads(request.content)["notifications"]
            attempts.append(batch)
            if len(attempts) == 1:
                raise httpx.ConnectError("offline", request=request)
            if len(attempts) == 2:
                return httpx.Response(503)
            delivered.set()
            return httpx.Response(200, json={"accepted": len(batch), "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            ingest._retry_delay = 0.001
            await ingest.enqueue("session.state.updated", {"status": "running"})
            await ingest.enqueue("session.state.updated", {"status": "idle"})
            task = asyncio.create_task(ingest.flush_loop())
            await asyncio.wait_for(delivered.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert attempts[0] == attempts[1] == attempts[2]
            assert [row["params"]["status"] for row in attempts[2]] == [
                "running",
                "idle",
            ]
            assert not ingest.has_pending

    asyncio.run(run())


def test_cancelled_http_batch_survives_flush_worker_restart():
    async def run():
        started = asyncio.Event()
        delivered = asyncio.Event()
        calls = []

        async def token(force):
            return "token"

        async def transport(request):
            calls.append(request.content)
            if len(calls) == 1:
                started.set()
                await asyncio.Event().wait()
            delivered.set()
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            await ingest.enqueue("timeline.sync", {"sessionId": "session", "items": []})
            task = asyncio.create_task(ingest.flush_loop())
            await started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert ingest.has_pending
            task = asyncio.create_task(ingest.flush_loop())
            await asyncio.wait_for(delivered.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert calls[0] == calls[1]

    asyncio.run(run())


def test_revoked_credentials_stop_retry_and_reject_new_admission():
    async def run():
        async def token(force):
            raise ConnectorAuthenticationError("revoked")

        ingest = ConnectorIngestClient(
            "https://server.test", token, lambda: None, lambda timeout: None
        )
        await ingest.enqueue("connector.heartbeat", {})
        with pytest.raises(ConnectorAuthenticationError):
            await asyncio.wait_for(ingest.flush_loop(), 1)
        with pytest.raises(ConnectorAuthenticationError):
            await ingest.enqueue("connector.heartbeat", {})

    asyncio.run(run())


def test_old_rpc_completion_cannot_reply_on_replacement_socket():
    async def run():
        class Socket:
            def __init__(self):
                self.frames = []

            async def send(self, payload):
                self.frames.append(payload)

        entered, release = asyncio.Event(), asyncio.Event()
        channel = ConnectorRpcChannel()
        old, new = Socket(), Socket()
        channel.set_connection(old)

        async def dispatch(method, params):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Some runtime operations finish cleanup despite cancellation.
                await release.wait()
            return {"status": "stopped"}

        channel.start_request(
            {"type": "request", "id": "old", "method": "runtime.stop"}, dispatch
        )
        await entered.wait()
        old_tasks = list(channel._request_tasks)
        channel.set_connection(new)
        release.set()
        await asyncio.gather(*old_tasks)
        assert not old.frames and not new.frames
        await channel.close_connection()

    asyncio.run(run())


def test_failed_websocket_sender_stops_admission_instead_of_hanging_next_send():
    async def run():
        class Socket:
            async def send(self, payload):
                raise ConnectionError("link lost")

        channel = ConnectorRpcChannel()
        channel.set_connection(Socket())
        with pytest.raises(ConnectionError):
            await channel.send_notification("connector.heartbeat", {})
        assert not channel.connected
        with pytest.raises(RuntimeError, match="not connected"):
            await asyncio.wait_for(
                channel.send_notification("connector.heartbeat", {}), 1
            )
        await channel.close_connection()


def _saturate(ingest: ConnectorIngestClient) -> None:
    """Fill the outbound queue the way a stalled upload does."""

    while not ingest._notify_queue.full():
        ingest._notify_queue.put_nowait(
            {"method": "timeline.itemUpsert", "params": {"item": {}}}
        )


def test_a_full_ingest_queue_drops_instead_of_blocking_the_producer():
    async def run():
        async def token(force):
            return "token"

        ingest = ConnectorIngestClient(
            "https://server.test", token, lambda: None, lambda timeout: None
        )
        ingest._enqueue_timeout_seconds = lambda: 0.01
        _saturate(ingest)
        await asyncio.wait_for(ingest.enqueue("session.state.updated", {}), 1)
        assert ingest.dropped == 1

    asyncio.run(run())


def test_session_state_read_answers_while_the_ingest_queue_is_saturated():
    """The Server reads session state before it will accept a message.

    Publishing the read's own state notification used to be what blocked, so a
    full ingest queue made sending impossible — reported to the user as
    "runtime did not report its state in time" — while the runtime itself was
    perfectly healthy. The read must answer regardless of the notification.
    """

    async def run():
        async def token(force):
            return "token"

        ingest = ConnectorIngestClient(
            "https://server.test", token, lambda: None, lambda timeout: None
        )
        ingest._enqueue_timeout_seconds = lambda: 0.01
        _saturate(ingest)

        host = ConnectorRuntimeHost(
            connector_id="conn_test",
            notifier=ingest.enqueue,
            attachment_downloader=_unused_download,
        )

        class Runtime:
            async def get_session_state(self, session_id, external_session_id=None):
                return SessionState(
                    session_id=session_id,
                    external_session_id=external_session_id,
                    runtime="openscience",
                    status="idle",
                )

        result = await asyncio.wait_for(
            read_session_state(
                Runtime(),
                host,
                {"sessionId": "sess_1", "externalSessionId": "ses_1"},
            ),
            2,
        )
        assert result["state"]["status"] == "idle"
        # The notification is held for the next upload instead of being dropped
        # behind a saturated queue, so the Server still learns the state.
        assert [entry["params"]["status"] for entry in ingest._latest.values()] == [
            "idle"
        ]

    asyncio.run(run())


async def _unused_download(session_id: str, file_id: str):
    raise AssertionError("attachments are not read by this test")


def test_a_stale_complete_timeline_snapshot_never_reaches_the_wire():
    """A superseded full snapshot must not hold up the queue.

    A busy session republishes its whole transcript every second, and one
    snapshot is megabytes. Queueing them all is what filled the queue, delayed
    every other session's history, and — through the blocked state read — made
    sending a message impossible. Only the newest complete snapshot per session
    is worth uploading; an incomplete one still upserts, so it is kept.
    """

    async def run():
        delivered = asyncio.Event()
        batches: list[list[dict]] = []

        async def token(force):
            return "token"

        async def transport(request):
            import json

            batches.append(json.loads(request.content)["notifications"])
            delivered.set()
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            for size in (1, 2, 3):
                await ingest.enqueue(
                    "timeline.sync",
                    {"sessionId": "s1", "complete": True, "items": list(range(size))},
                )
            await ingest.enqueue(
                "timeline.sync",
                {"sessionId": "s1", "complete": False, "items": ["partial"]},
            )
            task = asyncio.create_task(ingest.flush_loop())
            await asyncio.wait_for(delivered.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert [row["params"]["items"] for row in batches[0]] == [
            [0, 1, 2],
            ["partial"],
        ]
        # Connector-local bookkeeping must never reach the Server.
        assert all(set(row) == {"method", "params"} for row in batches[0])

    asyncio.run(run())


def test_latest_wins_notifications_never_queue_behind_a_full_backlog():
    """A state update is a value, not an event: it must not wait in line."""

    async def run():
        delivered = asyncio.Event()
        batches: list[list[dict]] = []

        async def token(force):
            return "token"

        async def transport(request):
            import json

            batches.append(json.loads(request.content)["notifications"])
            delivered.set()
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            ingest._enqueue_timeout_seconds = lambda: 0.01
            _saturate(ingest)
            for status in ("running", "idle"):
                await ingest.enqueue(
                    "session.state.updated", {"sessionId": "s1", "status": status}
                )
            assert ingest.dropped == 0
            assert [entry["params"]["status"] for entry in ingest._latest.values()] == [
                "idle"
            ]
            task = asyncio.create_task(ingest.flush_loop())
            await asyncio.wait_for(delivered.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        delivered_rows = [
            row for batch in batches for row in batch if row["method"] == "session.state.updated"
        ]
        assert [row["params"]["status"] for row in delivered_rows] == ["idle"]

    asyncio.run(run())


def test_draining_a_backlog_of_stale_snapshots_terminates():
    """The synchronous drain must not spin when everything it pulled was stale."""

    async def run():
        async def token(force):
            return "token"

        async def transport(request):
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            for size in range(1, 40):
                await ingest.enqueue(
                    "timeline.sync",
                    {"sessionId": "s1", "complete": True, "items": list(range(size))},
                )
            await asyncio.wait_for(ingest.post_batch([]), 2)
            assert not ingest.has_pending

    asyncio.run(run())


    asyncio.run(run())
