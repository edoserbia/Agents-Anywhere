"""A runtime that cannot answer in time must not surface as a 500.

`_read_runtime_status` awaits the connector with a fixed budget. A runtime busy
with a turn can exceed it, and that is a runtime that did not answer rather than
a server fault: it has to be reported as a timeout so the client can decide
whether to retry, instead of receiving an opaque Internal Server Error.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_server.core.models import SessionView
from agent_server.services.session_run import (
    SessionRunConflictError,
    SessionRunService,
    SessionRunTimeoutError,
)


class _TimingOutManager:
    """Stands in for a connector that accepts the call and never answers."""

    async def request(self, *_args, **_kwargs):
        raise TimeoutError


class _OfflineManager:
    async def request(self, *_args, **_kwargs):
        from agent_server.infra.connector_rpc import ConnectorOfflineError

        raise ConnectorOfflineError("connector is offline")


def _session() -> SessionView:
    return SessionView(
        id="sess_dsh_1",
        userId="usr_1",
        connectorId="conn_1",
        projectId="proj_1",
        runtime="dsh",
        runtimeId="rti_1",
        status="idle",
        connectorStatus="online",
        takeover=True,
        updatedSeq=1,
    )


def test_a_slow_runtime_reports_a_timeout_not_a_server_error() -> None:
    async def run() -> None:
        service = SessionRunService(store=None, manager=_TimingOutManager(), device_runtimes=None)
        with pytest.raises(SessionRunTimeoutError) as caught:
            await service._read_runtime_status(_session())
        # 504 is what the client sees. Leaving the TimeoutError uncaught made
        # this the generic 500, which reads as a server defect and hides that the
        # runtime simply did not answer in time.
        assert caught.value.status_code == 504

    asyncio.run(run())


def test_an_offline_connector_stays_a_conflict() -> None:
    async def run() -> None:
        # The neighbouring failure modes must keep their own meaning, so a
        # timeout cannot be confused with a connector that is not connected.
        service = SessionRunService(store=None, manager=_OfflineManager(), device_runtimes=None)
        with pytest.raises(SessionRunConflictError) as caught:
            await service._read_runtime_status(_session())
        assert caught.value.status_code == 409

    asyncio.run(run())


def test_a_cancelled_read_is_not_reported_as_a_timeout() -> None:
    """Cancellation is the caller going away, so it must propagate unchanged."""

    class CancellingManager:
        async def request(self, *_args, **_kwargs):
            raise asyncio.CancelledError

    async def run() -> None:
        service = SessionRunService(store=None, manager=CancellingManager(), device_runtimes=None)
        with pytest.raises(asyncio.CancelledError):
            await service._read_runtime_status(_session())

    asyncio.run(run())
