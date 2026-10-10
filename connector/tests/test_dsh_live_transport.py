from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from connector.core.config import ConnectorConfig
from connector.runtime_protocol import RuntimeInstanceHost, RuntimeInstanceSpec, timeline_content_hash
from connector.runtimes.dsh.bridge.sync import SyncRelay
from connector.server.client import BackendRpcClient
from connector.server.rpc import CONNECTOR_WS_MAX_NOTIFICATION_BYTES


def backend_client(tmp_path):
    client = BackendRpcClient(
        ConnectorConfig("http://test", "connector", "test-only", state_path=str(tmp_path / "sync.json")),
        agent_runtime_providers=(),
    )
    client._ingest.ingest_notifications = AsyncMock()
    client._ingest.enqueue = AsyncMock()
    client._timeline_notifications.send = AsyncMock(wraps=client._timeline_notifications.send)
    host = RuntimeInstanceHost(client.agent_runtime_host,
        RuntimeInstanceSpec(runtime_id="rti_dsh", runtime_type="dsh", name="DSH"))
    return client, host


def item_notification(text="streaming text"):
    content = {"text": text, "format": "markdown"}
    item = {"id": "reply", "sessionId": "session", "type": "message", "role": "assistant",
        "status": "inProgress", "orderSeq": 1, "revision": 1, "content": content,
        "source": {"runtime": "dsh"},
        "contentHash": timeline_content_hash("message", "inProgress", "assistant", content)}
    return {"method": "timeline.itemUpsert", "params": {
        "sessionId": "session", "runtime": "foreign", "runtimeId": "foreign", "item": item}}


def start_relay(host, notifications):
    acknowledged = asyncio.Event()
    acks = []

    async def request(method, params=None):
        if method == "runtime.sync.subscribe":
            return {"streamId": "stream", "projectionVersion": 2}
        assert method == "runtime.sync.ack"
        acks.append(params["batchSeq"])
        acknowledged.set()

    bridge = SimpleNamespace(request=request, writer=Mock())
    relay = SyncRelay(bridge, host)
    relay.start()
    relay.accept({"streamId": "stream", "batchSeq": 1, "projectionVersion": 2,
        "operations": [{"kind": "notifications", "notifications": notifications}]})
    return relay, acknowledged, acks


def test_live_batch_uses_websocket_in_order_without_waiting_for_the_send(tmp_path):
    """Live events travel the WebSocket, in order, not the HTTP ingest path.

    The batch is acknowledged on local receipt: the bridge is stop-and-wait, so
    an acknowledgement that waited for the delivery would cap the whole live
    stream at one remote round trip per batch while the bridge flushes thirty
    times a second.
    """

    async def exercise():
        client, host = backend_client(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        frames = []

        async def send(payload):
            frames.append(json.loads(payload))
            entered.set()
            await release.wait()

        client._rpc.set_connection(SimpleNamespace(send=send))
        notices = [
            {"method": "session.meta.upsert", "params": {"sessionId": "session", "externalSessionId": "native"}},
            item_notification(),
            {"method": "session.state.updated", "params": {"sessionId": "session", "status": "running"}},
        ]
        relay, acknowledged, acks = start_relay(host, notices)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            # Acknowledged while the first frame is still being written.
            await asyncio.wait_for(acknowledged.wait(), 1)
            assert acks == [1]
            assert len(frames) == 1
            release.set()
            for _ in range(200):
                if len(frames) == len(notices):
                    break
                await asyncio.sleep(0.01)
            assert [frame["method"] for frame in frames] == [notice["method"] for notice in notices]
            assert all(frame["type"] == "notification" for frame in frames)
            assert all(frame["params"]["runtime"] == "dsh" for frame in frames)
            assert all(frame["params"]["runtimeId"] == "rti_dsh" for frame in frames)
            client._ingest.ingest_notifications.assert_not_awaited()
            client._ingest.enqueue.assert_not_awaited()
            assert client._timeline_notifications.send.await_count == len(notices)
        finally:
            await relay.close()
            client._rpc.clear_connection()

    asyncio.run(exercise())


@pytest.mark.parametrize("transport", ["offline", "oversized"])
def test_live_events_reuse_the_existing_http_fallback_queue(tmp_path, transport):
    async def exercise():
        client, host = backend_client(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        websocket = SimpleNamespace(send=AsyncMock())
        if transport != "offline":
            client._rpc.set_connection(websocket)

        async def enqueue(_method, _params):
            entered.set()
            await release.wait()

        client._ingest.enqueue.side_effect = enqueue
        notice = item_notification("x" * CONNECTOR_WS_MAX_NOTIFICATION_BYTES if transport == "oversized" else "text")
        relay, acknowledged, acks = start_relay(host, [notice,
            {"method": "session.state.updated", "params": {"sessionId": "session", "status": "running"}},
        ])
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(acknowledged.wait(), 1)
            assert acks == [1]
            if transport == "oversized":
                websocket.send.assert_not_awaited()
            method, params = client._ingest.enqueue.call_args.args
            assert method == "timeline.itemUpsert"
            assert params["runtimeId"] == "rti_dsh"
            assert params["item"]["content"] == notice["params"]["item"]["content"]
            release.set()
            for _ in range(200):
                if client._ingest.enqueue.await_count == (2 if transport == "offline" else 1):
                    break
                await asyncio.sleep(0.01)
            relay.client.writer.close.assert_not_called()
            client._ingest.ingest_notifications.assert_not_awaited()
            assert client._ingest.enqueue.await_count == (2 if transport == "offline" else 1)
            assert client._timeline_notifications.send.await_count == 2
        finally:
            await relay.close()
            client._rpc.clear_connection()

    asyncio.run(exercise())


def test_complete_snapshot_and_inventory_still_use_ingest_when_websocket_is_connected(tmp_path):
    async def exercise():
        client, host = backend_client(tmp_path)
        websocket = SimpleNamespace(send=AsyncMock())
        client._rpc.set_connection(websocket)
        client._ingest.submit_notifications = AsyncMock()
        relay = SyncRelay(Mock(), host)
        try:
            await relay.operation({"kind": "notifications", "notifications": [
                {"method": "session.inventory.begin", "params": {"syncId": "inventory"}},
            ]})
            identity = {"sessionId": "session", "snapshotId": "capture"}
            await relay.operation({"kind": "snapshot.begin", **identity, "throughSeq": 1,
                "meta": {"externalSessionId": "native", "cwd": "/repo"}})
            await relay.operation({"kind": "snapshot.items", **identity, "items": [item_notification()["params"]["item"]]})
            assert client._ingest.submit_notifications.await_count == 1
            await relay.operation({"kind": "snapshot.commit", **identity, "throughSeq": 1, "totalItems": 1})
            await relay.operation({"kind": "notifications", "notifications": [
                {"method": "session.inventory.complete", "params": {"syncId": "inventory"}},
            ]})
            batches = [call.args[0] for call in client._ingest.submit_notifications.await_args_list]
            assert [[notice["method"] for notice in batch] for batch in batches] == [
                ["session.inventory.begin"], ["session.meta.upsert", "timeline.sync"], ["session.inventory.complete"],
            ]
            assert batches[1][1]["params"]["complete"] is True
            assert all(notice["params"]["runtimeId"] == "rti_dsh" for batch in batches for notice in batch)
            websocket.send.assert_not_awaited()
            client._ingest.enqueue.assert_not_awaited()
        finally:
            await relay.close()
            client._rpc.clear_connection()

    asyncio.run(exercise())
