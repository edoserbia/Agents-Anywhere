"""Bound how large a request body the Server will buffer.

The ingest route takes a parsed JSON body, so FastAPI buffers the whole thing
before any handler sees it. There is no ceiling on that, and the Connector that
is deployed today batches by *count* (`FLUSH_MAX = 64`) rather than by size — so
one request can carry an unbounded amount of timeline. On the deployment this
was written for the host has 1.9 GB of RAM, the application container is capped
at 900 MB, and the container has no swap, so a spike is an immediate kill rather
than a slow-down, and every kill is a window of 502s for every client.

The ceiling is measured rather than guessed. Parsing a JSON body costs resident
memory that is never given back, at roughly nine times the body size:

    body  1 MiB  ->  +8 MiB rss
    body  4 MiB  -> +30 MiB rss
    body 16 MiB  -> +148 MiB rss      (and the next request starts from there)

So a 64 MiB body would cost something like 600 MB, which on the 900 MB container
this was written for is an immediate kill. 8 MiB costs about 70 MB and is
thirty-two times what a Connector built from this repository sends (256 KiB per
request, and anything larger goes through the staged path in 256 KiB pieces).
It is still far above any single update a runtime produces.

A body over the ceiling is refused with 413 rather than read. That is a
permanent refusal as far as the Connector is concerned, which is the honest
answer: it will not fit, and retrying the same bytes will not change that.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

from starlette.requests import ClientDisconnect

from agent_server.core.api_namespace import api_v2_path

MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestBodyLimitMiddleware:
    """Refuse a body larger than `limit` without buffering it.

    `paths` are matched as prefixes, so this can be pointed at the routes that
    actually carry bulk data instead of every request the Server serves.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int = MAX_REQUEST_BODY_BYTES,
        paths: Iterable[str] = (api_v2_path("/connector/ingest"),),
    ) -> None:
        self.app = app
        self.limit = limit
        self.paths = tuple(paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._applies(scope):
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.limit:
            # Nothing has been read yet, so this costs nothing to refuse.
            await _refuse(send, self.limit)
            return

        # A chunked body declares no length, so it has to be counted as it
        # arrives. Reading it here rather than raising from inside the
        # application is deliberate: FastAPI catches anything raised while it
        # parses a body and answers 400, so an overflow would surface as "there
        # was an error parsing the body" instead of an answer the client can
        # act on. The bytes are replayed, so nothing downstream changes shape.
        body = await _read_limited(receive, self.limit)
        if body is None:
            await _refuse(send, self.limit)
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    def _applies(self, scope: Scope) -> bool:
        path = scope.get("path") or ""
        return any(path == prefix or path.startswith(f"{prefix}/") for prefix in self.paths)


async def _read_limited(receive: Receive, limit: int) -> bytes | None:
    """Collect the body, or return None as soon as it passes `limit`.

    Stopping early is the point: a body that is going to be refused must not be
    buffered first, or the ceiling protects nothing.
    """

    buffered = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ClientDisconnect
        if message["type"] != "http.request":
            continue
        buffered.extend(message.get("body", b""))
        if len(buffered) > limit:
            return None
        if not message.get("more_body", False):
            return bytes(buffered)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        try:
            return int(value)
        except ValueError:
            return None
    return None


async def _refuse(send: Send, limit: int) -> None:
    body = (
        b'{"detail":"request body is too large; send it in smaller pieces or '
        b'use the staged upload endpoints"}'
    )
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"x-max-body-bytes", str(limit).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})
