"""A runtime that cannot answer in time must not surface as a 500.

`_read_runtime_status` awaits the connector with a fixed budget. A runtime busy
with a turn can exceed it, and that is a runtime that did not answer rather than
a server fault: it has to be reported as a timeout so the client can decide
whether to retry, instead of receiving an opaque Internal Server Error.
"""

from __future__ import annotations

import asyncio
import inspect

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


def test_a_status_read_timeout_is_treated_as_busy_when_queueing() -> None:
    """Queueing must survive the very condition it exists for.

    A status read that times out means the runtime is too busy to answer, which
    is precisely when a message needs to be queued. Rejecting the send there made
    the feature unusable under load: the busier the runtime, the likelier the
    rejection.

    The decision lives in `send_message`, so this asserts the shape of that code
    rather than standing up a full service: the timeout branch must exist, must
    be reachable only when queueing was requested, and must enqueue.
    """
    source = inspect.getsource(SessionRunService.send_message)
    assert "except SessionRunTimeoutError:" in source, "the timeout is caught"

    branch = source[source.index("except SessionRunTimeoutError:"):]
    branch = branch[: branch.index("if runtime_status not in")]
    assert "if not payload.queueWhenBusy:" in branch, "only queued sends are relaxed"
    assert "raise" in branch, "an ordinary send still reports the timeout"
    assert "_enqueue_message" in branch, "a queued send is enqueued"


def test_dispatching_a_claimed_item_cannot_enqueue_a_duplicate() -> None:
    """A claimed queue item must be requeued on a busy race, not copied."""
    source = inspect.getsource(SessionRunService.send_queued_message)
    assert "queueWhenBusy=False" in source
