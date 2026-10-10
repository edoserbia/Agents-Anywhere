"""Accept compressed request bodies from Connectors.

A Connector's runtime sync is bounded by its uplink, and a runtime timeline is
verbose JSON that repeats a whole message on every revision. Measured on the
deployment this was written for: 19.4 KB/s up against 216 KB/s down. gzip
typically takes that text down by most of an order of magnitude, which is the
difference between a live session and a stream that never catches up.

Only bodies a client explicitly marked as compressed are touched, and only on
request: responses keep whatever the application and any response middleware
decide. Decompression is bounded so a small body cannot expand without limit.
"""

from __future__ import annotations

import zlib
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024

# zlib's 47 accepts a gzip *or* zlib wrapper, which covers both encodings a
# client may reasonably send.
_WBITS = {"gzip": 47, "deflate": 47}

# Advertised on the health endpoint so a Connector only compresses when the
# Server on the other end can read what it sends.
REQUEST_ENCODINGS = frozenset(_WBITS)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestDecompressionMiddleware:
    """Decompress `Content-Encoding: gzip|deflate` request bodies in place."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_DECOMPRESSED_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        encoding = _request_encoding(scope) if scope.get("type") == "http" else None
        if encoding is None:
            await self.app(scope, receive, send)
            return
        body = await _read_body(receive)
        try:
            decoded = decompress_request_body(encoding, body, self.max_bytes)
        except (zlib.error, ValueError):
            await send({
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"detail":"invalid compressed request body"}',
            })
            return
        scope = dict(scope)
        scope["headers"] = _rewritten_headers(scope.get("headers", []), len(decoded))
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": decoded, "more_body": False}

        await self.app(scope, replay, send)


def _request_encoding(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"content-encoding":
            continue
        encoding = value.decode("latin-1").strip().lower()
        return encoding if encoding in _WBITS else None
    return None


def _rewritten_headers(headers: Iterable[tuple[bytes, bytes]], length: int) -> list[tuple[bytes, bytes]]:
    """Drop the encoding and correct the length for the decoded body."""

    rewritten = [
        (name, value)
        for name, value in headers
        if name.lower() not in {b"content-encoding", b"content-length"}
    ]
    rewritten.append((b"content-length", str(length).encode("ascii")))
    return rewritten


def decompress_request_body(encoding: str, body: bytes, max_bytes: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Decode a body a client marked with `encoding`.

    Exposed because a staged upload is decompressed when it is committed rather
    than when it arrives: its pieces are opaque to the transport.
    """

    return _decompress(encoding, body, max_bytes)


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


def _decompress(encoding: str, body: bytes, max_bytes: int) -> bytes:
    decompressor = zlib.decompressobj(_WBITS[encoding])
    decoded = decompressor.decompress(body, max_bytes + 1)
    if len(decoded) > max_bytes or decompressor.unconsumed_tail:
        raise ValueError("compressed request body is too large")
    return decoded
