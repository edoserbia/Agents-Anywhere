"""Attach the Connector to an OpenScience server the user already started.

There is no spawn path here on purpose. OpenScience owns its own process,
project and data root, and a second server started behind the user's back would
be a different project with a different transcript. ``start()`` therefore only
discovers and connects; when nothing is listening it reports a retryable
``starting`` state carrying the discovery reason and keeps looking.

Two server-side properties decide the rest of the design:

* ``crashRecovery`` is ``interrupt``. A run whose owning process died is never
  resumed, so an unfinished receipt is reported as unfinished and never as
  "still working";
* ``decisionScope`` is ``connected_runtime``. A permission or question can only
  be answered by the process that holds its continuation, so a decision is
  always checked against a fresh snapshot before it is sent — replayed events
  are history, not pending work.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from time import monotonic
from typing import Any
from uuid import uuid4

from connector.logging import logger
from connector.runtime_protocol import (
    AgentRuntime,
    RuntimeAttachment,
    RuntimeCapability,
    RuntimeCapabilitySet,
    RuntimeConfig,
    RuntimeConflictError,
    RuntimeIdentity,
    RuntimeInvalidRequestError,
    RuntimeModelCatalog,
    RuntimeOperationResult,
    RuntimeProject,
    RuntimeTimelineSnapshot,
    RuntimeUnavailableError,
    RuntimeUnsupportedError,
    RuntimeUpstreamError,
    SessionMeta,
    SessionNotice,
    SessionSourceObservation,
    SessionState,
)
from connector.runtime_protocol.host import RuntimeHostClient
from connector.runtimes.catalog_revisions import runtime_catalog_revision
from connector.runtimes.openscience import discovery, models, provider_config
from connector.runtimes.openscience.client import (
    UNSCOPED,
    OpenScienceClient,
    OpenScienceConnectionError,
    OpenScienceCursorError,
    OpenScienceEvent,
    OpenScienceEventGapError,
    OpenScienceHTTPError,
    OpenScienceProtocolError,
    ProjectScope,
)
from connector.runtimes.session_identity import stable_runtime_session_id

RUNTIME = "openscience"

# How often the session inventory is re-read. Cheap and local, and it is what
# notices a server that went away between two event frames.
INVENTORY_INTERVAL_SECONDS = 5.0
# The relay's scheduler tick: shorter than the inventory so streamed text
# reaches the platform promptly.
RELAY_TICK_SECONDS = 0.25
# One subscription per session, because the journal is sequenced per session.
# Past this many live sessions the remainder is polled on the inventory tick so
# a large workbench cannot exhaust sockets.
MAX_SESSION_STREAMS = 24
# A streamed transcript is re-read at most this often; the event journal carries
# deltas, the durable transcript is the source of truth.
TIMELINE_REFRESH_SECONDS = 1.0
# Cursor writes are coalesced: the cost of a lost second is replaying a few
# events, which deduplication already handles.
CURSOR_FLUSH_SECONDS = 1.0
STREAM_RETRY_SECONDS = 2.0
MAX_STREAM_RETRY_SECONDS = 30.0
# Consecutive inventory failures before the runtime re-probes for a server that
# may have moved to another port.
INVENTORY_FAILURES_BEFORE_REPROBE = 3
MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
# The model catalog belongs to the server's configuration, not to this
# connector, and it is read for every selection a turn carries. It is cached
# briefly so a prompt does not pay for an 87 KB catalog read, and re-read
# whenever a selection does not resolve against the cached copy.
MODEL_CATALOG_CACHE_SECONDS = 30.0
# Folded into the catalog revision alongside the config revision, the way the
# other runtimes version a catalog that is not derived from config alone.
OPENSCIENCE_MODEL_CATALOG_STATIC_REVISION = 1

_ATTACHMENT_ID = re.compile(r"file_[\w-]{1,128}")
_TERMINAL_RUN_EVENTS = {
    "runtime.completed": "completed",
    "runtime.failed": "failed",
    "runtime.cancelled": "cancelled",
}
_RUN_EVENTS = {"runtime.accepted", "runtime.completed", "runtime.failed", "runtime.cancelled"}
# A terminal receipt is reported with the outcome names the platform accepts.
_TURN_OUTCOMES = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}
_DECISION_EVENTS = {
    "permission.asked",
    "permission.replied",
    "question.asked",
    "question.replied",
    "question.rejected",
}
# Selection scopes this runtime understands. `model` carries the provider,
# model and reasoning variant the platform's picker produced; `effort` is
# accepted on its own so a caller that knows only the research effort can still
# set it, independently of any model.
_SELECTION_SCOPES = frozenset({"model", "effort"})

ClientFactory = Callable[[str, Mapping[str, Any]], OpenScienceClient]
Prober = Callable[[dict[str, Any]], Awaitable[discovery.OpenScienceDiscovery]]


def runtime_capabilities() -> dict[str, bool]:
    """Capabilities this adapter actually implements.

    The model catalog is served from OpenScience's own ``/config/providers``,
    and each model's reasoning levels are that catalog's reasoning items, so
    ``modelCatalog`` also publishes ``catalog.effort``. Permissions, commands
    and steering are absent rather than false-by-omission: the platform reads
    these keys to decide which affordances to offer, and advertising one this
    runtime cannot serve is worse than omitting it.

    ``createProject`` says this runtime generates project directories itself, so
    a client must not ask the user for one — it is the flag the create-project
    form reads to hide its path field.
    """

    return {
        "modelCatalog": True,
        "permissionCatalog": False,
        "sessionDiscovery": True,
        "sessionSnapshot": True,
        "sessionState": True,
        "sessionNotices": True,
        "createAndStartSession": True,
        "createProject": True,
        "startTurn": True,
        "steerTurn": False,
        "interruptTurn": True,
        "commands": False,
        "interactions": True,
        "attachments": True,
        "ipc": True,
    }


def _default_client_factory(base_url: str, values: Mapping[str, Any]) -> OpenScienceClient:
    return OpenScienceClient(base_url=base_url, values=values)


class OpenScienceRuntime(AgentRuntime):
    """Protocol adapter for one attached OpenScience server."""

    def __init__(
        self,
        config: RuntimeConfig,
        host: RuntimeHostClient,
        *,
        prober: Prober | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self._prober = prober or discovery.probe
        self._client_factory = client_factory or _default_client_factory
        self._identity = RuntimeIdentity(RUNTIME, "unknown", "OpenScience")
        self._client: OpenScienceClient | None = None
        self._relay: OpenScienceRelay | None = None
        self._connect_lock = asyncio.Lock()
        self._stopping = False
        self._restart_task: asyncio.Task[None] | None = None
        self._unavailable_reason: str | None = None
        # Native session id -> platform session id, and back. The platform's own
        # id wins for sessions it created, so a session is never published twice
        # under two identities.
        self._sessions: dict[str, str] = {}
        self._platform_ids: dict[str, str] = {}
        # Native session id -> the project directory it lives in. Every
        # per-session route is scoped by that directory, and the server answers
        # 404 for a real session addressed from the wrong project, so it is
        # remembered from the inventory entry that discovered the session.
        self._directories: dict[str, str] = {}
        self._record_cache: dict[str, dict[str, Any]] = {}
        # Platform session id -> the model/variant/effort selection the platform
        # made. The platform sends a selection once (`session.selections.update`)
        # and not with every message, so the runtime is what keeps it applied; it
        # is republished with every state update so a refresh cannot drop it.
        self._selections: dict[str, dict[str, str | None]] = {}
        # (monotonic read time, catalog) for the server-owned model catalog.
        self._model_catalog_cache: tuple[float, RuntimeModelCatalog] | None = None

    @property
    def identity(self) -> RuntimeIdentity:
        return self._identity

    @property
    def sync_mode(self) -> str:
        return "events"

    async def start(self) -> None:
        self._stopping = False
        await self.host.runtime_health_update(
            "starting",
            {
                "code": "runtime_initializing",
                "message": "正在连接 OpenScience 并同步会话…",
                "retryable": True,
            },
        )
        try:
            await self._ensure_client()
        except RuntimeUnsupportedError:
            # A protocol this adapter does not speak is not a transient outage.
            with suppress(Exception):
                await self.host.runtime_health_update(
                    "error",
                    {
                        "code": "runtime_protocol_unsupported",
                        "message": "OpenScience 运行时协议版本不受支持，请升级 Agents Anywhere。",
                        "retryable": False,
                    },
                )
        except (OpenScienceConnectionError, OpenScienceProtocolError, OSError, RuntimeError, ValueError):
            with suppress(Exception):
                await self.host.runtime_health_update(
                    "starting",
                    {
                        "code": "runtime_unavailable",
                        "message": self._unavailable_reason or discovery.UNAVAILABLE_REASON,
                        "retryable": True,
                    },
                )
            self._schedule_restart()

    async def stop(self) -> None:
        self._stopping = True
        relay, self._relay = self._relay, None
        if relay is not None:
            await relay.close()
        if self._restart_task is not None:
            self._restart_task.cancel()
            await asyncio.gather(self._restart_task, return_exceptions=True)
            self._restart_task = None
        async with self._connect_lock:
            client, self._client = self._client, None
            if client is not None:
                await client.close()

    async def get_config(self) -> RuntimeConfig:
        return self.config

    async def get_runtime_capabilities(self) -> RuntimeCapabilitySet:
        return self._capability_set()

    async def list_model_catalog(
        self,
        query: str | None = None,
        limit: int = 100,
    ) -> RuntimeModelCatalog:
        """Publish the models OpenScience is configured with.

        The catalog is read from the server rather than declared here: the
        models, their display names, which one each provider defaults to and
        which of them reason are all OpenScience's configuration. Only
        filtering and the page limit are applied locally.
        """

        catalog = await self._model_catalog()
        selected = catalog.models
        if query:
            lowered = query.casefold()
            selected = tuple(
                model
                for model in selected
                if lowered in model.id.casefold()
                or lowered in model.title.casefold()
                or any(
                    lowered in str(model.metadata.get(key, "")).casefold()
                    for key in ("providerID", "providerName", "modelID")
                )
            )
        return RuntimeModelCatalog(
            runtime=RUNTIME,
            revision=catalog.revision,
            models=selected[: max(0, limit)],
        )

    async def create_project(
        self,
        name: str,
        sources: Sequence[Mapping[str, Any]] | None = None,
        operation_id: str | None = None,
    ) -> RuntimeProject:
        """Create a project and report the directory OpenScience generated.

        The path is never proposed here: OpenScience owns project identity and
        answers with the worktree it chose, which the caller stores as the
        project's workspace. A retryable caller passes its own ``operation_id``
        — the server replays the original project for the same name and sources
        rather than creating a second one, which matters because OpenScience
        has no delete-project route.
        """

        client = await self._ensure_client()

        async def create() -> RuntimeProject:
            payload = await client.create_project(
                name,
                sources=sources,
                operation_id=operation_id or str(uuid4()),
            )
            # The mapping runs inside the call so a server answer that cannot be
            # read as a project fails the same way a transport failure does.
            return models.created_project(payload, name=name)

        return await self._call(create, "project.create")

    async def list_sessions(
        self,
        limit: int = 100,
        cursor: str | None = None,
        force: bool = False,
    ) -> tuple[SessionMeta, ...]:
        """List every project's sessions. The route is not paginated."""

        _ = cursor, force
        client = await self._ensure_client()
        sessions = await self._call(
            lambda: self._list_all_sessions(client), "session.list"
        )
        return self._session_metas(sessions)[: max(0, limit)]

    async def list_complete_session_inventory(
        self,
        page_size: int = 100,
        force: bool = False,
    ) -> tuple[SessionMeta, ...]:
        _ = page_size, force
        client = await self._ensure_client()
        sessions = await self._call(
            lambda: self._list_all_sessions(client), "session.list"
        )
        return self._session_metas(sessions)

    async def _project_scopes(self, client: OpenScienceClient) -> list[ProjectScope]:
        """Every project the inventory has to read, the unscoped one first.

        The server scopes ``/session`` to one project, and its own
        working-directory project is absent from ``/project`` because it was
        never created through the app — so the inventory is the union of an
        unscoped listing and one listing per managed project. A catalog that
        cannot be read degrades to the unscoped listing instead of losing the
        whole inventory.
        """

        scopes: list[ProjectScope] = [UNSCOPED]
        try:
            projects = await client.list_projects()
        except Exception as error:  # noqa: BLE001 - the catalog is best effort
            logger.warning(
                "OpenScience project catalog failed error_type={}", type(error).__name__
            )
            return scopes
        for project in projects:
            worktree = project.get("worktree")
            if isinstance(worktree, str) and worktree and worktree not in scopes:
                scopes.append(worktree)
        return scopes

    async def _list_all_sessions(self, client: OpenScienceClient) -> list[dict[str, Any]]:
        """Read every project's sessions, isolating a project that fails.

        One unreadable project — removed mid-scan, or a directory the server no
        longer resolves — must not hide the others, exactly like one unreadable
        session in the relay. Only when no scope answers at all is the server
        itself considered gone, because then nothing can be trusted.
        """

        listed: list[dict[str, Any]] = []
        seen: set[str] = set()
        answered = False
        for scope in await self._project_scopes(client):
            try:
                payloads = await client.list_sessions(directory=scope)
            except Exception as error:  # noqa: BLE001 - isolate one unreadable project
                logger.warning(
                    "OpenScience project listing failed directory={} error_type={}",
                    scope if isinstance(scope, str) else "<server default>",
                    type(error).__name__,
                )
                continue
            answered = True
            for payload in payloads:
                external = payload.get("id")
                if not isinstance(external, str) or not external or external in seen:
                    continue
                seen.add(external)
                listed.append(payload)
        if not answered:
            raise OpenScienceConnectionError(
                "OpenScience session inventory is unreachable"
            )
        return listed

    async def get_session_snapshot(
        self,
        session_id: str,
        external_session_id: str | None = None,
        limit: int | None = None,
    ) -> RuntimeTimelineSnapshot:
        external = self._external_session(session_id, external_session_id)
        client = await self._ensure_client()
        messages = await self._call(
            lambda: client.messages(
                external, limit=limit, directory=self._directory_for(external)
            ),
            "session.messages",
        )
        return models.timeline_snapshot(
            messages, session_id=session_id, external_session_id=external
        )

    async def get_session_state(
        self,
        session_id: str,
        external_session_id: str | None = None,
    ) -> SessionState:
        external = self._external_session(session_id, external_session_id)
        snapshot = await self._snapshot(external)
        return models.session_state(
            snapshot, session_id=session_id, external_session_id=external
        )

    async def get_session_notices(
        self,
        session_id: str,
        external_session_id: str | None = None,
    ) -> tuple[SessionNotice, ...]:
        external = self._external_session(session_id, external_session_id)
        snapshot = await self._snapshot(external)
        return models.notices(
            snapshot, session_id=session_id, external_session_id=external
        )

    async def create_and_start_session(
        self,
        session_id: str,
        content: str,
        title: str | None = None,
        cwd: str | None = None,
        selections: Mapping[str, str | None] | None = None,
        attachments: tuple[RuntimeAttachment, ...] = (),
        client_message_id: str | None = None,
        runtime_options: Mapping[str, Any] | None = None,
    ) -> RuntimeOperationResult:
        if runtime_options:
            raise RuntimeUnsupportedError("runtimeOptions")
        if not content.strip() and not attachments:
            raise RuntimeInvalidRequestError("a message or an attachment is required")
        client = await self._ensure_client()
        scope = await self._session_scope(client, cwd)
        created = await self._call(
            lambda: client.create_session(
                title=title,
                workspace=self._workspace(scope),
                directory=scope,
            ),
            "session.create",
        )
        external = _required_string(created.get("id"), "created session id")
        # The platform already chose the identity for this session, so that id
        # is kept instead of deriving a second one from the native id.
        platform = self._register_session(external, session_id=session_id)
        await self._publish_meta(external, created)
        receipt = await self._submit(
            platform,
            external,
            content,
            client_message_id,
            attachments,
            selections,
        )
        return RuntimeOperationResult(
            result={
                "sessionId": platform,
                "externalSessionId": external,
                "runID": receipt.get("runID"),
                "acceptedAt": receipt.get("acceptedAt"),
            }
        )

    async def start_turn(
        self,
        session_id: str,
        external_session_id: str | None,
        content: str,
        selections: Mapping[str, str | None] | None = None,
        attachments: tuple[RuntimeAttachment, ...] = (),
        client_message_id: str | None = None,
        cwd: str | None = None,
    ) -> RuntimeOperationResult:
        _ = cwd
        if not content.strip() and not attachments:
            raise RuntimeInvalidRequestError("a message or an attachment is required")
        external = self._external_session(session_id, external_session_id)
        receipt = await self._submit(
            session_id,
            external,
            content,
            client_message_id,
            attachments,
            selections,
        )
        return RuntimeOperationResult(
            result={
                "sessionId": session_id,
                "externalSessionId": external,
                "runID": receipt.get("runID"),
                "acceptedAt": receipt.get("acceptedAt"),
            }
        )

    async def update_session_selections(
        self,
        session_id: str,
        external_session_id: str | None,
        selections: Mapping[str, str | None],
    ) -> RuntimeOperationResult:
        """Validate and remember the model, reasoning variant and effort.

        A selection is only accepted once it resolves against the catalog the
        server itself publishes, so the platform can never pin a model — or a
        reasoning level — OpenScience does not have. The choice is kept here
        because the platform sends it once and not with every message, and it
        is republished with every state update so a refresh cannot drop it.
        """

        if not selections:
            return RuntimeOperationResult(
                ok=False,
                code="openscience_empty_selection_update",
                message="At least one selection scope is required.",
                result={
                    "sessionId": session_id,
                    "externalSessionId": external_session_id,
                },
            )
        unsupported = set(selections) - _SELECTION_SCOPES
        if unsupported:
            return RuntimeOperationResult(
                ok=False,
                code="openscience_unsupported_selection_scope",
                message=f"Unsupported OpenScience selection scope: {min(unsupported)}",
                result={
                    "sessionId": session_id,
                    "externalSessionId": external_session_id,
                    "selections": dict(selections),
                },
            )
        effective = self._effective_selections(session_id, selections)
        try:
            await self._selection_route(effective)
        except RuntimeInvalidRequestError as error:
            return RuntimeOperationResult(
                ok=False,
                code="openscience_invalid_selection",
                message=str(error),
                result={
                    "sessionId": session_id,
                    "externalSessionId": external_session_id,
                    "selections": effective,
                },
            )
        self._selections[session_id] = effective
        return RuntimeOperationResult(
            result={
                "updated": True,
                "sessionId": session_id,
                "externalSessionId": external_session_id,
                "selections": effective,
            }
        )

    async def interrupt_session(
        self,
        session_id: str,
        reason: str | None = None,
    ) -> RuntimeOperationResult:
        _ = reason
        external = self._external_session(session_id, None)
        snapshot = await self._snapshot(external)
        run = models.latest_run(snapshot)
        run_id = _optional_string(run.get("runID")) if run is not None else None
        state = _optional_string(run.get("state")) if run is not None else None
        if run_id is None or state not in ("accepted", "running"):
            return RuntimeOperationResult(
                ok=True,
                code="openscience_run_not_active",
                message="OpenScience 当前没有正在运行的任务。",
                result={"sessionId": session_id, "externalSessionId": external},
            )
        client = await self._ensure_client()
        cancelled = await self._call(
            lambda: client.cancel_run(
                external, run_id, directory=self._directory_for(external)
            ),
            "runtime.cancel",
        )
        return RuntimeOperationResult(
            result={
                "sessionId": session_id,
                "externalSessionId": external,
                "runID": run_id,
                "state": cancelled.get("state"),
            }
        )

    async def respond_interaction(
        self,
        session_id: str,
        notice_id: str,
        action_id: str,
        input_data: Mapping[str, Any] | None = None,
    ) -> RuntimeOperationResult:
        """Answer a live decision, after confirming the server still holds it.

        Only a fresh snapshot can say whether a continuation is still pending:
        the request may have been cancelled or already resolved elsewhere.
        """

        target = models.interaction_target(notice_id)
        if target is None:
            return RuntimeOperationResult(
                ok=False,
                code="openscience_notice_not_found",
                message="OpenScience 交互请求不存在。",
            )
        kind, request_id = target
        external = self._external_session(session_id, None)
        snapshot = await self._snapshot(external)
        pending = _pending_request(snapshot, kind, request_id)
        if pending is None:
            return RuntimeOperationResult(
                ok=False,
                code="openscience_notice_not_pending",
                message="该请求已结束或不再等待回复，请刷新会话。",
            )
        client = await self._ensure_client()
        directory = self._directory_for(external)
        if kind == "permission":
            reply = models.permission_reply(action_id)
            await self._call(
                lambda: client.reply_permission(
                    external, request_id, reply, directory=directory
                ),
                "runtime.decision",
            )
            return RuntimeOperationResult(
                result={"requestId": request_id, "reply": reply, "kind": kind}
            )
        if action_id == "cancel":
            await self._call(
                lambda: client.reject_question(
                    external, request_id, directory=directory
                ),
                "runtime.decision",
            )
            return RuntimeOperationResult(result={"requestId": request_id, "kind": kind})
        try:
            answers = models.question_answers(pending, input_data)
        except ValueError as error:
            raise RuntimeInvalidRequestError(str(error)) from error
        await self._call(
            lambda: client.reply_question(
                external, request_id, answers, directory=directory
            ),
            "runtime.decision",
        )
        return RuntimeOperationResult(
            result={"requestId": request_id, "kind": kind, "answers": answers}
        )

    async def resynchronize(
        self, session_id: str | None = None, external_session_id: str | None = None
    ) -> None:
        """Recalibrate from the server instead of trusting a stale cursor."""

        relay = self._relay
        if relay is None:
            return
        if session_id is None:
            await relay.resync(None)
            return
        await relay.resync(self._external_session(session_id, external_session_id))

    def _capability_set(self) -> RuntimeCapabilitySet:
        declared = runtime_capabilities()
        return RuntimeCapabilitySet(
            runtime=RUNTIME,
            revision=1,
            capabilities=tuple(
                RuntimeCapability(
                    capability_id=protocol_id,
                    scope="runtime",
                    runtime=RUNTIME,
                    supported=declared[inventory_key],
                    available=declared[inventory_key],
                    allowed=declared[inventory_key],
                    unavailable_reason=(
                        None if declared[inventory_key] else "openscience.capabilities"
                    ),
                    metadata={"inventoryKey": inventory_key},
                )
                for inventory_key, protocol_id in _CAPABILITY_IDS
                if inventory_key in declared
            ),
            metadata={"source": "openscience.runtime.capabilities", **declared},
        )

    def _session_metas(self, sessions: list[dict[str, Any]]) -> tuple[SessionMeta, ...]:
        output: list[SessionMeta] = []
        for payload in sessions:
            external = _required_string(payload.get("id"), "session id")
            platform = self._register_session(
                external, directory=_session_directory(payload)
            )
            output.append(models.session_meta(payload, session_id=platform))
        return tuple(output)

    async def _session_scope(
        self, client: OpenScienceClient, cwd: str | None
    ) -> str | None:
        """The project a new session belongs to, or the configured default.

        The platform passes the workspace path of the project the user picked.
        Without a scope the server files the session under its own
        working-directory project, which is not the one the user was looking
        at, so the session would never appear where they expect it.

        Only a directory the server already manages is accepted: OpenScience
        opens a project for any path it is handed, and a path the platform
        invented would leave an empty project behind that nobody asked for.
        """

        if not isinstance(cwd, str) or not cwd:
            return None
        configured = self.config.values.get("directory")
        if isinstance(configured, str) and cwd == configured:
            return cwd
        return cwd if cwd in await self._project_worktrees(client) else None

    async def _project_worktrees(self, client: OpenScienceClient) -> set[str]:
        """Every worktree OpenScience manages, for placing a new session."""

        try:
            projects = await client.list_projects()
        except (
            OpenScienceConnectionError,
            OpenScienceProtocolError,
            OpenScienceHTTPError,
        ):
            # Placing the session in the server's own project beats refusing to
            # create it because the catalog could not be read this once.
            return set()
        return {
            worktree
            for project in projects
            if isinstance(worktree := project.get("worktree"), str) and worktree
        }

    def _workspace(self, scope: str | None) -> str:
        """Use the project workspace only for a directory the server manages.

        ``project`` makes relative tool paths resolve inside that project;
        anything else stays in the server's isolated scratch workspace, because
        the server cannot honor a directory it does not know.
        """

        return "project" if scope else "isolated"

    def _register_session(
        self,
        external_session_id: str,
        session_id: str | None = None,
        directory: str | None = None,
    ) -> str:
        """Bind a native session id to its platform id and project directory.

        The directory is only ever added, never cleared: an inventory entry
        that does not name one must not forget where an already known session
        lives, or its next per-session call would be sent to the wrong project.
        """

        known = self._platform_ids.get(external_session_id)
        platform = session_id or known or stable_runtime_session_id(
            self.host.connector_id, RUNTIME, external_session_id
        )
        self._sessions[platform] = external_session_id
        self._platform_ids[external_session_id] = platform
        if directory:
            self._directories[external_session_id] = directory
        return platform

    def _directory_for(self, external_session_id: str) -> str | None:
        """The project selector for one session, if the inventory saw one.

        ``None`` keeps the client's configured default, which is the behaviour
        a session addressed before it was ever listed has always had.
        """

        return self._directories.get(external_session_id)

    def _external_session(self, session_id: str, external_session_id: str | None) -> str:
        if external_session_id:
            return external_session_id
        known = self._sessions.get(session_id)
        if known is None:
            raise RuntimeInvalidRequestError("unknown OpenScience session")
        return known

    async def _snapshot(self, external_session_id: str) -> dict[str, Any]:
        client = await self._ensure_client()
        return await self._call(
            lambda: client.snapshot(
                external_session_id, directory=self._directory_for(external_session_id)
            ),
            "runtime.snapshot",
        )

    async def _publish_meta(self, external_session_id: str, payload: Mapping[str, Any]) -> None:
        """Publish one session's identity, and where it came from."""

        platform = self._register_session(
            external_session_id, directory=_session_directory(payload)
        )
        meta = models.session_meta(payload, session_id=platform)
        await self.host.session_meta_upsert(
            session_id=platform,
            runtime=RUNTIME,
            external_session_id=external_session_id,
            title=meta.title,
            cwd=meta.cwd,
            ordering_time=meta.ordering_time,
            metadata=meta.metadata,
        )
        if meta.source_state is not None:
            with suppress(Exception):
                await self.host.session_source_update(
                    SessionSourceObservation(
                        session_id=platform,
                        external_session_id=external_session_id,
                        runtime=RUNTIME,
                        state=meta.source_state,
                    )
                )

    def _effective_selections(
        self,
        session_id: str,
        selections: Mapping[str, str | None] | None,
    ) -> dict[str, str | None]:
        """Merge one update into the session's remembered selection.

        The platform patches single scopes, so an update naming only the model
        must not forget an effort chosen earlier (and the other way round).
        """

        effective = dict(self._selections.get(session_id, {}))
        effective.update(dict(selections or {}))
        return effective

    async def _model_catalog(self, *, force: bool = False) -> RuntimeModelCatalog:
        """Read the server's model catalog, briefly cached.

        The catalog is the server's configuration and only changes when the
        user edits it, so a short cache keeps a prompt from paying for a large
        read while still noticing a change on the next turn.
        """

        cached = self._model_catalog_cache
        now = monotonic()
        if (
            not force
            and cached is not None
            and now - cached[0] < MODEL_CATALOG_CACHE_SECONDS
        ):
            return cached[1]
        client = await self._ensure_client()
        payload = await self._call(client.list_providers, "config.providers")
        catalog = models.model_catalog(
            payload,
            revision=runtime_catalog_revision(
                self.config.revision, OPENSCIENCE_MODEL_CATALOG_STATIC_REVISION
            ),
        )
        self._model_catalog_cache = (now, catalog)
        return catalog

    async def _selection_route(
        self,
        selections: Mapping[str, str | None],
        *,
        force_catalog: bool = False,
    ) -> models.ModelRoute | None:
        """Resolve a platform model selection onto the server's routing.

        A selection the cached catalog does not know is checked once more
        against a fresh read before it is refused: the user may have picked a
        model, or a reasoning level, that appeared since the cache was filled.
        """

        selection_id = _optional_string(selections.get("model"))
        if selection_id is None:
            return None
        route = models.model_route(
            await self._model_catalog(force=force_catalog), selection_id
        )
        if route is None and not force_catalog:
            route = models.model_route(
                await self._model_catalog(force=True), selection_id
            )
        if route is None:
            raise RuntimeInvalidRequestError("unknown OpenScience model selection")
        return route

    async def _submit(
        self,
        session_id: str,
        external_session_id: str,
        content: str,
        client_message_id: str | None,
        attachments: tuple[RuntimeAttachment, ...],
        selections: Mapping[str, str | None] | None,
    ) -> dict[str, Any]:
        """Submit one turn, reusing the persisted requestID on a caller retry.

        The id is written before the prompt is sent, so a retry after an
        ambiguous transport failure recovers the original receipt instead of
        admitting the same message twice.
        """

        if not client_message_id:
            raise RuntimeInvalidRequestError(
                "a stable clientMessageId is required to submit a turn"
            )
        attachment_parts = await self._attachment_parts(session_id, attachments)
        if not content.strip() and not attachment_parts:
            raise RuntimeInvalidRequestError("a message or an attachment is required")
        # The API takes exactly one of `message` or `parts`, so a plain turn
        # stays a message and only a rich turn becomes parts.
        message: str | None
        parts: list[dict[str, Any]] | None
        if attachment_parts:
            parts = (
                [{"type": "text", "text": content}] if content.strip() else []
            ) + attachment_parts
            message = None
        else:
            parts, message = None, content
        # The turn's own selection wins, and it also becomes the session's
        # selection: the platform patches a selection once and then sends
        # messages without one, so a turn that names a model is a change of
        # preference, not a one-off.
        effective = self._effective_selections(session_id, selections)
        route = await self._selection_route(effective)
        if selections:
            self._selections[session_id] = effective
        model = (
            {"providerID": route.provider_id, "modelID": route.model_id}
            if route is not None
            else None
        )
        # The reasoning level the picker resolved, when the selection named one.
        # It indexes that model's own `variants` on the server and is a
        # different axis from `effort` below, so it is never derived from it.
        variant = route.variant if route is not None else None
        # OpenScience requires an effort on every prompt and its own default is
        # "normal" (`resolveResearchEffort`), so an unselected turn keeps
        # sending that default: omitting the field is rejected as invalid input
        # rather than treated as "use your default".
        effort = models.effort_value(effective.get("effort")) or models.DEFAULT_EFFORT
        fingerprint = _prompt_fingerprint(message, parts, model, variant, effort)
        key = _turn_key(external_session_id, client_message_id)
        record = await self._read_record(key)
        if record is not None and record.get("fingerprint") != fingerprint:
            raise RuntimeInvalidRequestError(
                "clientMessageId was already used for a different message"
            )
        request_id = _optional_string(record.get("requestID")) if record else None
        if request_id is None:
            request_id = f"aa-{uuid4().hex}"
            await self._write_record(
                key,
                {
                    "requestID": request_id,
                    "fingerprint": fingerprint,
                    "sessionId": external_session_id,
                    "clientMessageId": client_message_id,
                },
            )
        client = await self._ensure_client()
        receipt = await self._call(
            lambda: client.prompt(
                external_session_id,
                request_id=request_id,
                message=message,
                parts=parts,
                model=model,
                variant=variant,
                effort=effort,
                directory=self._directory_for(external_session_id),
            ),
            "runtime.prompt",
        )
        await self._write_record(
            key,
            {
                "requestID": request_id,
                "fingerprint": fingerprint,
                "sessionId": external_session_id,
                "clientMessageId": client_message_id,
                "runID": _optional_string(receipt.get("runID")),
            },
        )
        return receipt

    async def _attachment_parts(
        self,
        session_id: str,
        attachments: tuple[RuntimeAttachment, ...],
    ) -> list[dict[str, Any]]:
        """Inline uploaded attachments as the rich-input file parts the API takes."""

        if not attachments:
            return []
        seen: set[str] = set()
        total = 0
        parts: list[dict[str, Any]] = []
        for attachment in attachments:
            if not _ATTACHMENT_ID.fullmatch(attachment.file_id) or attachment.file_id in seen:
                raise RuntimeInvalidRequestError("invalid or duplicate attachment")
            seen.add(attachment.file_id)
            downloaded = await self.host.attachment_download(session_id, attachment.file_id)
            content = downloaded.content
            total += len(content)
            if total > MAX_ATTACHMENT_BYTES:
                raise RuntimeInvalidRequestError("attachments exceed the size limit")
            media_type = (downloaded.media_type or attachment.media_type or "application/octet-stream")
            media_type = media_type.split(";", 1)[0].strip().lower()
            if attachment.media_type and media_type != attachment.media_type:
                raise RuntimeInvalidRequestError("downloaded attachment type does not match its upload")
            if attachment.size is not None and len(content) != attachment.size:
                raise RuntimeInvalidRequestError("downloaded attachment size does not match its upload")
            if attachment.sha256 and hashlib.sha256(content).hexdigest() != attachment.sha256:
                raise RuntimeInvalidRequestError("downloaded attachment content does not match its upload")
            name = attachment.name or downloaded.name or attachment.file_id
            parts.append(
                {
                    "type": "file",
                    "mime": media_type,
                    "filename": name,
                    "url": f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}",
                }
            )
        return parts

    async def _read_record(self, key: str) -> dict[str, Any] | None:
        cached = self._record_cache.get(key)
        if cached is not None:
            return cached
        try:
            stored = await self.host.sync_state_read(key)
        except NotImplementedError:
            return None
        except Exception as error:  # noqa: BLE001 - a storage fault must not block a turn
            logger.warning(
                "OpenScience state record read failed error_type={}", type(error).__name__
            )
            return None
        if not isinstance(stored, Mapping):
            return None
        record = dict(stored)
        self._record_cache[key] = record
        return record

    async def _write_record(self, key: str, record: dict[str, Any]) -> None:
        self._record_cache[key] = record
        try:
            await self.host.sync_state_write(key, record)
        except NotImplementedError:
            # Without durable storage the in-process cache still makes a retry
            # from this runtime idempotent, which is the common case.
            logger.warning("OpenScience state records are not durable on this host")
        except Exception as error:  # noqa: BLE001 - never fail a turn over its receipt
            logger.warning(
                "OpenScience turn record write failed error_type={}", type(error).__name__
            )

    async def _call(self, operation: Callable[[], Awaitable[Any]], method: str) -> Any:
        """Run one client call, mapping transport failures onto protocol errors."""

        try:
            return await operation()
        except OpenScienceCursorError as error:
            raise RuntimeConflictError(str(error)) from error
        except OpenScienceHTTPError as error:
            if error.status == 404:
                raise RuntimeInvalidRequestError(f"{method}: not found") from error
            if error.status == 409:
                raise RuntimeConflictError(str(error)) from error
            if error.status == 400:
                raise RuntimeInvalidRequestError(str(error)) from error
            raise RuntimeUpstreamError(str(error)) from error
        except (OpenScienceConnectionError, OSError, TimeoutError) as error:
            logger.warning(
                "OpenScience request failed method={} error_type={}",
                method,
                type(error).__name__,
            )
            raise RuntimeUnavailableError("OpenScience 服务当前不可用。") from error
        except OpenScienceProtocolError as error:
            raise RuntimeUpstreamError(str(error)) from error
        except ValueError as error:
            raise RuntimeUpstreamError(str(error)) from error

    async def _ensure_client(self) -> OpenScienceClient:
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._stopping:
                raise RuntimeUnavailableError("OpenScience runtime is stopping")
            if self._client is not None:
                return self._client
            await self._connect()
            if self._client is None:
                raise RuntimeUnavailableError(
                    self._unavailable_reason or discovery.UNAVAILABLE_REASON
                )
            return self._client

    async def _connect(self) -> None:
        values = provider_config.normalized_config_values(dict(self.config.values))
        result = await self._prober(values)
        if not result.available or result.endpoint is None:
            self._unavailable_reason = result.reason or discovery.UNAVAILABLE_REASON
            raise RuntimeUnavailableError(self._unavailable_reason)
        capabilities = result.capabilities
        if capabilities is None:
            raise RuntimeUnavailableError("OpenScience 没有报告运行时协议能力。")
        _require_protocol(capabilities.protocol_version)
        client = self._client_factory(result.endpoint.base_url, values)
        try:
            live = await client.capabilities()
            version = live.get("protocolVersion")
            if not isinstance(version, str):
                raise OpenScienceProtocolError("runtime capabilities have no protocol version")
            _require_protocol(version)
        except BaseException:
            await client.close()
            raise
        self._unavailable_reason = None
        self._client = client
        self._identity = RuntimeIdentity(
            RUNTIME,
            capabilities.server_version or result.endpoint.version or "unknown",
            "OpenScience",
            protocol_version=version,
            runtime_id=result.endpoint.run_id,
        )
        await self.host.runtime_capabilities_update(self._capability_set())
        relay = OpenScienceRelay(client=client, host=self.host, runtime=self)
        self._relay = relay
        relay.start()

    def _schedule_restart(self) -> None:
        if not self._stopping and (self._restart_task is None or self._restart_task.done()):
            self._restart_task = asyncio.create_task(
                self._restart_loop(), name="openscience-reconnect"
            )

    async def _restart_loop(self) -> None:
        """Keep looking for a server; a terminal server advertises nothing.

        The endpoint is re-probed on every attempt because a desktop sidecar
        picks a new random port each time it starts.
        """

        while not self._stopping:
            await asyncio.sleep(INVENTORY_INTERVAL_SECONDS)
            if self._stopping:
                return
            try:
                await self._ensure_client()
                return
            except RuntimeUnsupportedError:
                return
            except Exception as error:  # noqa: BLE001 - health already carries the reason
                logger.debug(
                    "OpenScience reconnect attempt failed error_type={}",
                    type(error).__name__,
                )
                continue

    async def _handle_disconnect(self) -> None:
        """Drop a server that stopped answering and look for it again."""

        async with self._connect_lock:
            relay, self._relay = self._relay, None
            client, self._client = self._client, None
        if relay is not None:
            await relay.close()
        if client is not None:
            await client.close()
        if self._stopping:
            return
        with suppress(Exception):
            await self.host.runtime_error(
                RUNTIME,
                "OPENSCIENCE_UNAVAILABLE",
                "OpenScience server is no longer reachable",
                details={"retryable": True},
            )
        with suppress(Exception):
            await self.host.runtime_health_update(
                "error",
                {
                    "code": "runtime_unavailable",
                    "message": "OpenScience 已断开，正在等待服务恢复；请确认 OpenScience 仍在运行。",
                    "retryable": True,
                },
            )
        self._schedule_restart()


class OpenScienceRelay:
    """Keep the platform's view of every OpenScience session current.

    One subscription per session, because the journal is sequenced per session.
    A failure inside one session is contained to that stream: the inventory and
    every other session keep updating, and the failed session retries from its
    persisted cursor — or, when that cursor left the retained window, from a
    fresh snapshot. It never re-prompts, because a prompt is work, not a read.
    """

    def __init__(
        self,
        *,
        client: OpenScienceClient,
        host: RuntimeHostClient,
        runtime: OpenScienceRuntime,
        inventory_interval: float = INVENTORY_INTERVAL_SECONDS,
        tick: float = RELAY_TICK_SECONDS,
        max_streams: int = MAX_SESSION_STREAMS,
    ) -> None:
        self.client = client
        self.host = host
        self.runtime = runtime
        self.inventory_interval = inventory_interval
        self.tick = tick
        self.max_streams = max_streams
        self.task: asyncio.Task[None] | None = None
        self._streams: dict[str, asyncio.Task[None]] = {}
        self._cursors: dict[str, int] = {}
        self._cursor_dirty: set[str] = set()
        self._cursor_written: dict[str, float] = {}
        self._dirty_timeline: dict[str, float] = {}
        self._meta_markers: dict[str, int] = {}
        self._stopping = False
        self._announced = False
        self._inventory_failures = 0

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="openscience-relay")

    async def close(self) -> None:
        self._stopping = True
        task = self.task
        self.task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        streams = list(self._streams.values())
        self._streams.clear()
        for stream in streams:
            stream.cancel()
        if streams:
            await asyncio.gather(*streams, return_exceptions=True)
        for external in tuple(self._cursor_dirty):
            with suppress(Exception):
                await self._persist_cursor(external)

    async def resync(self, external_session_id: str | None) -> None:
        """Force a snapshot for one session, or for every known session."""

        targets = (
            [external_session_id]
            if external_session_id is not None
            else list(self._streams)
        )
        for external in targets:
            if external is None:
                continue
            with suppress(Exception):
                await self._resnapshot(external)

    async def _run(self) -> None:
        last_inventory = 0.0
        try:
            while not self._stopping:
                now = monotonic()
                if now - last_inventory >= self.inventory_interval:
                    last_inventory = now
                    if not await self._inventory():
                        return
                await self._flush_timelines()
                await self._flush_cursors()
                await asyncio.sleep(self.tick)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "OpenScience relay stopped error_type={}", type(error).__name__
            )
            raise

    async def _inventory(self) -> bool:
        try:
            sessions = await self.runtime._list_all_sessions(self.client)
        except Exception as error:  # noqa: BLE001 - a lost server is an expected state
            self._inventory_failures += 1
            logger.warning(
                "OpenScience inventory failed attempt={} error_type={}",
                self._inventory_failures,
                type(error).__name__,
            )
            if self._inventory_failures >= INVENTORY_FAILURES_BEFORE_REPROBE:
                await self.runtime._handle_disconnect()
                return False
            return True
        self._inventory_failures = 0
        known: set[str] = set()
        for payload in sessions:
            external = payload.get("id")
            if not isinstance(external, str) or not external:
                continue
            known.add(external)
            try:
                await self._sync_session(payload)
            except Exception as error:  # noqa: BLE001 - isolate one unreadable session
                logger.warning(
                    "OpenScience session sync failed error_type={}", type(error).__name__
                )
        for external in set(self._streams) - known:
            stream = self._streams.pop(external)
            stream.cancel()
        if not self._announced:
            self._announced = True
            with suppress(Exception):
                await self.host.runtime_health_update("running")
        return True

    async def _sync_session(self, payload: Mapping[str, Any]) -> None:
        external = _required_string(payload.get("id"), "session id")
        # Register the project before anything addresses the session: the poll
        # path below is already a per-session call, and it must carry the
        # directory the session was discovered in.
        platform = self.runtime._register_session(
            external, directory=_session_directory(payload)
        )
        marker = _updated_at(payload)
        if self._meta_markers.get(external) != marker:
            self._meta_markers[external] = marker
            await self._publish_meta(external, payload)
        if len(self._streams) < self.max_streams or external in self._streams:
            if external not in self._streams or self._streams[external].done():
                self._streams[external] = asyncio.create_task(
                    self._stream(external), name=f"openscience-events-{external}"
                )
            return
        # Beyond the stream budget a session is polled instead: the same
        # snapshot and transcript, just at the inventory cadence.
        snapshot = await self.client.snapshot(
            external, directory=self.runtime._directory_for(external)
        )
        await self._publish_state(platform, external, snapshot)
        if _sequence(snapshot.get("latestSequence")) != self._cursors.get(external):
            await self._refresh_timeline(platform, external)
            self._cursors[external] = _sequence(snapshot.get("latestSequence"))
            self._cursor_dirty.add(external)

    async def _publish_meta(self, external: str, payload: Mapping[str, Any]) -> None:
        await self.runtime._publish_meta(external, payload)

    async def _stream(self, external: str) -> None:
        backoff = STREAM_RETRY_SECONDS
        while not self._stopping:
            try:
                await self._consume(external)
                backoff = STREAM_RETRY_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - retry this session only
                logger.warning(
                    "OpenScience event stream interrupted error_type={}",
                    type(error).__name__,
                )
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_STREAM_RETRY_SECONDS)

    async def _consume(self, external: str) -> None:
        cursor = await self._cursor(external)
        if cursor is None:
            await self._resnapshot(external)
            cursor = self._cursors.get(external)
        try:
            async for event in self.client.events(
                external,
                after_sequence=cursor,
                directory=self.runtime._directory_for(external),
            ):
                await self._apply(external, event)
        except (OpenScienceCursorError, OpenScienceEventGapError) as error:
            # The retained window moved past the cursor, or a frame skipped
            # ahead. A snapshot is the only correct recovery.
            logger.warning(
                "OpenScience cursor needs calibration error_type={}", type(error).__name__
            )
            await self._resnapshot(external)
            return

    async def _apply(self, external: str, event: OpenScienceEvent) -> None:
        applied = self._cursors.get(external)
        if applied is not None and event.sequence <= applied:
            return
        self._cursors[external] = event.sequence
        self._cursor_dirty.add(external)
        if event.type in _RUN_EVENTS or event.type in _DECISION_EVENTS:
            snapshot = await self.client.snapshot(
                external, directory=self.runtime._directory_for(external)
            )
            await self._publish_state(
                self.runtime._register_session(external), external, snapshot
            )
        if event.type in _TERMINAL_RUN_EVENTS:
            await self._report_turn_end(
                external,
                run_id=event.run_id,
                outcome=_TERMINAL_RUN_EVENTS[event.type],
            )
        if event.type.startswith("message.") or event.type in _RUN_EVENTS:
            self._dirty_timeline.setdefault(external, monotonic())

    async def _report_turn_end(
        self, external: str, *, run_id: str | None, outcome: str | None
    ) -> None:
        """Report a terminal run exactly once, even when its event was missed.

        The journal is a bounded window and a subscription can begin after the
        run already ended, so the durable receipt — not the event — decides
        that a turn finished. The persisted marker is what keeps the report
        single across both paths and across a Connector restart.
        """

        if run_id is None or outcome is None:
            return
        key = _run_key(external)
        record = await self.runtime._read_record(key)
        if record is not None and record.get("reportedRunId") == run_id:
            return
        platform = self.runtime._register_session(external)
        with suppress(Exception):
            await self.host.session_turn_ended(
                session_id=platform,
                runtime=RUNTIME,
                external_session_id=external,
                turn_id=run_id,
                outcome=outcome,
                metadata={
                    "runId": run_id,
                    "crashRecovery": "interrupt",
                    "source": "openscience.runtime.snapshot",
                },
            )
        await self.runtime._write_record(
            key, {"reportedRunId": run_id, "outcome": outcome}
        )

    async def _publish_state(
        self, platform: str, external: str, snapshot: Mapping[str, Any]
    ) -> None:
        state = models.session_state(
            snapshot, session_id=platform, external_session_id=external
        )
        await self.host.session_state_update(
            session_id=platform,
            runtime=RUNTIME,
            status=state.status,
            external_session_id=external,
            status_reason=state.status_reason,
            error=state.error,
            # The platform replaces the stored selection with whatever a state
            # update carries, so the session's chosen model, reasoning level and
            # effort are republished here; omitting them would erase the user's
            # choice.
            selections=self.runtime._selections.get(platform, {}),
            metadata=state.metadata,
        )
        for notice in models.notices(
            snapshot, session_id=platform, external_session_id=external
        ):
            await self.host.notice_upsert(notice)
        run = models.latest_run(snapshot)
        if run is not None:
            await self._report_turn_end(
                external,
                run_id=_optional_string(run.get("runID")),
                outcome=_TURN_OUTCOMES.get(_optional_string(run.get("state")) or ""),
            )

    async def _resnapshot(self, external: str) -> None:
        platform = self.runtime._register_session(external)
        snapshot = await self.client.snapshot(
            external, directory=self.runtime._directory_for(external)
        )
        await self._publish_state(platform, external, snapshot)
        await self._refresh_timeline(platform, external)
        self._cursors[external] = _sequence(snapshot.get("latestSequence"))
        self._cursor_dirty.add(external)

    async def _refresh_timeline(self, platform: str, external: str) -> None:
        messages = await self.client.messages(
            external, directory=self.runtime._directory_for(external)
        )
        snapshot = models.timeline_snapshot(
            messages, session_id=platform, external_session_id=external
        )
        await self.host.timeline_sync(
            session_id=platform,
            runtime=RUNTIME,
            items=snapshot.items,
            external_session_id=external,
            complete=snapshot.complete,
            metadata=snapshot.metadata,
        )

    async def _flush_timelines(self) -> None:
        now = monotonic()
        for external, marked in tuple(self._dirty_timeline.items()):
            if now - marked < TIMELINE_REFRESH_SECONDS:
                continue
            del self._dirty_timeline[external]
            platform = self.runtime._register_session(external)
            try:
                await self._refresh_timeline(platform, external)
            except Exception as error:  # noqa: BLE001 - isolate one unreadable session
                logger.warning(
                    "OpenScience timeline refresh failed error_type={}",
                    type(error).__name__,
                )

    async def _flush_cursors(self) -> None:
        now = monotonic()
        for external in tuple(self._cursor_dirty):
            if now - self._cursor_written.get(external, 0.0) < CURSOR_FLUSH_SECONDS:
                continue
            await self._persist_cursor(external)

    async def _cursor(self, external: str) -> int | None:
        known = self._cursors.get(external)
        if known is not None:
            return known
        try:
            stored = await self.host.sync_state_read(_cursor_key(external))
        except NotImplementedError:
            return None
        except Exception as error:  # noqa: BLE001 - a storage fault is not a cursor
            logger.warning(
                "OpenScience cursor read failed error_type={}", type(error).__name__
            )
            return None
        sequence = stored.get("sequence") if isinstance(stored, Mapping) else None
        if type(sequence) is not int or sequence < 0:
            return None
        self._cursors[external] = sequence
        return sequence

    async def _persist_cursor(self, external: str) -> None:
        sequence = self._cursors.get(external)
        if sequence is None:
            return
        self._cursor_dirty.discard(external)
        self._cursor_written[external] = monotonic()
        try:
            await self.host.sync_state_write(
                _cursor_key(external), {"sequence": sequence, "runtime": RUNTIME}
            )
        except NotImplementedError:
            # Without durable state the stream still deduplicates in memory.
            return
        except Exception as error:  # noqa: BLE001 - a cursor write must not stop the feed
            logger.warning(
                "OpenScience cursor write failed error_type={}", type(error).__name__
            )


# Inventory capability key -> protocol capability id. `modelCatalog` publishes
# two ids because the reasoning-level picker is the model catalog's reasoning
# items, the same pairing `connector/server/capabilities.py` applies to this
# runtime.
_CAPABILITY_IDS: tuple[tuple[str, str], ...] = (
    ("modelCatalog", "catalog.model"),
    ("modelCatalog", "catalog.effort"),
    ("permissionCatalog", "catalog.permission"),
    ("createProject", "project.create"),
    ("startTurn", "session.send_message"),
    ("steerTurn", "session.steer"),
    ("interruptTurn", "session.interrupt"),
    ("commands", "session.commands"),
    ("interactions", "session.interaction.approval"),
    ("attachments", "runtime.attachment"),
)


def _require_protocol(version: str) -> None:
    if version not in discovery.SUPPORTED_PROTOCOL_VERSIONS:
        raise RuntimeUnsupportedError(f"OpenScience runtime protocol {version}")


def _prompt_fingerprint(
    message: str | None,
    parts: list[dict[str, Any]] | None,
    model: Mapping[str, str] | None,
    variant: str | None,
    effort: str,
) -> str:
    """Bind a requestID to the exact input it was admitted with.

    The model, its reasoning variant and the effort are part of that input: the
    server answers 409 when a requestID is reused with different input, so the
    same comparison is made locally — including the selection — to explain the
    conflict instead of surfacing a bare transport error, and to stop a retry
    from silently running a different model, or the same model at a different
    reasoning level, under an id the server already admitted.
    """

    payload = {
        "message": message,
        "parts": parts,
        "model": tuple(sorted(model.items())) if model is not None else None,
        "variant": variant,
        "effort": effort,
    }
    encoded = repr(sorted(payload.items(), key=lambda item: item[0]))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _turn_key(external_session_id: str, client_message_id: str) -> str:
    return f"openscience/turns/{external_session_id}/{client_message_id}"


def _cursor_key(external_session_id: str) -> str:
    return f"openscience/events/{external_session_id}"


def _run_key(external_session_id: str) -> str:
    return f"openscience/runs/{external_session_id}"


def _pending_request(
    snapshot: Mapping[str, Any], kind: str, request_id: str
) -> Mapping[str, Any] | None:
    field = "permissions" if kind == "permission" else "questions"
    requests = snapshot.get(field)
    if not isinstance(requests, list):
        return None
    for request in requests:
        if isinstance(request, Mapping) and request.get("id") == request_id:
            return request
    return None


def _sequence(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _updated_at(payload: Mapping[str, Any]) -> int:
    time = payload.get("time")
    updated = time.get("updated") if isinstance(time, Mapping) else None
    return updated if type(updated) is int else 0


def _session_directory(payload: Mapping[str, Any]) -> str | None:
    """The project a session belongs to, as the server reports it.

    The same value is the session's ``cwd`` on the platform — that is how
    Agents Anywhere groups sessions into projects — and the selector every
    per-session route needs, so it is read once here and reused for both.
    """

    return _optional_string(payload.get("directory"))


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeUpstreamError(f"OpenScience {label} is missing")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
