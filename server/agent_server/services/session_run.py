from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent_server.core.api_namespace import api_v2_path
from agent_server.core.capabilities import (
    CATALOG_MODEL,
    CATALOG_PERMISSION,
    RUNTIME_ATTACHMENT,
    SESSION_INTERRUPT,
    SESSION_SEND_MESSAGE,
    SESSION_STEER,
    capability_unavailable_detail,
)
from agent_server.core.models import (
    AttachmentRef,
    InlineAttachmentRef,
    MessageCreateRequest,
    QueueItemUpdateRequest,
    RpcResponsePayload,
    SessionCreateAndStartRequest,
    SessionCreateRequest,
    SessionRuntimeState,
    SessionSelectionPatchRequest,
    SessionStatus,
    SessionSteerRequest,
    SessionView,
)
from agent_server.core.protocol import ProtocolCapabilitySet
from agent_server.core.runtime_identity import (
    SessionRuntimeBindingError,
    resolve_session_runtime_binding,
)
from agent_server.core.utc import utc_now
from agent_server.infra.connector_rpc import (
    ConnectorOfflineError,
    ConnectorRpcError,
    ConnectorRpcManager,
)
from agent_server.infra.repositories.projects import _clean_workspace_path
from agent_server.services.device_runtimes import (
    DeviceRuntimeError,
    DeviceRuntimeService,
)
from agent_server.services.message_queue import (
    MessageQueueError,
    MessageQueueService,
)
from agent_server.services.message_queue_dispatch import QueueDispatchBusy
from agent_server.services.effective_capabilities import (
    derive_session_effective_capabilities,
    read_session_capability_facts,
)
from agent_server.services.repository_ports import SessionRunRepository


class SessionRunError(RuntimeError):
    status_code = 500

    def __init__(self, detail: Any) -> None:
        super().__init__(detail)
        self.detail = detail


class SessionRunNotFoundError(SessionRunError):
    status_code = 404


class SessionRunConflictError(SessionRunError):
    status_code = 409


class SessionRunUpstreamError(SessionRunError):
    status_code = 502


class SessionRunTimeoutError(SessionRunError):
    status_code = 504


class SessionRunInvalidConfigError(SessionRunError):
    status_code = 422


SESSION_SOURCE_ERROR_CODES = {
    "archived": "session_archived",
    "unavailable": "session_unavailable",
    "deleted": "session_deleted",
    "missing": "session_missing",
}


@dataclass(frozen=True, slots=True)
class PersistedInlineAttachment:
    file_id: str
    name: str
    media_type: str
    size: int
    sha256: str


class SessionRunService:
    def __init__(
        self,
        store: SessionRunRepository,
        manager: ConnectorRpcManager,
        device_runtimes: DeviceRuntimeService,
        message_queue: MessageQueueService | None = None,
        queue_dispatch: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._manager = manager
        self._device_runtimes = device_runtimes
        # Optional so tests and callers that never queue keep working; the API
        # always passes one.
        self._message_queue = message_queue or MessageQueueService(store)
        self._queue_dispatch = queue_dispatch

    def set_queue_dispatcher(
        self, dispatch: Callable[[str], Awaitable[bool]] | None
    ) -> None:
        """Attach the dispatcher after construction to avoid a dependency cycle."""
        self._queue_dispatch = dispatch

    async def create_session(
        self,
        payload: SessionCreateRequest,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        try:
            connector = await self._store.get_connector(payload.connectorId)
            if connector.userId != user_id:
                raise KeyError(payload.connectorId)
        except KeyError:
            raise SessionRunNotFoundError("connector not found") from None
        project_id, project_cwd = await self._validate_project_binding(
            payload.projectId,
            connector_id=payload.connectorId,
            cwd=payload.cwd,
            user_id=user_id,
        )
        payload.cwd = project_cwd
        runtime_id = _request_runtime_id(payload)
        await self._require_runtime_instance(
            payload.connectorId,
            payload.runtime,
            runtime_id,
            user_id=user_id,
            require_running=False,
        )

        connector_result = None
        if payload.externalSessionId is not None:
            session = await self._store.create_session(
                connector_id=payload.connectorId,
                project_id=project_id,
                user_id=user_id,
                runtime=payload.runtime,
                runtime_id=runtime_id,
                external_session_id=payload.externalSessionId,
                title=payload.title,
                cwd=payload.cwd,
            )
            return {"session": session, "connectorResult": connector_result}

        raise SessionRunInvalidConfigError(
            "new sessions must use /sessions/create-and-start"
        )

    async def create_and_start_session(
        self,
        payload: SessionCreateAndStartRequest,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        try:
            connector = await self._store.get_connector(payload.connectorId)
            if connector.userId != user_id:
                raise KeyError(payload.connectorId)
        except KeyError:
            raise SessionRunNotFoundError("connector not found") from None
        project_id, project_cwd = await self._validate_project_binding(
            payload.projectId,
            connector_id=payload.connectorId,
            cwd=payload.cwd,
            user_id=user_id,
        )
        payload.cwd = project_cwd
        runtime_id = _request_runtime_id(payload)
        await self._require_runtime_instance(
            payload.connectorId,
            payload.runtime,
            runtime_id,
            user_id=user_id,
            require_running=True,
        )
        if not await self._manager.is_online(payload.connectorId):
            raise SessionRunConflictError("connector is offline")
        if payload.attachments:
            await self._require_runtime_capability(
                payload.connectorId,
                payload.runtime,
                runtime_id,
                RUNTIME_ATTACHMENT,
                user_id=user_id,
                attachment_media_types=[attachment.mediaType for attachment in payload.attachments],
            )

        selections = _selections_from_mapping(payload.selections)
        session = await self._store.create_session(
            connector_id=payload.connectorId,
            project_id=project_id,
            user_id=user_id,
            runtime=payload.runtime,
            runtime_id=runtime_id,
            external_session_id=None,
            title=payload.title,
            cwd=payload.cwd,
            selections=selections,
            takeover=True,
        )
        params: dict[str, Any] = {
            "runtime": payload.runtime,
            "runtimeId": runtime_id,
            "sessionId": session.id,
            "content": payload.content,
        }
        if payload.title is not None:
            params["title"] = payload.title
        if payload.cwd is not None:
            params["cwd"] = payload.cwd
        if selections:
            params["selections"] = selections
        if payload.runtimeOptions:
            params["runtimeOptions"] = dict(payload.runtimeOptions)
        if payload.clientMessageId:
            params["clientMessageId"] = payload.clientMessageId
        persisted_attachment_refs: list[dict[str, Any]] = []
        if payload.attachments:
            persisted_attachments = await self._persist_inline_attachments(
                session_id=session.id,
                user_id=user_id,
                attachments=payload.attachments,
            )
            persisted_attachment_refs = [
                _timeline_payload_from_persisted_inline_attachment(attachment)
                for attachment in persisted_attachments
            ]
            params["attachments"] = [
                _connector_attachment_reference_payload(attachment)
                for attachment in persisted_attachments
            ]
            params["timelineAttachments"] = persisted_attachment_refs

        await self._store.start_active_run(
            session_id=session.id,
            runtime=payload.runtime,
            runtime_id=runtime_id,
            params=params,
        )
        try:
            connector_result = await self._manager.request(
                payload.connectorId,
                "session.create",
                params,
                timeout=60,
            )
        except ConnectorOfflineError as exc:
            await self._store.clear_active_run(session.id)
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            await self._store.clear_active_run(session.id)
            raise SessionRunUpstreamError(exc.message or exc.code) from exc

        if not isinstance(connector_result, dict):
            await self._mark_create_and_start_failed(
                session.id,
                runtime=payload.runtime,
                code="invalid_connector_result",
                message="connector did not return a session result",
            )
            raise SessionRunUpstreamError("connector did not return a session result")
        try:
            resolve_session_runtime_binding(
                connector_result,
                session_id=session.id,
                runtime_type=payload.runtime,
                runtime_id=runtime_id,
            )
        except SessionRuntimeBindingError as exc:
            await self._mark_create_and_start_failed(
                session.id,
                runtime=payload.runtime,
                code="invalid_runtime_binding",
                message=str(exc),
            )
            raise SessionRunUpstreamError(str(exc)) from exc
        external_session_id = connector_result.get("externalSessionId")
        if external_session_id is not None and (
            not isinstance(external_session_id, str) or not external_session_id
        ):
            await self._mark_create_and_start_failed(
                session.id,
                runtime=payload.runtime,
                code="missing_external_session_id",
                message="connector returned an invalid external session id",
            )
            raise SessionRunUpstreamError("connector returned an invalid external session id")
        await self._store.start_active_run(
            session_id=session.id,
            runtime=payload.runtime,
            runtime_id=runtime_id,
            external_session_id=external_session_id if isinstance(external_session_id, str) else None,
            params=params,
        )
        session = await self._store.upsert_connector_session(
            connector_id=payload.connectorId,
            session_id=session.id,
            runtime=payload.runtime,
            runtime_id=runtime_id,
            external_session_id=external_session_id if isinstance(external_session_id, str) else None,
            title=payload.title,
            cwd=payload.cwd,
            last_synced_at=utc_now(),
            origin="platform",
        )
        return {
            "session": session,
            "connectorResult": connector_result,
            "attachments": persisted_attachment_refs,
        }

    async def _validate_project_binding(
        self,
        project_id: str | None,
        *,
        connector_id: str,
        cwd: str | None,
        user_id: str,
    ) -> tuple[str, str]:
        if not project_id:
            raise SessionRunInvalidConfigError(
                {
                    "code": "project_required",
                    "message": "a project must be selected for every session",
                }
            )
        try:
            project = await self._store.get_project(project_id, user_id=user_id)
        except KeyError:
            raise SessionRunNotFoundError("project not found") from None
        if project.connectorId != connector_id:
            raise SessionRunInvalidConfigError(
                {
                    "code": "project_workspace_mismatch",
                    "message": (
                        "project connector and workspace must match the session"
                    ),
                }
            )
        if cwd is None:
            # Keep the connector request and the persisted session on the
            # project's canonical workspace path.
            cwd = project.workspacePath
        else:
            try:
                _cleaned_cwd, cwd_key = _clean_workspace_path(cwd, None)
                _project_path, project_key = _clean_workspace_path(
                    project.workspacePath,
                    None,
                )
            except ValueError:
                cwd_key = ""
                project_key = "__invalid__"
            if cwd_key != project_key:
                raise SessionRunInvalidConfigError(
                    {
                        "code": "project_workspace_mismatch",
                        "message": (
                            "project connector and workspace must match the session"
                        ),
                    }
                )
        return project.id, project.workspacePath

    async def _mark_create_and_start_failed(
        self,
        session_id: str,
        *,
        runtime: str,
        code: str,
        message: str,
    ) -> None:
        await self._store.clear_active_run(session_id)

    async def send_message(
        self,
        session_id: str,
        payload: MessageCreateRequest,
        *,
        user_id: str,
    ) -> RpcResponsePayload:
        try:
            session = await self._store.get_session(session_id, user_id=user_id)
        except KeyError:
            raise SessionRunNotFoundError("session not found") from None

        if session.archived:
            raise SessionRunConflictError(_session_source_error_detail(session))
        if not session.takeover:
            raise SessionRunConflictError("session is read-only until takeover is enabled")
        if not await self._manager.is_online(session.connectorId):
            raise SessionRunConflictError("connector is offline")
        await self._ensure_session_runtime_running(session, user_id=user_id)
        try:
            runtime_status = await self._read_runtime_status(session)
        except SessionRunTimeoutError:
            # A runtime too busy to answer the status read is, by that fact,
            # mid-turn — which is exactly the case queueing exists for. Failing
            # here made the feature unusable under the load it was built for:
            # the busier the runtime, the more likely the send was rejected.
            if not payload.queueWhenBusy:
                raise
            await self._require_session_capability(
                session,
                SESSION_SEND_MESSAGE,
                user_id=user_id,
            )
            # Native runtimes replace the old database _enqueue_message path.
            return await self._queue_runtime_message(session, payload, user_id=user_id)
        if runtime_status not in {"idle", "error"}:
            # The runtime is mid-turn. With queueing requested, keep the message
            # instead of rejecting it; it is dispatched as this turn finishes.
            # Capabilities are still checked, because a runtime that cannot send
            # at all will not be able to send the queued message either.
            if payload.queueWhenBusy:
                await self._require_session_capability(
                    session,
                    SESSION_SEND_MESSAGE,
                    user_id=user_id,
                )
                # Native runtimes replace the old database _enqueue_message path.
                return await self._queue_runtime_message(session, payload, user_id=user_id)
            raise SessionRunConflictError(f"session is {runtime_status}")
        await self._require_session_capability(
            session,
            SESSION_SEND_MESSAGE,
            user_id=user_id,
        )
        params: dict[str, Any] = {
            "sessionId": session_id,
            "runtime": session.runtime,
            "runtimeId": _session_runtime_id(session),
            "content": payload.content,
        }
        if session.cwd:
            params["cwd"] = session.cwd
        if session.externalSessionId:
            params["externalSessionId"] = session.externalSessionId
        if payload.clientMessageId:
            params["clientMessageId"] = payload.clientMessageId
        if payload.attachments:
            attachment_payloads = await self._attachment_payloads(
                session_id=session_id,
                user_id=user_id,
                file_ids=[a.fileId for a in payload.attachments],
            )
            await self._require_session_capability(
                session,
                RUNTIME_ATTACHMENT,
                user_id=user_id,
                attachment_media_types=[item["mediaType"] for item in attachment_payloads],
            )
            params["attachments"] = attachment_payloads
            params["timelineAttachments"] = [_timeline_attachment_payload(item) for item in attachment_payloads]

        await self._store.start_active_run(
            session_id=session_id,
            runtime=session.runtime,
            runtime_id=_session_runtime_id(session),
            external_session_id=session.externalSessionId,
            params=params,
        )
        try:
            result = await self._manager.request(
                session.connectorId,
                "session.send_message",
                params,
            )
        except ConnectorOfflineError as exc:
            await self._store.clear_active_run(session_id)
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            await self._store.clear_active_run(session_id)
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        if isinstance(result, dict) and result.get("ok") is False:
            await self._store.clear_active_run(session_id)
            await self._persist_operation_source_state(session, result)
            code = result.get("code")
            message = result.get("message")
            raise SessionRunConflictError(
                {
                    "code": code if isinstance(code, str) else "runtime_operation_failed",
                    "message": (
                        message
                        if isinstance(message, str)
                        else "runtime operation failed"
                    ),
                }
            )
        return RpcResponsePayload(
            ok=True,
            result={
                **(result if isinstance(result, dict) else {}),
                # Only the runtime can decide whether this request entered its
                # native queue. A queue option on an idle send is still a
                # normal timeline message.
                "queued": bool(result.get("queued")) if isinstance(result, dict) else False,
            },
        )

    async def _persist_operation_source_state(
        self,
        session: SessionView,
        result: dict[str, Any],
    ) -> None:
        source_state = result.get("sourceState")
        if not isinstance(source_state, dict):
            return
        availability = source_state.get("availability")
        observation_origin = source_state.get("observationOrigin")
        if availability not in SESSION_SOURCE_ERROR_CODES:
            return
        if observation_origin not in {"event", "inventory", "operation"}:
            return
        if source_state.get("sessionId") not in {None, session.id}:
            return
        if source_state.get("runtime") not in {None, session.runtime}:
            return
        reason = source_state.get("reason")
        observed_at = source_state.get("observedAt")
        await self._store.update_session_source_state(
            session.id,
            availability=availability,
            reason=reason if isinstance(reason, str) else None,
            observed_at=observed_at if isinstance(observed_at, str) else None,
            observation_origin=observation_origin,
        )

    async def _read_runtime_status(self, session: SessionView) -> SessionStatus:
        params: dict[str, Any] = {
            "sessionId": session.id,
            "runtime": session.runtime,
            "runtimeId": _session_runtime_id(session),
        }
        if session.externalSessionId:
            params["externalSessionId"] = session.externalSessionId
        try:
            result = await self._manager.request(
                session.connectorId,
                "session.state",
                params,
                timeout=10,
            )
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except TimeoutError as exc:
            # A runtime busy with a turn can be too slow to answer within the
            # read budget. That is a runtime that did not answer, not a server
            # fault, so it must be reported as a timeout: letting it escape as a
            # bare TimeoutError surfaces as an opaque 500 and tells the reader
            # nothing about whether to retry.
            raise SessionRunTimeoutError(
                "runtime did not report its state in time"
            ) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        if not isinstance(result, dict):
            return "idle"
        state = result.get("state")
        if not isinstance(state, dict):
            return "idle"
        try:
            resolve_session_runtime_binding(
                state,
                session_id=session.id,
                runtime_type=session.runtime,
                runtime_id=_session_runtime_id(session),
            )
        except SessionRuntimeBindingError as exc:
            raise SessionRunUpstreamError(str(exc)) from exc
        status = state.get("status")
        if status in {
            "idle",
            "waiting",
            "pending",
            "running",
            "stopping",
            "waiting_approval",
            "error",
            "blocked",
        }:
            return status
        return "idle"

    async def update_session_selections(
        self,
        session_id: str,
        payload: SessionSelectionPatchRequest,
        *,
        user_id: str,
    ) -> tuple[SessionRuntimeState, dict[str, Any] | None]:
        try:
            session = await self._store.get_session(session_id, user_id=user_id)
        except KeyError:
            raise SessionRunNotFoundError("session not found") from None
        if not session.takeover:
            raise SessionRunConflictError("session is read-only until takeover is enabled")
        if not await self._manager.is_online(session.connectorId):
            raise SessionRunConflictError("connector is offline")
        for selection_type, capability_id in (
            ("model", CATALOG_MODEL),
            ("permission", CATALOG_PERMISSION),
        ):
            if selection_type in payload.selections:
                await self._require_session_capability(
                    session,
                    capability_id,
                    user_id=user_id,
                )
        await self._ensure_session_runtime_running(session, user_id=user_id)
        params: dict[str, Any] = {
            "sessionId": session_id,
            "runtime": session.runtime,
            "runtimeId": _session_runtime_id(session),
            "selections": payload.selections,
        }
        if session.externalSessionId:
            params["externalSessionId"] = session.externalSessionId
        try:
            result = await self._manager.request(
                session.connectorId,
                "session.selections.update",
                params,
                timeout=30,
            )
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        if isinstance(result, dict) and result.get("ok") is False:
            code = result.get("code")
            message = result.get("message")
            raise SessionRunUpstreamError(
                str(message or code or "runtime rejected selection update")
            )
        try:
            state = _runtime_state_from_selection_result(
                session,
                payload.selections,
                result,
            )
        except SessionRuntimeBindingError as exc:
            raise SessionRunUpstreamError(str(exc)) from exc
        return state, result if isinstance(result, dict) else None

    async def steer_session(
        self,
        session_id: str,
        payload: SessionSteerRequest,
        *,
        user_id: str,
    ) -> RpcResponsePayload:
        try:
            session = await self._store.get_session(session_id, user_id=user_id)
        except KeyError:
            raise SessionRunNotFoundError("session not found") from None
        if not session.takeover:
            raise SessionRunConflictError(
                "session is read-only until takeover is enabled"
            )
        if not await self._manager.is_online(session.connectorId):
            raise SessionRunConflictError("connector is offline")
        await self._ensure_session_runtime_running(session, user_id=user_id)
        runtime_status = await self._read_runtime_status(session)
        if runtime_status != "running":
            raise SessionRunConflictError("session is not running")
        await self._require_session_capability(
            session,
            SESSION_STEER,
            user_id=user_id,
        )

        params: dict[str, Any] = {
            "sessionId": session_id,
            "runtime": session.runtime,
            "runtimeId": _session_runtime_id(session),
            "content": payload.content,
        }
        external_session_id = session.externalSessionId
        if external_session_id:
            params["externalSessionId"] = external_session_id
        if session.cwd:
            params["cwd"] = session.cwd
        if payload.clientMessageId:
            params["clientMessageId"] = payload.clientMessageId
        if payload.attachments:
            attachment_payloads = await self._attachment_payloads(
                session_id=session_id,
                user_id=user_id,
                file_ids=[attachment.fileId for attachment in payload.attachments],
            )
            await self._require_session_capability(
                session,
                RUNTIME_ATTACHMENT,
                user_id=user_id,
                attachment_media_types=[item["mediaType"] for item in attachment_payloads],
            )
            params["attachments"] = attachment_payloads
            params["timelineAttachments"] = [
                _timeline_attachment_payload(item) for item in attachment_payloads
            ]

        try:
            result = await self._manager.request(
                session.connectorId,
                "session.steer",
                params,
            )
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        return RpcResponsePayload(ok=True, result=result)

    async def _require_runtime_capability(
        self,
        connector_id: str,
        runtime: str,
        runtime_id: str,
        capability_id: str,
        *,
        user_id: str,
        attachment_media_types: list[str] | None = None,
    ) -> None:
        capability_set = ProtocolCapabilitySet.model_validate(
            await self._store.get_protocol_capabilities(
                connector_id,
                user_id=user_id,
            )
        )
        capability = next(
            (
                item
                for item in capability_set.capabilities
                if item.runtime == runtime
                and item.scope == "runtime"
                and item.capabilityId == capability_id
                and _capability_runtime_id(item) == runtime_id
            ),
            None,
        )
        if capability is None:
            capability = next(
                (
                    item
                    for item in capability_set.capabilities
                    if item.runtime == runtime
                    and item.scope == "runtime"
                    and item.capabilityId == capability_id
                    and _capability_runtime_id(item) is None
                ),
                None,
            )
        if capability is None or not (
            capability.supported and capability.available and capability.allowed
        ):
            raise SessionRunConflictError(
                f"runtime capability is unavailable: {capability_id}"
            )

        if attachment_media_types:
            _validate_attachment_mime_types(_capability_metadata(capability), attachment_media_types)

    async def _require_session_capability(
        self,
        session: SessionView,
        capability_id: str,
        *,
        user_id: str,
        attachment_media_types: list[str] | None = None,
    ) -> None:
        try:
            capability_set = await read_session_capability_facts(
                self._manager, session, runtime_id=_session_runtime_id(session),
            )
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except TimeoutError as exc:
            raise SessionRunTimeoutError({
                "code": "runtime_capabilities_timeout",
                "message": "connector session capabilities request timed out",
            }) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        except ValueError as exc:
            raise SessionRunUpstreamError(str(exc)) from exc
        online_session = session.model_copy(update={"connectorStatus": "online"})
        effective = derive_session_effective_capabilities(
            session=online_session,
            runtime_capabilities=capability_set,
        )
        capability = next(
            (
                item
                for item in effective.capabilities
                if item.capabilityId == capability_id
            ),
            None,
        )
        if capability is None or not (
            capability.supported and capability.available and capability.allowed
        ):
            # Preserve the machine-readable reason so clients can explain why
            # the action is blocked. A bare string flattened "runtime is not
            # running", "not taken over" and "connector offline" into one
            # message the user could not act on.
            raise SessionRunConflictError(
                capability_unavailable_detail(effective, capability_id)
            )

        if attachment_media_types:
            _validate_attachment_mime_types(_capability_metadata(capability), attachment_media_types)

    async def _require_runtime_instance(
        self,
        connector_id: str,
        runtime: str,
        runtime_id: str,
        *,
        user_id: str,
        require_running: bool,
    ) -> None:
        try:
            await self._device_runtimes.ensure_session_routable(
                connector_id,
                runtime_type=runtime,
                runtime_id=runtime_id,
                user_id=user_id,
                ensure_running=require_running,
            )
        except DeviceRuntimeError as exc:
            self._raise_device_runtime_error(exc)

    async def _ensure_session_runtime_running(
        self,
        session: SessionView,
        *,
        user_id: str,
    ) -> None:
        await self._require_runtime_instance(
            session.connectorId,
            session.runtime,
            _session_runtime_id(session),
            user_id=user_id,
            require_running=True,
        )

    @staticmethod
    def _raise_device_runtime_error(exc: DeviceRuntimeError) -> None:
        if exc.status_code == 404:
            raise SessionRunNotFoundError(exc.message) from exc
        if exc.status_code == 422:
            raise SessionRunInvalidConfigError(exc.detail) from exc
        if exc.status_code == 502:
            raise SessionRunUpstreamError(exc.detail) from exc
        raise SessionRunConflictError(exc.detail) from exc

    async def _attachment_payloads(
        self,
        *,
        session_id: str,
        user_id: str,
        file_ids: list[str],
    ) -> list[PersistedInlineAttachment]:
        payloads: list[dict[str, Any]] = []
        for file_id in file_ids:
            try:
                metadata = await self._store.read_uploaded_file(
                    session_id=session_id,
                    file_id=file_id,
                    user_id=user_id,
                )
            except KeyError:
                raise SessionRunNotFoundError(f"attachment not found: {file_id}") from None
            except ValueError as exc:
                raise SessionRunInvalidConfigError(str(exc)) from exc
            payloads.append(
                {
                    "fileId": metadata.get("fileId") or file_id,
                    "name": metadata.get("name") or file_id,
                    "mediaType": metadata.get("mediaType") or "",
                    "size": metadata.get("size"),
                    "sha256": metadata.get("sha256"),
                    "downloadUrl": api_v2_path(f"/connector/sessions/{session_id}/attachments/{file_id}/content"),
                    "platformOpenUrl": api_v2_path(f"/sessions/{session_id}/attachments/{file_id}/open"),
                }
            )
        return payloads

    async def _enqueue_message(
        self,
        session: SessionView,
        payload: MessageCreateRequest,
        *,
        user_id: str,
    ) -> RpcResponsePayload:
        """Accept a message for later dispatch while the runtime is busy.

        Attachments are persisted now rather than at dispatch time: the client is
        about to drop its local copy, and the queued item must survive a restart.
        """
        persisted_refs: list[dict[str, Any]] = []
        if payload.attachments:
            persisted = await self._persist_inline_attachments(
                session_id=session.id,
                user_id=user_id,
                attachments=payload.attachments,
            )
            persisted_refs = [
                _timeline_payload_from_persisted_inline_attachment(attachment)
                for attachment in persisted
            ]
        try:
            item = await self._message_queue.enqueue(
                session_id=session.id,
                user_id=user_id,
                content=payload.content,
                attachments=persisted_refs,
                client_message_id=payload.clientMessageId,
            )
        except MessageQueueError as exc:
            raise SessionRunConflictError(exc.detail()) from None
        return RpcResponsePayload(
            ok=True,
            result={
                "queued": True,
                "item": item.to_payload(),
            },
        )

    async def _dispatch_enqueued(self, session_id: str) -> None:
        """Close the enqueue/idle transition race.

        A runtime can finish between the status read and the queue insert. In
        that case its idle notification has already been processed, so waiting
        for another turn-end event would leave the item invisible to the
        runtime indefinitely. The optional callback performs an immediate,
        lock-protected drain; busy races are requeued by the dispatcher.
        """
        if self._queue_dispatch is None:
            return
        try:
            await self._queue_dispatch(session_id)
        except Exception:
            # The normal runtime status notification remains the durable retry
            # path; enqueue itself must still succeed when that callback fails.
            return

    async def list_queue(self, session_id: str, *, user_id: str) -> list[dict[str, Any]]:
        session = await self._require_session(session_id, user_id=user_id)
        await self._ensure_session_runtime_running(session, user_id=user_id)
        try:
            result = await self._manager.request(
                session.connectorId,
                "session.queue.list",
                self._runtime_session_params(session),
            )
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        return _runtime_queue_items(result, session.id)

    async def update_queue_item(
        self,
        session_id: str,
        item_id: str,
        payload: QueueItemUpdateRequest,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        session = await self._require_session(session_id, user_id=user_id)
        if payload.content is None:
            raise SessionRunConflictError("queue content is required")
        await self._require_session_capability(
            session, SESSION_SEND_MESSAGE, user_id=user_id,
        )
        await self._ensure_session_runtime_running(session, user_id=user_id)
        try:
            result = await self._manager.request(
                session.connectorId,
                "session.queue.update",
                {**self._runtime_session_params(session), "itemId": item_id, "content": payload.content},
            )
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        return _runtime_queue_item(
            {**(result if isinstance(result, dict) else {}), "id": item_id,
             "status": "queued", "content": payload.content},
            session.id,
            item_id,
        )

    async def remove_queue_item(
        self,
        session_id: str,
        item_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        session = await self._require_session(session_id, user_id=user_id)
        await self._require_session_capability(
            session, SESSION_SEND_MESSAGE, user_id=user_id,
        )
        await self._ensure_session_runtime_running(session, user_id=user_id)
        try:
            result = await self._manager.request(
                session.connectorId,
                "session.queue.delete",
                {**self._runtime_session_params(session), "itemId": item_id},
            )
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        return _runtime_queue_item(result, session.id, item_id)

    async def _queue_runtime_message(
        self,
        session: SessionView,
        payload: MessageCreateRequest,
        *,
        user_id: str,
    ) -> RpcResponsePayload:
        """Submit a busy-session message to the engine's native queue."""
        params = self._runtime_session_params(session)
        params["content"] = payload.content
        if payload.clientMessageId:
            params["clientMessageId"] = payload.clientMessageId
        if session.cwd:
            params["cwd"] = session.cwd
        if payload.attachments:
            attachment_payloads = await self._attachment_payloads(
                session_id=session.id,
                user_id=user_id,
                file_ids=[attachment.fileId for attachment in payload.attachments],
            )
            await self._require_session_capability(
                session,
                RUNTIME_ATTACHMENT,
                user_id=user_id,
                attachment_media_types=[item["mediaType"] for item in attachment_payloads],
            )
            params["attachments"] = attachment_payloads
            params["timelineAttachments"] = [_timeline_attachment_payload(item) for item in attachment_payloads]
        try:
            result = await self._manager.request(session.connectorId, "session.queue", params)
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        if isinstance(result, dict) and result.get("ok") is False:
            raise SessionRunConflictError({
                "code": result.get("code") or "runtime_queue_failed",
                "message": result.get("message") or "runtime queue rejected the message",
            })
        return RpcResponsePayload(ok=True, result={**(result if isinstance(result, dict) else {}), "queued": True})

    @staticmethod
    def _runtime_session_params(session: SessionView) -> dict[str, Any]:
        params: dict[str, Any] = {
            "sessionId": session.id,
            "runtime": session.runtime,
            "runtimeId": _session_runtime_id(session),
        }
        if session.externalSessionId:
            params["externalSessionId"] = session.externalSessionId
        return params

    async def _require_session(self, session_id: str, *, user_id: str) -> SessionView:
        try:
            return await self._store.get_session(session_id, user_id=user_id)
        except KeyError:
            raise SessionRunNotFoundError("session not found") from None

    async def send_queued_message(
        self,
        session_id: str,
        item: dict[str, Any],
        *,
        user_id: str | None,
    ) -> None:
        """Dispatch one queued item as a normal turn.

        This is the queue dispatcher's sender. It re-enters the ordinary send
        path so a queued message is indistinguishable from one the user sent
        directly: same capability checks, same runtime call, same timeline.
        """
        if user_id is None:
            raise SessionRunConflictError("queued message has no owner to send as")
        attachments = item.get("attachments")
        refs = [
            AttachmentRef(fileId=str(entry["fileId"]))
            for entry in attachments or []
            if isinstance(entry, dict) and entry.get("fileId")
        ]
        payload = MessageCreateRequest(
            content=str(item.get("content") or ""),
            attachments=refs,
            clientMessageId=item.get("clientMessageId"),
            # This item is already claimed by the dispatcher. If the runtime
            # became busy again, the dispatcher must requeue this same item;
            # allowing the ordinary send path to enqueue here would duplicate
            # it.
            queueWhenBusy=False,
        )
        # A busy runtime here means the session picked up other work between the
        # turn ending and this dispatch. The dispatcher requeues on this signal
        # instead of failing the message.
        try:
            await self.send_message(session_id, payload, user_id=user_id)
        except SessionRunConflictError as exc:
            detail = str(getattr(exc, "detail", "") or exc)
            if "session is " in detail:
                raise QueueDispatchBusy(detail) from None
            raise
        except SessionRunTimeoutError as exc:
            # The runtime stayed unreachable for the whole read budget. That is
            # still "not now" rather than "never", so the item goes back to the
            # head and the next turn completion retries it.
            raise QueueDispatchBusy(str(getattr(exc, "detail", "") or exc)) from None

    async def _persist_inline_attachments(
        self,
        *,
        session_id: str,
        user_id: str,
        attachments: list[InlineAttachmentRef],
    ) -> list[dict[str, Any]]:
        """Persist create-and-start inline attachments into session file storage.

        Side effects:
        - decodes request base64
        - writes each attachment into the server session-scoped file store
        """

        persisted: list[PersistedInlineAttachment] = []
        for attachment in attachments:
            data = _decode_inline_attachment(attachment)
            saved = await self._store.save_user_uploaded_file(
                session_id=session_id,
                user_id=user_id,
                name=attachment.name,
                data=data,
                media_type=attachment.mediaType,
            )
            persisted.append(
                PersistedInlineAttachment(
                    file_id=str(saved["fileId"]),
                    name=str(saved["name"]),
                    media_type=str(saved.get("mediaType") or ""),
                    size=int(saved["size"]),
                    sha256=str(saved["sha256"]),
                )
            )
        return persisted

    async def interrupt_session(
        self,
        session_id: str,
        *,
        user_id: str,
    ) -> RpcResponsePayload:
        return await self._interrupt_session(session_id, user_id=user_id, require_takeover=True)

    async def interrupt_session_internal(
        self,
        session_id: str,
        *,
        user_id: str,
    ) -> RpcResponsePayload:
        return await self._interrupt_session(session_id, user_id=user_id, require_takeover=False)

    async def _interrupt_session(
        self,
        session_id: str,
        *,
        user_id: str,
        require_takeover: bool,
    ) -> RpcResponsePayload:
        try:
            session = await self._store.get_session(session_id, user_id=user_id)
        except KeyError:
            raise SessionRunNotFoundError("session not found") from None
        if require_takeover and not session.takeover:
            raise SessionRunConflictError("session is read-only until takeover is enabled")
        if require_takeover:
            await self._require_session_capability(
                session,
                SESSION_INTERRUPT,
                user_id=user_id,
            )
        await self._ensure_session_runtime_running(session, user_id=user_id)
        params: dict[str, Any] = {
            "sessionId": session_id,
            "runtime": session.runtime,
            "runtimeId": _session_runtime_id(session),
        }
        try:
            result = await self._manager.request(
                session.connectorId,
                "session.interrupt",
                params,
            )
        except ConnectorOfflineError as exc:
            raise SessionRunConflictError(str(exc)) from exc
        except ConnectorRpcError as exc:
            raise SessionRunUpstreamError(exc.message or exc.code) from exc
        await self._store.clear_active_run(session_id)
        return RpcResponsePayload(ok=True, result=result)


def _session_source_error_detail(session: SessionView) -> dict[str, str]:
    availability = session.sourceAvailability
    code = SESSION_SOURCE_ERROR_CODES.get(availability, "session_archived")
    if session.userArchived and session.archiveSource == "user":
        message = "session is archived in Agents Anywhere"
    else:
        message = f"session is {availability} in the local runtime"
    return {"code": code, "message": message}


def _selections_from_mapping(value: dict[str, str | None]) -> dict[str, str]:
    return {key: item for key, item in value.items() if isinstance(item, str) and item}


def _runtime_queue_item(
    result: Any,
    session_id: str,
    item_id: str,
) -> dict[str, Any]:
    if isinstance(result, dict):
        for candidate in result.get("items", []):
            if isinstance(candidate, dict) and str(candidate.get("id")) == item_id:
                return _normalize_runtime_queue_item(candidate, session_id, 0)
    return _normalize_runtime_queue_item(
        {"id": item_id, "status": "removed", "content": ""}, session_id, 0
    )


def _runtime_queue_items(result: Any, session_id: str) -> list[dict[str, Any]]:
    raw_items = result.get("items", []) if isinstance(result, dict) else []
    runtime = result.get("runtime") if isinstance(result, dict) else None
    if not isinstance(raw_items, list):
        return []
    return [
        _normalize_runtime_queue_item(item, session_id, position, runtime=runtime)
        for position, item in enumerate(raw_items)
        if isinstance(item, dict)
    ]


def _normalize_runtime_queue_item(
    item: dict[str, Any], session_id: str, position: int, *, runtime: Any = None
) -> dict[str, Any]:
    now = utc_now()
    content = item.get("content")
    return {
        "id": str(item.get("id") or ""),
        "sessionId": session_id,
        "position": int(item.get("position", position)),
        "status": str(item.get("status") or "queued"),
        "content": content if isinstance(content, str) else "",
        "attachments": item.get("attachments") if isinstance(item.get("attachments"), list) else [],
        "clientMessageId": item.get("clientMessageId") if isinstance(item.get("clientMessageId"), str) else None,
        "errorCode": item.get("errorCode") if isinstance(item.get("errorCode"), str) else None,
        "errorMessage": item.get("errorMessage") if isinstance(item.get("errorMessage"), str) else None,
        "createdAt": str(item.get("createdAt") or now),
        "updatedAt": str(item.get("updatedAt") or now),
        "runtime": item.get("runtime") or runtime or "native",
        "placement": item.get("placement") or "queued",
    }


def _request_runtime_id(
    payload: SessionCreateRequest | SessionCreateAndStartRequest,
) -> str:
    return payload.runtimeId or payload.runtime


def _session_runtime_id(session: SessionView) -> str:
    return session.runtimeId or session.runtime


def _capability_runtime_id(capability: Any) -> str | None:
    value = getattr(capability, "runtimeId", None)
    return value if isinstance(value, str) and value else None


def _runtime_state_from_selection_result(
    session: SessionView,
    selections: dict[str, str | None],
    result: object,
) -> SessionRuntimeState:
    now = utc_now()
    raw_state = result.get("state") if isinstance(result, dict) else None
    if isinstance(raw_state, dict):
        session_id, runtime, runtime_id = resolve_session_runtime_binding(
            raw_state,
            session_id=session.id,
            runtime_type=session.runtime,
            runtime_id=_session_runtime_id(session),
        )
        return SessionRuntimeState.model_validate(
            {
                "sessionId": session_id,
                "runtime": runtime,
                "runtimeId": runtime_id,
                "externalSessionId": raw_state.get("externalSessionId")
                or session.externalSessionId,
                "status": raw_state.get("status") or "idle",
                "selections": raw_state.get("selections")
                if isinstance(raw_state.get("selections"), dict)
                else selections,
                "statusReason": raw_state.get("statusReason"),
                "error": raw_state.get("error")
                if isinstance(raw_state.get("error"), dict)
                else None,
                "metadata": raw_state.get("metadata")
                if isinstance(raw_state.get("metadata"), dict)
                else {},
                "updatedSeq": session.updatedSeq,
                "createdAt": now,
                "updatedAt": now,
            }
        )
    return SessionRuntimeState(
        sessionId=session.id,
        runtime=session.runtime,
        runtimeId=_session_runtime_id(session),
        externalSessionId=session.externalSessionId,
        status="idle",
        selections=selections,
        updatedSeq=session.updatedSeq,
        createdAt=now,
        updatedAt=now,
    )


def _timeline_attachment_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "fileId": value.get("fileId"),
        "name": value.get("name"),
        "mediaType": value.get("mediaType"),
        "size": value.get("size"),
        "sha256": value.get("sha256"),
    }


def _connector_attachment_reference_payload(attachment: PersistedInlineAttachment) -> dict[str, Any]:
    return {
        "fileId": attachment.file_id,
        "name": attachment.name,
        "mediaType": attachment.media_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
    }


def _timeline_payload_from_persisted_inline_attachment(
    attachment: PersistedInlineAttachment,
) -> dict[str, Any]:
    return {
        "fileId": attachment.file_id,
        "name": attachment.name,
        "mediaType": attachment.media_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
    }


def _decode_inline_attachment(attachment: InlineAttachmentRef) -> bytes:
    try:
        data = base64.b64decode(attachment.contentBase64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SessionRunInvalidConfigError(
            f"attachment {attachment.fileId} contentBase64 is invalid"
        ) from exc
    if attachment.size is not None and attachment.size != len(data):
        raise SessionRunInvalidConfigError(
            f"attachment {attachment.fileId} size does not match content"
        )
    if attachment.sha256 is not None:
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if attachment.sha256 != actual_sha256:
            raise SessionRunInvalidConfigError(
                f"attachment {attachment.fileId} sha256 does not match content"
            )
    return data


def _capability_metadata(capability: Any) -> dict[str, Any]:
    """Optional runtime restrictions a capability may not publish at all."""
    metadata = getattr(capability, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _validate_attachment_mime_types(metadata: dict[str, Any], media_types: list[str]) -> None:
    """Enforce optional runtime MIME restrictions on stored upload metadata."""
    if "allowedMimeTypes" not in metadata:
        return
    raw = metadata["allowedMimeTypes"]
    allowed = {
        value.strip().lower() for value in raw if isinstance(value, str)
        and re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", value.strip().lower())
    } if isinstance(raw, list) else set()
    for media_type in media_types:
        normalized = media_type.split(";", 1)[0].strip().lower()
        if normalized not in allowed:
            raise SessionRunConflictError(f"attachment type is not supported by this runtime: {normalized}")


def _queue_error_to_run_error(error: MessageQueueError) -> SessionRunError:
    """Map a queue failure onto the HTTP status the client should see.

    A missing or already-dispatched item is a conflict rather than a 404: the
    item may have existed and been sent, which the client resolves by refreshing
    the queue rather than treating the id as invalid.
    """
    if error.code == "session/queue-item-not-found":
        return SessionRunConflictError(error.detail())
    return SessionRunInvalidConfigError(error.detail())
