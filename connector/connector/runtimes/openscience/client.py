"""Async HTTP and SSE transport for the OpenScience runtime protocol.

This client only ever talks to a server somebody else started: OpenScience owns
the agent loop, the permissions and the event journal, and the Connector is a
reader of them. Three protocol properties shape the whole module:

* a prompt is admitted by ``requestID``, so a transport failure after a
  submission is ambiguous rather than failed. Nothing here retries a command;
  recovery means re-reading the receipt with the same id;
* events are sequenced per session and retained in a bounded window, so a
  cursor can expire. That is reported as its own error kind, because the only
  correct recovery is a fresh snapshot;
* the server scopes *every* route to one project, selected by
  ``x-openscience-directory``. A session that lives in another project is a 404
  on the per-session routes even though its id is real, so the selector is a
  per-call argument here rather than a fixed client header.
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from connector.runtimes.openscience import discovery, models, provider_config

# Payload ceilings mirror the reference SDK: a runtime response is bounded, and
# a single SSE frame that exceeds its budget is a protocol failure rather than
# something to hold in memory.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 4 * 1024 * 1024

# The server heartbeats an idle subscription every 30 seconds. A read that
# stays silent for three heartbeat periods means the stream is gone; the caller
# resumes from its persisted cursor.
STREAM_READ_TIMEOUT_SECONDS = 90.0

# ``POST /global/project`` refuses more than ten source locations, and the
# server is the authority on that bound: checking it here turns a rejected
# round trip into an immediate, attributable error.
MAX_PROJECT_SOURCES = 10


class OpenScienceError(RuntimeError):
    """Base class for every failure this transport reports."""


class OpenScienceProtocolError(OpenScienceError):
    """The response cannot be interpreted as the advertised protocol."""


class OpenScienceConnectionError(OpenScienceError):
    """The transport failed; a submitted command may already have been accepted."""


class OpenScienceHTTPError(OpenScienceError):
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        self.code = body.get("error") if isinstance(body, Mapping) else None
        super().__init__(
            f"OpenScience HTTP {status}" + (f": {self.code}" if self.code else "")
        )


class OpenScienceCursorError(OpenScienceHTTPError):
    """The event cursor left the retained window; take a new snapshot."""

    @property
    def oldest_sequence(self) -> int | None:
        value = self.body.get("oldestSequence") if isinstance(self.body, Mapping) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def latest_sequence(self) -> int | None:
        value = self.body.get("latestSequence") if isinstance(self.body, Mapping) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class OpenScienceEventGapError(OpenScienceProtocolError):
    """A live frame skipped past the cursor, so the journal has a hole."""

    def __init__(self, expected: int, received: int) -> None:
        self.expected = expected
        self.received = received
        super().__init__(
            f"runtime event gap: expected {expected}, received {received}"
        )


@dataclass(frozen=True, slots=True)
class OpenScienceEvent:
    """One sequenced journal entry, already validated against its SSE frame."""

    sequence: int
    type: str
    session_id: str
    run_id: str
    time: int
    properties: Mapping[str, Any]


class _Unscoped:
    """Type of :data:`UNSCOPED`; a distinct type keeps the scope seam honest."""


# ``None`` selects the client's configured default project. This marker instead
# suppresses the selector, which is how the server's own working-directory
# project — the one its sessions live in when it was never given a directory —
# and the global project catalog are reached even when a default is configured.
UNSCOPED: Final[_Unscoped] = _Unscoped()

# One call's project selector: an explicit directory, the configured default
# (``None``), or no selector at all (:data:`UNSCOPED`).
ProjectScope = str | None | _Unscoped


def _headers(values: Mapping[str, Any]) -> dict[str, str]:
    """Mirror the discovery probe's credentials, minus the project selector.

    The server resolves the project from ``x-openscience-directory`` exactly as
    it does from the ``directory`` query parameter, but a client-wide header
    would pin every call to one project — including the per-session calls of a
    session that lives elsewhere. The selector is therefore attached per
    request by :meth:`OpenScienceClient._scope_headers`, and only the
    credentials that never vary stay on the pool.
    """

    headers = {"accept": "application/json"}
    token = values.get("authToken")
    if isinstance(token, str) and token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _timeout_seconds(values: Mapping[str, Any]) -> float:
    value = values.get("requestTimeoutMs")
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0.1, value / 1000)
    return provider_config.DEFAULT_REQUEST_TIMEOUT_MS / 1000


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OpenScienceProtocolError("runtime response is not valid JSON") from error


def _decode_error(status: int, raw: bytes) -> OpenScienceHTTPError:
    body: Any = None
    if len(raw) <= MAX_RESPONSE_BYTES:
        try:
            body = _decode(raw)
        except OpenScienceProtocolError:
            body = None
    if (
        status == 409
        and isinstance(body, Mapping)
        and body.get("error") in ("cursor_expired", "cursor_ahead")
    ):
        return OpenScienceCursorError(status, body)
    return OpenScienceHTTPError(status, body)


def _parse_event(
    data: bytes, *, event_id: str, event_type: str, session_id: str
) -> OpenScienceEvent:
    """Validate one frame against the sequence contract before it is applied.

    An SSE id that disagrees with the body, or a name that disagrees with the
    payload type, means the stream is not the protocol this client speaks.
    Accepting it would corrupt the persisted cursor.
    """

    parsed = _decode(data)
    if not isinstance(parsed, dict):
        raise OpenScienceProtocolError("runtime SSE data must be an object")
    sequence = parsed.get("sequence")
    if type(sequence) is not int or sequence < 1 or event_id != str(sequence):
        raise OpenScienceProtocolError("SSE id must match a positive runtime sequence")
    if not isinstance(parsed.get("type"), str) or event_type != parsed["type"]:
        raise OpenScienceProtocolError("SSE event name must match the runtime event type")
    for key in ("sessionID", "runID"):
        if not isinstance(parsed.get(key), str) or not parsed[key]:
            raise OpenScienceProtocolError("runtime event is missing its run identity")
    if parsed["sessionID"] != session_id:
        raise OpenScienceProtocolError("runtime stream contains a different session")
    if type(parsed.get("time")) is not int or not isinstance(parsed.get("properties"), dict):
        raise OpenScienceProtocolError("runtime event is missing its timestamp or properties")
    return OpenScienceEvent(
        sequence=sequence,
        type=parsed["type"],
        session_id=parsed["sessionID"],
        run_id=parsed["runID"],
        time=parsed["time"],
        properties=parsed["properties"],
    )


async def _iter_sse(
    response: httpx.Response, session_id: str
) -> AsyncIterator[OpenScienceEvent]:
    """Decode an event stream, dispatching only on a complete blank-line frame.

    A socket that closes mid-frame must not be applied as a shorter event, so a
    trailing partial frame is reported as a connection failure instead.
    """

    data: list[str] = []
    event_id = ""
    event_type = "message"
    size = 0
    async for line in response.aiter_lines():
        size += len(line.encode("utf-8")) + 1
        if size > MAX_EVENT_BYTES:
            raise OpenScienceProtocolError("runtime SSE event exceeds the size limit")
        if not line:
            if data:
                yield _parse_event(
                    "\n".join(data).encode("utf-8"),
                    event_id=event_id,
                    event_type=event_type,
                    session_id=session_id,
                )
            data, event_id, event_type, size = [], "", "message", 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "data":
            data.append(value)
        elif field == "event":
            event_type = value
        elif field == "id" and "\x00" not in value:
            event_id = value
    if data:
        raise OpenScienceConnectionError("runtime event stream ended within an SSE event")


class OpenScienceClient:
    """One connection pool speaking runtime protocol 1.0 to one server."""

    def __init__(
        self,
        *,
        base_url: str,
        values: Mapping[str, Any],
        request_timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.values = dict(values)
        self.request_timeout = (
            request_timeout if request_timeout is not None else _timeout_seconds(values)
        )
        self.default_directory = _optional_directory(values.get("directory"))
        self._headers = _headers(values)
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    def _scope_headers(self, directory: ProjectScope) -> dict[str, str]:
        """Resolve one call's project selector.

        ``None`` falls back to the configured ``directory`` so a runtime pinned
        to one project keeps behaving exactly as before, while
        :data:`UNSCOPED` sends nothing and lets the server use its own working
        directory's project.
        """

        if isinstance(directory, _Unscoped):
            return {}
        resolved = self.default_directory if directory is None else directory
        return {"x-openscience-directory": resolved} if resolved else {}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(
                    self.request_timeout, connect=min(10.0, self.request_timeout)
                ),
                follow_redirects=False,
                transport=self._transport,
            )
        return self._http

    async def close(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        directory: ProjectScope = None,
    ) -> Any:
        try:
            response = await self._client().request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
                headers=self._scope_headers(directory),
            )
        except (httpx.HTTPError, OSError) as error:
            raise OpenScienceConnectionError(
                "runtime connection failed; a submitted command may already be accepted"
            ) from error
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise OpenScienceProtocolError("runtime response exceeds the size limit")
        if response.status_code >= 400:
            raise _decode_error(response.status_code, response.content)
        if not response.content:
            return None
        return _decode(response.content)

    async def capabilities(self) -> dict[str, Any]:
        body = await self._request("GET", discovery.CAPABILITIES_PATH)
        if not isinstance(body, dict):
            raise OpenScienceProtocolError("runtime capabilities must be an object")
        return body

    async def list_projects(self) -> list[dict[str, Any]]:
        """List the app-created projects this server owns.

        The route is global: the server accepts the legacy ``directory`` query
        and never resolves it, so the catalog is read with no selector at all
        and stays complete even when this client has a default project.
        """

        return _object_array(
            await self._request("GET", "/project", directory=UNSCOPED), "projects"
        )

    async def list_providers(self) -> dict[str, Any]:
        """Read the model catalog the server itself is configured with.

        The route is global for the same reason ``/project`` is: providers are
        resolved from OpenScience's own configuration, not from a project, so
        the catalog is read with no selector and a client pinned to one project
        still sees every provider. The payload is returned as it arrived — the
        mapping onto the Connector's catalog is ``models.model_catalog``.
        """

        return _object(
            await self._request("GET", "/config/providers", directory=UNSCOPED),
            "provider catalog",
        )

    async def create_project(
        self,
        name: str,
        *,
        sources: Sequence[Mapping[str, Any]] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an app-managed project whose directory the server generates.

        The route is ``POST /global/project`` — global, like ``/project`` and
        ``/config/providers``, because a project is what a selector would point
        at and it does not exist yet. Nothing here derives a path: the server
        owns project identity and answers with the ``worktree`` it chose, which
        is what the caller stores as the project's workspace.

        ``operation_id`` is OpenScience's idempotency key: replaying it with the
        same name and sources returns the original project (200) instead of
        creating a second one, while reusing it for a different draft is a 409.
        A caller that may retry should therefore supply its own id and persist
        it before the call; one is generated per attempt otherwise, which is
        enough for a single fire-and-forget creation.
        """

        if not isinstance(name, str) or not name.strip():
            raise ValueError("a project name is required")
        body: dict[str, Any] = {"name": name}
        if sources is not None:
            body["sources"] = _project_sources(sources)
        if operation_id is not None:
            try:
                uuid.UUID(operation_id)
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("operation_id must be a UUID") from error
            body["operation_id"] = operation_id
        return _object(
            await self._request(
                "POST", "/global/project", body=body, directory=UNSCOPED
            ),
            "project",
        )

    async def list_sessions(self, *, directory: ProjectScope = None) -> list[dict[str, Any]]:
        """List one project's sessions; the route is not paginated.

        The server answers for exactly one project, so the inventory has to ask
        once per project directory rather than trusting the default.
        """

        return _object_array(
            await self._request("GET", "/session", directory=directory), "sessions"
        )

    async def create_session(
        self,
        *,
        title: str | None = None,
        workspace: str | None = None,
        directory: ProjectScope = None,
    ) -> dict[str, Any]:
        """Create a session in one project.

        The receipt names the project the server actually chose, and the
        runtime remembers it from there, so every later call for this session
        is scoped by its own directory rather than by this request.
        """

        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if workspace:
            body["workspace"] = workspace
        return _object(
            await self._request("POST", "/session", body=body, directory=directory),
            "session",
        )

    async def messages(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        directory: ProjectScope = None,
    ) -> list[dict[str, Any]]:
        params = {"limit": limit} if limit is not None else None
        return _object_array(
            await self._request(
                "GET",
                f"/session/{_segment(session_id)}/message",
                params=params,
                directory=directory,
            ),
            "messages",
        )

    async def snapshot(
        self, session_id: str, *, directory: ProjectScope = None
    ) -> dict[str, Any]:
        return _object(
            await self._request(
                "GET",
                "/runtime/snapshot",
                params={"sessionID": session_id},
                directory=directory,
            ),
            "snapshot",
        )

    async def get_run(
        self, session_id: str, run_id: str, *, directory: ProjectScope = None
    ) -> dict[str, Any]:
        return _object(
            await self._request(
                "GET",
                "/runtime/run",
                params={"sessionID": session_id, "runID": run_id},
                directory=directory,
            ),
            "run",
        )

    async def prompt(
        self,
        session_id: str,
        *,
        request_id: str,
        message: str | None = None,
        parts: list[dict[str, Any]] | None = None,
        model: Mapping[str, str] | None = None,
        variant: str | None = None,
        effort: str | None = None,
        message_id: str | None = None,
        directory: ProjectScope = None,
    ) -> dict[str, Any]:
        """Admit one run, optionally pinning the model, its reasoning level and effort.

        ``model``, ``variant`` and ``effort`` are omitted when not supplied so
        the server applies its own configuration; the caller decides that,
        because only it knows whether the user actually chose something. When
        they are supplied they are passed through verbatim — OpenScience owns
        the catalog, the per-model variant vocabulary and the research-effort
        enum, and translating any of them here would run the user's turn at a
        level they did not pick.

        ``variant`` names one of the chosen model's own ``variants`` and
        ``effort`` is OpenScience's research effort: two independent axes, so
        neither is ever derived from the other.
        """

        if not request_id or not request_id.strip():
            raise ValueError("a persisted requestID is required before submitting work")
        if (message is None) == (parts is None):
            raise ValueError("supply exactly one of message or parts")
        if variant is not None and (not isinstance(variant, str) or not variant.strip()):
            raise ValueError("variant must be a non-empty string")
        if effort is not None and effort not in models.OPENSCIENCE_EFFORTS:
            raise ValueError(
                "effort must be one of " + ", ".join(models.OPENSCIENCE_EFFORTS)
            )
        if model is not None:
            provider_id = model.get("providerID")
            model_id = model.get("modelID")
            if not isinstance(provider_id, str) or not provider_id:
                raise ValueError("model.providerID must be a non-empty string")
            if not isinstance(model_id, str) or not model_id:
                raise ValueError("model.modelID must be a non-empty string")
        body: dict[str, Any] = {
            "sessionID": session_id,
            "requestID": request_id,
        }
        if model is not None:
            body["model"] = {
                "providerID": model["providerID"],
                "modelID": model["modelID"],
            }
        if variant is not None:
            body["variant"] = variant
        if effort is not None:
            body["effort"] = effort
        if message is not None:
            body["message"] = message
        if parts is not None:
            body["parts"] = parts
        if message_id is not None:
            body["messageID"] = message_id
        return _object(
            await self._request(
                "POST", "/runtime/prompt", body=body, directory=directory
            ),
            "receipt",
        )

    async def cancel_run(
        self, session_id: str, run_id: str, *, directory: ProjectScope = None
    ) -> dict[str, Any]:
        return _object(
            await self._request(
                "POST",
                "/runtime/cancel",
                body={"sessionID": session_id, "runID": run_id},
                directory=directory,
            ),
            "run",
        )

    async def reply_permission(
        self,
        session_id: str,
        request_id: str,
        reply: str,
        *,
        directory: ProjectScope = None,
    ) -> dict[str, Any]:
        if reply not in ("once", "session", "project", "always", "reject"):
            raise ValueError("invalid permission reply")
        return _object(
            await self._request(
                "POST",
                "/runtime/decision",
                body={
                    "kind": "permission",
                    "sessionID": session_id,
                    "requestID": request_id,
                    "reply": reply,
                },
                directory=directory,
            ),
            "decision",
        )

    async def reply_question(
        self,
        session_id: str,
        request_id: str,
        answers: list[list[str]],
        *,
        directory: ProjectScope = None,
    ) -> dict[str, Any]:
        return _object(
            await self._request(
                "POST",
                "/runtime/decision",
                body={
                    "kind": "question",
                    "sessionID": session_id,
                    "requestID": request_id,
                    "answers": answers,
                },
                directory=directory,
            ),
            "decision",
        )

    async def reject_question(
        self, session_id: str, request_id: str, *, directory: ProjectScope = None
    ) -> dict[str, Any]:
        return _object(
            await self._request(
                "POST",
                "/runtime/decision",
                body={
                    "kind": "question_reject",
                    "sessionID": session_id,
                    "requestID": request_id,
                },
                directory=directory,
            ),
            "decision",
        )

    async def events(
        self,
        session_id: str,
        *,
        after_sequence: int | None = None,
        directory: ProjectScope = None,
    ) -> AsyncIterator[OpenScienceEvent]:
        """Stream one session's journal, resuming strictly after a cursor.

        Duplicates at or below the cursor are dropped, and a frame that skips
        ahead raises instead of being applied: the caller re-snapshots rather
        than observing a session with a hole in its history.
        """

        if after_sequence is not None and (
            type(after_sequence) is not int or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a nonnegative integer")
        cursor = after_sequence
        headers = {"accept": "text/event-stream", **self._scope_headers(directory)}
        params: dict[str, Any] = {"sessionID": session_id}
        if cursor is not None:
            headers["Last-Event-ID"] = str(cursor)
            params["afterSequence"] = cursor
        try:
            async with self._client().stream(
                "GET",
                f"{self.base_url}/runtime/events",
                params=params,
                headers=headers,
                timeout=httpx.Timeout(
                    self.request_timeout,
                    connect=min(10.0, self.request_timeout),
                    read=STREAM_READ_TIMEOUT_SECONDS,
                ),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _decode_error(response.status_code, response.content)
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type.lower():
                    raise OpenScienceProtocolError(
                        "runtime subscription did not return text/event-stream"
                    )
                async for event in _iter_sse(response, session_id):
                    if cursor is not None and event.sequence <= cursor:
                        continue
                    if cursor is not None and event.sequence != cursor + 1:
                        raise OpenScienceEventGapError(cursor + 1, event.sequence)
                    cursor = event.sequence
                    yield event
        except httpx.HTTPError as error:
            raise OpenScienceConnectionError(
                "runtime event stream interrupted; reconnect from the last applied sequence"
            ) from error


def _segment(value: str) -> str:
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ValueError("resource ids must be nonempty strings")
    return urllib.parse.quote(value, safe="")


def _project_sources(sources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the source grants a project is created with.

    A source is a path the user explicitly selected plus the access level the
    server should record for it. ``access`` is passed through verbatim because
    OpenScience owns that vocabulary; only the shape and the documented limit
    are checked here, so a bad grant fails before the request rather than as an
    unattributed 400.
    """

    if isinstance(sources, str | bytes) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence of objects")
    if len(sources) > MAX_PROJECT_SOURCES:
        raise ValueError(f"at most {MAX_PROJECT_SOURCES} project sources are allowed")
    normalized: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise TypeError("each project source must be an object")
        path = source.get("path")
        if not isinstance(path, str):
            raise TypeError("each project source path must be a string")
        if not path.strip():
            raise ValueError("each project source needs a non-empty path")
        entry: dict[str, Any] = {"path": path}
        access = source.get("access")
        if access is not None:
            if not isinstance(access, str) or not access:
                raise ValueError("project source access must be a non-empty string")
            entry["access"] = access
        normalized.append(entry)
    return normalized


def _optional_directory(value: Any) -> str | None:
    """Normalize a configured project directory; blank means unconfigured."""

    return value if isinstance(value, str) and value else None


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OpenScienceProtocolError(f"runtime {label} must be an object")
    return value


def _object_array(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise OpenScienceProtocolError(f"runtime {label} must be an array of objects")
    return value
