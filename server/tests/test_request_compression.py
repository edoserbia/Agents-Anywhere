"""A Connector may compress its ingest body; the Server must read it.

Measured on the deployment this was written for: a 19.4 KB/s uplink against a
216 KB/s downlink, carrying runtime timelines that repeat a whole message on
every revision. Compression is the difference between a live session and a
stream that never catches up, so the negotiated path is covered directly.
"""

from __future__ import annotations

import gzip
import json

from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.infra.request_compression import (
    REQUEST_ENCODINGS,
    RequestDecompressionMiddleware,
)


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path / "compression.sqlite3"))


def test_health_advertises_the_ingest_encodings_it_can_read(tmp_path) -> None:
    response = _client(tmp_path).get("/api/v2/health")

    assert response.status_code == 200
    assert response.json()["ingest"]["encodings"] == sorted(REQUEST_ENCODINGS)
    assert "gzip" in response.json()["ingest"]["encodings"]


def test_a_gzipped_ingest_body_is_read_like_a_plain_one(tmp_path) -> None:
    client = _client(tmp_path)
    # A Connector always states the decoded content type; the encoding is
    # separate from it, which is what lets the middleware hand the route a body
    # it parses exactly as the plain one.
    headers = {"Authorization": "Bearer invalid", "Content-Type": "application/json"}
    payload = {"notifications": [{"method": "connector.heartbeat", "params": {}}]}
    plain = client.post("/api/v2/connector/ingest", json=payload, headers=headers)
    compressed = client.post(
        "/api/v2/connector/ingest",
        content=gzip.compress(json.dumps(payload).encode("utf-8")),
        headers={**headers, "Content-Encoding": "gzip"},
    )

    # Both reach the same authorization decision, which is all this covers: the
    # body was decoded rather than rejected as malformed.
    assert plain.status_code == 401
    assert compressed.status_code == plain.status_code
    assert compressed.json() == plain.json()


def test_a_truncated_compressed_body_is_rejected_not_crashed(tmp_path) -> None:
    client = _client(tmp_path)
    body = gzip.compress(
        json.dumps({"notifications": [{"method": "connector.heartbeat", "params": {}}]}).encode("utf-8")
    )

    response = client.post(
        "/api/v2/connector/ingest",
        content=body[: len(body) // 2],
        headers={
            "Authorization": "Bearer invalid",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )

    # Whatever the exact rejection, a body that cannot be decoded must never
    # reach the ingest service as garbage.
    assert response.status_code >= 400
    assert response.status_code != 401


def test_an_uncompressed_body_still_works(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v2/connector/ingest",
        json={"notifications": [{"method": "connector.heartbeat", "params": {}}]},
        headers={"Authorization": "Bearer invalid"},
    )

    assert response.status_code == 401


def test_the_route_receives_the_decoded_object_not_a_string() -> None:
    """End-to-end through the real ASGI stack, without needing credentials.

    The failure this guards against is subtle: a body that arrives as the JSON
    *text* of the payload rather than the payload itself still parses, and only
    fails much later as a validation error nobody connects to compression.
    """

    from fastapi import FastAPI

    inner = FastAPI()

    @inner.post("/echo")
    async def echo(payload: dict) -> dict:
        return {"received": payload}

    app = RequestDecompressionMiddleware(inner)
    client = TestClient(app)
    payload = {"notifications": [{"method": "connector.heartbeat", "params": {"n": 1}}]}
    headers = {"Content-Type": "application/json"}

    plain = client.post("/echo", json=payload, headers=headers)
    compressed = client.post(
        "/echo",
        content=gzip.compress(json.dumps(payload).encode("utf-8")),
        headers={**headers, "Content-Encoding": "gzip"},
    )

    assert compressed.status_code == plain.status_code == 200
    assert compressed.json() == plain.json() == {"received": payload}


def test_decompression_is_bounded(tmp_path) -> None:
    """A small body must not be able to expand without limit."""

    from fastapi import FastAPI

    inner = FastAPI()

    @inner.post("/echo")
    async def echo(payload: dict) -> dict:
        return {"received": payload}

    app = RequestDecompressionMiddleware(inner, max_bytes=1024)
    client = TestClient(app)

    response = client.post(
        "/echo",
        content=gzip.compress(json.dumps({"blob": "x" * 20000}).encode("utf-8")),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )

    assert response.status_code == 400
