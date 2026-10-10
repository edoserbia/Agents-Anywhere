"""A loopback data plane for the client on this same machine.

The Server stays the authority: this does not replace it, and nothing here is
allowed to be the only place a fact exists. What it does is take the traffic
that both starts and ends on this machine off the public network — a session's
current state, its timeline, its notices — so the reader sees activity as the
runtime produces it instead of after a round trip through a proxy that, on the
deployment this was written for, measured 19.4 KB/s up.

The shape is deliberately copied from the plugin bridge, which is already
deployed and reviewed: an ephemeral port on `127.0.0.1`, a random token in a
`0600` descriptor, a deadline for authenticating, and nothing but an explicit
allowlist of methods. Those properties are what make a new listener acceptable,
so they are not invented again here.

Read-only for now. A method that is not on the allowlist is refused with an
error rather than a disconnect, so a client can fall back per call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve

from connector.logging import logger
from connector.paths import connector_data_dir

DESCRIPTOR_NAME = "local-endpoint.json"
DESCRIPTOR_VERSION = 1

# How long a connection has to authenticate before it is dropped. A listener on
# loopback is still reachable by anything running as this user, so an
# unauthenticated connection must not be able to sit there.
DEFAULT_AUTH_DEADLINE_SECONDS = 10.0

# What a client may ask for. Everything here reads state the Connector already
# holds; the write methods exist in the dispatcher but are deliberately absent,
# because a local write path has to keep the Server informed and that is a
# separate piece of work.
READ_ONLY_METHODS = frozenset(
    {
        "runtime.discover",
        "runtime.capabilities",
        "runtime.commands",
        "runtime.modelCatalog",
        "runtime.permissionCatalog",
        "runtime.configSchema",
        "session.discover",
        "session.state",
        "session.capabilities",
        "session.notices",
        "session.commands",
    }
)

Dispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]


class LocalEndpointError(RuntimeError):
    """The endpoint could not be started."""


def descriptor_path() -> Path:
    return connector_data_dir() / DESCRIPTOR_NAME


class LocalEndpoint:
    """Serve a read-only view of this Connector over loopback.

    One endpoint per Connector process. The descriptor is what tells a client
    where to find it, and it carries the token, so it is written `0600` and
    removed on the way out.
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        path: Path | None = None,
        auth_deadline_seconds: float = DEFAULT_AUTH_DEADLINE_SECONDS,
        methods: frozenset[str] = READ_ONLY_METHODS,
    ) -> None:
        self._dispatch = dispatcher
        self._path = path if path is not None else descriptor_path()
        self._auth_deadline_seconds = auth_deadline_seconds
        self._methods = methods
        self._token = secrets.token_urlsafe(32)
        self._server: Server | None = None
        self._port: int | None = None

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def token(self) -> str:
        return self._token

    def descriptor(self) -> dict[str, Any]:
        """What a client needs to connect, and nothing more."""

        if self._port is None:
            raise LocalEndpointError("the local endpoint is not running")
        return {
            "version": DESCRIPTOR_VERSION,
            "host": "127.0.0.1",
            "port": self._port,
            "token": self._token,
            "pid": os.getpid(),
            "createdAt": time.time(),
        }

    async def start(self) -> dict[str, Any]:
        if self._server is not None:
            raise LocalEndpointError("the local endpoint is already running")
        try:
            # No `logger=` here: websockets drives a stdlib logger and loguru
            # is not a drop-in for it — passing ours made every handshake fail
            # before the handler was ever reached.
            self._server = await serve(self._handle, "127.0.0.1", 0, max_size=None)
        except OSError as exc:
            raise LocalEndpointError(f"could not bind the local endpoint: {exc}") from exc
        sockets = self._server.sockets
        if not sockets:
            await self.stop()
            raise LocalEndpointError("the local endpoint bound no socket")
        self._port = int(sockets[0].getsockname()[1])
        descriptor = self.descriptor()
        self._write_descriptor(descriptor)
        logger.info("connector local endpoint listening port={}", self._port)
        return descriptor

    async def stop(self) -> None:
        server, self._server = self._server, None
        self._port = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._remove_descriptor()

    async def _handle(self, connection: ServerConnection) -> None:
        """Authenticate first, then answer allowed methods one at a time."""

        if not await self._authenticate(connection):
            return
        async for message in connection:
            if not isinstance(message, str):
                await connection.close(1003, "text frames only")
                return
            try:
                request = json.loads(message)
            except ValueError:
                await self._send(connection, _error_response(None, "invalid_json", "not JSON"))
                continue
            if not isinstance(request, dict) or request.get("type") != "request":
                await self._send(
                    connection,
                    _error_response(
                        request.get("id") if isinstance(request, dict) else None,
                        "invalid_request",
                        "expected a request message",
                    ),
                )
                continue
            await self._send(connection, await self._answer(request))

    async def _authenticate(self, connection: ServerConnection) -> bool:
        try:
            raw = await asyncio.wait_for(
                connection.recv(), timeout=self._auth_deadline_seconds
            )
        except (TimeoutError, websockets.exceptions.ConnectionClosed):
            logger.warning("connector local endpoint dropped an unauthenticated connection")
            with contextlib.suppress(Exception):
                await connection.close(4001, "authentication required")
            return False
        token = None
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                message = json.loads(raw)
                if isinstance(message, dict) and message.get("type") == "initialize":
                    candidate = message.get("token")
                    if isinstance(candidate, str):
                        token = candidate
        # Constant time: a token compared with `==` leaks its prefix to anyone
        # who can measure the reply.
        if token is None or not secrets.compare_digest(token, self._token):
            logger.warning("connector local endpoint rejected a bad token")
            with contextlib.suppress(Exception):
                await connection.close(4001, "authentication required")
            return False
        await self._send(
            connection,
            {"type": "initialized", "version": DESCRIPTOR_VERSION},
        )
        return True

    async def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params")
        if not isinstance(method, str):
            return _error_response(request_id, "invalid_request", "method must be a string")
        if method not in self._methods:
            # Refused, not disconnected: a client that asks for something this
            # channel does not serve should fall back for that call only.
            return _error_response(
                request_id, "forbidden_method", f"{method} is not served locally"
            )
        if params is not None and not isinstance(params, dict):
            return _error_response(request_id, "invalid_params", "params must be an object")
        try:
            result = await self._dispatch(method, params or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return _error_response(request_id, "dispatch_failed", str(exc) or type(exc).__name__)
        return {"type": "response", "id": request_id, "result": result}

    async def _send(self, connection: ServerConnection, message: dict[str, Any]) -> None:
        with contextlib.suppress(websockets.exceptions.ConnectionClosed):
            await connection.send(json.dumps(message, ensure_ascii=False))

    def _write_descriptor(self, descriptor: dict[str, Any]) -> None:
        contents = json.dumps(descriptor, indent=2, ensure_ascii=False) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.{uuid.uuid4()}.tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_descriptor(self) -> None:
        """Remove the descriptor, but only if it is still the one we wrote.

        A successor process may have replaced it already; deleting that would
        take a working endpoint away from the client.
        """

        try:
            current = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(current, dict) and current.get("token") == self._token:
            self._path.unlink(missing_ok=True)


def _error_response(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "type": "response",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
