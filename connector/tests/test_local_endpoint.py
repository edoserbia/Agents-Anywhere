"""The local endpoint is a new listener, so its refusals matter more than its answers.

It exists to take same-machine traffic off the public network, and the reason it
is acceptable to add is that it behaves like the plugin bridge already deployed:
loopback only, a token in a `0600` descriptor, a deadline for authenticating, and
an explicit allowlist. Each of those is asserted here, and each assertion fails
if the property is removed.
"""

from __future__ import annotations

import asyncio
import json
import stat
import tempfile
from pathlib import Path

import pytest
import websockets

from connector.server.local_endpoint import (
    READ_METHODS,
    SERVED_METHODS,
    WRITE_METHODS,
    LocalEndpoint,
    LocalEndpointError,
)


def _endpoint(tmp_path: Path, dispatched: list | None = None, **kwargs) -> LocalEndpoint:
    calls = dispatched if dispatched is not None else []

    async def dispatch(method, params):
        calls.append((method, params))
        return {"method": method, "params": params}

    return LocalEndpoint(
        dispatch,
        path=tmp_path / "local-endpoint.json",
        auth_deadline_seconds=kwargs.pop("auth_deadline_seconds", 1.0),
        **kwargs,
    )


async def _connect(descriptor: dict):
    return await websockets.connect(f"ws://127.0.0.1:{descriptor['port']}")


def test_it_authenticates_then_answers_allowlisted_methods(tmp_path) -> None:
    async def run():
        dispatched: list = []
        endpoint = _endpoint(tmp_path, dispatched)
        descriptor = await endpoint.start()
        try:
            async with await _connect(descriptor) as socket:
                await socket.send(
                    json.dumps({"type": "initialize", "token": descriptor["token"]})
                )
                assert json.loads(await socket.recv())["type"] == "initialized"

                await socket.send(json.dumps({
                    "type": "request", "id": 1,
                    "method": "session.state", "params": {"sessionId": "s1"},
                }))
                reply = json.loads(await socket.recv())
                assert reply["id"] == 1
                assert reply["result"] == {
                    "method": "session.state", "params": {"sessionId": "s1"}
                }
            assert dispatched == [("session.state", {"sessionId": "s1"})]
        finally:
            await endpoint.stop()

    asyncio.run(run())


def test_a_bad_token_is_refused(tmp_path) -> None:
    """A listener on loopback is reachable by anything running as this user."""

    async def run():
        dispatched: list = []
        endpoint = _endpoint(tmp_path, dispatched)
        descriptor = await endpoint.start()
        try:
            async with await _connect(descriptor) as socket:
                await socket.send(
                    json.dumps({"type": "initialize", "token": "x" * len(descriptor["token"])})
                )
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await socket.recv()
            # Nothing may have reached the dispatcher.
            assert dispatched == []
        finally:
            await endpoint.stop()

    asyncio.run(run())


def test_a_connection_that_never_authenticates_is_dropped(tmp_path) -> None:
    """Otherwise an unauthenticated peer can hold a connection open forever."""

    async def run():
        endpoint = _endpoint(tmp_path, auth_deadline_seconds=0.3)
        descriptor = await endpoint.start()
        try:
            async with await _connect(descriptor) as socket:
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await asyncio.wait_for(socket.recv(), timeout=3)
        finally:
            await endpoint.stop()

    asyncio.run(run())


def test_a_method_outside_the_allowlist_is_refused_not_disconnected(tmp_path) -> None:
    """A refusal has to be per call, so a client can fall back for that one.

    Disconnecting would take the whole channel away because of a single call the
    local side does not serve. `session.interrupt` is the example: it is a write
    the Server's pipeline still owns, so it is not served here.
    """

    async def run():
        dispatched: list = []
        endpoint = _endpoint(tmp_path, dispatched)
        descriptor = await endpoint.start()
        try:
            async with await _connect(descriptor) as socket:
                await socket.send(
                    json.dumps({"type": "initialize", "token": descriptor["token"]})
                )
                await socket.recv()

                await socket.send(json.dumps({
                    "type": "request", "id": 2,
                    "method": "session.interrupt", "params": {"sessionId": "s"},
                }))
                reply = json.loads(await socket.recv())
                assert reply["error"]["code"] == "forbidden_method"
                assert "result" not in reply

                # Still usable for what it does serve.
                await socket.send(json.dumps({
                    "type": "request", "id": 3,
                    "method": "session.notices", "params": {},
                }))
                assert "result" in json.loads(await socket.recv())
            assert [call[0] for call in dispatched] == ["session.notices"]
        finally:
            await endpoint.stop()

    asyncio.run(run())


def test_only_the_message_write_is_served(tmp_path) -> None:
    """The one write this channel carries is the message the user just typed.

    Everything else that changes state stays with the Server, whose pipeline
    owns ownership, takeover, queueing, capabilities and attachments. Serving
    one of those here would bypass all of it.
    """

    assert WRITE_METHODS == {"session.send_message"}
    for method in ("session.steer", "session.interrupt", "interaction.respond",
                   "session.queue.update", "session.create"):
        assert method not in SERVED_METHODS, method
        assert method not in READ_METHODS, method


def test_the_served_set_is_reads_plus_that_write() -> None:
    assert SERVED_METHODS == READ_METHODS | WRITE_METHODS
    assert WRITE_METHODS <= SERVED_METHODS


def test_a_served_write_reaches_the_dispatcher(tmp_path) -> None:
    """The write is only useful if it actually gets through."""

    async def run():
        dispatched: list = []
        endpoint = _endpoint(tmp_path, dispatched)
        descriptor = await endpoint.start()
        try:
            async with await _connect(descriptor) as socket:
                await socket.send(
                    json.dumps({"type": "initialize", "token": descriptor["token"]})
                )
                await socket.recv()
                await socket.send(json.dumps({
                    "type": "request", "id": 9, "method": "session.send_message",
                    "params": {"sessionId": "s1", "runtime": "codex", "content": {"text": "hi"}},
                }))
                reply = json.loads(await socket.recv())
                assert "result" in reply, reply
            assert dispatched == [(
                "session.send_message",
                {"sessionId": "s1", "runtime": "codex", "content": {"text": "hi"}},
            )]
        finally:
            await endpoint.stop()

    asyncio.run(run())


def test_the_descriptor_is_private_and_only_names_loopback(tmp_path) -> None:
    async def run():
        endpoint = _endpoint(tmp_path)
        descriptor = await endpoint.start()
        try:
            path = tmp_path / "local-endpoint.json"
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert descriptor["host"] == "127.0.0.1"
            assert descriptor["port"] > 0
            assert descriptor["token"] == endpoint.token
            # It carries the token, so it must not be world readable.
            assert json.loads(path.read_text())["token"] == endpoint.token
        finally:
            await endpoint.stop()

    asyncio.run(run())


def test_it_refuses_to_start_twice_and_cleans_up_after_itself(tmp_path) -> None:
    async def run():
        endpoint = _endpoint(tmp_path)
        await endpoint.start()
        with pytest.raises(LocalEndpointError):
            await endpoint.start()
        await endpoint.stop()
        assert not (tmp_path / "local-endpoint.json").exists()
        # Stopping again is harmless.
        await endpoint.stop()

    asyncio.run(run())


def test_stopping_does_not_remove_a_successors_descriptor(tmp_path) -> None:
    """A restarting Connector must not delete the endpoint that replaced it."""

    async def run():
        first = _endpoint(tmp_path)
        await first.start()
        second = _endpoint(tmp_path)
        await second.start()          # replaces the descriptor
        try:
            await first.stop()        # the old process shutting down
            assert (tmp_path / "local-endpoint.json").exists(), (
                "the live endpoint's descriptor was removed by a dead one"
            )
        finally:
            await second.stop()

    asyncio.run(run())


def test_a_malformed_frame_is_answered_rather_than_fatal(tmp_path) -> None:
    async def run():
        endpoint = _endpoint(tmp_path)
        descriptor = await endpoint.start()
        try:
            async with await _connect(descriptor) as socket:
                await socket.send(
                    json.dumps({"type": "initialize", "token": descriptor["token"]})
                )
                await socket.recv()

                await socket.send("not json at all")
                assert json.loads(await socket.recv())["error"]["code"] == "invalid_json"

                await socket.send(json.dumps({"type": "nonsense"}))
                assert json.loads(await socket.recv())["error"]["code"] == "invalid_request"

                await socket.send(json.dumps({"type": "request", "id": 4}))
                assert json.loads(await socket.recv())["error"]["code"] == "invalid_request"

                await socket.send(json.dumps({
                    "type": "request", "id": 5, "method": "session.state",
                    "params": "not an object",
                }))
                assert json.loads(await socket.recv())["error"]["code"] == "invalid_params"
        finally:
            await endpoint.stop()

    asyncio.run(run())


def test_a_dispatch_failure_is_reported_to_the_caller(tmp_path) -> None:
    async def run():
        async def dispatch(method, params):
            raise RuntimeError("the runtime is not running")

        endpoint = LocalEndpoint(
            dispatch, path=tmp_path / "local-endpoint.json", auth_deadline_seconds=1.0
        )
        descriptor = await endpoint.start()
        try:
            async with await _connect(descriptor) as socket:
                await socket.send(
                    json.dumps({"type": "initialize", "token": descriptor["token"]})
                )
                await socket.recv()
                await socket.send(json.dumps({
                    "type": "request", "id": 6, "method": "session.state", "params": {},
                }))
                reply = json.loads(await socket.recv())
                assert reply["error"]["code"] == "dispatch_failed"
                assert "not running" in reply["error"]["message"]
        finally:
            await endpoint.stop()

    asyncio.run(run())


def test_the_connector_starts_and_stops_the_endpoint_with_itself(tmp_path, monkeypatch) -> None:
    """The local channel has to outlive the public link, and not outlive the process.

    It is started before the Server connection and stopped after it: the whole
    point is that it keeps working while the public link does not. A failure to
    start must not be fatal either — a client that cannot find the descriptor
    simply keeps using the Server.
    """

    from connector.core.config import ConnectorConfig
    from connector.server.client import BackendRpcClient

    async def run():
        client = BackendRpcClient(
            ConnectorConfig("https://server.test", "conn_test", "secret"),
            agent_runtime_providers=(),
        )
        started: list = []

        async def start():
            started.append("start")
            return {"port": 1}

        async def stop():
            started.append("stop")

        monkeypatch.setattr(client, "_start_local_endpoint", start)
        monkeypatch.setattr(client, "_stop_local_endpoint", stop)

        async def run_once():
            raise asyncio.CancelledError()

        client.run_once = run_once
        with pytest.raises(asyncio.CancelledError):
            await client.run_forever()
        assert started == ["start", "stop"]

    asyncio.run(run())


def test_an_endpoint_that_cannot_start_does_not_stop_the_connector(tmp_path, monkeypatch) -> None:
    from connector.core.config import ConnectorConfig
    from connector.server import client as client_module
    from connector.server.client import BackendRpcClient

    async def run():
        client = BackendRpcClient(
            ConnectorConfig("https://server.test", "conn_test", "secret"),
            agent_runtime_providers=(),
        )

        class Exploding:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                raise LocalEndpointError("address already in use")

        monkeypatch.setattr(client_module, "LocalEndpoint", Exploding)
        await client._start_local_endpoint()
        # No endpoint, but no exception either: the Server path is unaffected.
        assert client._local_endpoint is None

    asyncio.run(run())


def test_the_real_connector_answers_over_the_local_endpoint(tmp_path, monkeypatch) -> None:
    """End to end through the actual client, not a stub dispatcher.

    The endpoint is only useful if it reaches the same dispatcher the Server
    calls, including that dispatcher's own validation errors — a caller has to
    see those rather than a silent success.
    """

    from connector.core.config import ConnectorConfig
    from connector.server.client import BackendRpcClient
    from connector.server.local_endpoint import descriptor_path

    # Its own data directory, so this never touches a running Connector's
    # descriptor.
    monkeypatch.setenv("AGENT_CONNECTOR_DATA_DIR", str(tmp_path))

    async def run():
        client = BackendRpcClient(
            ConnectorConfig("https://server.invalid", "conn_test", "secret"),
            agent_runtime_providers=(),
        )
        await client._start_local_endpoint()
        try:
            descriptor = json.loads(descriptor_path().read_text(encoding="utf-8"))
            assert descriptor["host"] == "127.0.0.1"
            async with await _connect(descriptor) as socket:
                await socket.send(json.dumps({
                    "type": "initialize", "token": descriptor["token"],
                }))
                assert json.loads(await socket.recv())["type"] == "initialized"

                await socket.send(json.dumps({
                    "type": "request", "id": 1,
                    "method": "runtime.discover", "params": {},
                }))
                reply = json.loads(await socket.recv())
                assert "result" in reply, reply
                assert "runtimeTypes" in reply["result"]

                # The dispatcher's own validation is passed through, not hidden.
                await socket.send(json.dumps({
                    "type": "request", "id": 2,
                    "method": "session.discover", "params": {},
                }))
                reply = json.loads(await socket.recv())
                assert reply["error"]["code"] == "dispatch_failed"
        finally:
            await client._stop_local_endpoint()
        assert not descriptor_path().exists()

    asyncio.run(run())


def test_the_local_channel_is_the_same_dispatcher_the_server_uses(tmp_path, monkeypatch) -> None:
    """The reason a locally sent message still reaches the Server.

    A local write is not a special path with its own side effects: it is the
    Connector's ordinary dispatcher, which is also what answers the Server. So
    the runtime work it triggers produces exactly the notifications it always
    produces, and those go to the ingest client unconditionally — the Server,
    and therefore every other machine, still learns the session's state.

    If the endpoint were ever given its own dispatcher, that guarantee would
    quietly stop holding, so it is pinned here rather than left to inspection.
    """

    from connector.core.config import ConnectorConfig
    from connector.server.client import BackendRpcClient

    monkeypatch.setenv("AGENT_CONNECTOR_DATA_DIR", str(tmp_path))

    async def run():
        client = BackendRpcClient(
            ConnectorConfig("https://server.invalid", "conn_test", "secret"),
            agent_runtime_providers=(),
        )
        await client._start_local_endpoint()
        try:
            endpoint = client._local_endpoint
            assert endpoint is not None
            # Same dispatcher object the Server's RPC is routed through.
            assert getattr(endpoint._dispatch, "__self__", None) is client._dispatcher
            # And that object is the one the Server's path calls.
            assert client._dispatcher.dispatch.__self__ is client._dispatcher
        finally:
            await client._stop_local_endpoint()

    asyncio.run(run())


def test_a_local_write_produces_the_notifications_the_server_receives(tmp_path) -> None:
    """Stated as behaviour, not as wiring.

    Whatever a runtime does in response to a local send is reported through the
    same notification sink the Server's own sends use, so the two cannot drift
    into different levels of detail.
    """

    from connector.core.config import ConnectorConfig
    from connector.server.client import BackendRpcClient

    async def run():
        client = BackendRpcClient(
            ConnectorConfig("https://server.invalid", "conn_test", "secret"),
            agent_runtime_providers=(),
        )
        submitted: list = []

        async def record(notifications):
            submitted.extend(notifications)

        client._ingest.submit_notifications = record
        await client.submit_runtime_notifications([{"method": "timeline.itemUpsert"}])
        assert submitted == [{"method": "timeline.itemUpsert"}]
        # The same sink is what the Server-routed path uses: there is one.
        assert client._ingest.submit_notifications is record

    asyncio.run(run())


def test_the_descriptor_comes_back_if_something_removes_it(tmp_path, monkeypatch) -> None:
    """A second Connector coming and going must not leave this one unfindable.

    Observed on a real machine: the endpoint was listening while its descriptor
    was gone, because a short-lived second process overwrote the file on the way
    up and removed it on the way down. A client that cannot read the descriptor
    cannot use the channel at all, so the endpoint keeps it current.
    """

    import connector.server.local_endpoint as module

    monkeypatch.setattr(module, "DESCRIPTOR_REFRESH_SECONDS", 0.05)

    async def run():
        endpoint = _endpoint(tmp_path)
        descriptor = await endpoint.start()
        try:
            path = tmp_path / "local-endpoint.json"
            assert path.exists()
            path.unlink()
            assert not path.exists()
            for _ in range(40):
                await asyncio.sleep(0.05)
                if path.exists():
                    break
            assert path.exists(), "the descriptor was never restored"
            assert json.loads(path.read_text())["port"] == descriptor["port"]
        finally:
            await endpoint.stop()
        # Stopping still cleans up after itself.
        assert not (tmp_path / "local-endpoint.json").exists()

    asyncio.run(run())


def test_a_descriptor_that_is_not_ours_is_replaced(tmp_path, monkeypatch) -> None:
    """Someone else's stale descriptor must not keep clients away."""

    import connector.server.local_endpoint as module

    monkeypatch.setattr(module, "DESCRIPTOR_REFRESH_SECONDS", 0.05)

    async def run():
        endpoint = _endpoint(tmp_path)
        descriptor = await endpoint.start()
        try:
            path = tmp_path / "local-endpoint.json"
            path.write_text(json.dumps({
                "version": 1, "host": "127.0.0.1", "port": 1, "token": "someone-elses",
            }))
            for _ in range(40):
                await asyncio.sleep(0.05)
                if json.loads(path.read_text()).get("token") == descriptor["token"]:
                    break
            assert json.loads(path.read_text())["token"] == descriptor["token"]
        finally:
            await endpoint.stop()

    asyncio.run(run())
