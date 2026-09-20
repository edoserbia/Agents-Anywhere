from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from agent_server.core.models import (
    ConnectorIngestRejectedNotification,
    ConnectorIngestRequest,
    ConnectorIngestResponse,
    ConnectorNotification,
    SessionRuntimeState,
    SessionView,
)
from agent_server.core.runtime_identity import resolve_session_runtime_binding
from agent_server.core.utc import utc_now
from agent_server.infra.timeline_broker import TimelineBroker
from agent_server.services.connector_notifications import (
    ConnectorNotificationService,
    NotificationValidationError,
)
from agent_server.services.connector_presence import (
    ConnectorPresencePort,
    with_effective_session_connector_status,
)
from agent_server.services.dashboard_events import publish_dashboard_changed
from agent_server.services.device_runtimes import (
    DeviceRuntimeNotFoundError,
    DeviceRuntimeService,
)
from agent_server.services.message_queue_dispatch import (
    DISPATCHABLE_STATUSES,
    MessageQueueDispatcher,
)
from agent_server.services.effective_capabilities import (
    project_session_capabilities,
    publish_connector_session_capabilities,
)
from agent_server.services.ingest_effects import IngestEffect
from agent_server.services.repository_ports import ConnectorIngestRepository
from agent_server.services.runtime_ingress import runtime_notification_is_allowed
from agent_server.services.session_runtime_state_cache import (
    SessionRuntimeStateCache,
)

INGEST_REJECTION_MESSAGE_MAX_LENGTH = 500


def ingest_rejection_from_exception(
    index: int,
    notification: ConnectorNotification,
    error: Exception,
) -> ConnectorIngestRejectedNotification:
    message = str(error) or type(error).__name__
    if len(message) > INGEST_REJECTION_MESSAGE_MAX_LENGTH:
        message = f"{message[:INGEST_REJECTION_MESSAGE_MAX_LENGTH].rstrip()}..."
    return ConnectorIngestRejectedNotification(
        index=index,
        method=notification.method,
        code="notification_failed",
        message=message,
        errorType=type(error).__name__,
    )



# One sweep per connector at a time. A newer request replaces the pending one
# because the sweep reads current state when it runs, so superseding work is
# never lost — only redundant passes are.
_CAPABILITY_REPUBLISH_TASKS: dict[str, asyncio.Task[None]] = {}


def _schedule_capability_republish(
    store: Any,
    presence: Any,
    publisher: Any,
    connector_id: str,
) -> None:
    """Run the capability sweep off the ingest response path.

    The sweep is idempotent: it projects the connector's current capability
    facts onto its sessions. Running it slightly later therefore produces the
    same result, while running it inline delayed every ingest by the length of
    the sweep.
    """
    existing = _CAPABILITY_REPUBLISH_TASKS.get(connector_id)
    if existing is not None and not existing.done():
        return
    _CAPABILITY_REPUBLISH_TASKS[connector_id] = asyncio.create_task(
        _run_capability_republish(store, presence, publisher, connector_id),
        name=f"capability-republish-{connector_id}",
    )


async def _run_capability_republish(
    store: Any,
    presence: Any,
    publisher: Any,
    connector_id: str,
) -> None:
    try:
        await publish_connector_session_capabilities(
            store,
            presence,
            publisher,
            connector_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a background sweep must not crash the loop
        logger.exception(
            "capability republish failed connector_id={}", connector_id
        )
    finally:
        _CAPABILITY_REPUBLISH_TASKS.pop(connector_id, None)


class ConnectorIngestService:
    def __init__(
        self,
        store: ConnectorIngestRepository,
        notifications: ConnectorNotificationService,
        timeline_broker: TimelineBroker,
        device_runtimes: DeviceRuntimeService,
        presence: ConnectorPresencePort,
        runtime_state_cache: SessionRuntimeStateCache,
        queue_dispatcher: MessageQueueDispatcher | None = None,
    ) -> None:
        self._store = store
        self._notifications = notifications
        self._timeline_broker = timeline_broker
        self._device_runtimes = device_runtimes
        self._presence = presence
        self._runtime_state_cache = runtime_state_cache
        # Optional: ingest still works without queueing wired in.
        self._queue_dispatcher = queue_dispatcher

    async def ingest(
        self,
        *,
        connector_id: str,
        payload: ConnectorIngestRequest,
    ) -> ConnectorIngestResponse:
        await self._store.record_connector_activity(connector_id)
        effects = []
        accepted = 0
        rejected: list[ConnectorIngestRejectedNotification] = []
        protocol_capabilities_changed = False
        runtime_scoped_capabilities_changed = False
        # A runtime changing status changes what its sessions can do. Capability
        # publication is what tells an already-open client to re-enable its
        # composer, so a runtime that recovers must republish rather than leave
        # the client holding stale "cannot send" facts until it reconnects.
        runtime_status_changed = False
        unconfigured_runtimes = await self._store.get_unconfigured_runtime_ids(connector_id)
        for index, notification in enumerate(payload.notifications):
            if not runtime_notification_is_allowed(notification.method, notification.params, unconfigured_runtimes):
                rejected.append(ConnectorIngestRejectedNotification(
                    index=index, method=notification.method, code="runtime_not_configured",
                    message="runtime must be configured before accepting session notifications", errorType="RuntimeNotConfigured",
                ))
                continue
            try:
                effect = await self.apply_ingest_notification(
                    connector_id,
                    notification,
                )
            except NotificationValidationError:
                raise
            except Exception as exc:  # noqa: BLE001
                rejected.append(
                    ingest_rejection_from_exception(
                        index,
                        notification,
                        exc,
                    )
                )
                logger.exception(
                    "connector ingest notification rejected connector_id={} index={} method={} error_type={}",
                    connector_id,
                    index,
                    notification.method,
                    type(exc).__name__,
                )
                continue
            accepted += 1
            if notification.method == "runtime.statusChanged":
                runtime_status_changed = True
                continue
            effects.append(effect)
            if notification.method == "protocol.capabilitiesUpdated":
                protocol_capabilities_changed = (
                    protocol_capabilities_changed or effect.protocol_changed
                )
            if notification.method == "runtime.capability.updated":
                runtime_scoped_capabilities_changed = (
                    runtime_scoped_capabilities_changed
                    or (
                        effect.protocol_changed
                        and not isinstance(notification.params.get("sessionId"), str)
                    )
                )
        dashboard_changed = await self._publish_effects(effects)
        # Republish whenever a runtime's capability-affecting status changed, so a
        # runtime that recovers does not leave open clients holding stale facts.
        if runtime_status_changed or protocol_capabilities_changed or runtime_scoped_capabilities_changed:
            # Republishing capability facts touches every session of this
            # connector, so its cost grows with the workspace rather than with
            # the notification. Awaiting it here made the ingest response take
            # as long as that sweep — measured at 61-65s for 633 sessions — which
            # overran the DSH bridge's 60s batch-ACK budget, so the bridge
            # declared the stream failed and restarted from the first batch
            # forever. The sweep is idempotent and converges on the same state
            # whenever it runs, so it does not need to hold the response.
            _schedule_capability_republish(
                self._store,
                self._presence,
                self._timeline_broker,
                connector_id,
            )
        if dashboard_changed:
            await publish_dashboard_changed(
                self._store,
                self._timeline_broker,
                connector_id=connector_id,
                reason="connector.ingest",
            )
        return ConnectorIngestResponse(
            accepted=accepted,
            rejected=rejected,
            serverTime=utc_now(),
        )

    async def apply_ingest_notification(
        self,
        connector_id: str,
        notification: ConnectorNotification,
    ) -> IngestEffect:
        """Apply one connector ingest notification.

        Side effects:
        - may write connector/runtime/session/timeline state
        - may update runtime inventory rows
        - does not publish session WebSocket effects; caller owns publication
        """
        if notification.method == "runtime.statusChanged":
            await self._apply_runtime_status(connector_id, notification.params)
            return IngestEffect()
        return await self._notifications.apply(
            connector_id=connector_id,
            method=notification.method,
            params=notification.params,
        )

    async def handle_notification_message(
        self,
        *,
        connector_id: str,
        method: str,
        params: dict,
        connection_id: str | None = None,
    ) -> None:
        if method == "runtime.statusChanged":
            await self._apply_runtime_status(
                connector_id,
                params,
                connection_id=connection_id,
            )
            return

        effect = await self._notifications.apply(
            connector_id=connector_id,
            method=method,
            params=params,
        )
        dashboard_changed = await self._publish_effects([effect])
        if method == "protocol.capabilitiesUpdated" and effect.protocol_changed:
            _schedule_capability_republish(
                self._store,
                self._presence,
                self._timeline_broker,
                connector_id,
            )
        if (
            method == "runtime.capability.updated"
            and effect.protocol_changed
            and not isinstance(params.get("sessionId"), str)
        ):
            _schedule_capability_republish(
                self._store,
                self._presence,
                self._timeline_broker,
                connector_id,
            )
        if dashboard_changed:
            await publish_dashboard_changed(
                self._store,
                self._timeline_broker,
                connector_id=connector_id,
                reason=method,
            )

    async def _publish_effects(self, effects: list[IngestEffect]) -> bool:
        dashboard_changed = any(effect.dashboard_changed for effect in effects)
        by_session: dict[str, dict[str, Any]] = {}
        for effect in effects:
            target_session_ids = []
            if effect.session_id is not None:
                target_session_ids.append(effect.session_id)
            if effect.session_ids:
                target_session_ids.extend(effect.session_ids)
            if not target_session_ids:
                continue
            for session_id in sorted(set(target_session_ids)):
                bucket = by_session.setdefault(
                    session_id,
                    {
                        "items": [],
                        "runtime_state": None,
                        "timeline_reset": False,
                        "session": False,
                        "capability_changed": False,
                        "notices": [],
                        "catalogs": {},
                        "refetch": False,
                        "deferred_timeline_only": True,
                        "accepted_sequence": None,
                    },
                )
                if not effect.timeline_pending:
                    bucket["deferred_timeline_only"] = False
                if effect.session_id == session_id:
                    if effect.accepted_sequence is not None:
                        current_accepted_sequence = bucket["accepted_sequence"]
                        bucket["accepted_sequence"] = max(
                            effect.accepted_sequence,
                            current_accepted_sequence or 0,
                        )
                    if effect.timeline_published:
                        pass
                    elif effect.timeline_reset and not effect.needs_refetch:
                        bucket["items"] = list(effect.items or [])
                        bucket["timeline_reset"] = True
                        bucket["refetch"] = False
                    elif effect.needs_refetch:
                        bucket["items"] = []
                        bucket["timeline_reset"] = False
                        bucket["refetch"] = True
                    elif effect.item is not None or effect.items:
                        if bucket["timeline_reset"] or bucket["refetch"]:
                            bucket["items"] = []
                            bucket["timeline_reset"] = False
                            bucket["refetch"] = True
                        else:
                            if effect.item is not None and not effect.timeline_published:
                                bucket["items"].append(effect.item)
                            if effect.items:
                                bucket["items"].extend(effect.items)
                    if effect.runtime_state is not None:
                        bucket["runtime_state"] = effect.runtime_state
                    bucket["session"] = bucket["session"] or effect.session_changed
                    bucket["capability_changed"] = (
                        bucket["capability_changed"] or effect.protocol_changed
                    )
                    if effect.notices:
                        bucket["notices"].extend(effect.notices)
                if effect.catalogs:
                    bucket["catalogs"].update(effect.catalogs)

        async def publish_bucket(
            session_id: str,
            bucket: dict[str, Any],
        ) -> bool:
            status_changed = False
            try:
                next_seq = await self._store.get_session_seq(session_id)
            except KeyError:
                return False
            if bucket["accepted_sequence"] is not None:
                next_seq = max(next_seq, bucket["accepted_sequence"])
            envelope_sequence = (
                max(next_seq, 1)
                if bucket["notices"] or bucket["catalogs"]
                else next_seq
            )
            envelope: dict[str, Any] = {
                "sessionId": session_id,
                "nextSeq": envelope_sequence,
            }
            if bucket["refetch"]:
                envelope["refetch"] = True
            elif bucket["timeline_reset"]:
                envelope["timelineReset"] = True
                envelope["items"] = bucket["items"]
            elif bucket["items"]:
                envelope["items"] = bucket["items"]
            runtime_state: SessionRuntimeState | None = None
            if bucket["runtime_state"]:
                bound_session = await self._store.get_session(session_id)
                runtime_state = runtime_state_from_ingest_effect(
                    bound_session,
                    next_seq,
                    bucket["runtime_state"],
                )
                previous_runtime_state = await self._runtime_state_cache.get(session_id)
                if runtime_states_semantically_equal(
                    previous_runtime_state,
                    runtime_state,
                ):
                    runtime_state = None
                else:
                    if (
                        previous_runtime_state is None
                        or previous_runtime_state.status != runtime_state.status
                    ):
                        logger.info(
                            "session_status_trace layer=server session_id={} runtime={} "
                            "runtime_id={} previous_status={} next_status={} source={} "
                            "previous_updated_seq={} ingest_next_seq={}",
                            session_id,
                            runtime_state.runtime,
                            runtime_state.runtimeId,
                            previous_runtime_state.status
                            if previous_runtime_state is not None
                            else None,
                            runtime_state.status,
                            runtime_state.metadata.get("source"),
                            previous_runtime_state.updatedSeq
                            if previous_runtime_state is not None
                            else None,
                            next_seq,
                        )
                    persisted_session = await self._store.set_session_status(
                        session_id,
                        runtime_state.status,
                        mark_read_on_change=True,
                    )
                    status_changed = persisted_session.status != bound_session.status
                    next_seq = max(
                        await self._store.get_session_seq(session_id),
                        persisted_session.updatedSeq,
                    )
                    envelope["nextSeq"] = max(envelope_sequence, next_seq)
                    runtime_state = runtime_state.model_copy(
                        update={"updatedSeq": envelope["nextSeq"]}
                    )
                    await self._runtime_state_cache.put(runtime_state)
                    envelope["runtimeState"] = runtime_state.model_dump(mode="json")
                    bucket["session"] = True
            if bucket["session"]:
                try:
                    session = await self._store.get_session(session_id)
                    if runtime_state is not None:
                        session = session.model_copy(
                            update={"status": runtime_state.status}
                        )
                    effective_capabilities = None
                    if bucket["capability_changed"]:
                        (
                            session,
                            _runtime_capabilities,
                            effective_capabilities,
                        ) = await project_session_capabilities(
                            self._store,
                            self._presence,
                            session,
                        )
                    else:
                        session = await with_effective_session_connector_status(
                            self._presence,
                            session,
                        )
                    envelope["session"] = session.model_dump(mode="json")
                    if effective_capabilities is not None:
                        envelope["capabilitySet"] = effective_capabilities.model_dump(
                            mode="json"
                        )
                except KeyError:
                    pass
            if bucket["notices"]:
                envelope["notices"] = [
                    notice.model_dump(mode="json")
                    for notice in bucket["notices"]
                ]
            if bucket["catalogs"]:
                envelope["catalogs"] = bucket["catalogs"]
            if not any(
                key in envelope
                for key in (
                    "refetch",
                    "timelineReset",
                    "items",
                    "runtimeState",
                    "session",
                    "capabilitySet",
                    "notices",
                    "catalogs",
                )
            ):
                return status_changed
            await self._timeline_broker.publish(session_id, envelope)
            return status_changed

        settled: list[str] = []
        for session_id, bucket in by_session.items():
            if not (
                bucket["items"]
                or bucket["runtime_state"] is not None
                or bucket["timeline_reset"]
                or bucket["session"]
                or bucket["capability_changed"]
                or bucket["notices"]
                or bucket["catalogs"]
                or bucket["refetch"]
            ):
                continue
            if bucket["deferred_timeline_only"]:
                dashboard_changed = (
                    await publish_bucket(session_id, bucket)
                    or dashboard_changed
                )
                continue
            async with self._store.session_revision_fence(session_id):
                if (
                    await publish_bucket(session_id, bucket)
                ):
                    dashboard_changed = True
            # A turn just ended, so this session's queue may have work waiting.
            # The bucket holds the serialized effect payload, so status is read
            # from the mapping rather than an attribute.
            runtime_state = bucket["runtime_state"]
            status = runtime_state.get("status") if isinstance(runtime_state, dict) else None
            if status in DISPATCHABLE_STATUSES:
                settled.append(session_id)
        await self._dispatch_queues(settled)
        return dashboard_changed

    async def _dispatch_queues(self, settled: list[str]) -> None:
        """Hand the next queued message to each session whose turn just ended.

        Dispatch runs after the timeline and status changes for this batch have
        been published, so clients already see the finished turn before the next
        one starts.

        Failures are logged rather than raised: a queue problem must not fail the
        ingest that observed the status change.
        """
        dispatcher = self._queue_dispatcher
        if dispatcher is None or not settled:
            return
        for session_id in settled:
            try:
                await dispatcher.dispatch_next(session_id)
            except Exception as error:  # noqa: BLE001 - never break ingest
                logger.warning(
                    "queued message dispatch error session_id={} error={}",
                    session_id,
                    error,
                )

    async def _apply_runtime_status(
        self,
        connector_id: str,
        params: dict,
        *,
        connection_id: str | None = None,
    ) -> None:
        runtime_id = params.get("runtimeId")
        status = params.get("status")
        if not isinstance(runtime_id, str) or not runtime_id:
            raise ValueError("runtime.statusChanged requires runtimeId")
        if not isinstance(status, str) or not status:
            raise ValueError("runtime.statusChanged requires status")
        error = params.get("error") if isinstance(params.get("error"), dict) else None
        try:
            await self._device_runtimes.apply_status(
                connector_id,
                runtime_id,
                status,
                error=error,
                expected_connection_id=connection_id,
            )
        except DeviceRuntimeNotFoundError:
            # Runtime lifecycle notifications may arrive while the connector is
            # still producing the inventory snapshot for a fresh pairing or
            # reconnect. Inventory is the source that creates runtime rows; a
            # pre-inventory status must not tear down the connector WebSocket.
            return


def runtime_state_from_ingest_effect(
    session: SessionView,
    next_seq: int,
    raw_state: dict[str, Any],
) -> SessionRuntimeState:
    now = utc_now()
    session_id, runtime, runtime_id = resolve_session_runtime_binding(
        raw_state,
        session_id=session.id,
        runtime_type=session.runtime,
        runtime_id=session.runtimeId or session.runtime,
    )
    return SessionRuntimeState.model_validate(
        {
            "sessionId": session_id,
            "runtime": runtime,
            "runtimeId": runtime_id,
            "externalSessionId": raw_state.get("externalSessionId"),
            "status": raw_state.get("status") or "idle",
            "selections": raw_state.get("selections")
            if isinstance(raw_state.get("selections"), dict)
            else {},
            "statusReason": raw_state.get("statusReason"),
            "error": raw_state.get("error")
            if isinstance(raw_state.get("error"), dict)
            else None,
            "metadata": raw_state.get("metadata")
            if isinstance(raw_state.get("metadata"), dict)
            else {},
            "updatedSeq": next_seq,
            "createdAt": now,
            "updatedAt": now,
        }
    )


def runtime_states_semantically_equal(
    left: SessionRuntimeState | None,
    right: SessionRuntimeState,
) -> bool:
    if left is None:
        return False
    return runtime_state_fingerprint(left) == runtime_state_fingerprint(right)


def runtime_state_fingerprint(value: SessionRuntimeState) -> dict[str, Any]:
    return {
        "sessionId": value.sessionId,
        "runtime": value.runtime,
        "runtimeId": value.runtimeId,
        "externalSessionId": value.externalSessionId,
        "status": value.status,
        "selections": value.selections,
        "statusReason": value.statusReason,
        "error": value.error,
    }
