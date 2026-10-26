"""Connector Stop/Start must not abandon native writers in the same process."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_runtime_protocol_supervisor import FakeHost, FakeProvider, FakeRuntime

from connector.core.config import ConnectorConfig
from connector.runtime_protocol import (
    RuntimeInstanceSpec,
    RuntimeSupervisor,
    RuntimeUnavailableError,
)
from connector.server.auth import ConnectorAuthenticationError
from connector.server.client import BackendRpcClient

INSTANCE = RuntimeInstanceSpec(runtime_id="rti_writer", runtime_type="fake", name="Writer")


class WriterRuntime(FakeRuntime):
    def __init__(self, lock_path: Path) -> None:
        super().__init__()
        self.lock_path = lock_path
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        # The same ownership primitive Codex uses. The process stays alive even
        # if its Python owner is discarded, unless stop() actually closes it.
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c",
            "import fcntl,sys,time; f=open(sys.argv[1], 'a'); "
            "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); "
            "print('ready', flush=True); time.sleep(120)",
            str(self.lock_path), stdout=asyncio.subprocess.PIPE,
        )
        assert self.process.stdout is not None
        assert await self.process.stdout.readline() == b"ready\n"
        self.started = True

    async def stop(self) -> None:
        assert self.process is not None
        if self.process.returncode is None:
            self.process.terminate()
        await self.process.wait()
        self.stopped = True


class WriterProvider(FakeProvider):
    def __init__(self, lock_path: Path) -> None:
        super().__init__()
        self.lock_path = lock_path
        self.writers: list[WriterRuntime] = []

    async def create_runtime(self, config: Any, host: Any) -> WriterRuntime:
        runtime = WriterRuntime(self.lock_path)
        self.writers.append(runtime)
        return runtime


@pytest.mark.skipif(os.name == "nt", reason="regression exercises Codex's POSIX writer lock")
@pytest.mark.parametrize("exit_kind", ["cancel", "auth"])
def test_connector_lifetime_releases_writer_before_restart(tmp_path: Path, exit_kind: str) -> None:
    async def run() -> None:
        provider = WriterProvider(tmp_path / "thread.lock")
        # Restart twice without terminating the test's Python process, just as
        # the desktop's connector.stop / connector.start control flow does.
        for _ in range(2):
            client = BackendRpcClient(
                ConnectorConfig("https://server.test", "conn_test", "not-a-real-token"),
                agent_runtime_providers=(provider,), agent_runtime_host=FakeHost(),
            )
            async def offline_status(*args: Any) -> None:
                if provider.writers:
                    raise RuntimeError("backend websocket is not connected")

            async def run_once(client: BackendRpcClient = client) -> None:
                # Startup succeeds; shutdown must still work with no backend.
                client.agent_runtime_supervisor._status_sink = None
                await client.agent_runtime_supervisor.start(INSTANCE, {})
                client.agent_runtime_supervisor._status_sink = offline_status
                if exit_kind == "auth":
                    raise ConnectorAuthenticationError("expired test credential")
                raise asyncio.CancelledError()

            client.run_once = run_once  # type: ignore[method-assign]
            try:
                with pytest.raises((asyncio.CancelledError, ConnectorAuthenticationError)):
                    await client.run_forever()
                assert provider.writers[-1].stopped
                assert provider.writers[-1].process.returncode is not None
                assert client.agent_runtime_supervisor.entry(INSTANCE.runtime_id).runtime is None
            finally:
                # Test hygiene even on the buggy implementation.
                if not provider.writers[-1].stopped:
                    await provider.writers[-1].stop()

        assert len(provider.writers) == 2

    asyncio.run(run())


def test_shutdown_attempts_all_providers_and_rejects_late_start() -> None:
    async def run() -> None:
        bad, good = FakeProvider(), FakeProvider()
        good.runtime_type = good.runtime = good.config_runtime = "other"
        supervisor = RuntimeSupervisor((bad, good), FakeHost())
        first = await supervisor.start(INSTANCE, {})
        other = RuntimeInstanceSpec(runtime_id="rti_other", runtime_type="other", name="Other")
        second = await supervisor.start(other, {})
        bad.fail_stop = True
        await supervisor.shutdown()
        assert len(bad.stopped) == 1
        assert second.native_runtime.stopped
        assert supervisor.entry(INSTANCE.runtime_id).status == "error"
        with pytest.raises(RuntimeUnavailableError, match="shutting down"):
            await supervisor.start(INSTANCE, {})
        # A provider that temporarily failed can still be cleaned up on retry.
        bad.fail_stop = False
        await supervisor.shutdown()
        assert first.native_runtime.stopped
        assert len(good.stopped) == 1

    asyncio.run(run())


def test_public_link_reconnect_keeps_runtime_until_connector_exits() -> None:
    async def run() -> None:
        provider = FakeProvider()
        client = BackendRpcClient(
            ConnectorConfig("https://server.test", "conn_test", "not-a-real-token", reconnect_seconds=0),
            agent_runtime_providers=(provider,), agent_runtime_host=FakeHost(),
        )
        client.agent_runtime_supervisor._status_sink = None
        calls = 0

        async def run_once() -> None:
            nonlocal calls
            calls += 1
            await client.agent_runtime_supervisor.start(INSTANCE, {})
            assert provider.stopped == []
            if calls == 1:
                raise OSError("simulated wake/network reconnect")
            raise asyncio.CancelledError()

        client.run_once = run_once  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await client.run_forever()
        assert calls == 2
        assert len(provider.created) == len(provider.stopped) == 1

    asyncio.run(run())


@pytest.mark.skipif(os.name == "nt", reason="regression exercises Codex's POSIX writer lock")
def test_cancelled_start_closes_half_started_writer_even_when_backend_is_offline(tmp_path: Path) -> None:
    async def run() -> None:
        started = asyncio.Event()

        class PendingWriter(WriterRuntime):
            async def start(self) -> None:
                await super().start()
                started.set()
                await asyncio.Event().wait()

        class PendingProvider(WriterProvider):
            async def create_runtime(self, config: Any, host: Any) -> WriterRuntime:
                writer = PendingWriter(self.lock_path)
                self.writers.append(writer)
                return writer

        provider = PendingProvider(tmp_path / "cancelled.lock")
        supervisor = RuntimeSupervisor((provider,), FakeHost())

        async def offline_status(*args: Any) -> None:
            raise RuntimeError("backend websocket is not connected")

        task = asyncio.create_task(supervisor.start(INSTANCE, {}))
        await asyncio.wait_for(started.wait(), timeout=3)
        supervisor._status_sink = offline_status
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert provider.writers[0].stopped
            assert provider.writers[0].process.returncode is not None
            entry = supervisor.entry(INSTANCE.runtime_id)
            assert entry.runtime is None and entry.resource_claims == ()
            assert entry.status == "stopped"
        finally:
            if not provider.writers[0].stopped:
                await provider.writers[0].stop()

    asyncio.run(run())
