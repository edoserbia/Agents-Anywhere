from __future__ import annotations

import asyncio
import gzip
import json
import os

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

    while len(ingest._notify_queue) < ingest._queue_limit():
        ingest._notify_queue.append(
            ingest._admit("timeline.itemUpsert", {"item": {}})
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


def test_streaming_timeline_upserts_are_latest_wins_when_queue_is_saturated():
    """A busy DSH transcript must not drop the live tail behind history."""

    async def run():
        async def token(force):
            return "token"

        ingest = ConnectorIngestClient(
            "https://server.test", token, lambda: None, lambda timeout: None
        )
        ingest._enqueue_timeout_seconds = lambda: 0.01
        _saturate(ingest)

        await ingest.enqueue(
            "timeline.itemUpsert",
            {"sessionId": "s1", "item": {"id": "assistant-1", "revision": 1}},
        )
        await ingest.enqueue(
            "timeline.itemUpsert",
            {"sessionId": "s1", "item": {"id": "assistant-1", "revision": 2}},
        )

        assert ingest.dropped == 0
        assert ingest._latest_timeline["timeline.itemUpsert:s1:assistant-1"]["params"]["item"]["revision"] == 2

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
        assert [entry["params"]["status"] for entry in ingest._latest_value.values()] == [
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
            assert [entry["params"]["status"] for entry in ingest._latest_value.values()] == [
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


def test_durable_submission_does_not_wait_for_the_upload_and_the_barrier_does():
    """A runtime feed acknowledges its peer on local receipt.

    The DSH bridge is stop-and-wait and flushes thirty batches a second, so a
    batch acknowledgement that waited for a remote upload capped the whole live
    stream at a couple of batches per second: the bridge's event buffer
    overflowed and every session was recalibrated in a loop. Accepting locally
    is what fixes that; the barrier is what keeps the promise honest.
    """

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        batches: list[list[str]] = []

        async def token(force):
            return "token"

        async def transport(request):
            import json

            batches.append(
                [row["method"] for row in json.loads(request.content)["notifications"]]
            )
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            for index in range(200):
                await asyncio.wait_for(
                    ingest.submit_notifications([
                        {"method": "timeline.itemUpsert",
                         "params": {"sessionId": "s1", "item": {"id": f"i{index}"}}}
                    ]),
                    1,
                )
            # 200 notifications accepted while nothing has been uploaded.
            assert batches == []

            flusher = asyncio.create_task(ingest.flush_loop())
            await asyncio.wait_for(entered.wait(), 1)
            barrier = asyncio.create_task(ingest.flush_notifications())
            await asyncio.sleep(0.05)
            assert not barrier.done(), "the barrier must not pass before the upload"
            release.set()
            await asyncio.wait_for(barrier, 2)
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        delivered = [row for batch in batches for row in batch]
        assert len(delivered) == 200
        assert all(row == "timeline.itemUpsert" for row in delivered)

    asyncio.run(run())


def test_a_complete_snapshot_is_never_overtaken_by_the_increments_after_it():
    """Order is data: the Server applies what it receives to one timeline."""

    async def run():
        batches: list[list[str]] = []

        async def token(force):
            return "token"

        async def transport(request):
            import json

            batches.append(
                [row["method"] for row in json.loads(request.content)["notifications"]]
            )
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            await ingest.submit_notifications([
                {"method": "timeline.sync",
                 "params": {"sessionId": "s1", "complete": True, "items": []}},
            ])
            # An increment admitted after the snapshot, through the other lane.
            await ingest.enqueue(
                "timeline.itemUpsert", {"sessionId": "s1", "item": {"id": "next"}}
            )
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        assert [row for batch in batches for row in batch] == [
            "timeline.sync",
            "timeline.itemUpsert",
        ]

    asyncio.run(run())


def test_a_superseded_item_revision_never_consumes_uplink():
    """A streaming message is re-sent whole on every revision.

    On an asymmetric link that volume — not the round trip — is what makes
    activity arrive minutes late, so a revision still waiting to be uploaded is
    replaced by the newer one instead of queueing behind it.
    """

    async def run():
        batches: list[list[dict]] = []

        async def token(force):
            return "token"

        async def transport(request):
            import json

            batches.append(json.loads(request.content)["notifications"])
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            for revision in range(1, 31):
                await ingest.submit_notifications([
                    {"method": "timeline.itemUpsert", "params": {
                        "sessionId": "s1", "item": {"id": "reply", "revision": revision}}}
                ])
            # Thirty revisions of one item are still one outstanding piece of
            # work: each new revision resolves the one it replaces.
            assert ingest._durable_submitted - ingest._durable_resolved == 1
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        delivered = [row for batch in batches for row in batch]
        assert len(delivered) == 1
        assert delivered[0]["params"]["item"]["revision"] == 30

    asyncio.run(run())


def test_a_delivered_item_does_not_absorb_a_later_revision():
    """Coalescing is only for what is still waiting; later edits must ship."""

    async def run():
        batches: list[list[dict]] = []

        async def token(force):
            return "token"

        async def transport(request):
            import json

            batches.append(json.loads(request.content)["notifications"])
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            flusher = asyncio.create_task(ingest.flush_loop())
            for revision, expected in ((1, 1), (2, 2)):
                await ingest.submit_notifications([
                    {"method": "timeline.itemUpsert", "params": {
                        "sessionId": "s1", "item": {"id": "reply", "revision": revision}}}
                ])
                await ingest.flush_notifications()
                delivered = [row for batch in batches for row in batch]
                assert len(delivered) == expected
                assert delivered[-1]["params"]["item"]["revision"] == revision
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

    asyncio.run(run())


def _gzip_server(requests: list) -> "httpx.MockTransport":
    """A Server that advertises gzip ingest support."""

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "ingest": {"encodings": ["gzip"]}})
        return httpx.Response(200, json={"accepted": 1, "rejected": []})

    return httpx.MockTransport(handler)


def test_a_large_body_is_compressed_only_when_the_server_says_it_can_read_it():
    """Text JSON repeats whole messages; that volume is what an uplink runs out of."""

    async def run():
        async def token(force):
            return "token"

        requests: list = []
        async with httpx.AsyncClient(transport=_gzip_server(requests)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            big = {"method": "timeline.itemUpsert", "params": {
                "sessionId": "s1", "item": {"id": "reply", "text": "x" * 40000}}}
            await ingest.submit_notifications([big])
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        uploads = [r for r in requests if r.url.path.endswith("/connector/ingest")]
        assert len(uploads) == 1
        assert uploads[0].headers["content-encoding"] == "gzip"
        import gzip as gziplib
        import json as jsonlib

        body = gziplib.decompress(uploads[0].content)
        assert jsonlib.loads(body)["notifications"][0]["params"]["item"]["text"] == "x" * 40000
        # The compressed body is a fraction of the original.
        assert len(uploads[0].content) < 40000 // 4

    asyncio.run(run())


def test_a_server_that_does_not_advertise_compression_gets_plain_json():
    """A new Connector must keep working against an old Server."""

    async def run():
        async def token(force):
            return "token"

        requests: list = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/health"):
                return httpx.Response(200, json={"status": "ok", "version": "2.0.25"})
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            big = {"method": "timeline.itemUpsert", "params": {
                "sessionId": "s1", "item": {"id": "reply", "text": "x" * 40000}}}
            await ingest.submit_notifications([big])
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        uploads = [r for r in requests if r.url.path.endswith("/connector/ingest")]
        assert len(uploads) == 1
        assert "content-encoding" not in uploads[0].headers
        assert b"x" * 100 in uploads[0].content

    asyncio.run(run())


def test_live_notifications_are_not_compressed():
    """Latency rides on the small notifications; compressing them costs CPU only."""

    async def run():
        async def token(force):
            return "token"

        requests: list = []
        async with httpx.AsyncClient(transport=_gzip_server(requests)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            await ingest.submit_notifications([
                {"method": "session.state.updated", "params": {"sessionId": "s1", "status": "idle"}}
            ])
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        uploads = [r for r in requests if r.url.path.endswith("/connector/ingest")]
        assert "content-encoding" not in uploads[0].headers

    asyncio.run(run())


def test_one_request_never_carries_more_than_the_byte_budget():
    """A failed upload is re-sent whole.

    On a link that is slow enough for a request to fail partway, an unbounded
    request throws away everything it carried. Bounding it means each successful
    request is progress the Server keeps, and a retry only repeats a small piece.
    """

    async def run():
        batches: list[int] = []

        async def token(force):
            return "token"

        async def transport(request):
            import json

            if request.url.path.endswith("/connector/ingest"):
                batches.append(len(json.loads(request.content)["notifications"]))
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            ingest._flush_bytes = lambda: 8192
            for index in range(200):
                await ingest.submit_notifications([
                    {"method": "timeline.itemUpsert", "params": {
                        "sessionId": "s1",
                        "item": {"id": f"i{index}", "text": "x" * 400},
                    }}
                ])
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        # Split across requests instead of one 80 KB body, and nothing lost.
        assert len(batches) > 1
        assert sum(batches) == 200
        assert max(batches) < 200

    asyncio.run(run())


def test_a_single_oversized_notification_still_travels_alone():
    """The budget must never make a notification undeliverable."""

    async def run():
        sizes: list[int] = []

        async def token(force):
            return "token"

        async def transport(request):
            if request.url.path.endswith("/connector/ingest"):
                sizes.append(len(request.content))
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            ingest._flush_bytes = lambda: 1024
            await ingest.submit_notifications([
                {"method": "timeline.sync", "params": {
                    "sessionId": "s1", "complete": True,
                    "items": [{"id": "big", "text": "y" * 8000}],
                }},
                {"method": "session.state.updated", "params": {
                    "sessionId": "s1", "status": "idle"}},
            ])
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        assert len(sizes) == 2
        assert sizes[0] > 1024

    asyncio.run(run())


def _staging_server(state: dict, requests: list):
    """A Server that stages resumable ingest bodies and drops one mid-transfer."""

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/health"):
            return httpx.Response(200, json={
                "status": "ok", "ingest": {"encodings": ["gzip"], "resumable": True}})
        if path.endswith("/connector/ingest/uploads"):
            state.setdefault("received", bytearray())
            state["id"] = "u" * 24
            # The staged body's encoding travels in its own header: this request
            # has no body of its own for `Content-Encoding` to describe.
            state["create_encoding"] = request.headers.get("x-upload-encoding")
            return httpx.Response(200, json={"uploadId": state["id"], "receivedBytes": 0})
        if path.endswith(f"/uploads/{state.get('id')}"):
            if request.method == "DELETE":
                state["discarded"] = True
                return httpx.Response(204)
            if request.method == "GET":
                return httpx.Response(200, json={
                    "uploadId": state["id"], "receivedBytes": len(state["received"])})
            offset = int(request.headers["x-upload-offset"])
            if offset != len(state["received"]):
                return httpx.Response(409, json={"detail": {"receivedBytes": len(state["received"])}})
            state["received"].extend(request.content)
            state["put_bytes"] = state.get("put_bytes", 0) + len(request.content)
            if "cut" not in state:
                # The connection dies part way through, as it would on a bad link.
                state["cut"] = True
                state["staged_before"] = len(state["received"])
                raise httpx.ReadError("connection reset", request=request)
            return httpx.Response(200, json={"uploadId": state["id"], "receivedBytes": len(state["received"])})
        if path.endswith("/commit"):
            state["committed"] = bytes(state["received"])
            return httpx.Response(200, json={"accepted": 1, "rejected": [], "nextSeq": 1})
        raise AssertionError(f"unexpected request {request.method} {path}")

    return httpx.MockTransport(handler)


def test_an_interrupted_large_body_resumes_instead_of_restarting():
    """Re-sending a whole snapshot on every drop is what leaves a session stuck.

    A complete timeline snapshot cannot be split — the Server replaces the whole
    timeline with it — so a 90%-complete transfer is only worth anything if the
    bytes survived the drop.
    """

    from connector.server import ingest as ingest_module

    state: dict = {}
    requests: list = []

    async def token(force):
        return "token"

    async def exercise():
        async with httpx.AsyncClient(transport=_staging_server(state, requests)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            await ingest.submit_notifications([{
                "method": "timeline.sync",
                "params": {
                    "sessionId": "s1", "complete": True,
                    "items": [{"id": "big", "text": "z" * 600000}],
                },
            }])
            # First attempt: stages bytes, then the link drops.
            flusher = asyncio.create_task(ingest.flush_loop())
            for _ in range(200):
                if state.get("staged_before"):
                    break
                await asyncio.sleep(0.01)
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

            assert state.get("staged_before", 0) > 0, "nothing was staged"
            assert not state.get("discarded"), "a retryable failure discarded the progress"

            # Second attempt: the same batch, resumed.
            flusher = asyncio.create_task(ingest.flush_loop())
            for _ in range(400):
                if "committed" in state:
                    break
                await asyncio.sleep(0.01)
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

    original_min = ingest_module.RESUMABLE_UPLOAD_MIN_BYTES
    original_chunk = ingest_module.RESUMABLE_UPLOAD_CHUNK_BYTES
    # The threshold is on the encoded body, which is what actually travels; the
    # chunk is shrunk so the drop lands mid-body rather than after the last one.
    ingest_module.RESUMABLE_UPLOAD_MIN_BYTES = 512
    ingest_module.RESUMABLE_UPLOAD_CHUNK_BYTES = 256
    try:
        asyncio.run(exercise())
    finally:
        ingest_module.RESUMABLE_UPLOAD_MIN_BYTES = original_min
        ingest_module.RESUMABLE_UPLOAD_CHUNK_BYTES = original_chunk

    committed = state.get("committed")
    assert committed, "the body was never applied"
    wire_bytes = len(committed)
    if committed[:2] == b"\x1f\x8b":
        committed = gzip.decompress(committed)
    delivered = json.loads(committed)["notifications"]
    assert delivered[0]["params"]["complete"] is True
    assert delivered[0]["params"]["items"][0]["text"] == "z" * 600000
    # The retry continued from what the Server held rather than starting over:
    # every byte crossed the wire exactly once.
    assert state["staged_before"] > 0
    assert state["create_encoding"] == "gzip"
    puts = [r for r in requests if r[0] == "PUT"]
    assert len(puts) > 2
    assert state["put_bytes"] == wire_bytes


def test_a_bypass_list_httpx_cannot_parse_does_not_stop_the_connector(monkeypatch):
    """Client construction must not fail on an environment variable.

    A bracketed IPv6 entry — `[::1]`, which macOS merges in from the system
    proxy configuration — makes httpx raise while *building the client*, so a
    Connector launched from such an environment never starts at all, while the
    same build launched from the desktop app starts fine.
    """

    from connector.core.proxy_env import normalize_no_proxy

    bypass = "localhost,127.0.0.1,[::1],.internal"
    monkeypatch.setenv("NO_PROXY", bypass)
    monkeypatch.setenv("no_proxy", bypass)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")

    with pytest.raises(httpx.InvalidURL):
        httpx.AsyncClient(timeout=5, trust_env=True)

    changed = normalize_no_proxy()

    assert sorted(changed) == ["NO_PROXY", "no_proxy"]
    # Everything httpx can use survives; only the unusable entry goes, and the
    # proxy variables are never touched.
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,.internal"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    client = httpx.AsyncClient(timeout=5, trust_env=True)
    assert client is not None
    asyncio.run(client.aclose())
    # Safe to call before every construction.
    assert normalize_no_proxy() == []


def test_a_streaming_reply_does_not_send_every_revision():
    """The dominant uplink cost: a reply re-sent whole as it streams.

    A runtime produces revisions as fast as it can, and each one carries the
    message's entire text. Sending all of them is what makes a chatty session
    ask for more uplink than a slow link has — so the newest revision of an item
    is held for one window and the ones it supersedes are never sent at all.
    """

    async def run():
        sent: list[int] = []

        async def token(force):
            return "token"

        async def transport(request):
            if request.url.path.endswith("/connector/ingest"):
                sent.append(len(json.loads(request.content)["notifications"]))
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            ingest._timeline_window_seconds = lambda: 0.2
            for revision in range(1, 31):
                await ingest.submit_notifications([{
                    "method": "timeline.itemUpsert",
                    "params": {"sessionId": "s1",
                               "item": {"id": "reply", "revision": revision}},
                }])
                await asyncio.sleep(0.002)
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        # Thirty revisions crossed the wire as one, and the survivor is the last.
        assert sum(sent) == 1
        assert sent == [1]

    asyncio.run(run())


def test_a_window_never_lets_a_later_change_overtake_an_earlier_one():
    """Holding one entry must not let a later one pass it.

    The Server applies what it receives to one timeline, so an increment that
    overtook a complete snapshot would be lost when the snapshot landed.
    """

    async def run():
        order: list[str] = []

        async def token(force):
            return "token"

        async def transport(request):
            if request.url.path.endswith("/connector/ingest"):
                for row in json.loads(request.content)["notifications"]:
                    order.append(row["params"].get("complete") and "snapshot" or "increment")
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            ingest._timeline_window_seconds = lambda: 0.2
            # An increment first, then the snapshot that already contains it.
            await ingest.submit_notifications([{
                "method": "timeline.itemUpsert",
                "params": {"sessionId": "s1", "item": {"id": "reply", "revision": 2}},
            }])
            await ingest.submit_notifications([{
                "method": "timeline.sync",
                "params": {"sessionId": "s1", "complete": True, "items": []},
            }])
            flusher = asyncio.create_task(ingest.flush_loop())
            await ingest.flush_notifications()
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

        assert order == ["increment", "snapshot"]

    asyncio.run(run())


def test_a_refused_batch_does_not_wedge_every_later_batch():
    """One rejected batch must not stop the whole Connector.

    A permanent rejection is reachable without any bug in the runtime: a single
    oversized item is answered with 413, and a malformed one with 422. Both used
    to poison the client for good, so every session on that machine stopped
    syncing until the process was restarted — the same "only a restart fixes
    it" shape as the frozen-proxy incident.

    The guarantee that must survive: a checkpoint covering refused data still
    fails, because those notifications never reached the Server.
    """

    async def run():
        refuse = True

        async def token(force):
            return "token"

        async def transport(request):
            if request.url.path.endswith("/connector/ingest"):
                if refuse:
                    return httpx.Response(413, json={"code": "too_large"})
            return httpx.Response(200, json={"accepted": 1, "rejected": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            ingest = ConnectorIngestClient(
                "https://server.test", token, lambda: http, lambda timeout: http
            )
            flusher = asyncio.create_task(ingest.flush_loop())

            await ingest.submit_notifications([{
                "method": "session.meta.upsert",
                "params": {"sessionId": "s1", "title": "first"},
            }])
            # The checkpoint covering the refused data must not pass.
            with pytest.raises(httpx.HTTPStatusError):
                await asyncio.wait_for(ingest.flush_notifications(timeout=3), 4)

            # New data is still accepted, and a later checkpoint over it can
            # pass: the refusal is scoped to what it covered.
            refuse = False
            await ingest.submit_notifications([{
                "method": "session.meta.upsert",
                "params": {"sessionId": "s1", "title": "second"},
            }])
            await asyncio.wait_for(ingest.flush_notifications(timeout=3), 4)

            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

    asyncio.run(run())
