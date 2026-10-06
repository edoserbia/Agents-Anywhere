"""OpenScience runtime tests.

The payloads used here are the server's own shapes: tool parts follow
`tooling/sdk/openapi.json` (`ToolPart` plus `ToolStatePending|Running|Completed|
Error`), session records and run receipts follow the routes in
`backend/cli/src/server/routes/`, and events follow `RuntimeEvents.Event`. The
point of the fixtures is that a change in those shapes fails here rather than
in a user's session.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Self
from unittest.mock import AsyncMock

import httpx
import pytest

from connector.runtime_protocol import (
    RuntimeAttachment,
    RuntimeConfig,
    RuntimeConflictError,
    RuntimeInvalidRequestError,
    RuntimeOperationResult,
    RuntimeUnavailableError,
    RuntimeUnsupportedError,
    RuntimeUpstreamError,
)
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
)
from connector.runtimes.openscience.provider import OpenScienceProvider
from connector.runtimes.openscience.runtime import (
    OPENSCIENCE_MODEL_CATALOG_STATIC_REVISION,
    OpenScienceRelay,
    OpenScienceRuntime,
    _prompt_fingerprint,
    runtime_capabilities,
)
from connector.runtimes.providers import default_runtime_providers
from connector.server.protocol import protocol_selection_id

# --- fixtures --------------------------------------------------------------


def values(**overrides: Any) -> dict[str, Any]:
    return provider_config.normalized_config_values(overrides)


def session_payload(session_id: str = "ses_1", **overrides: Any) -> dict[str, Any]:
    return {
        "id": session_id,
        "slug": "cosmic-squid",
        "version": "2.0.147",
        "projectID": "prj_1",
        "directory": "/private/tmp/osproj",
        "workspace": "isolated",
        "title": "Calibration analysis",
        "time": {"created": 1_700_000_000_000, "updated": 1_700_000_060_000},
        **overrides,
    }


def snapshot_payload(
    session_id: str = "ses_1",
    *,
    runs: list[dict[str, Any]] | None = None,
    permissions: list[dict[str, Any]] | None = None,
    questions: list[dict[str, Any]] | None = None,
    latest: int = 0,
) -> dict[str, Any]:
    return {
        "sessionID": session_id,
        "runs": list(runs or []),
        "permissions": list(permissions or []),
        "questions": list(questions or []),
        "oldestSequence": 1,
        "latestSequence": latest,
        "decisionScope": "connected_runtime",
    }


def run_payload(
    state: str = "running",
    *,
    run_id: str = "run_1",
    session_id: str = "ses_1",
    updated_at: int = 5,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "runID": run_id,
        "sessionID": session_id,
        "messageID": "msg_1",
        "state": state,
        "acceptedAt": 1,
        "updatedAt": updated_at,
        **overrides,
    }


def message_payload(
    message_id: str,
    role: str,
    parts: list[dict[str, Any]],
    *,
    parent: str | None = None,
    completed: bool = True,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": message_id,
        "sessionID": "ses_1",
        "role": role,
        "time": {"created": 1, **({"completed": 2} if completed else {})},
    }
    if parent is not None:
        info["parentID"] = parent
    return {"info": info, "parts": parts}


def text_part(part_id: str, text: str, message_id: str = "msg_1", **overrides: Any) -> dict[str, Any]:
    return {
        "id": part_id,
        "sessionID": "ses_1",
        "messageID": message_id,
        "type": "text",
        "text": text,
        **overrides,
    }


def tool_part(
    state: dict[str, Any],
    *,
    part_id: str = "prt_tool_1",
    call_id: str = "call_abc",
    tool: str = "bash",
    message_id: str = "msg_2",
) -> dict[str, Any]:
    """A `ToolPart` exactly as `tooling/sdk/openapi.json` requires it."""

    return {
        "id": part_id,
        "sessionID": "ses_1",
        "messageID": message_id,
        "type": "tool",
        "callID": call_id,
        "tool": tool,
        "state": state,
    }


# Tool states copied from the generated schema's required fields.
TOOL_PENDING = {"status": "pending", "input": {"command": "ls -la"}, "raw": ""}
TOOL_RUNNING = {"status": "running", "input": {"command": "ls -la"}, "time": {"start": 1}}
TOOL_COMPLETED = {
    "status": "completed",
    "input": {"command": "ls -la"},
    "output": "total 0\n",
    "title": "ls -la",
    "metadata": {},
    "time": {"start": 1, "end": 2},
}
TOOL_ERROR = {
    "status": "error",
    "input": {"command": "ls -la"},
    "error": "exit status 1",
    "time": {"start": 1, "end": 2},
}


def permission_payload(request_id: str = "per_1", **overrides: Any) -> dict[str, Any]:
    return {
        "id": request_id,
        "sessionID": "ses_1",
        "permission": "bash",
        "patterns": ["ls *"],
        "metadata": {"shell": {"command": "ls -la"}},
        "always": ["ls *"],
        **overrides,
    }


def question_payload(request_id: str = "que_1") -> dict[str, Any]:
    return {
        "id": request_id,
        "sessionID": "ses_1",
        "questions": [
            {
                "question": "Which dataset?",
                "header": "Dataset",
                "options": [
                    {"label": "calibration", "description": "the calibration run"},
                    {"label": "validation", "description": "the validation run"},
                ],
                "multiple": False,
                "custom": True,
            }
        ],
    }


def model_payload(
    model_id: str,
    name: str,
    *,
    provider_id: str,
    reasoning: bool = True,
    status: str = "active",
    variants: tuple[str, ...] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One `Provider.Info.models` entry, shaped like the server's own model.

    ``variants`` is the server's own derivation (`ProviderTransform.variants`)
    keyed by level, so a model either carries the exact levels it supports or
    carries no ``variants`` key at all — the two live cases.
    """

    return {
        "id": model_id,
        "name": name,
        "providerID": provider_id,
        "api": {"id": model_id, "npm": "@ai-sdk/openai-compatible"},
        "status": status,
        "capabilities": {
            "temperature": True,
            "reasoning": reasoning,
            "attachment": False,
            "toolcall": True,
            "input": {"text": True, "image": False, "audio": False, "video": False, "pdf": False},
            "output": {"text": True, "image": False, "audio": False, "video": False, "pdf": False},
            "interleaved": False,
        },
        "cost": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}},
        "limit": {"context": 200_000, "output": 200_000},
        **(
            {"variants": {level: {} for level in variants}}
            if variants is not None
            else {}
        ),
        **overrides,
    }


def provider_catalog() -> dict[str, Any]:
    """A realistic `GET /config/providers` payload.

    It carries what the live server actually exercises: two providers whose
    default model is not the first key in `models`, models whose variant sets
    differ in size (five levels, three levels, none at all, and an empty
    object), a model that does not reason, a model the server no longer
    considers active, and one model name served by both providers.
    """

    return {
        "default": {"cc-proxy": "gpt-5.6-terra", "local-lab": "lab-small"},
        "providers": [
            {
                "id": "cc-proxy",
                "name": "Command Code Proxy",
                "env": [],
                "options": {},
                "source": "config",
                "models": {
                    "MiniMaxAI/MiniMax-M2.5": model_payload(
                        "MiniMaxAI/MiniMax-M2.5", "MiniMax M2.5", provider_id="cc-proxy"
                    ),
                    "gpt-5.6-terra": model_payload(
                        "gpt-5.6-terra",
                        "GPT-5.6 Terra",
                        provider_id="cc-proxy",
                        variants=("low", "medium", "high", "xhigh", "max"),
                    ),
                    "gpt-5.6-sol": model_payload(
                        "gpt-5.6-sol",
                        "GPT-5.6 Sol",
                        provider_id="cc-proxy",
                        status="deprecated",
                        variants=("low", "high", "max"),
                    ),
                },
            },
            {
                "id": "local-lab",
                "name": "Local Lab",
                "env": [],
                "options": {},
                "source": "config",
                "models": {
                    "lab-small": model_payload(
                        "lab-small",
                        "Lab Small",
                        provider_id="local-lab",
                        reasoning=False,
                        # An empty object is the other "no dial" shape.
                        variants=(),
                    ),
                    "lab-terra": model_payload(
                        "lab-terra",
                        "GPT-5.6 Terra",
                        provider_id="local-lab",
                        # A live ladder that is not alphabetical: `minimal`
                        # leads, which is exactly why the order is preserved.
                        variants=("minimal", "low", "medium", "high", "xhigh"),
                    ),
                },
            },
        ],
    }


def selection_id_for(
    catalog: Any,
    provider_id: str,
    model_id: str,
    variant: str | None = None,
) -> str:
    """The platform selection id a picker would send for one model/variant."""

    for model in catalog.models:
        if (
            model.metadata["providerID"] != provider_id
            or model.metadata["modelID"] != model_id
        ):
            continue
        if variant is None:
            assert model.selection_id is not None
            return model.selection_id
        for reasoning in model.reasoning_items:
            if reasoning.id == variant:
                return reasoning.selection_id
    raise AssertionError(f"{provider_id}/{model_id} is not in the catalog")


def make_host(**overrides: Any) -> SimpleNamespace:
    """A host with a real key/value store so cursor and receipt tests are honest."""

    state: dict[str, Any] = {}

    async def read(key: str) -> Any:
        return state.get(key)

    async def write(key: str, value: Any) -> None:
        state[key] = dict(value)

    host = SimpleNamespace(
        connector_id="conn-test",
        runtime_health_update=AsyncMock(),
        runtime_capabilities_update=AsyncMock(),
        runtime_error=AsyncMock(),
        session_meta_upsert=AsyncMock(),
        session_state_update=AsyncMock(),
        session_source_update=AsyncMock(),
        session_turn_ended=AsyncMock(),
        notice_upsert=AsyncMock(),
        timeline_sync=AsyncMock(),
        timeline_item_upsert=AsyncMock(),
        attachment_download=AsyncMock(),
        sync_state_read=AsyncMock(side_effect=read),
        sync_state_write=AsyncMock(side_effect=write),
        sync_state_delete=AsyncMock(),
    )
    host.sync_state = state
    for key, value in overrides.items():
        setattr(host, key, value)
    return host


class FakeClient:
    """A scripted OpenScience server, shaped like the real client.

    ``sessions`` is the whole server; ``list_sessions`` scopes it the way the
    real route does — by the session's own ``directory`` — so a session in a
    project that was never asked for is invisible, exactly as it is live.
    ``current_directory`` is the project the server itself runs in, which an
    unscoped listing answers for.
    """

    def __init__(
        self,
        *,
        sessions: list[dict[str, Any]] | None = None,
        projects: list[dict[str, Any]] | None = None,
        messages: dict[str, list[dict[str, Any]]] | None = None,
        snapshots: dict[str, dict[str, Any]] | None = None,
        events: dict[str, list[OpenScienceEvent]] | None = None,
        protocol: str = "1.0",
        server_version: str = "2.0.147",
        current_directory: str | None = None,
        providers: dict[str, Any] | None = None,
    ) -> None:
        self.sessions = list(sessions or [])
        self.projects = list(projects or [])
        self.current_directory = current_directory
        self.messages_by_session = dict(messages or {})
        self.snapshots = dict(snapshots or {})
        self.event_scripts = dict(events or {})
        self.providers = providers if providers is not None else provider_catalog()
        self.protocol = protocol
        self.server_version = server_version
        self.prompts: list[dict[str, Any]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.cancels: list[tuple[str, str]] = []
        self.decisions: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.calls: list[str] = []
        # (method, session id, project selector) for every call, so a test can
        # assert the scope a per-session route was addressed with.
        self.scopes: list[tuple[str, str | None, Any]] = []
        self.fail_snapshot: set[str] = set()
        self.fail_messages: set[str] = set()
        self.fail_projects = False
        self.fail_providers = False
        self.fail_list: set[str] = set()
        self.gap_sessions: set[str] = set()
        self.expired_sessions: set[str] = set()
        self.closed = False
        # Every project creation this fake was asked for, and the error to
        # answer with instead, so a test can assert the name, sources and
        # idempotency key the runtime sent.
        self.project_requests: list[dict[str, Any]] = []
        self.create_project_error: Exception | None = None

    def scope_of(self, method: str, session_id: str | None = None) -> Any:
        """The selector of the last matching call, for scope assertions."""

        for called, called_session, directory in reversed(self.scopes):
            if called == method and (session_id is None or called_session == session_id):
                return directory
        raise AssertionError(f"no {method} call recorded")

    async def capabilities(self) -> dict[str, Any]:
        self.calls.append("capabilities")
        return {
            "protocolVersion": self.protocol,
            "serverVersion": self.server_version,
            "idempotentPrompts": True,
            "richInputs": True,
            "runSnapshots": True,
            "eventRetention": 2048,
            "crashRecovery": "interrupt",
            "decisionScope": "connected_runtime",
        }

    async def list_projects(self) -> list[dict[str, Any]]:
        self.calls.append("list_projects")
        if self.fail_projects:
            raise OpenScienceConnectionError("project catalog unavailable")
        return [dict(item) for item in self.projects]

    async def list_providers(self) -> dict[str, Any]:
        self.calls.append("list_providers")
        if self.fail_providers:
            raise OpenScienceConnectionError("provider catalog unavailable")
        return copy.deepcopy(self.providers)

    async def create_project(
        self,
        name: str,
        *,
        sources: Any = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append("create_project")
        self.project_requests.append(
            {"name": name, "sources": sources, "operation_id": operation_id}
        )
        if self.create_project_error is not None:
            raise self.create_project_error
        project = {
            "id": f"prj_new{len(self.project_requests)}",
            # The generated directory is the server's decision, which is exactly
            # what this path must never take from the caller.
            "worktree": f"/srv/openscience/{name}-{len(self.project_requests)}",
            "name": name,
            "origin": "openscience",
            "icon": {"color": "lime"},
        }
        self.projects.append(project)
        return project

    async def list_sessions(self, *, directory: Any = None) -> list[dict[str, Any]]:
        self.calls.append("list_sessions")
        self.scopes.append(("list_sessions", None, directory))
        if isinstance(directory, str):
            if directory in self.fail_list:
                raise OpenScienceConnectionError("project unavailable")
            return [dict(item) for item in self.sessions if item.get("directory") == directory]
        # No selector: the server's own project. A fake that models no such
        # project answers with everything, which is how it behaved before the
        # inventory learned about projects at all.
        if self.current_directory is None:
            return [dict(item) for item in self.sessions]
        if self.current_directory in self.fail_list:
            raise OpenScienceConnectionError("project unavailable")
        return [
            dict(item)
            for item in self.sessions
            if item.get("directory") == self.current_directory
        ]

    async def create_session(
        self,
        *,
        title: str | None = None,
        workspace: str | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append("create_session")
        self.created.append({"title": title, "workspace": workspace, "directory": directory})
        payload = session_payload(f"ses_new{len(self.created)}", title=title)
        self.sessions.append(payload)
        self.snapshots[payload["id"]] = snapshot_payload(payload["id"])
        return payload

    async def messages(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        directory: Any = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(f"messages:{session_id}")
        self.scopes.append(("messages", session_id, directory))
        if session_id in self.fail_messages:
            raise OpenScienceConnectionError("messages unavailable")
        return [dict(item) for item in self.messages_by_session.get(session_id, [])]

    async def snapshot(self, session_id: str, *, directory: Any = None) -> dict[str, Any]:
        self.calls.append(f"snapshot:{session_id}")
        self.scopes.append(("snapshot", session_id, directory))
        if session_id in self.fail_snapshot:
            raise OpenScienceConnectionError("snapshot unavailable")
        return dict(self.snapshots.get(session_id, snapshot_payload(session_id)))

    async def get_run(
        self, session_id: str, run_id: str, *, directory: Any = None
    ) -> dict[str, Any]:
        self.calls.append(f"get_run:{session_id}")
        self.scopes.append(("get_run", session_id, directory))
        return {"runID": run_id, "sessionID": session_id, "state": "running"}

    async def prompt(
        self,
        session_id: str,
        *,
        request_id: str,
        message: str | None = None,
        parts: list[dict[str, Any]] | None = None,
        model: dict[str, str] | None = None,
        variant: str | None = None,
        effort: str | None = None,
        message_id: str | None = None,
        directory: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(f"prompt:{session_id}")
        self.scopes.append(("prompt", session_id, directory))
        self.prompts.append(
            {
                "sessionID": session_id,
                "requestID": request_id,
                "message": message,
                "parts": parts,
                "model": dict(model) if model is not None else None,
                "variant": variant,
                "effort": effort,
            }
        )
        # The server is idempotent by requestID: the same id returns the
        # original receipt instead of admitting the message twice.
        receipt = self.receipts.get(request_id)
        if receipt is None:
            receipt = {"runID": f"run_{len(self.receipts) + 1}", "acceptedAt": 1_700_000_000_000}
            self.receipts[request_id] = receipt
        return dict(receipt)

    async def cancel_run(
        self, session_id: str, run_id: str, *, directory: Any = None
    ) -> dict[str, Any]:
        self.cancels.append((session_id, run_id))
        self.scopes.append(("cancel_run", session_id, directory))
        return {"runID": run_id, "state": "cancelled"}

    async def reply_permission(
        self, session_id: str, request_id: str, reply: str, *, directory: Any = None
    ) -> dict[str, Any]:
        self.scopes.append(("reply_permission", session_id, directory))
        self.decisions.append(
            {"kind": "permission", "sessionID": session_id, "requestID": request_id, "reply": reply}
        )
        return {"status": "resolved"}

    async def reply_question(
        self,
        session_id: str,
        request_id: str,
        answers: list[list[str]],
        *,
        directory: Any = None,
    ) -> dict[str, Any]:
        self.scopes.append(("reply_question", session_id, directory))
        self.decisions.append(
            {"kind": "question", "sessionID": session_id, "requestID": request_id, "answers": answers}
        )
        return {"status": "resolved"}

    async def reject_question(
        self, session_id: str, request_id: str, *, directory: Any = None
    ) -> dict[str, Any]:
        self.scopes.append(("reject_question", session_id, directory))
        self.decisions.append(
            {"kind": "question_reject", "sessionID": session_id, "requestID": request_id}
        )
        return {"status": "resolved"}

    async def events(
        self, session_id: str, *, after_sequence: int | None = None, directory: Any = None
    ):
        self.calls.append(f"events:{session_id}")
        self.scopes.append(("events", session_id, directory))
        if session_id in self.expired_sessions:
            raise OpenScienceCursorError(
                409, {"error": "cursor_expired", "oldestSequence": 5, "latestSequence": 9}
            )
        if session_id in self.gap_sessions:
            cursor = after_sequence or 0
            raise OpenScienceEventGapError(cursor + 1, cursor + 5)
        for event in self.event_scripts.get(session_id, []):
            yield event

    async def close(self) -> None:
        self.closed = True


def runtime_config(**overrides: Any) -> RuntimeConfig:
    return RuntimeConfig(
        runtime="openscience",
        revision=1,
        values=values(**overrides),
        metadata={},
    )


def build_runtime(
    host: SimpleNamespace,
    *,
    client: FakeClient | None = None,
    available: bool = True,
    reason: str | None = None,
    protocol: str = "1.0",
    factory: Any = None,
    config: RuntimeConfig | None = None,
) -> OpenScienceRuntime:
    async def prober(_values: dict[str, Any]) -> discovery.OpenScienceDiscovery:
        if not available:
            return discovery.OpenScienceDiscovery(
                False, False, None, reason=reason or discovery.UNAVAILABLE_REASON
            )
        return discovery.OpenScienceDiscovery(
            True,
            True,
            discovery.OpenScienceEndpoint(
                base_url="http://127.0.0.1:41999",
                port=41999,
                source="configured",
                version="2.0.147",
            ),
            discovery.OpenScienceCapabilities(
                protocol_version=protocol,
                server_version="2.0.147",
                idempotent_prompts=True,
                event_retention=2048,
            ),
            metadata={"protocolVersion": "1.0"},
        )

    return OpenScienceRuntime(
        config=config or runtime_config(),
        host=host,
        prober=prober,
        client_factory=factory or (lambda base_url, values: client),
    )


def event(
    sequence: int,
    *,
    type: str = "message.part.updated",
    session_id: str = "ses_1",
    run_id: str = "run_1",
    **properties: Any,
) -> OpenScienceEvent:
    return OpenScienceEvent(
        sequence=sequence,
        type=type,
        session_id=session_id,
        run_id=run_id,
        time=1_700_000_000_000,
        properties=properties,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def platform_session(runtime: OpenScienceRuntime) -> str:
    """Register the server's session the way the supervisor does."""

    sessions = await runtime.list_sessions()
    assert len(sessions) == 1
    return sessions[0].session_id


# --- provider --------------------------------------------------------------


def test_openscience_is_the_fourth_default_provider() -> None:
    assert [provider.runtime for provider in default_runtime_providers()] == [
        "codex",
        "claude",
        "dsh",
        "openscience",
    ]


def test_provider_identity_and_descriptor_capabilities() -> None:
    async def exercise() -> None:
        provider = OpenScienceProvider()
        assert provider.runtime == "openscience"
        assert provider.runtime_type == "openscience"
        assert provider.implementation_type == "local-service"
        assert provider.instance_policy == "single"
        assert provider.max_instances == 1
        assert provider.display_name == "OpenScience"

        descriptor = await provider.discover()
        assert descriptor.available is True
        assert descriptor.reason is None
        assert descriptor.runtime_type == "openscience"
        assert descriptor.instance_policy == "single"
        assert descriptor.capabilities["sessionDiscovery"] is True
        assert descriptor.capabilities["interactions"] is True
        assert descriptor.capabilities["attachments"] is True
        assert descriptor.capabilities["ipc"] is True
        assert descriptor.capabilities["modelCatalog"] is True
        assert descriptor.capabilities["createProject"] is True
        assert descriptor.capabilities["commands"] is False
        assert descriptor.capabilities["steerTurn"] is False
        assert descriptor.capabilities["permissionCatalog"] is False
        assert descriptor.metadata["attachOnly"] is True
        assert descriptor.config_schema is not None

    run(exercise())


def test_provider_config_schema_and_validation(tmp_path: Path) -> None:
    async def exercise() -> None:
        provider = OpenScienceProvider()
        schema = await provider.get_config_schema()
        assert schema.runtime == "openscience"
        assert schema.defaults["discoveryTimeoutMs"] == 2000
        assert schema.ui_schema is not None
        assert schema.ui_schema["order"][0] == "baseUrl"

        config = await provider.validate_config(
            {"baseUrl": "http://127.0.0.1:41999/", "directory": str(tmp_path)}
        )
        assert config.runtime == "openscience"
        assert config.values["baseUrl"] == "http://127.0.0.1:41999/"
        assert config.metadata["attachOnly"] is True
        assert config.metadata["readOnly"] is False

        with pytest.raises(RuntimeInvalidRequestError):
            await provider.validate_config({"baseUrl": "127.0.0.1:41999"})
        with pytest.raises(RuntimeInvalidRequestError):
            await provider.validate_config({"directory": "relative/path"})
        with pytest.raises(RuntimeInvalidRequestError):
            await provider.validate_config({"unexpected": True})

    run(exercise())


def test_validate_config_stays_valid_while_the_server_is_absent(tmp_path: Path) -> None:
    """Offline is temporary, so a missing server must not invalidate a config."""

    async def exercise() -> None:
        async def offline(_values: dict[str, Any]) -> discovery.OpenScienceDiscovery:
            return discovery.OpenScienceDiscovery(
                False, False, None, reason=discovery.UNAVAILABLE_REASON
            )

        provider = OpenScienceProvider(discoverer=offline)
        config = await provider.validate_config({"dataRoot": str(tmp_path)})
        assert config.values["dataRoot"] == str(tmp_path)
        assert config.metadata["attached"] is False
        descriptor = await provider.discover()
        assert descriptor.available is False
        assert descriptor.reason == discovery.UNAVAILABLE_REASON

    run(exercise())


def test_provider_claims_the_attached_server_or_the_data_root(tmp_path: Path) -> None:
    async def exercise() -> None:
        provider = OpenScienceProvider()
        configured = await provider.validate_config({"baseUrl": "http://127.0.0.1:41999"})
        claims = provider.resource_claims(configured)
        assert [claim.kind for claim in claims] == ["openscience_server"]
        assert claims[0].key == "http://127.0.0.1:41999"
        source = provider.session_source_key(configured)
        assert source.kind == "openscience_server"
        assert source.key == "http://127.0.0.1:41999"

        discovered = await provider.validate_config({"dataRoot": str(tmp_path)})
        claims = provider.resource_claims(discovered)
        assert [claim.kind for claim in claims] == ["openscience_data_root"]
        assert provider.session_source_key(discovered).kind == "openscience_data_root"

    run(exercise())


def test_provider_creates_an_attaching_runtime() -> None:
    async def exercise() -> None:
        provider = OpenScienceProvider()
        config = await provider.validate_config({})
        runtime = await provider.create_runtime(config, make_host())
        assert isinstance(runtime, OpenScienceRuntime)
        assert runtime.sync_mode == "events"

    run(exercise())


# --- attach only -----------------------------------------------------------


def test_start_reports_the_discovery_reason_and_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenScience is the user's application; the Connector only attaches."""

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the connector must never start an OpenScience server")

    monkeypatch.setattr("subprocess.Popen", explode)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", explode)

    async def exercise() -> None:
        host = make_host()
        runtime = build_runtime(host, available=False, reason="请先打开 OpenScience，再在 Agents Anywhere 中连接。")
        await runtime.start()
        try:
            status, error = host.runtime_health_update.await_args.args
            assert status == "starting"
            assert error["retryable"] is True
            assert error["message"] == "请先打开 OpenScience，再在 Agents Anywhere 中连接。"
            assert runtime._client is None
            assert runtime._restart_task is not None
        finally:
            await runtime.stop()

    run(exercise())


def test_an_unsupported_protocol_version_is_refused_not_retried() -> None:
    async def exercise() -> None:
        host = make_host()
        runtime = build_runtime(host, client=FakeClient(), protocol="2.0")
        await runtime.start()
        try:
            status, error = host.runtime_health_update.await_args.args
            assert status == "error"
            assert error["retryable"] is False
            assert error["code"] == "runtime_protocol_unsupported"
            assert runtime._client is None
            assert runtime._restart_task is None
        finally:
            await runtime.stop()

    run(exercise())


def test_a_web_ui_on_the_same_port_is_never_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Port 4096 is OpenCode web in this environment, not OpenScience.

    It answers every path with HTML, so the probe must refuse it and the
    runtime must stay unattached instead of driving another product's API.
    """

    class FakeResponse:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "text/html"}

        def json(self) -> Any:
            raise AssertionError("HTML is not JSON")

    class FakeHttpClient:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def get(self, url: str, headers: Any = None) -> FakeResponse:
            assert url == "http://127.0.0.1:4096/runtime/capabilities"
            return FakeResponse()

    monkeypatch.setattr(discovery.httpx, "AsyncClient", lambda **_kwargs: FakeHttpClient())

    async def exercise() -> None:
        host = make_host()
        created: list[str] = []

        def factory(base_url: str, _values: Any) -> FakeClient:
            created.append(base_url)
            return FakeClient()

        config = RuntimeConfig(
            runtime="openscience",
            revision=1,
            values=values(baseUrl="http://127.0.0.1:4096"),
        )
        runtime = OpenScienceRuntime(config=config, host=host, client_factory=factory)
        await runtime.start()
        try:
            status, error = host.runtime_health_update.await_args.args
            assert status == "starting"
            assert "4096" in error["message"]
            assert created == []
            assert runtime._client is None
        finally:
            await runtime.stop()

    run(exercise())


def test_start_attaches_and_publishes_capabilities() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            assert runtime.identity.runtime == "openscience"
            assert runtime.identity.runtime_version == "2.0.147"
            assert runtime.identity.display_name == "OpenScience"
            host.runtime_capabilities_update.assert_awaited()
            capabilities = host.runtime_capabilities_update.await_args.args[0]
            assert capabilities.runtime == "openscience"
            ids = {item.capability_id for item in capabilities.capabilities}
            assert {"session.send_message", "session.interrupt", "runtime.attachment"} <= ids
            declined = {item.capability_id: item for item in capabilities.capabilities}
            assert declined["catalog.model"].supported is True
            assert declined["catalog.effort"].supported is True
            assert declined["catalog.permission"].supported is False
            assert declined["session.send_message"].supported is True
            assert capabilities.metadata["sessionNotices"] is True

            await asyncio.sleep(0.1)
            assert host.session_meta_upsert.await_count >= 1
            assert host.runtime_health_update.await_args.args[0] == "running"
        finally:
            await runtime.stop()

    run(exercise())


# --- sessions and turns ----------------------------------------------------


def test_list_sessions_maps_native_ids_to_platform_ids() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1"), session_payload("ses_2")])
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            sessions = await runtime.list_sessions()
            assert [item.external_session_id for item in sessions] == ["ses_1", "ses_2"]
            assert all(item.runtime == "openscience" for item in sessions)
            assert all(item.session_id.startswith("sess_openscience_") for item in sessions)
            assert sessions[0].title == "Calibration analysis"
            assert sessions[0].cwd == "/private/tmp/osproj"
            assert sessions[0].ordering_time == "2023-11-14T22:14:20Z"
            assert sessions[0].source_state is not None
            assert sessions[0].source_state.availability == "available"
        finally:
            await runtime.stop()

    run(exercise())


def test_create_and_start_creates_then_prompts() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient()
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            result = await runtime.create_and_start_session(
                "sess_platform",
                "Analyze the calibration data",
                title="Calibration",
                client_message_id="cm_1",
            )
            assert result.ok is True
            assert result.result["externalSessionId"] == "ses_new1"
            assert result.result["sessionId"] == "sess_platform"
            assert client.created == [
                {"title": "Calibration", "workspace": "isolated", "directory": None}
            ]
            assert client.prompts[0]["message"] == "Analyze the calibration data"
            assert client.prompts[0]["requestID"]
            host.session_meta_upsert.assert_awaited()
        finally:
            await runtime.stop()

    run(exercise())


def test_create_uses_the_project_workspace_only_when_it_matches(tmp_path: Path) -> None:
    """A directory the server does not manage must not become a project."""

    async def exercise() -> None:
        host = make_host()
        managed = str(tmp_path / "managed")
        client = FakeClient(projects=[{"id": "prj_1", "name": "Managed", "worktree": managed}])
        runtime = build_runtime(
            host, client=client, config=runtime_config(directory=str(tmp_path))
        )
        await runtime.start()
        try:
            await runtime.create_and_start_session(
                "sess_a", "hello", cwd=str(tmp_path), client_message_id="cm_1"
            )
            await runtime.create_and_start_session(
                "sess_b", "hello", cwd=managed, client_message_id="cm_2"
            )
            await runtime.create_and_start_session(
                "sess_c", "hello", cwd="/somewhere/else", client_message_id="cm_3"
            )
            assert [item["workspace"] for item in client.created] == [
                "project",
                "project",
                "isolated",
            ]
            assert [item["directory"] for item in client.created] == [
                str(tmp_path),
                managed,
                None,
            ]
        finally:
            await runtime.stop()

    run(exercise())


def test_start_turn_reuses_the_persisted_request_id() -> None:
    """A retry after an ambiguous failure must recover the original receipt."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            first = await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
            second = await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
            assert first.result["runID"] == second.result["runID"]
            assert len(client.prompts) == 2
            assert client.prompts[0]["requestID"] == client.prompts[1]["requestID"]
            assert host.sync_state["openscience/turns/ses_1/cm_1"]["requestID"]
            assert host.sync_state["openscience/turns/ses_1/cm_1"]["runID"] == "run_1"
        finally:
            await runtime.stop()

    run(exercise())


def test_a_plain_turn_is_a_message_not_empty_parts() -> None:
    """The API takes exactly one of `message` or `parts`.

    A live run caught the first version sending `message` together with an
    empty `parts` list, which the server refuses as an ambiguous input.
    """

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
            assert client.prompts[0]["message"] == "hello"
            assert client.prompts[0]["parts"] is None
        finally:
            await runtime.stop()

    run(exercise())


def test_start_turn_rejects_a_reused_client_message_id_with_new_content() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
            with pytest.raises(RuntimeInvalidRequestError):
                await runtime.start_turn("sess_x", "ses_1", "different", client_message_id="cm_1")
            assert len(client.prompts) == 1
        finally:
            await runtime.stop()

    run(exercise())


def test_start_turn_requires_a_stable_client_message_id() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            with pytest.raises(RuntimeInvalidRequestError):
                await runtime.start_turn("sess_x", "ses_1", "hello")
            with pytest.raises(RuntimeInvalidRequestError):
                await runtime.start_turn("sess_x", "ses_1", "   ", client_message_id="cm_1")
            assert client.prompts == []
        finally:
            await runtime.stop()

    run(exercise())


# --- model catalog and selections -------------------------------------------


def test_model_catalog_publishes_each_models_own_reasoning_levels() -> None:
    """The catalog is OpenScience's, translated and not re-invented."""

    catalog = models.model_catalog(provider_catalog(), revision=7)
    assert catalog.runtime == "openscience"
    assert catalog.revision == 7
    by_id = {model.id: model for model in catalog.models}
    assert list(by_id) == [
        "cc-proxy/gpt-5.6-terra",
        "cc-proxy/MiniMaxAI/MiniMax-M2.5",
        "cc-proxy/gpt-5.6-sol",
        "local-lab/lab-small",
        "local-lab/lab-terra",
    ]
    # The provider's own default leads its group: the Connector catalog has no
    # default marker, so order is what the picker falls back to.
    terra = by_id["cc-proxy/gpt-5.6-terra"]
    assert terra.title == "GPT-5.6 Terra · Command Code Proxy"
    assert terra.metadata["default"] is True
    assert terra.metadata["providerName"] == "Command Code Proxy"
    assert terra.enabled is True
    # A model with five levels publishes exactly those five, under the labels
    # OpenScience's own picker uses (`xhigh` is "Extra high" there too).
    assert [item.id for item in terra.reasoning_items] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert [item.title for item in terra.reasoning_items] == [
        "Low",
        "Medium",
        "High",
        "Extra high",
        "Max",
    ]
    assert terra.metadata["variants"] == ["low", "medium", "high", "xhigh", "max"]
    # A model with levels is selectable only through one of them: the level is
    # the choice, so the model itself carries no plain selection.
    assert terra.selection_id is None
    # The payload's own order is kept even when it is not sorted, because it is
    # the server's ladder and not a display accident.
    lab = by_id["local-lab/lab-terra"]
    assert lab.title == "GPT-5.6 Terra · Local Lab"
    assert [item.id for item in lab.reasoning_items] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    # A model with no `variants` key has no effort dial at all: no reasoning
    # items, and a plain model selection — the live majority of 133 models.
    minimax = by_id["cc-proxy/MiniMaxAI/MiniMax-M2.5"]
    assert minimax.reasoning_items == ()
    assert minimax.selection_id is not None
    assert minimax.metadata["variants"] == []
    # An empty variants object is the same answer as a missing key. The title
    # names the provider even though no other provider serves this model.
    small = by_id["local-lab/lab-small"]
    assert small.title == "Lab Small · Local Lab"
    assert small.reasoning_items == ()
    assert small.selection_id is not None
    # A model the server retired stays visible but cannot be selected.
    retired = by_id["cc-proxy/gpt-5.6-sol"]
    assert retired.enabled is False
    assert retired.disabled_reason == "OpenScience reports this model as deprecated"
    # Every selection id resolves back to the exact route that produced it.
    assert models.model_route(catalog, terra.reasoning_items[3].selection_id) == (
        models.ModelRoute(
            provider_id="cc-proxy", model_id="gpt-5.6-terra", variant="xhigh"
        )
    )
    assert models.model_route(catalog, minimax.selection_id) == models.ModelRoute(
        provider_id="cc-proxy", model_id="MiniMaxAI/MiniMax-M2.5"
    )
    # Two levels of one model are two different routes, not one model id.
    assert models.model_route(
        catalog, terra.reasoning_items[0].selection_id
    ) != models.model_route(catalog, terra.reasoning_items[-1].selection_id)
    # A selection id that resolves to nothing is refused, never defaulted.
    assert models.model_route(catalog, "sel_model_unknown") is None


def test_model_catalog_rejects_a_payload_without_providers() -> None:
    with pytest.raises(TypeError):
        models.model_catalog({}, revision=1)
    with pytest.raises(TypeError):
        models.model_catalog({"providers": "cc-proxy"}, revision=1)


def test_model_catalog_titles_always_name_the_provider() -> None:
    """A name alone is not an identity: the provider is always in the title.

    The live catalog serves eight model ids from two or three providers, so a
    picker showing only the name would offer rows the user cannot tell apart
    even though each routes to a different backend. The provider is therefore
    appended unconditionally, not only when two providers happen to collide.
    """

    payload = {
        "default": {},
        "providers": [
            {
                "id": "cc-proxy",
                "name": "Command Code Proxy",
                "models": {
                    "gpt-5.6-terra": {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
                },
            },
            {
                "id": "local-lab",
                "name": "Local Lab",
                "models": {
                    # The same model id, served by a second provider.
                    "gpt-5.6-terra": {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
                    # A model no other provider serves is still qualified.
                    "lab-small": {"id": "lab-small", "name": "Lab Small"},
                },
            },
        ],
    }
    catalog = models.model_catalog(payload, revision=1)
    assert [model.title for model in catalog.models] == [
        "GPT-5.6 Terra · Command Code Proxy",
        "GPT-5.6 Terra · Local Lab",
        "Lab Small · Local Lab",
    ]
    # The two routes stay distinct by id *and* by title.
    by_id = {model.id: model for model in catalog.models}
    assert by_id["cc-proxy/gpt-5.6-terra"].title != by_id["local-lab/gpt-5.6-terra"].title
    assert by_id["cc-proxy/gpt-5.6-terra"].selection_id != (
        by_id["local-lab/gpt-5.6-terra"].selection_id
    )


def test_model_catalog_keeps_the_id_for_a_name_one_provider_repeats() -> None:
    """One provider's own name collision is the one case a provider cannot break.

    Two different model ids published by the same provider under one display
    name would still read identically, so the model id stays in the title —
    that is the collision `name_is_repeated` detects, and it is a different
    question from the same name appearing under two providers.
    """

    payload = {
        "default": {},
        "providers": [
            {
                "id": "cc-proxy",
                "name": "Command Code Proxy",
                "models": {
                    "gpt-5.6-terra": {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
                    "gpt-5.6-terra-preview": {
                        "id": "gpt-5.6-terra-preview",
                        "name": "GPT-5.6 Terra",
                    },
                },
            },
        ],
    }
    catalog = models.model_catalog(payload, revision=1)
    assert [model.title for model in catalog.models] == [
        "GPT-5.6 Terra · Command Code Proxy [gpt-5.6-terra]",
        "GPT-5.6 Terra · Command Code Proxy [gpt-5.6-terra-preview]",
    ]
    assert [model.id for model in catalog.models] == [
        "cc-proxy/gpt-5.6-terra",
        "cc-proxy/gpt-5.6-terra-preview",
    ]


def test_the_runtime_publishes_the_servers_model_catalog() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        catalog = await runtime.list_model_catalog()
        assert len(catalog.models) == 5
        assert catalog.revision == runtime_catalog_revision(
            runtime.config.revision, OPENSCIENCE_MODEL_CATALOG_STATIC_REVISION
        )
        # One read serves both the picker and the resolution of a selection.
        await runtime.list_model_catalog()
        assert client.calls.count("list_providers") == 1

        filtered = await runtime.list_model_catalog(query="local lab")
        assert [model.id for model in filtered.models] == [
            "local-lab/lab-small",
            "local-lab/lab-terra",
        ]
        limited = await runtime.list_model_catalog(limit=2)
        assert [model.id for model in limited.models] == [
            "cc-proxy/gpt-5.6-terra",
            "cc-proxy/MiniMaxAI/MiniMax-M2.5",
        ]

    run(exercise())


def test_a_selected_model_and_reasoning_level_reach_the_prompt() -> None:
    """The platform patches a selection once; every later turn keeps using it."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        catalog = await runtime.list_model_catalog()
        selection = selection_id_for(catalog, "cc-proxy", "gpt-5.6-terra", "xhigh")

        result = await runtime.update_session_selections(
            "sess_x", "ses_1", {"model": selection}
        )
        assert result.ok is True
        assert result.result["selections"] == {"model": selection}

        await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
        assert client.prompts[0]["model"] == {
            "providerID": "cc-proxy",
            "modelID": "gpt-5.6-terra",
        }
        # The level the picker resolved is what reaches the server's `variant`
        # field; the research effort stays its own default.
        assert client.prompts[0]["variant"] == "xhigh"
        assert client.prompts[0]["effort"] == "normal"

    run(exercise())


def test_a_turn_can_carry_its_own_selection() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        catalog = await runtime.list_model_catalog()
        selection = selection_id_for(catalog, "local-lab", "lab-terra", "high")

        await runtime.start_turn(
            "sess_x",
            "ses_1",
            "hello",
            selections={"model": selection},
            client_message_id="cm_1",
        )
        assert client.prompts[0]["model"] == {
            "providerID": "local-lab",
            "modelID": "lab-terra",
        }
        assert client.prompts[0]["variant"] == "high"
        assert client.prompts[0]["effort"] == "normal"
        # The turn's selection is also the session's, so the next message that
        # carries none still uses it.
        await runtime.start_turn("sess_x", "ses_1", "again", client_message_id="cm_2")
        assert client.prompts[1]["model"] == {
            "providerID": "local-lab",
            "modelID": "lab-terra",
        }
        assert client.prompts[1]["variant"] == "high"

    run(exercise())


def test_a_prompt_without_a_selection_sends_no_model_and_the_servers_default() -> None:
    """Nothing is selected, so OpenScience runs its own default model.

    No model and no variant are invented, and the effort is still sent because
    the server's prompt schema requires one and resolves an unselected turn to
    `normal`; that is what the runtime has always sent and what the server
    itself would default to.
    """

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
        assert client.prompts[0]["model"] is None
        assert client.prompts[0]["variant"] is None
        assert client.prompts[0]["effort"] == "normal"
        # No selection means no catalog read: a plain turn stays one request.
        assert "list_providers" not in client.calls

    run(exercise())


def test_an_effort_scope_alone_is_passed_through() -> None:
    """The research effort is its own axis: no model, no variant, still sent."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        await runtime.update_session_selections("sess_x", "ses_1", {"effort": "ultra"})
        await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
        assert client.prompts[0]["model"] is None
        assert client.prompts[0]["variant"] is None
        assert client.prompts[0]["effort"] == "ultra"

    run(exercise())


def test_a_model_that_cannot_reason_offers_no_reasoning_items() -> None:
    """A model's `variants` decide the picker's reasoning items.

    A model without them — here one that cannot reason at all, and the live
    majority that simply has no effort dial — gets a plain model selection and
    no reasoning items, so the platform never shows a level the server cannot
    apply. The prompt still carries the server's default effort, because the
    endpoint requires one.
    """

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        catalog = await runtime.list_model_catalog()
        small = next(m for m in catalog.models if m.id == "local-lab/lab-small")
        assert small.reasoning_items == ()
        assert small.metadata["reasoning"] is False
        assert small.metadata["variants"] == []

        await runtime.start_turn(
            "sess_x",
            "ses_1",
            "hello",
            selections={"model": small.selection_id},
            client_message_id="cm_1",
        )
        assert client.prompts[0]["model"] == {
            "providerID": "local-lab",
            "modelID": "lab-small",
        }
        assert client.prompts[0]["variant"] is None
        assert client.prompts[0]["effort"] == "normal"

    run(exercise())


def test_an_unknown_or_unsupported_selection_is_refused() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)

        unknown = await runtime.update_session_selections(
            "sess_x", "ses_1", {"model": "sel_model_nope"}
        )
        assert unknown.ok is False
        assert unknown.code == "openscience_invalid_selection"
        assert "list_providers" in client.calls

        scope = await runtime.update_session_selections(
            "sess_x", "ses_1", {"permission": "sel_permission_1"}
        )
        assert scope.ok is False
        assert scope.code == "openscience_unsupported_selection_scope"

        empty = await runtime.update_session_selections("sess_x", "ses_1", {})
        assert empty.ok is False
        assert empty.code == "openscience_empty_selection_update"

        # A refused selection never reaches a prompt.
        await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
        assert client.prompts[0]["model"] is None
        assert client.prompts[0]["variant"] is None
        assert client.prompts[0]["effort"] == "normal"

    run(exercise())


def test_a_reasoning_level_the_model_does_not_offer_is_refused() -> None:
    """A stale or foreign level must not resolve to the model or another level.

    A selection id persisted before this change named a level out of the old
    research-effort vocabulary, which no model's own `variants` contains; the
    id has to fail rather than round to the model's default.
    """

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        await runtime.list_model_catalog()
        stale = protocol_selection_id(
            "openscience",
            "model",
            {"provider_id": "cc-proxy", "model_id": "gpt-5.6-terra", "effort": "ultra"},
        )

        result = await runtime.update_session_selections(
            "sess_x", "ses_1", {"model": stale}
        )
        assert result.ok is False
        assert result.code == "openscience_invalid_selection"
        # A level one model does not offer is not borrowed from another model
        # that does: `local-lab/lab-terra` has no `max`, so its id is unknown.
        catalog = await runtime.list_model_catalog()
        foreign = selection_id_for(catalog, "cc-proxy", "gpt-5.6-terra", "max")
        borrowed = protocol_selection_id(
            "openscience",
            "model",
            {
                "provider_id": "local-lab",
                "model_id": "lab-terra",
                "variant": "max",
            },
        )
        assert models.model_route(catalog, foreign) is not None
        assert models.model_route(catalog, borrowed) is None

        await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
        assert client.prompts[0]["model"] is None
        assert client.prompts[0]["variant"] is None

    run(exercise())


def test_the_reasoning_level_is_part_of_the_prompt_fingerprint() -> None:
    """A retry cannot silently run the same model at a different level."""

    model = {"providerID": "cc-proxy", "modelID": "gpt-5.6-terra"}
    low = _prompt_fingerprint("hello", None, model, "low", "normal")
    assert low != _prompt_fingerprint("hello", None, model, "high", "normal")
    # The effort is a separate axis: changing only it changes the input too.
    assert low != _prompt_fingerprint("hello", None, model, "low", "ultra")
    # An unchanged input keeps its fingerprint, which is what a retry needs.
    assert low == _prompt_fingerprint("hello", None, model, "low", "normal")
    # No selection at all is a value of its own, not an empty variant.
    assert _prompt_fingerprint("hello", None, None, None, "normal") != (
        _prompt_fingerprint("hello", None, model, None, "normal")
    )


def test_a_retry_with_a_different_selection_is_refused_locally() -> None:
    """The selection is part of what a requestID was admitted with."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        catalog = await runtime.list_model_catalog()
        low = selection_id_for(catalog, "cc-proxy", "gpt-5.6-terra", "low")
        max_level = selection_id_for(catalog, "cc-proxy", "gpt-5.6-terra", "max")

        await runtime.start_turn(
            "sess_x",
            "ses_1",
            "hello",
            selections={"model": low},
            client_message_id="cm_1",
        )
        # Same model, same message, same requestID — only the reasoning level
        # changed, and that is still a different input.
        with pytest.raises(RuntimeInvalidRequestError):
            await runtime.start_turn(
                "sess_x",
                "ses_1",
                "hello",
                selections={"model": max_level},
                client_message_id="cm_1",
            )
        assert len(client.prompts) == 1
        # The identical retry still recovers the original receipt.
        again = await runtime.start_turn(
            "sess_x",
            "ses_1",
            "hello",
            selections={"model": low},
            client_message_id="cm_1",
        )
        assert again.result["runID"] == "run_1"
        assert client.prompts[1]["requestID"] == client.prompts[0]["requestID"]

    run(exercise())


def test_state_updates_republish_the_session_selection() -> None:
    """A state refresh must not erase the model the user picked."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime, relay = connected_relay(host, client)
        catalog = await runtime.list_model_catalog()
        selection = selection_id_for(catalog, "cc-proxy", "gpt-5.6-terra", "xhigh")
        await runtime.update_session_selections("sess_x", "ses_1", {"model": selection})
        runtime._register_session("ses_1", session_id="sess_x")

        await relay._publish_state("sess_x", "ses_1", snapshot_payload("ses_1"))
        published = host.session_state_update.await_args
        assert published.kwargs["selections"] == {"model": selection}

    run(exercise())


def test_a_selection_missing_from_the_cache_is_re_read_before_it_is_refused() -> None:
    """A model added after the cache was filled is still selectable."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        await runtime.list_model_catalog()
        assert client.calls.count("list_providers") == 1
        # A model the cached catalog has never seen, published on the next read.
        client.providers["providers"][0]["models"]["gpt-6-new"] = model_payload(
            "gpt-6-new",
            "GPT-6 New",
            provider_id="cc-proxy",
            variants=("low", "high", "max"),
        )
        fresh = models.model_catalog(client.providers, revision=1)
        selection = selection_id_for(fresh, "cc-proxy", "gpt-6-new", "high")

        result = await runtime.update_session_selections(
            "sess_x", "ses_1", {"model": selection}
        )
        assert result.ok is True
        # The stale cache was not trusted: one forced re-read resolved it.
        assert client.calls.count("list_providers") == 2
        await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
        assert client.prompts[0]["model"] == {
            "providerID": "cc-proxy",
            "modelID": "gpt-6-new",
        }
        assert client.prompts[0]["variant"] == "high"
        assert client.prompts[0]["effort"] == "normal"

    run(exercise())


def test_a_catalog_read_failure_does_not_invent_a_selection() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = connected(host, client)
        client.fail_providers = True
        with pytest.raises(RuntimeUnavailableError):
            await runtime.list_model_catalog()
        with pytest.raises(RuntimeUnavailableError):
            await runtime.start_turn(
                "sess_x",
                "ses_1",
                "hello",
                selections={"model": "sel_model_anything"},
                client_message_id="cm_1",
            )
        assert client.prompts == []

    run(exercise())


def test_interrupt_uses_the_live_run_and_is_honest_when_idle() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload(runs=[run_payload("running")])},
        )
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            platform = await platform_session(runtime)
            result = await runtime.interrupt_session(platform)
            assert client.cancels == [("ses_1", "run_1")]
            assert result.result["runID"] == "run_1"

            client.snapshots["ses_1"] = snapshot_payload(runs=[run_payload("completed")])
            idle = await runtime.interrupt_session(platform)
            assert idle.ok is True
            assert idle.code == "openscience_run_not_active"
            assert len(client.cancels) == 1
        finally:
            await runtime.stop()

    run(exercise())


def test_respond_interaction_answers_a_pending_permission() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={
                "ses_1": snapshot_payload(
                    runs=[run_payload("running")], permissions=[permission_payload()]
                )
            },
        )
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            platform = await platform_session(runtime)
            result = await runtime.respond_interaction(
                platform, "notice_openscience_permission_per_1", "session"
            )
            assert result.ok is True
            assert client.decisions == [
                {
                    "kind": "permission",
                    "sessionID": "ses_1",
                    "requestID": "per_1",
                    "reply": "session",
                }
            ]
            unknown = await runtime.respond_interaction(
                platform, "notice_openscience_permission_per_1", "definitely-not-a-grant"
            )
            assert client.decisions[-1]["reply"] == "reject"
            assert unknown.ok is True
        finally:
            await runtime.stop()

    run(exercise())


def test_respond_interaction_refuses_a_decision_the_server_no_longer_holds() -> None:
    """`decisionScope` is connected_runtime: only a live request is actionable."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload(runs=[run_payload("running")])},
        )
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            platform = await platform_session(runtime)
            result = await runtime.respond_interaction(
                platform, "notice_openscience_permission_per_1", "once"
            )
            assert result.ok is False
            assert result.code == "openscience_notice_not_pending"
            assert client.decisions == []
            missing = await runtime.respond_interaction(platform, "notice_other", "once")
            assert missing.code == "openscience_notice_not_found"
        finally:
            await runtime.stop()

    run(exercise())


def test_respond_interaction_submits_question_labels() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={
                "ses_1": snapshot_payload(
                    runs=[run_payload("running")], questions=[question_payload()]
                )
            },
        )
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            platform = await platform_session(runtime)
            result = await runtime.respond_interaction(
                platform,
                "notice_openscience_question_que_1",
                "submit",
                {"answers": {"q_0": {"optionIds": ["o_1"]}}},
            )
            assert result.ok is True
            assert client.decisions == [
                {
                    "kind": "question",
                    "sessionID": "ses_1",
                    "requestID": "que_1",
                    "answers": [["validation"]],
                }
            ]
        finally:
            await runtime.stop()

    run(exercise())


def test_respond_interaction_cancels_a_question() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={
                "ses_1": snapshot_payload(
                    runs=[run_payload("running")], questions=[question_payload()]
                )
            },
        )
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            platform = await platform_session(runtime)
            result = await runtime.respond_interaction(
                platform, "notice_openscience_question_que_1", "cancel"
            )
            assert result.ok is True
            assert client.decisions == [
                {"kind": "question_reject", "sessionID": "ses_1", "requestID": "que_1"}
            ]
        finally:
            await runtime.stop()

    run(exercise())


def test_get_session_snapshot_state_and_notices() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            messages={
                "ses_1": [
                    message_payload("msg_1", "user", [text_part("prt_1", "hello", "msg_1")]),
                    message_payload(
                        "msg_2",
                        "assistant",
                        [text_part("prt_2", "hi", "msg_2")],
                        parent="msg_1",
                    ),
                ]
            },
            snapshots={
                "ses_1": snapshot_payload(
                    runs=[run_payload("running")], permissions=[permission_payload()]
                )
            },
        )
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            snapshot = await runtime.get_session_snapshot("sess_x", "ses_1")
            assert snapshot.complete is True
            assert snapshot.external_session_id == "ses_1"
            assert [item.type for item in snapshot.items] == ["message", "message"]

            state = await runtime.get_session_state("sess_x", "ses_1")
            assert state.status == "waiting_approval"

            notices = await runtime.get_session_notices("sess_x", "ses_1")
            assert [notice.notice_id for notice in notices] == [
                "notice_openscience_permission_per_1"
            ]
        finally:
            await runtime.stop()

    run(exercise())


def test_attachments_are_inlined_as_file_parts() -> None:
    async def exercise() -> None:
        host = make_host()
        content = b"col,value\n1,2\n"
        host.attachment_download.return_value = SimpleNamespace(
            file_id="file_1",
            name="data.csv",
            media_type="text/csv",
            content=content,
            sha256=None,
        )
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            await runtime.start_turn(
                "sess_x",
                "ses_1",
                "analyse this",
                attachments=(
                    RuntimeAttachment(
                        file_id="file_1",
                        name="data.csv",
                        media_type="text/csv",
                        size=len(content),
                    ),
                ),
                client_message_id="cm_1",
            )
            parts = client.prompts[0]["parts"]
            assert parts[0] == {"type": "text", "text": "analyse this"}
            assert parts[1]["type"] == "file"
            assert parts[1]["mime"] == "text/csv"
            assert parts[1]["filename"] == "data.csv"
            assert parts[1]["url"] == (
                "data:text/csv;base64," + base64.b64encode(content).decode("ascii")
            )
            assert client.prompts[0]["message"] is None
        finally:
            await runtime.stop()

    run(exercise())


def test_attachments_are_validated_against_their_upload() -> None:
    async def exercise() -> None:
        host = make_host()
        host.attachment_download.return_value = SimpleNamespace(
            file_id="file_1",
            name="data.csv",
            media_type="text/csv",
            content=b"short",
            sha256=None,
        )
        client = FakeClient(sessions=[session_payload("ses_1")])
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            with pytest.raises(RuntimeInvalidRequestError):
                await runtime.start_turn(
                    "sess_x",
                    "ses_1",
                    "analyse this",
                    attachments=(
                        RuntimeAttachment(file_id="file_1", media_type="text/csv", size=999),
                    ),
                    client_message_id="cm_1",
                )
            with pytest.raises(RuntimeInvalidRequestError):
                await runtime.start_turn(
                    "sess_x",
                    "ses_1",
                    "analyse this",
                    attachments=(RuntimeAttachment(file_id="../escape"),),
                    client_message_id="cm_2",
                )
            assert client.prompts == []
        finally:
            await runtime.stop()

    run(exercise())


# --- client ----------------------------------------------------------------


def http_client(handler: Any, **overrides: Any) -> OpenScienceClient:
    return OpenScienceClient(
        base_url="http://127.0.0.1:41999",
        values=values(**overrides),
        transport=httpx.MockTransport(handler),
    )


def sse_body(*frames: str) -> bytes:
    return "".join(f"{frame}\n\n" for frame in frames).encode("utf-8")


def event_frame(sequence: int, type: str = "runtime.accepted", **properties: Any) -> str:
    payload = {
        "sequence": sequence,
        "type": type,
        "sessionID": "ses_1",
        "runID": "run_1",
        "time": 1_700_000_000_000,
        "properties": properties,
    }
    return f"id: {sequence}\nevent: {type}\ndata: {json.dumps(payload)}"


def test_client_sends_credentials_and_project_selector() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"protocolVersion": "1.0"})

    async def exercise() -> None:
        client = http_client(
            handler, authToken="secret-token", directory="/private/tmp/osproj"
        )
        await client.capabilities()
        await client.close()
        assert seen[0].headers["authorization"] == "Bearer secret-token"
        assert seen[0].headers["x-openscience-directory"] == "/private/tmp/osproj"
        assert seen[0].url.path == "/runtime/capabilities"

    run(exercise())


def test_client_omits_credentials_when_unconfigured() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"protocolVersion": "1.0"})

    async def exercise() -> None:
        client = http_client(handler)
        await client.capabilities()
        await client.close()
        assert "authorization" not in seen[0].headers
        assert "x-openscience-directory" not in seen[0].headers

    run(exercise())


def test_client_scopes_each_call_and_defaults_to_the_configured_project() -> None:
    """The selector is per call: a fixed default, an explicit project, or none."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/project":
            return httpx.Response(200, json=[{"id": "prj_a", "worktree": "/srv/alpha"}])
        return httpx.Response(200, json=[])

    async def exercise() -> None:
        client = http_client(handler, directory="/srv/default")
        projects = await client.list_projects()
        await client.list_sessions()
        await client.list_sessions(directory="/srv/alpha")
        await client.list_sessions(directory=UNSCOPED)
        await client.close()
        assert projects == [{"id": "prj_a", "worktree": "/srv/alpha"}]
        # The catalog is global: it is never pinned to the default project.
        assert "x-openscience-directory" not in seen[0].headers
        assert seen[1].headers["x-openscience-directory"] == "/srv/default"
        assert seen[2].headers["x-openscience-directory"] == "/srv/alpha"
        assert "x-openscience-directory" not in seen[3].headers

    run(exercise())


def test_client_creates_a_project_through_the_global_route() -> None:
    """Creation is global and carries no selector, like the project catalog."""

    seen: list[httpx.Request] = []
    created = {
        "id": "prj_c68a67abd3884a99af1365cced363be0",
        "worktree": "/e/wzj/opensciense_projects/b472bf7e-cbd4-417d-8d6c-5a2f3042ab6d",
        "name": "industry_fault_20261006",
        "origin": "openscience",
        "icon": {"color": "lime"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=created)

    async def exercise() -> None:
        client = http_client(handler, directory="/srv/default")
        project = await client.create_project(
            "industry_fault_20261006",
            sources=[{"path": "/data/inputs", "access": "read"}],
            operation_id="6f1e6a4e-0000-4000-8000-000000000000",
        )
        await client.close()
        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == "/global/project"
        # A project is what a selector would point at, so there is none.
        assert "x-openscience-directory" not in request.headers
        assert json.loads(request.content) == {
            "name": "industry_fault_20261006",
            "sources": [{"path": "/data/inputs", "access": "read"}],
            "operation_id": "6f1e6a4e-0000-4000-8000-000000000000",
        }
        assert project == created

    run(exercise())


def test_client_omits_empty_project_options() -> None:
    """A plain named project sends only its name; the server defaults sources."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "prj_1", "worktree": "/srv/one"})

    async def exercise() -> None:
        client = http_client(handler)
        await client.create_project("quarterly")
        await client.close()
        assert json.loads(seen[0].content) == {"name": "quarterly"}

    run(exercise())


def test_client_rejects_a_project_request_the_server_would_refuse() -> None:
    """The local checks exist so a bad request never reaches the server."""

    async def exercise() -> None:
        client = http_client(lambda request: httpx.Response(201, json={}))
        with pytest.raises(ValueError):
            await client.create_project("   ")
        with pytest.raises(ValueError):
            await client.create_project("ok", operation_id="not-a-uuid")
        with pytest.raises(ValueError):
            await client.create_project("ok", sources=[{"path": " "}])
        with pytest.raises(TypeError):
            await client.create_project("ok", sources=[{"path": 7}])
        with pytest.raises(ValueError):
            await client.create_project(
                "ok", sources=[{"path": f"/data/{index}"} for index in range(11)]
            )
        await client.close()

    run(exercise())


def test_client_reports_a_project_operation_conflict_as_http_409() -> None:
    """Reusing an operation id for a different draft is a conflict, not a retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": "project_operation_conflict",
                "message": "This project operation is already bound to a different workspace choice.",
            },
        )

    async def exercise() -> None:
        client = http_client(handler)
        with pytest.raises(OpenScienceHTTPError) as raised:
            await client.create_project(
                "industry_fault_20261006",
                operation_id="6f1e6a4e-0000-4000-8000-000000000000",
            )
        await client.close()
        assert raised.value.status == 409
        assert raised.value.code == "project_operation_conflict"

    run(exercise())


def test_every_per_session_call_carries_its_project() -> None:
    """Every route that addresses one session must name that session's project.

    The server answers 404 for a real session addressed from another project,
    so a missing selector is a silent data loss rather than a slow path.
    """

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/message"):
            return httpx.Response(200, json=[])
        if path == "/runtime/prompt":
            return httpx.Response(202, json={"runID": "run_1", "acceptedAt": 1})
        if path == "/runtime/cancel":
            return httpx.Response(200, json={"runID": "run_1", "state": "cancelled"})
        if path == "/runtime/decision":
            return httpx.Response(200, json={"status": "resolved"})
        if path == "/runtime/run":
            return httpx.Response(200, json={"runID": "run_1", "state": "running"})
        return httpx.Response(200, json={"sessionID": "ses_1", "runs": []})

    async def exercise() -> None:
        client = http_client(handler, directory="/srv/default")
        await client.messages("ses_1", directory="/srv/alpha")
        await client.snapshot("ses_1", directory="/srv/alpha")
        await client.get_run("ses_1", "run_1", directory="/srv/alpha")
        await client.prompt(
            "ses_1", request_id="req_1", message="hi", directory="/srv/alpha"
        )
        await client.cancel_run("ses_1", "run_1", directory="/srv/alpha")
        await client.reply_permission("ses_1", "per_1", "once", directory="/srv/alpha")
        await client.reply_question("ses_1", "que_1", [["yes"]], directory="/srv/alpha")
        await client.reject_question("ses_1", "que_1", directory="/srv/alpha")
        await client.close()
        assert len(seen) == 8
        assert {request.headers["x-openscience-directory"] for request in seen} == {
            "/srv/alpha"
        }

    run(exercise())


def test_event_stream_carries_the_project_selector() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body(event_frame(1)),
        )

    async def exercise() -> None:
        client = http_client(handler, directory="/srv/default")
        events = [item async for item in client.events("ses_1", directory="/srv/alpha")]
        await client.close()
        assert [item.sequence for item in events] == [1]
        assert seen[0].headers["x-openscience-directory"] == "/srv/alpha"

    run(exercise())


def test_prompt_is_addressed_by_request_id() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(202, json={"runID": "run_7", "acceptedAt": 5})

    async def exercise() -> None:
        client = http_client(handler)
        receipt = await client.prompt(
            "ses_1",
            request_id="req_1",
            message="hello",
            model={"providerID": "cc-proxy", "modelID": "gpt-5.6-terra"},
            variant="xhigh",
            effort="ultra",
        )
        await client.close()
        assert receipt == {"runID": "run_7", "acceptedAt": 5}
        assert seen[0] == {
            "sessionID": "ses_1",
            "requestID": "req_1",
            "model": {"providerID": "cc-proxy", "modelID": "gpt-5.6-terra"},
            "variant": "xhigh",
            "effort": "ultra",
            "message": "hello",
        }

    run(exercise())


def test_prompt_omits_the_selection_it_was_not_given() -> None:
    """No field is invented: an absent selection stays absent."""

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(202, json={"runID": "run_7", "acceptedAt": 5})

    async def exercise() -> None:
        client = http_client(handler)
        await client.prompt("ses_1", request_id="req_1", message="hello")
        await client.prompt("ses_1", request_id="req_2", message="hello", effort="normal")
        await client.prompt(
            "ses_1",
            request_id="req_3",
            message="hello",
            model={"providerID": "cc-proxy", "modelID": "gpt-5.6-terra"},
            variant="high",
        )
        await client.close()
        assert seen[0] == {
            "sessionID": "ses_1",
            "requestID": "req_1",
            "message": "hello",
        }
        assert seen[1] == {
            "sessionID": "ses_1",
            "requestID": "req_2",
            "effort": "normal",
            "message": "hello",
        }
        # The variant travels with the model it belongs to, and only then.
        assert seen[2] == {
            "sessionID": "ses_1",
            "requestID": "req_3",
            "model": {"providerID": "cc-proxy", "modelID": "gpt-5.6-terra"},
            "variant": "high",
            "message": "hello",
        }

    run(exercise())


def test_list_providers_reads_the_global_catalog() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=provider_catalog())

    async def exercise() -> None:
        client = http_client(handler, directory="/srv/default")
        payload = await client.list_providers()
        await client.close()
        assert seen[0].url.path == "/config/providers"
        # Providers come from the server's own configuration, not a project.
        assert "x-openscience-directory" not in seen[0].headers
        assert [provider["id"] for provider in payload["providers"]] == [
            "cc-proxy",
            "local-lab",
        ]

    run(exercise())


def test_prompt_requires_a_request_id_and_exactly_one_input() -> None:
    async def exercise() -> None:
        client = http_client(lambda request: httpx.Response(202, json={}))
        with pytest.raises(ValueError):
            await client.prompt("ses_1", request_id="  ", message="hello")
        with pytest.raises(ValueError):
            await client.prompt("ses_1", request_id="req_1")
        with pytest.raises(ValueError):
            await client.prompt("ses_1", request_id="req_1", message="a", parts=[{"type": "text"}])
        with pytest.raises(ValueError):
            await client.prompt("ses_1", request_id="req_1", message="a", effort="turbo")
        with pytest.raises(ValueError):
            await client.prompt("ses_1", request_id="req_1", message="a", variant="  ")
        with pytest.raises(ValueError):
            await client.prompt("ses_1", request_id="req_1", message="a", variant=7)
        with pytest.raises(ValueError):
            await client.prompt(
                "ses_1",
                request_id="req_1",
                message="a",
                model={"providerID": "", "modelID": "gpt-5.6-terra"},
            )
        with pytest.raises(ValueError):
            await client.prompt(
                "ses_1",
                request_id="req_1",
                message="a",
                model={"providerID": "cc-proxy"},
            )
        await client.close()

    run(exercise())


def test_event_stream_yields_sequenced_events_and_skips_replays() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body(
                ": heartbeat",
                event_frame(1),
                event_frame(2, "message.part.updated", part={"id": "prt_1"}),
            ),
        )

    async def exercise() -> None:
        client = http_client(handler)
        events = [item async for item in client.events("ses_1", after_sequence=1)]
        await client.close()
        assert [item.sequence for item in events] == [2]
        assert events[0].properties == {"part": {"id": "prt_1"}}
        assert seen[0].headers["last-event-id"] == "1"
        assert seen[0].url.params["afterSequence"] == "1"
        assert seen[0].url.params["sessionID"] == "ses_1"

    run(exercise())


def test_event_stream_rejects_a_gap() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body(event_frame(1), event_frame(3)),
        )

    async def exercise() -> None:
        client = http_client(handler)
        with pytest.raises(OpenScienceEventGapError):
            [item async for item in client.events("ses_1")]
        await client.close()

    run(exercise())


def test_expired_cursor_is_its_own_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": "cursor_expired", "oldestSequence": 12, "latestSequence": 40},
        )

    async def exercise() -> None:
        client = http_client(handler)
        with pytest.raises(OpenScienceCursorError) as caught:
            [item async for item in client.events("ses_1", after_sequence=3)]
        await client.close()
        assert caught.value.oldest_sequence == 12
        assert caught.value.latest_sequence == 40

    run(exercise())


def test_other_http_failures_are_not_cursor_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_found"})

    async def exercise() -> None:
        client = http_client(handler)
        with pytest.raises(OpenScienceHTTPError) as caught:
            await client.snapshot("ses_1")
        await client.close()
        assert not isinstance(caught.value, OpenScienceCursorError)
        assert caught.value.status == 404

    run(exercise())


def test_event_stream_requires_the_event_stream_content_type() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, json={})

    async def exercise() -> None:
        client = http_client(handler)
        with pytest.raises(OpenScienceProtocolError):
            [item async for item in client.events("ses_1")]
        await client.close()

    run(exercise())


def test_a_partial_final_frame_is_a_connection_failure() -> None:
    """A socket closing mid-event must never apply a shortened event."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'id: 1\nevent: runtime.accepted\ndata: {"sequence":1,',
        )

    async def exercise() -> None:
        client = http_client(handler)
        with pytest.raises(OpenScienceConnectionError):
            [item async for item in client.events("ses_1")]
        await client.close()

    run(exercise())


@pytest.mark.parametrize(
    "frame",
    [
        'id: 2\nevent: runtime.accepted\ndata: {"sequence":1,"type":"runtime.accepted","sessionID":"ses_1","runID":"run_1","time":1,"properties":{}}',
        'id: 1\nevent: runtime.failed\ndata: {"sequence":1,"type":"runtime.accepted","sessionID":"ses_1","runID":"run_1","time":1,"properties":{}}',
        'id: 1\nevent: runtime.accepted\ndata: {"sequence":1,"type":"runtime.accepted","sessionID":"ses_2","runID":"run_1","time":1,"properties":{}}',
        'id: 1\nevent: runtime.accepted\ndata: {"sequence":1,"type":"runtime.accepted","sessionID":"ses_1","runID":"","time":1,"properties":{}}',
        'id: 1\nevent: runtime.accepted\ndata: {"sequence":1,"type":"runtime.accepted","sessionID":"ses_1","runID":"run_1","time":1}',
    ],
)
def test_event_frame_must_agree_with_its_id_and_name(frame: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(frame + "\n\n").encode("utf-8"),
        )

    async def exercise() -> None:
        client = http_client(handler)
        with pytest.raises(OpenScienceProtocolError):
            [item async for item in client.events("ses_1")]
        await client.close()

    run(exercise())


def test_connection_failure_is_reported_as_ambiguous() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def exercise() -> None:
        client = http_client(handler)
        with pytest.raises(OpenScienceConnectionError):
            await client.prompt("ses_1", request_id="req_1", message="hello")
        await client.close()

    run(exercise())


# --- models ----------------------------------------------------------------


def test_real_tool_part_maps_end_to_end() -> None:
    """Every tool state the generated schema defines becomes a platform item.

    Shape source: `tooling/sdk/openapi.json` -> `ToolPart` and `ToolState*`,
    which is what `GET /session/:id/message` returns for a part of type
    `tool` (`id`, `sessionID`, `messageID`, `type`, `callID`, `tool`, `state`).
    """

    messages = [
        message_payload(
            "msg_2",
            "assistant",
            [
                tool_part(TOOL_PENDING, part_id="prt_t1", call_id="call_1"),
                tool_part(TOOL_RUNNING, part_id="prt_t2", call_id="call_2"),
                tool_part(TOOL_COMPLETED, part_id="prt_t3", call_id="call_3"),
                tool_part(TOOL_ERROR, part_id="prt_t4", call_id="call_4"),
            ],
            parent="msg_1",
        )
    ]

    items = models.timeline_items(messages, session_id="sess_x", external_session_id="ses_1")

    assert [item.id for item in items] == [
        "os_tool_call_1",
        "os_tool_call_2",
        "os_tool_call_3",
        "os_tool_call_4",
    ]
    assert [item.type for item in items] == ["tool"] * 4
    assert [item.role for item in items] == ["tool"] * 4
    assert [item.status for item in items] == ["pending", "running", "done", "failed"]
    assert [item.order_seq for item in items] == [1, 2, 3, 4]
    assert all(item.turn_id == "msg_1" for item in items)

    completed = dict(items[2].content)
    assert completed["kind"] == "command"
    assert completed["command"] == "ls -la"
    assert completed["input"] == {"command": "ls -la"}
    assert completed["output"] == "total 0\n"
    assert completed["title"] == "bash"
    assert completed["result"] == {"status": "completed", "title": "ls -la", "callID": "call_3"}
    assert completed["isError"] is False

    failed = dict(items[3].content)
    assert failed["kind"] == "command"
    assert failed["output"] == "exit status 1"
    assert failed["isError"] is True
    assert failed["error"] == "exit status 1"

    assert items[0].source["itemId"] == "prt_t1"
    assert items[0].source["itemType"] == "tool"
    assert items[0].source["event"] == "session.message.tool"
    assert items[0].source["sessionId"] == "ses_1"


def test_a_non_shell_tool_stays_a_generic_tool_call() -> None:
    part = tool_part(
        {
            "status": "completed",
            "input": {"path": "report.md"},
            "output": "written",
            "title": "write",
            "metadata": {},
            "time": {"start": 1, "end": 2},
        },
        tool="write",
        part_id="prt_w",
        call_id="call_w",
    )
    items = models.timeline_items(
        [message_payload("msg_2", "assistant", [part], parent="msg_1")],
        session_id="sess_x",
        external_session_id="ses_1",
    )
    assert len(items) == 1
    assert items[0].content["kind"] == "file_change"
    assert items[0].content["input"] == {"path": "report.md"}


def test_timeline_keeps_user_and_assistant_turns_distinct() -> None:
    messages = [
        message_payload("msg_1", "user", [text_part("prt_1", "hello", "msg_1")]),
        message_payload(
            "msg_2",
            "assistant",
            [
                {"id": "prt_r", "sessionID": "ses_1", "messageID": "msg_2", "type": "reasoning",
                 "text": "thinking", "time": {"start": 1, "end": 2}},
                text_part("prt_2", "hi", "msg_2"),
                {"id": "prt_s", "sessionID": "ses_1", "messageID": "msg_2", "type": "step-finish"},
            ],
            parent="msg_1",
        ),
    ]
    snapshot = models.timeline_snapshot(messages, session_id="sess_x", external_session_id="ses_1")
    assert snapshot.complete is True
    assert [(item.type, item.role, item.turn_id) for item in snapshot.items] == [
        ("message", "user", "msg_1"),
        ("system", "assistant", "msg_1"),
        ("message", "assistant", "msg_1"),
    ]
    assert all(item.status == "done" for item in snapshot.items)
    assert snapshot.items[0].content["text"] == "hello"


def test_a_streaming_assistant_message_is_in_progress() -> None:
    messages = [
        message_payload("msg_2", "assistant", [text_part("prt_2", "partial", "msg_2")], completed=False)
    ]
    items = models.timeline_items(messages, session_id="sess_x", external_session_id="ses_1")
    assert items[0].status == "inProgress"
    assert items[0].role == "assistant"


def test_an_assistant_error_becomes_a_system_item() -> None:
    message = message_payload("msg_2", "assistant", [text_part("prt_2", "oops", "msg_2")], parent="msg_1")
    message["info"]["error"] = {"name": "ProviderError", "message": "upstream refused"}
    items = models.timeline_items([message], session_id="sess_x", external_session_id="ses_1")
    assert [item.type for item in items] == ["message", "system"]
    assert items[1].status == "failed"
    assert items[1].content["kind"] == "error"
    assert items[1].content["message"] == "upstream refused"


def test_interrupted_run_is_reported_as_finished() -> None:
    """crashRecovery is `interrupt`, so this must never read as still running."""

    state = models.session_state(
        snapshot_payload(runs=[run_payload("interrupted")]),
        session_id="sess_x",
        external_session_id="ses_1",
    )
    assert state.status == "idle"
    assert "does not resume" in (state.status_reason or "")
    assert state.metadata["crashRecovery"] == "interrupt"


def test_pending_decisions_make_the_session_wait_for_approval() -> None:
    state = models.session_state(
        snapshot_payload(
            runs=[run_payload("running")],
            permissions=[permission_payload()],
            questions=[question_payload()],
        ),
        session_id="sess_x",
        external_session_id="ses_1",
    )
    assert state.status == "waiting_approval"
    assert state.metadata["pendingPermissions"] == 1
    assert state.metadata["pendingQuestions"] == 1


def test_a_pending_decision_without_a_receipt_still_waits_for_approval() -> None:
    """The snapshot lists live decisions; the receipt may lag behind them."""

    state = models.session_state(
        snapshot_payload(permissions=[permission_payload()]),
        session_id="sess_x",
        external_session_id="ses_1",
    )
    assert state.status == "waiting_approval"


def test_a_failed_run_carries_its_error() -> None:
    state = models.session_state(
        snapshot_payload(
            runs=[
                run_payload(
                    "failed",
                    error={"code": "run_failed", "message": "model refused"},
                )
            ]
        ),
        session_id="sess_x",
        external_session_id="ses_1",
    )
    assert state.status == "error"
    assert state.error == {"code": "run_failed", "message": "model refused"}


def test_question_form_round_trips_labels() -> None:
    form = models.question_form(question_payload())
    assert form.questions[0].prompt == "Which dataset?"
    assert form.questions[0].header == "Dataset"
    assert [option.label for option in form.questions[0].options] == ["calibration", "validation"]
    assert form.questions[0].allow_custom is True
    assert models.question_answers(
        question_payload(), {"answers": {"q_0": {"optionIds": ["o_0"]}}}
    ) == [["calibration"]]
    assert models.question_answers(
        question_payload(), {"answers": {"q_0": {"customText": "plus notes"}}}
    ) == [["plus notes"]]


def test_interaction_target_parsing_and_permission_reply() -> None:
    assert models.interaction_target("notice_openscience_permission_per_1") == ("permission", "per_1")
    assert models.interaction_target("notice_openscience_question_que_1") == ("question", "que_1")
    assert models.interaction_target("notice_something_else") is None
    assert models.permission_reply("once") == "once"
    assert models.permission_reply("project") == "project"
    assert models.permission_reply("something-else") == "reject"


def test_permission_notice_only_offers_scopes_the_request_supports() -> None:
    limited = models.permission_notice(
        permission_payload(always=[]),
        session_id="sess_x",
        external_session_id="ses_1",
    )
    assert [action["actionId"] for action in limited.actions] == ["once", "session", "reject"]
    external = models.permission_notice(
        permission_payload(permission="external_directory", always=[]),
        session_id="sess_x",
        external_session_id="ses_1",
    )
    assert [action["actionId"] for action in external.actions] == [
        "once",
        "session",
        "project",
        "reject",
    ]


def test_declared_capabilities_are_implemented_ones() -> None:
    declared = runtime_capabilities()
    assert declared["sessionDiscovery"] is True
    assert declared["attachments"] is True
    assert declared["interactions"] is True
    assert declared["ipc"] is True
    assert declared["modelCatalog"] is True
    assert declared["createProject"] is True
    assert declared["permissionCatalog"] is False
    assert declared["commands"] is False
    assert declared["steerTurn"] is False


def test_the_create_project_capability_is_published_for_the_client() -> None:
    """The client hides its path field on this flag, so it must be published."""

    async def exercise() -> None:
        host = make_host()
        runtime = build_runtime(host, client=FakeClient())
        capabilities = await runtime.get_runtime_capabilities()
        published = {
            capability.capability_id: capability
            for capability in capabilities.capabilities
        }
        assert published["project.create"].supported is True
        assert published["project.create"].available is True
        assert published["project.create"].scope == "runtime"

    run(exercise())


def test_runtime_creates_a_project_and_reports_the_generated_worktree() -> None:
    """The worktree is the server's answer, never a path the caller proposed."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient()
        runtime = build_runtime(host, client=client)
        try:
            project = await runtime.create_project("industry_fault_20261006")
        finally:
            await runtime.stop()
        assert project.project_id == "prj_new1"
        assert project.name == "industry_fault_20261006"
        assert project.worktree == "/srv/openscience/industry_fault_20261006-1"
        assert project.metadata == {"origin": "openscience", "icon": {"color": "lime"}}
        request = client.project_requests[0]
        assert request["name"] == "industry_fault_20261006"
        assert request["sources"] is None
        # No id was supplied, so one is generated per attempt — and it is a real
        # UUID, because the server validates it as one.
        assert uuid.UUID(request["operation_id"]).version == 4
        # The catalog route is global; a created project is too.
        assert client.calls == ["capabilities", "create_project"]

    run(exercise())


def test_runtime_replays_a_caller_supplied_operation_id() -> None:
    """A retryable caller keeps its idempotency key across attempts."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient()
        runtime = build_runtime(host, client=client)
        operation_id = "6f1e6a4e-0000-4000-8000-000000000000"
        try:
            await runtime.create_project(
                "quarterly",
                sources=[{"path": "/data/inputs", "access": "read"}],
                operation_id=operation_id,
            )
        finally:
            await runtime.stop()
        assert client.project_requests[0] == {
            "name": "quarterly",
            "sources": [{"path": "/data/inputs", "access": "read"}],
            "operation_id": operation_id,
        }

    run(exercise())


def test_runtime_refuses_a_created_project_without_a_worktree() -> None:
    """A project whose directory is unknown cannot be stored as a workspace."""

    async def exercise() -> None:
        host = make_host()
        client = FakeClient()
        client.create_project = AsyncMock(  # type: ignore[method-assign]
            return_value={"id": "prj_1", "name": "half"}
        )
        runtime = build_runtime(host, client=client)
        try:
            with pytest.raises(RuntimeUpstreamError):
                await runtime.create_project("half")
        finally:
            await runtime.stop()

    run(exercise())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OpenScienceHTTPError(400, {"error": "invalid_request"}), RuntimeInvalidRequestError),
        (OpenScienceHTTPError(409, {"error": "project_operation_conflict"}), RuntimeConflictError),
        (OpenScienceHTTPError(500, {"error": "boom"}), RuntimeUpstreamError),
        (OpenScienceConnectionError("gone"), RuntimeUnavailableError),
    ],
)
def test_runtime_maps_project_creation_failures_onto_protocol_errors(
    error: Exception, expected: type[Exception]
) -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient()
        client.create_project_error = error
        runtime = build_runtime(host, client=client)
        try:
            with pytest.raises(expected):
                await runtime.create_project("anything")
        finally:
            await runtime.stop()

    run(exercise())


def test_the_model_catalog_publishes_both_model_and_effort_capabilities() -> None:
    """The effort picker is the catalog's reasoning items, so it is advertised too."""

    async def exercise() -> None:
        host = make_host()
        runtime = build_runtime(host, client=FakeClient())
        capabilities = await runtime.get_runtime_capabilities()
        published = {
            capability.capability_id: capability for capability in capabilities.capabilities
        }
        assert published["catalog.model"].supported is True
        assert published["catalog.model"].available is True
        assert published["catalog.effort"].supported is True
        assert published["catalog.effort"].available is True
        assert published["catalog.permission"].supported is False

    run(exercise())


# --- relay -----------------------------------------------------------------


def relay_for(
    host: SimpleNamespace,
    client: FakeClient,
    *,
    max_streams: int = 24,
) -> tuple[OpenScienceRuntime, OpenScienceRelay]:
    runtime = build_runtime(host, client=client)
    return runtime, OpenScienceRelay(
        client=client, host=host, runtime=runtime, max_streams=max_streams
    )


def test_relay_publishes_the_inventory_and_announces_running() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        _, relay = relay_for(host, client)
        assert await relay._inventory() is True
        assert host.session_meta_upsert.await_count == 1
        assert host.session_source_update.await_count == 1
        assert host.runtime_health_update.await_args.args[0] == "running"
        assert "ses_1" in relay._streams

        # A second inventory tick must not re-announce an unchanged session.
        assert await relay._inventory() is True
        assert host.session_meta_upsert.await_count == 1
        await relay.close()

    run(exercise())


def test_relay_starts_a_stream_and_prunes_deleted_sessions() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        _, relay = relay_for(host, client)
        await relay._inventory()
        stream = relay._streams["ses_1"]
        client.sessions = []
        await relay._inventory()
        assert relay._streams == {}
        await asyncio.sleep(0.05)
        assert stream.done()
        await relay.close()

    run(exercise())


def test_one_unreadable_session_does_not_stop_the_inventory() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_bad"), session_payload("ses_good")])
        client.fail_snapshot.add("ses_bad")
        _, relay = relay_for(host, client, max_streams=0)
        assert await relay._inventory() is True
        assert "ses_bad" not in relay._streams
        published = {call.kwargs["external_session_id"] for call in host.session_meta_upsert.await_args_list}
        assert published == {"ses_bad", "ses_good"}
        await relay.close()

    run(exercise())


def test_cursor_gap_resnapshots_instead_of_reprompting() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload("ses_1", runs=[run_payload("running")], latest=9)},
            messages={"ses_1": [message_payload("msg_1", "user", [text_part("prt_1", "hi", "msg_1")])]},
        )
        client.gap_sessions.add("ses_1")
        _, relay = relay_for(host, client)
        await host.sync_state_write("openscience/events/ses_1", {"sequence": 4})
        await relay._consume("ses_1")
        assert client.prompts == []
        assert relay._cursors["ses_1"] == 9
        assert "snapshot:ses_1" in client.calls
        assert host.timeline_sync.await_count == 1
        await relay.close()

    run(exercise())


def test_an_expired_cursor_resnapshots_too() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload("ses_1", latest=40)},
        )
        client.expired_sessions.add("ses_1")
        _, relay = relay_for(host, client)
        await relay._consume("ses_1")
        assert client.prompts == []
        assert relay._cursors["ses_1"] == 40
        await relay.close()

    run(exercise())


def test_the_cursor_is_persisted_after_applying_events() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        _, relay = relay_for(host, client)
        await relay._apply("ses_1", event(7, part={"id": "prt_1"}))
        await relay._flush_cursors()
        assert host.sync_state["openscience/events/ses_1"] == {
            "sequence": 7,
            "runtime": "openscience",
        }
        await relay.close()

    run(exercise())


def test_a_replayed_event_is_deduplicated() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload("ses_1", latest=7)},
        )
        _, relay = relay_for(host, client)
        relay._cursors["ses_1"] = 7
        await relay._apply("ses_1", event(7, type="runtime.completed"))
        assert host.session_state_update.await_count == 0
        assert host.session_turn_ended.await_count == 0
        assert client.prompts == []
        await relay.close()

    run(exercise())


def test_a_terminal_run_event_ends_the_turn() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload("ses_1", runs=[run_payload("completed")], latest=3)},
        )
        _, relay = relay_for(host, client)
        await relay._apply("ses_1", event(3, type="runtime.completed", messageID="msg_9"))
        assert host.session_turn_ended.await_args.kwargs["outcome"] == "completed"
        assert host.session_turn_ended.await_args.kwargs["turn_id"] == "run_1"
        state = host.session_state_update.await_args.kwargs
        assert state["status"] == "idle"
        await relay.close()

    run(exercise())


def test_a_turn_that_ended_while_unsubscribed_is_still_reported() -> None:
    """A run can finish before the Connector ever subscribes to its session.

    The durable receipt decides that the turn ended, and the persisted marker
    keeps the report to exactly one per run.
    """

    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload("ses_1", runs=[run_payload("completed")], latest=9)},
        )
        _, relay = relay_for(host, client)
        await relay._resnapshot("ses_1")
        assert host.session_turn_ended.await_count == 1
        assert host.session_turn_ended.await_args.kwargs["outcome"] == "completed"
        assert host.session_turn_ended.await_args.kwargs["turn_id"] == "run_1"
        assert host.sync_state["openscience/runs/ses_1"]["reportedRunId"] == "run_1"

        await relay._resnapshot("ses_1")
        assert host.session_turn_ended.await_count == 1
        await relay.close()

    run(exercise())


def test_an_interrupted_run_is_reported_as_interrupted() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload("ses_1", runs=[run_payload("interrupted")], latest=9)},
        )
        _, relay = relay_for(host, client)
        await relay._resnapshot("ses_1")
        assert host.session_turn_ended.await_args.kwargs["outcome"] == "interrupted"
        await relay.close()

    run(exercise())


def test_a_failed_run_event_reports_the_failure() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={
                "ses_1": snapshot_payload(
                    "ses_1",
                    runs=[run_payload("failed", error={"code": "run_failed", "message": "boom"})],
                    latest=3,
                )
            },
        )
        _, relay = relay_for(host, client)
        await relay._apply("ses_1", event(3, type="runtime.failed", message="boom"))
        assert host.session_turn_ended.await_args.kwargs["outcome"] == "failed"
        assert host.session_state_update.await_args.kwargs["status"] == "error"
        await relay.close()

    run(exercise())


def test_a_decision_event_republishes_the_pending_requests() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={
                "ses_1": snapshot_payload(
                    "ses_1",
                    runs=[run_payload("running")],
                    permissions=[permission_payload()],
                    latest=4,
                )
            },
        )
        _, relay = relay_for(host, client)
        await relay._apply("ses_1", event(4, type="permission.asked"))
        assert host.notice_upsert.await_count == 1
        assert host.session_state_update.await_args.kwargs["status"] == "waiting_approval"
        await relay.close()

    run(exercise())


def test_a_message_event_refreshes_the_transcript_once() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            messages={"ses_1": [message_payload("msg_1", "user", [text_part("prt_1", "hi", "msg_1")])]},
        )
        _, relay = relay_for(host, client)
        await relay._apply("ses_1", event(5, part={"id": "prt_1"}))
        assert "ses_1" in relay._dirty_timeline
        relay._dirty_timeline["ses_1"] -= 5
        await relay._flush_timelines()
        assert host.timeline_sync.await_count == 1
        assert host.timeline_sync.await_args.kwargs["complete"] is True
        assert "ses_1" not in relay._dirty_timeline
        await relay.close()

    run(exercise())


def test_a_transcript_read_failure_does_not_stop_the_relay() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])
        client.fail_messages.add("ses_1")
        _, relay = relay_for(host, client)
        relay._dirty_timeline["ses_1"] = 0.0
        await relay._flush_timelines()
        assert host.timeline_sync.await_count == 0
        assert await relay._inventory() is True
        await relay.close()

    run(exercise())


def test_repeated_inventory_failures_detach_and_reprobe() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient()
        client.list_sessions = AsyncMock(side_effect=OpenScienceConnectionError("gone"))
        runtime, relay = relay_for(host, client)
        runtime._client = client
        runtime._relay = relay
        for _ in range(2):
            assert await relay._inventory() is True
        assert await relay._inventory() is False
        assert runtime._client is None
        assert runtime._relay is None
        assert host.runtime_health_update.await_args.args[0] == "error"
        assert runtime._restart_task is not None
        await runtime.stop()

    run(exercise())


def test_resynchronize_forces_a_snapshot_without_prompting() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(
            sessions=[session_payload("ses_1")],
            snapshots={"ses_1": snapshot_payload("ses_1", latest=11)},
        )
        runtime, relay = relay_for(host, client)
        runtime._relay = relay
        await runtime.resynchronize("sess_x", "ses_1")
        assert relay._cursors["ses_1"] == 11
        assert client.prompts == []
        await relay.close()

    run(exercise())


def test_runtime_operations_fail_cleanly_without_a_server() -> None:
    async def exercise() -> None:
        host = make_host()
        runtime = build_runtime(host, available=False, reason=discovery.UNAVAILABLE_REASON)
        with pytest.raises(RuntimeUnavailableError):
            await runtime.list_sessions()
        with pytest.raises(RuntimeUnavailableError):
            await runtime.get_session_state("sess_x", "ses_1")
        assert runtime.sync_mode == "events"

    run(exercise())


def test_a_conflicting_prompt_is_reported_as_a_conflict() -> None:
    async def exercise() -> None:
        host = make_host()
        client = FakeClient(sessions=[session_payload("ses_1")])

        async def conflicting(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise OpenScienceHTTPError(409, {"error": "request_conflict"})

        client.prompt = conflicting  # type: ignore[method-assign]
        runtime = build_runtime(host, client=client)
        await runtime.start()
        try:
            with pytest.raises(RuntimeConflictError):
                await runtime.start_turn("sess_x", "ses_1", "hello", client_message_id="cm_1")
        finally:
            await runtime.stop()

    run(exercise())


def test_unsupported_operations_are_not_advertised() -> None:
    async def exercise() -> None:
        host = make_host()
        runtime = build_runtime(host, client=FakeClient())
        await runtime.start()
        try:
            with pytest.raises(RuntimeUnsupportedError):
                await runtime.steer_turn("sess_x", "ses_1", "more")
            with pytest.raises(RuntimeUnsupportedError):
                await runtime.list_permission_catalog()
            with pytest.raises(RuntimeUnsupportedError):
                await runtime.create_and_start_session(
                    "sess_x", "hello", runtime_options={"model": "x"}
                )
        finally:
            await runtime.stop()

    run(exercise())


def test_result_helpers_are_plain_dataclasses() -> None:
    result = RuntimeOperationResult(ok=True, result={"runID": "run_1"})
    assert result.result["runID"] == "run_1"


# --- multi-project inventory -------------------------------------------------

# The server's own working-directory project is not in `/project`: it was never
# created through the app, so only an unscoped listing reaches it.
CURRENT_PROJECT = "/srv/openscience-source"
PROJECT_ALPHA = "/srv/projects/alpha"
PROJECT_BETA = "/srv/projects/beta"


def project_payload(project_id: str, worktree: str, name: str) -> dict[str, Any]:
    """A `GET /project` entry, shaped like the server's own `Project.Info`."""

    return {
        "id": project_id,
        "worktree": worktree,
        "sandboxes": [],
        "name": name,
        "origin": "openscience",
        "time": {"created": 1, "updated": 2, "activity": 3},
    }


def multi_project_client(
    *,
    client_class: type[FakeClient] = FakeClient,
    **overrides: Any,
) -> FakeClient:
    """The server's own project plus two app-created ones, each with sessions."""

    return client_class(
        sessions=[
            session_payload("ses_cur", directory=CURRENT_PROJECT),
            session_payload("ses_a1", directory=PROJECT_ALPHA),
            session_payload("ses_a2", directory=PROJECT_ALPHA),
            session_payload("ses_b1", directory=PROJECT_BETA),
        ],
        projects=[
            project_payload("prj_a", PROJECT_ALPHA, "alpha"),
            project_payload("prj_b", PROJECT_BETA, "beta"),
        ],
        current_directory=CURRENT_PROJECT,
        **overrides,
    )


def connected(host: SimpleNamespace, client: FakeClient) -> OpenScienceRuntime:
    """A runtime already attached to the fake server, without a relay task."""

    runtime = build_runtime(host, client=client)
    runtime._client = client
    return runtime


def connected_relay(
    host: SimpleNamespace,
    client: FakeClient,
    *,
    max_streams: int = 0,
) -> tuple[OpenScienceRuntime, OpenScienceRelay]:
    runtime = connected(host, client)
    return runtime, OpenScienceRelay(
        client=client, host=host, runtime=runtime, max_streams=max_streams
    )


def test_the_inventory_covers_every_project_and_keeps_each_cwd() -> None:
    async def exercise() -> None:
        host = make_host()
        client = multi_project_client()
        runtime = connected(host, client)
        inventory = await runtime.list_complete_session_inventory()
        assert {meta.external_session_id: meta.cwd for meta in inventory} == {
            "ses_cur": CURRENT_PROJECT,
            "ses_a1": PROJECT_ALPHA,
            "ses_a2": PROJECT_ALPHA,
            "ses_b1": PROJECT_BETA,
        }
        assert {
            scope for method, _, scope in client.scopes if method == "list_sessions"
        } == {UNSCOPED, PROJECT_ALPHA, PROJECT_BETA}

    run(exercise())


def test_the_relay_publishes_every_project_with_its_directory() -> None:
    """`cwd` is what groups sessions into projects on the platform."""

    async def exercise() -> None:
        host = make_host()
        client = multi_project_client()
        _, relay = connected_relay(host, client)
        assert await relay._inventory() is True
        published = {
            call.kwargs["external_session_id"]: call.kwargs["cwd"]
            for call in host.session_meta_upsert.await_args_list
        }
        assert published == {
            "ses_cur": CURRENT_PROJECT,
            "ses_a1": PROJECT_ALPHA,
            "ses_a2": PROJECT_ALPHA,
            "ses_b1": PROJECT_BETA,
        }
        await relay.close()

    run(exercise())


def test_per_session_calls_are_scoped_to_the_sessions_project() -> None:
    async def exercise() -> None:
        host = make_host()
        client = multi_project_client(
            messages={
                "ses_a1": [
                    message_payload("msg_1", "user", [text_part("prt_1", "hi", "msg_1")])
                ]
            },
            snapshots={
                "ses_a1": snapshot_payload(
                    "ses_a1",
                    runs=[run_payload("running", session_id="ses_a1")],
                    permissions=[permission_payload()],
                )
            },
        )
        runtime, relay = connected_relay(host, client)
        await relay._inventory()
        platform = runtime._platform_ids["ses_a1"]
        await runtime.get_session_snapshot(platform, "ses_a1")
        await runtime.get_session_state(platform, "ses_a1")
        await runtime.start_turn(platform, "ses_a1", "hello", client_message_id="cm_a")
        await runtime.interrupt_session(platform)
        await runtime.respond_interaction(
            platform, "notice_openscience_permission_per_1", "session"
        )
        await relay._consume("ses_a1")
        await relay._refresh_timeline(platform, "ses_a1")
        await relay.close()
        for method in (
            "messages",
            "snapshot",
            "prompt",
            "cancel_run",
            "reply_permission",
            "events",
        ):
            assert client.scope_of(method, "ses_a1") == PROJECT_ALPHA, method

    run(exercise())


def test_a_created_session_is_addressed_in_its_own_project() -> None:
    """The create receipt names the project, so the first prompt is scoped too."""

    async def exercise() -> None:
        host = make_host()
        client = multi_project_client()
        runtime = connected(host, client)
        await runtime.create_and_start_session(
            "sess_new", "hello", client_message_id="cm_new"
        )
        assert client.scope_of("prompt", "ses_new1") == "/private/tmp/osproj"

    run(exercise())


def test_a_project_that_fails_to_list_does_not_stop_the_others() -> None:
    async def exercise() -> None:
        host = make_host()
        client = multi_project_client()
        runtime, relay = connected_relay(host, client, max_streams=24)
        assert await relay._inventory() is True
        assert set(relay._streams) == {"ses_cur", "ses_a1", "ses_a2", "ses_b1"}
        stream = relay._streams["ses_b1"]
        client.fail_list.add(PROJECT_BETA)
        assert await relay._inventory() is True
        published = {
            call.kwargs["external_session_id"]
            for call in host.session_meta_upsert.await_args_list
        }
        assert published == {"ses_cur", "ses_a1", "ses_a2", "ses_b1"}
        # The unreadable project's sessions are pruned rather than frozen, and
        # the failure is not mistaken for the server itself going away.
        assert set(relay._streams) == {"ses_cur", "ses_a1", "ses_a2"}
        assert relay._inventory_failures == 0
        assert host.runtime_error.await_count == 0
        assert runtime._client is client
        await asyncio.sleep(0.05)
        assert stream.done()
        await relay.close()

    run(exercise())


def test_a_project_catalog_failure_still_lists_the_current_project() -> None:
    async def exercise() -> None:
        host = make_host()
        client = multi_project_client()
        client.fail_projects = True
        runtime = connected(host, client)
        inventory = await runtime.list_complete_session_inventory()
        assert [meta.external_session_id for meta in inventory] == ["ses_cur"]
        assert inventory[0].cwd == CURRENT_PROJECT

    run(exercise())


def test_the_inventory_reports_a_lost_server_when_no_project_answers() -> None:
    async def exercise() -> None:
        host = make_host()
        client = multi_project_client()
        client.fail_list.update({CURRENT_PROJECT, PROJECT_ALPHA, PROJECT_BETA})
        runtime, relay = connected_relay(host, client)
        assert await relay._inventory() is True
        assert relay._inventory_failures == 1
        with pytest.raises(RuntimeUnavailableError):
            await runtime.list_complete_session_inventory()
        await relay.close()

    run(exercise())


class DuplicatingClient(FakeClient):
    """A server whose two projects both report the same session id."""

    async def list_sessions(self, *, directory: Any = None) -> list[dict[str, Any]]:
        listed = await super().list_sessions(directory=directory)
        if directory == PROJECT_BETA:
            listed.append(session_payload("ses_a1", directory=PROJECT_ALPHA))
        return listed


def test_a_session_reported_by_two_projects_is_published_once() -> None:
    async def exercise() -> None:
        host = make_host()
        client = multi_project_client(client_class=DuplicatingClient)
        runtime, relay = connected_relay(host, client, max_streams=24)
        assert await relay._inventory() is True
        inventory = await runtime.list_complete_session_inventory()
        ids = [meta.external_session_id for meta in inventory]
        assert ids == ["ses_cur", "ses_a1", "ses_a2", "ses_b1"]
        assert len(ids) == len(set(ids))
        published = [
            call.kwargs["external_session_id"]
            for call in host.session_meta_upsert.await_args_list
        ]
        assert published.count("ses_a1") == 1
        # The directory it was first discovered in is the one kept, so its
        # per-session calls keep going to the project that really owns it.
        assert runtime._directory_for("ses_a1") == PROJECT_ALPHA
        await relay.close()

    run(exercise())
