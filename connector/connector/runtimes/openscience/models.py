"""Pure mappings from OpenScience payloads to the Connector's protocol models.

Everything here is a function of its arguments: the runtime owns transport and
lifecycle, the relay owns cursors, and this module owns the translation so the
mapping rules can be tested without a server. Three shapes recur:

* a **session** is OpenScience's ``ses_…`` record, and its transcript is the
  durable message list rather than the event journal — the journal is a bounded
  window, the transcript is not;
* a **decision** (permission or question) is only actionable while the
  connected server still lists it in a fresh snapshot, so a notice always
  carries the native request id that a decision is addressed to;
* a **model** comes from the server's own ``/config/providers`` catalog, and a
  platform selection is only an opaque id until it is resolved back to the
  provider, model and effort the server would run.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from connector.runtime_protocol import (
    ArtifactTimelineItem,
    CommandToolContent,
    CompactMarkerContent,
    ErrorSystemContent,
    FileArtifactContent,
    FileChangeToolContent,
    GenericSystemContent,
    InputRequestForm,
    InputRequestOption,
    InputRequestQuestion,
    InputRequestValidationError,
    MarkdownMessageContent,
    MarkerTimelineItem,
    MessageTimelineItem,
    PlatformTimelineItem,
    ReasoningSystemContent,
    RuntimeModelCatalog,
    RuntimeModelItem,
    RuntimeProject,
    RuntimeReasoningItem,
    RuntimeStatus,
    RuntimeTimelineItem,
    RuntimeTimelineSnapshot,
    SessionMeta,
    SessionNotice,
    SessionSourceState,
    SessionState,
    SystemTimelineItem,
    TimelineRole,
    TimelineSource,
    ToolCallContent,
    ToolTimelineContent,
    ToolTimelineItem,
    complete_tool_content,
)
from connector.server.protocol import protocol_selection_id

RUNTIME = "openscience"

# The reasoning-effort vocabulary is OpenScience's own (`ResearchEffort`), and
# it is a closed two-value enum on the server. The catalog republishes exactly
# these ids and the prompt passes the chosen one through untouched: a third
# value, or a translation into another runtime's labels, would name an effort
# the server does not have.
OPENSCIENCE_EFFORTS: tuple[str, str] = ("normal", "ultra")
# `resolveResearchEffort` falls back to "normal", so that is the server's own
# default and the value an unselected turn keeps sending.
DEFAULT_EFFORT = "normal"
_EFFORT_TITLES = {"normal": "Normal", "ultra": "Ultra"}

PERMISSION_NOTICE_PREFIX = "notice_openscience_permission_"
QUESTION_NOTICE_PREFIX = "notice_openscience_question_"

_PERMISSION_REPLIES = frozenset({"once", "session", "project", "always", "reject"})

# Native run states, as reported by GET /runtime/snapshot. `accepted` means the
# server admitted the work; it is not yet evidence that anything is executing.
_ACTIVE_RUN_STATES = frozenset({"accepted", "running"})

_SHELL_TOOLS = frozenset({"bash", "shell", "run", "terminal", "exec", "command"})
_FILE_TOOLS = frozenset({"edit", "write", "patch", "str_replace", "multiedit", "notebookedit"})

_CATALOG_SOURCE = "openscience.config.providers"


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """The native routing a platform model selection resolves to.

    The platform only ever hands back the opaque ``selectionId`` it was shown,
    so the provider/model pair — which is what ``POST /runtime/prompt`` needs —
    has to be recovered from the catalog that produced the id.
    """

    provider_id: str
    model_id: str
    effort: str | None = None


def model_catalog(payload: Mapping[str, Any], *, revision: int) -> RuntimeModelCatalog:
    """Map ``GET /config/providers`` onto the platform's model catalog.

    OpenScience owns the model configuration, so this is a translation and not
    a policy: one item per provider/model pair, named exactly as the server
    names it, with the provider's ``default`` model first in its group because
    the Connector's catalog has no default marker of its own and the platform
    falls back to the first enabled item. A model's ``capabilities.reasoning``
    decides whether the two OpenScience effort values are offered as reasoning
    items — the picker's effort options *are* those items, which is why the
    effort capability is published alongside the model catalog.

    Two models may legitimately share a name (the live catalog has twenty such
    names across providers), so the title is qualified only when it is actually
    ambiguous. Ids stay unique regardless: they are the routing pair.
    """

    data = _mapping(payload, "provider catalog")
    raw_providers = data.get("providers")
    if not isinstance(raw_providers, Sequence) or isinstance(raw_providers, (str, bytes)):
        raise TypeError("OpenScience provider catalog must carry a providers array")
    defaults = data.get("default") if isinstance(data.get("default"), Mapping) else {}

    resolved: list[tuple[str, str, str, str, bool, Mapping[str, Any]]] = []
    for raw_provider in raw_providers:
        provider = _mapping(raw_provider, "provider")
        provider_id = _required_string(provider.get("id"), "provider id")
        provider_name = _optional_string(provider.get("name")) or provider_id
        raw_models = provider.get("models")
        if not isinstance(raw_models, Mapping):
            continue
        default_model_id = _optional_string(defaults.get(provider_id))
        models = [model for model in raw_models.values() if isinstance(model, Mapping)]
        # `default[providerID]` names the model OpenScience itself would run,
        # so it leads its group; the rest keep the server's own order.
        models.sort(key=lambda model: 0 if model.get("id") == default_model_id else 1)
        for model in models:
            model_id = _optional_string(model.get("id"))
            if model_id is None:
                continue
            model_name = _optional_string(model.get("name")) or model_id
            resolved.append(
                (
                    provider_id,
                    provider_name,
                    model_id,
                    model_name,
                    model_id == default_model_id,
                    model,
                )
            )

    provider_count_by_name = Counter(
        (provider_id, model_name)
        for provider_id, _, _, model_name, _, _ in resolved
    )
    providers_by_name: dict[str, set[str]] = {}
    for provider_id, _, _, model_name, _, _ in resolved:
        providers_by_name.setdefault(model_name, set()).add(provider_id)

    items = tuple(
        _model_item(
            provider_id=provider_id,
            provider_name=provider_name,
            model_id=model_id,
            model_name=model_name,
            is_default=is_default,
            model=model,
            name_is_shared=len(providers_by_name[model_name]) > 1,
            name_is_repeated=provider_count_by_name[(provider_id, model_name)] > 1,
        )
        for (
            provider_id,
            provider_name,
            model_id,
            model_name,
            is_default,
            model,
        ) in resolved
    )
    return RuntimeModelCatalog(runtime=RUNTIME, revision=revision, models=items)


def model_route(
    catalog: RuntimeModelCatalog, selection_id: str
) -> ModelRoute | None:
    """Recover the native routing behind one selection id, if it is known.

    A selection id that resolves to nothing is not silently treated as the
    server default: the caller refuses it, because the user asked for a
    specific model and quietly running another one is worse than an error.
    """

    for model in catalog.models:
        provider_id = _metadata_string(model.metadata, "providerID")
        model_id = _metadata_string(model.metadata, "modelID")
        if provider_id is None or model_id is None:
            continue
        if model.selection_id == selection_id:
            return ModelRoute(provider_id=provider_id, model_id=model_id)
        for reasoning in model.reasoning_items:
            if reasoning.selection_id == selection_id:
                return ModelRoute(
                    provider_id=provider_id, model_id=model_id, effort=reasoning.id
                )
    return None


def effort_value(value: Any) -> str | None:
    """Return the value only when it is an effort OpenScience actually accepts."""

    return value if isinstance(value, str) and value in OPENSCIENCE_EFFORTS else None


def _model_item(
    *,
    provider_id: str,
    provider_name: str,
    model_id: str,
    model_name: str,
    is_default: bool,
    model: Mapping[str, Any],
    name_is_shared: bool,
    name_is_repeated: bool,
) -> RuntimeModelItem:
    status = _optional_string(model.get("status")) or "active"
    reasoning = _reasoning_supported(model)
    title = model_name
    if name_is_shared:
        title = f"{title} · {provider_name}"
    if name_is_repeated:
        title = f"{title} [{model_id}]"
    return RuntimeModelItem(
        # The provider/model pair, because the same model id is served by
        # several providers and a platform id has to select exactly one route.
        id=f"{provider_id}/{model_id}",
        title=title,
        selection_id=(
            None
            if reasoning
            else protocol_selection_id(
                RUNTIME, "model", _selection_identity(provider_id, model_id, None)
            )
        ),
        reasoning_items=(
            tuple(
                RuntimeReasoningItem(
                    id=effort,
                    title=_EFFORT_TITLES[effort],
                    selection_id=protocol_selection_id(
                        RUNTIME, "model", _selection_identity(provider_id, model_id, effort)
                    ),
                    metadata={
                        "source": _CATALOG_SOURCE,
                        "effort": effort,
                        "default": effort == DEFAULT_EFFORT,
                    },
                )
                for effort in OPENSCIENCE_EFFORTS
            )
            if reasoning
            else ()
        ),
        enabled=status == "active",
        disabled_reason=(
            None if status == "active" else f"OpenScience reports this model as {status}"
        ),
        metadata={
            "source": _CATALOG_SOURCE,
            "providerID": provider_id,
            "providerName": provider_name,
            "modelID": model_id,
            "modelName": model_name,
            "reasoning": reasoning,
            "status": status,
            "default": is_default,
        },
    )


def _selection_identity(
    provider_id: str, model_id: str, effort: str | None
) -> dict[str, Any]:
    """The routing a selection id commits to, hashed by the protocol helper."""

    return {"provider_id": provider_id, "model_id": model_id, "effort": effort}


def _reasoning_supported(model: Mapping[str, Any]) -> bool:
    capabilities = model.get("capabilities")
    return isinstance(capabilities, Mapping) and capabilities.get("reasoning") is True


def _metadata_string(metadata: Mapping[str, Any], key: str) -> str | None:
    return _optional_string(metadata.get(key))


def session_meta(
    payload: Mapping[str, Any],
    *,
    session_id: str,
    observed_at: str | None = None,
) -> SessionMeta:
    """Map one ``GET /session`` entry onto the platform's session inventory."""

    data = _mapping(payload, "session")
    external_id = _required_string(data.get("id"), "session id")
    time = _mapping(data.get("time"), "session time")
    return SessionMeta(
        session_id=session_id,
        external_session_id=external_id,
        runtime=RUNTIME,
        title=_optional_string(data.get("title")),
        cwd=_optional_string(data.get("directory")),
        ordering_time=_iso_from_millis(time.get("updated") or time.get("created")),
        source_state=SessionSourceState(
            availability="available",
            reason="openscience.session.list active",
            observed_at=observed_at or _now_iso(),
            observation_origin="inventory",
        ),
        metadata={
            "source": "openscience.session.list",
            "slug": _optional_string(data.get("slug")),
            "projectID": _optional_string(data.get("projectID")),
            "workspace": _optional_string(data.get("workspace")),
        },
    )


def created_project(payload: Mapping[str, Any], *, name: str) -> RuntimeProject:
    """Map the project OpenScience created onto the identity the caller stores.

    Both the id and the ``worktree`` are required. They are the whole point of
    this call — the server generated the directory and is the only party that
    knows it — so a response missing either is a protocol failure rather than
    something to fill in from the request.
    """

    info = _mapping(payload, "project")
    metadata: dict[str, Any] = {}
    origin = _optional_string(info.get("origin"))
    if origin is not None:
        metadata["origin"] = origin
    icon = info.get("icon")
    if isinstance(icon, Mapping):
        metadata["icon"] = {
            str(key): value for key, value in icon.items() if isinstance(key, str)
        }
    return RuntimeProject(
        project_id=_required_string(info.get("id"), "project id"),
        # The server normalizes the name, so its answer wins; the requested name
        # is only a fallback for a server that echoes nothing.
        name=_optional_string(info.get("name")) or name,
        worktree=_required_string(info.get("worktree"), "project worktree"),
        metadata=metadata,
    )


def latest_run(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the run that currently describes the session, if any.

    Runs are append-only receipts, so the newest receipt is the one whose state
    the platform should show. An unfinished run stays visible as unfinished:
    OpenScience never resumes work after its owning server dies.
    """

    runs = _mapping(snapshot, "snapshot").get("runs")
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)):
        return None
    newest: Mapping[str, Any] | None = None
    newest_key = -1
    for raw in runs:
        if not isinstance(raw, Mapping):
            continue
        key = raw.get("updatedAt")
        if type(key) is not int:
            key = raw.get("acceptedAt") if type(raw.get("acceptedAt")) is int else 0
        if newest is None or key >= newest_key:
            newest, newest_key = raw, key
    return newest


def session_state(
    snapshot: Mapping[str, Any],
    *,
    session_id: str,
    external_session_id: str | None,
) -> SessionState:
    """Map a snapshot's durable receipts and live decisions onto a session state.

    Pending decisions are read from the snapshot rather than from replayed
    events: ``decisionScope`` is ``connected_runtime``, so only what this
    server process still holds can actually be answered.
    """

    data = _mapping(snapshot, "snapshot")
    run = latest_run(data)
    permissions = _request_list(data.get("permissions"))
    questions = _request_list(data.get("questions"))
    state = _optional_string(run.get("state")) if run is not None else None
    status, reason = _run_status(state)
    # A live decision means the agent is blocked on the user. Only a failed
    # receipt outranks it: a cancelled or completed run cannot still be
    # waiting, and the server drops its pending requests when that happens.
    if (
        status != "error"
        and (permissions or questions)
        and (state is None or state in _ACTIVE_RUN_STATES)
    ):
        status, reason = "waiting_approval", "waiting for a permission or question reply"
    error: dict[str, Any] | None = None
    if run is not None and isinstance(run.get("error"), Mapping):
        error = {
            "code": _optional_string(run["error"].get("code")) or "run_failed",
            "message": _optional_string(run["error"].get("message")) or "",
        }
    return SessionState(
        session_id=session_id,
        external_session_id=external_session_id,
        runtime=RUNTIME,
        status=status,
        status_reason=reason,
        error=error,
        metadata={
            "source": "openscience.runtime.snapshot",
            "runId": _optional_string(run.get("runID")) if run is not None else None,
            "runState": state,
            "crashRecovery": "interrupt",
            "pendingPermissions": len(permissions),
            "pendingQuestions": len(questions),
        },
    )


def _run_status(state: str | None) -> tuple[RuntimeStatus, str | None]:
    if state == "accepted":
        return "pending", "run accepted"
    if state == "running":
        return "running", "run in progress"
    if state == "failed":
        return "error", "run failed"
    if state == "cancelled":
        return "idle", "run cancelled"
    if state == "interrupted":
        # crashRecovery is `interrupt`: the server does not resume this run.
        return "idle", "run interrupted; OpenScience does not resume unfinished runs"
    return "idle", None


def timeline_snapshot(
    messages: Sequence[Mapping[str, Any]],
    *,
    session_id: str,
    external_session_id: str | None,
) -> RuntimeTimelineSnapshot:
    """Map ``GET /session/:id/message`` onto a complete platform snapshot."""

    items = timeline_items(messages, session_id=session_id, external_session_id=external_session_id)
    return RuntimeTimelineSnapshot(
        session_id=session_id,
        external_session_id=external_session_id,
        runtime=RUNTIME,
        items=items,
        complete=True,
        metadata={"source": "openscience.session.messages", "items": len(items)},
    )


def timeline_items(
    messages: Sequence[Mapping[str, Any]],
    *,
    session_id: str,
    external_session_id: str | None,
) -> tuple[RuntimeTimelineItem, ...]:
    """Project the durable transcript, one platform item per native part.

    Ordering follows the transcript: message order, then part order. Item ids
    are derived from native part ids so a later refresh updates the same
    platform item instead of appending a second copy of the same text.
    """

    output: list[RuntimeTimelineItem] = []
    order = 0
    for raw in messages:
        message = _mapping(raw, "message")
        info = _mapping(message.get("info"), "message info")
        parts = message.get("parts")
        if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
            raise TypeError("OpenScience message parts must be an array")
        role = _optional_string(info.get("role")) or "assistant"
        message_id = _required_string(info.get("id"), "message id")
        turn_id = _turn_id(info, role, message_id)
        streaming = role == "assistant" and not _is_completed(info)
        for part in parts:
            item = _part_item(
                _mapping(part, "part"),
                role=role,
                message_id=message_id,
                turn_id=turn_id,
                streaming=streaming,
                external_session_id=external_session_id,
            )
            if item is None:
                continue
            order += 1
            output.append(item.to_platform_item(session_id=session_id, order_seq=order))
        error = info.get("error")
        if role == "assistant" and isinstance(error, Mapping):
            order += 1
            output.append(
                SystemTimelineItem(
                    id=f"os_error_{message_id}",
                    type="system",
                    status="failed",
                    role="system",
                    turn_id=turn_id,
                    content=ErrorSystemContent(
                        message=_optional_string(error.get("message")) or "run failed",
                        metadata={"name": _optional_string(error.get("name"))},
                    ),
                    source=_source(
                        external_session_id,
                        turn_id=turn_id,
                        native_item_id=message_id,
                        native_item_type="error",
                        event="session.message.error",
                    ),
                ).to_platform_item(session_id=session_id, order_seq=order)
            )
    return tuple(output)


def _part_item(
    part: Mapping[str, Any],
    *,
    role: str,
    message_id: str,
    turn_id: str,
    streaming: bool,
    external_session_id: str | None,
) -> PlatformTimelineItem | None:
    part_type = _optional_string(part.get("type"))
    part_id = _required_string(part.get("id"), "part id")
    source = _source(
        external_session_id,
        turn_id=turn_id,
        native_item_id=part_id,
        native_item_type=part_type,
        event=f"session.message.{part_type}",
    )
    if part_type == "text":
        if part.get("synthetic") is True or part.get("ignored") is True:
            # Synthetic text is the runtime's own scaffolding, not the conversation.
            return None
        text = part.get("text")
        if not isinstance(text, str):
            return None
        return MessageTimelineItem(
            id=f"os_msg_{part_id}",
            type="message",
            status=_part_status(part, streaming),
            role=cast(TimelineRole, role if role in ("user", "assistant") else "assistant"),
            turn_id=turn_id,
            content=MarkdownMessageContent(text=text, metadata={"messageId": message_id}),
            source=source,
        )
    if part_type == "reasoning":
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return None
        return SystemTimelineItem(
            id=f"os_reason_{part_id}",
            type="system",
            status=_part_status(part, streaming),
            role="assistant",
            turn_id=turn_id,
            content=ReasoningSystemContent(text=text, metadata={"messageId": message_id}),
            source=source,
        )
    if part_type == "tool":
        return _tool_item(part, part_id=part_id, turn_id=turn_id, source=source)
    if part_type == "file":
        return _file_item(part, part_id=part_id, turn_id=turn_id, source=source)
    if part_type == "compaction":
        return MarkerTimelineItem(
            id=f"os_compact_{part_id}",
            type="marker",
            status="done",
            turn_id=turn_id,
            content=CompactMarkerContent(
                label="Conversation compacted",
                metadata={"automatic": part.get("auto") is True},
            ),
            source=source,
        )
    if part_type == "retry":
        return SystemTimelineItem(
            id=f"os_retry_{part_id}",
            type="system",
            status="done",
            role="system",
            turn_id=turn_id,
            content=GenericSystemContent(
                text="Provider request retried",
                severity="warning",
                metadata={"attempt": part.get("attempt")},
            ),
            source=source,
        )
    # step-start, step-finish, snapshot, patch, agent, conversation and subtask
    # parts describe runtime bookkeeping that the platform already derives from
    # the surrounding items, so they are deliberately not projected.
    return None


def _tool_item(
    part: Mapping[str, Any],
    *,
    part_id: str,
    turn_id: str,
    source: TimelineSource,
) -> ToolTimelineItem | None:
    state = part.get("state")
    if not isinstance(state, Mapping):
        return None
    status = _optional_string(state.get("status")) or "pending"
    tool_name = _optional_string(part.get("tool")) or "tool"
    tool_input = state.get("input") if isinstance(state.get("input"), Mapping) else {}
    call = _tool_content(tool_name, dict(tool_input))
    if status in ("completed", "error"):
        call = complete_tool_content(
            call,
            output=state.get("output") if status == "completed" else state.get("error"),
            result={
                "status": status,
                "title": _optional_string(state.get("title")),
                "callID": _optional_string(part.get("callID")),
            },
            is_error=status == "error",
        )
    return ToolTimelineItem(
        id=f"os_tool_{_optional_string(part.get('callID')) or part_id}",
        type="tool",
        status={"pending": "pending", "running": "running", "completed": "done", "error": "failed"}.get(
            status, "running"
        ),
        role="tool",
        turn_id=turn_id,
        content=call,
        source=source,
    )


def _tool_content(tool_name: str, tool_input: dict[str, Any]) -> ToolTimelineContent:
    normalized = tool_name.casefold()
    if normalized in _SHELL_TOOLS and isinstance(tool_input.get("command"), str):
        return CommandToolContent(
            command=tool_input["command"],
            input=tool_input,
            title=tool_name,
            metadata={"toolName": tool_name},
        )
    if normalized in _FILE_TOOLS:
        return FileChangeToolContent(
            input=tool_input,
            title=tool_name,
            metadata={"toolName": tool_name},
        )
    return ToolCallContent(title=tool_name, input=tool_input, metadata={"toolName": tool_name})


def _file_item(
    part: Mapping[str, Any],
    *,
    part_id: str,
    turn_id: str,
    source: TimelineSource,
) -> ArtifactTimelineItem | None:
    mime = _optional_string(part.get("mime"))
    filename = _optional_string(part.get("filename"))
    if mime is None and filename is None:
        return None
    return ArtifactTimelineItem(
        id=f"os_file_{part_id}",
        type="artifact",
        status="done",
        turn_id=turn_id,
        content=FileArtifactContent(
            path=filename,
            action="attached",
            metadata={"mediaType": mime, "name": filename},
        ),
        source=source,
    )


def permission_notice(
    request: Mapping[str, Any],
    *,
    session_id: str,
    external_session_id: str | None,
) -> SessionNotice:
    """Map a live permission request onto an approval interaction."""

    data = _mapping(request, "permission request")
    request_id = _required_string(data.get("id"), "permission id")
    permission = _optional_string(data.get("permission")) or "permission"
    patterns = _string_list(data.get("patterns"))
    always = _string_list(data.get("always"))
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    actions: list[Mapping[str, Any]] = [
        {"actionId": "once", "label": "允许一次", "style": "primary"},
        {"actionId": "session", "label": "本会话内允许", "style": "secondary"},
    ]
    if always:
        actions.append({"actionId": "always", "label": "始终允许", "style": "secondary"})
    if permission == "external_directory":
        actions.append({"actionId": "project", "label": "本项目内允许", "style": "secondary"})
    actions.append({"actionId": "reject", "label": "拒绝", "style": "danger"})
    return SessionNotice(
        notice_id=f"{PERMISSION_NOTICE_PREFIX}{request_id}",
        session_id=session_id,
        runtime=RUNTIME,
        type="interaction",
        title="OpenScience 请求授权",
        message=_permission_summary(permission, patterns, metadata),
        severity="warning",
        status="open",
        interaction_type="approval",
        blocking={"scope": "session", "targetId": session_id},
        response_required=True,
        actions=tuple(actions),
        source={
            "component": "openscience.permission",
            "permission": permission,
            **({"sessionId": external_session_id} if external_session_id else {}),
        },
        context={
            "approvalId": request_id,
            "approvalStatus": "pending",
            "kind": "permission",
            "permission": permission,
            "patterns": patterns,
            "always": always,
            "tool": dict(data["tool"]) if isinstance(data.get("tool"), Mapping) else {},
            "toolMetadata": dict(metadata),
            **({"sessionId": external_session_id} if external_session_id else {}),
        },
        metadata={"source": "openscience.runtime.snapshot"},
    )


def question_notice(
    request: Mapping[str, Any],
    *,
    session_id: str,
    external_session_id: str | None,
) -> SessionNotice:
    """Map a live question request onto an input-request interaction."""

    data = _mapping(request, "question request")
    request_id = _required_string(data.get("id"), "question id")
    form = question_form(data)
    first = form.questions[0]
    return SessionNotice(
        notice_id=f"{QUESTION_NOTICE_PREFIX}{request_id}",
        session_id=session_id,
        runtime=RUNTIME,
        type="interaction",
        title="OpenScience 需要你的输入",
        message=first.prompt,
        severity="info",
        status="open",
        interaction_type="input_request",
        blocking={"scope": "session", "targetId": session_id},
        response_required=True,
        actions=(
            form.action(label="提交"),
            {"actionId": "cancel", "label": "取消", "style": "secondary"},
        ),
        source={
            "component": "openscience.question",
            **({"sessionId": external_session_id} if external_session_id else {}),
        },
        context={
            "inputStatus": "pending",
            "requestKind": "questionnaire",
            "questions": [_mapping(item, "question") for item in data.get("questions", []) if isinstance(item, Mapping)],
            "tool": dict(data["tool"]) if isinstance(data.get("tool"), Mapping) else {},
            **({"sessionId": external_session_id} if external_session_id else {}),
        },
        metadata={"source": "openscience.runtime.snapshot"},
    )


def notices(
    snapshot: Mapping[str, Any],
    *,
    session_id: str,
    external_session_id: str | None,
) -> tuple[SessionNotice, ...]:
    """Map every decision the connected server still holds for one session."""

    data = _mapping(snapshot, "snapshot")
    output: list[SessionNotice] = []
    for request in _request_list(data.get("permissions")):
        output.append(
            permission_notice(
                request, session_id=session_id, external_session_id=external_session_id
            )
        )
    for request in _request_list(data.get("questions")):
        output.append(
            question_notice(
                request, session_id=session_id, external_session_id=external_session_id
            )
        )
    return tuple(output)


def question_form(request: Mapping[str, Any]) -> InputRequestForm:
    """Build the platform form the question interaction is rendered from.

    Option ids are positional because OpenScience options carry only labels; the
    labels are what the decision endpoint expects back, so the ids never leave
    the connector.
    """

    data = _mapping(request, "question request")
    raw_questions = data.get("questions")
    if not isinstance(raw_questions, Sequence) or isinstance(raw_questions, (str, bytes)) or not raw_questions:
        raise InputRequestValidationError("question request must contain questions")
    questions: list[InputRequestQuestion] = []
    for index, raw in enumerate(raw_questions):
        item = _mapping(raw, "question")
        prompt = _required_string(item.get("question"), "question text")
        raw_options = item.get("options")
        options: list[InputRequestOption] = []
        if isinstance(raw_options, Sequence) and not isinstance(raw_options, (str, bytes)):
            for option_index, raw_option in enumerate(raw_options):
                option = _mapping(raw_option, "question option")
                options.append(
                    InputRequestOption(
                        option_id=f"o_{option_index}",
                        label=_required_string(option.get("label"), "option label"),
                        description=_optional_string(option.get("description")),
                    )
                )
        questions.append(
            InputRequestQuestion(
                question_id=f"q_{index}",
                header=_optional_string(item.get("header")),
                prompt=prompt,
                options=tuple(options),
                multiple=item.get("multiple") is True,
                allow_custom=item.get("custom") is not False,
            )
        )
    return InputRequestForm(questions=tuple(questions))


def question_answers(
    request: Mapping[str, Any],
    input_data: Mapping[str, Any] | None,
) -> list[list[str]]:
    """Convert submitted form answers back into the labels the server expects."""

    form = question_form(request)
    parsed = form.parse_answers(input_data)
    labels: list[list[str]] = []
    for question in form.questions:
        answer = parsed[question.question_id]
        option_labels = {option.option_id: option.label for option in question.options}
        values = [option_labels[option_id] for option_id in answer.option_ids]
        if answer.custom_text:
            values.append(answer.custom_text)
        labels.append(values)
    return labels


def interaction_target(notice_id: str) -> tuple[str, str] | None:
    """Recover the native decision kind and request id from a notice id."""

    if notice_id.startswith(PERMISSION_NOTICE_PREFIX):
        return "permission", notice_id[len(PERMISSION_NOTICE_PREFIX):]
    if notice_id.startswith(QUESTION_NOTICE_PREFIX):
        return "question", notice_id[len(QUESTION_NOTICE_PREFIX):]
    return None


def permission_reply(action_id: str) -> str:
    """Map a platform action id onto the permission reply enum.

    Anything the platform sends that is not a grant is a rejection: silently
    turning an unknown action into a grant would widen authority.
    """

    return action_id if action_id in _PERMISSION_REPLIES else "reject"


def _request_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _permission_summary(
    permission: str, patterns: list[str], metadata: Mapping[str, Any]
) -> str:
    shell = metadata.get("shell")
    if isinstance(shell, Mapping) and isinstance(shell.get("command"), str):
        return shell["command"]
    filesystem = metadata.get("filesystem")
    if isinstance(filesystem, Mapping) and isinstance(filesystem.get("path"), str):
        return f"{permission}: {filesystem['path']}"
    network = metadata.get("network")
    if isinstance(network, Mapping) and isinstance(network.get("host"), str):
        return f"{permission}: {network['host']}"
    if patterns:
        return f"{permission}: {', '.join(patterns)}"
    return permission


def _source(
    external_session_id: str | None,
    *,
    turn_id: str | None,
    native_item_id: str,
    native_item_type: str | None,
    event: str,
) -> TimelineSource:
    return TimelineSource(
        runtime=RUNTIME,
        external_session_id=external_session_id,
        turn_id=turn_id,
        native_item_id=native_item_id,
        native_item_type=native_item_type,
        event=event,
    )


def _turn_id(info: Mapping[str, Any], role: str, message_id: str) -> str:
    if role == "assistant":
        parent = _optional_string(info.get("parentID"))
        if parent:
            return parent
    return message_id


def _part_status(part: Mapping[str, Any], streaming: bool) -> str:
    time = part.get("time")
    if isinstance(time, Mapping) and type(time.get("end")) is int:
        return "done"
    return "inProgress" if streaming else "done"


def _is_completed(info: Mapping[str, Any]) -> bool:
    time = info.get("time")
    return isinstance(time, Mapping) and type(time.get("completed")) is int


def _iso_from_millis(value: Any) -> str | None:
    if type(value) is not int:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"OpenScience {label} must be an object")
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"OpenScience {label} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str)]
