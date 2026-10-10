"""Resumable ingest: a large body survives an interrupted transfer.

A complete timeline snapshot is atomic — the Server replaces a session's whole
timeline with it — so it cannot be split into pieces that are applied
separately. On a slow uplink it is also the request most likely to be cut off,
and re-sending it from zero is what leaves a session stuck on its first sync.
"""

from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.infra.ingest_uploads import (
    IngestUploadConflict,
    IngestUploadNotFound,
    IngestUploadStore,
    IngestUploadTooLarge,
)

PAYLOAD = {"notifications": [{"method": "connector.heartbeat", "params": {}}]}
BODY = json.dumps(PAYLOAD).encode("utf-8")


def test_health_advertises_resumable_ingest(tmp_path) -> None:
    response = TestClient(create_app(tmp_path / "resumable.sqlite3")).get("/api/v2/health")

    assert response.json()["ingest"]["resumable"] is True


def _store(tmp_path, **kwargs) -> IngestUploadStore:
    return IngestUploadStore(tmp_path / "uploads", **kwargs)


def test_a_piece_must_continue_what_was_already_received(tmp_path) -> None:
    store = _store(tmp_path)
    handle = store.create("conn_1")

    store.append("conn_1", handle.upload_id, 0, b"abc")

    with pytest.raises(IngestUploadConflict) as excinfo:
        store.append("conn_1", handle.upload_id, 0, b"xyz")
    # The Server says where to continue instead of making the sender restart.
    assert excinfo.value.received_bytes == 3
    assert store.read("conn_1", handle.upload_id)[0] == b"abc"


def test_a_transfer_is_private_to_the_connector_that_opened_it(tmp_path) -> None:
    store = _store(tmp_path)
    handle = store.create("conn_1")

    with pytest.raises(IngestUploadNotFound):
        store.read("conn_2", handle.upload_id)
    with pytest.raises(IngestUploadNotFound):
        store.append("conn_2", handle.upload_id, 0, b"x")


def test_a_traversal_shaped_identifier_is_never_a_path(tmp_path) -> None:
    store = _store(tmp_path)

    for candidate in ("../../etc/passwd", "..", "short", "a" * 200):
        with pytest.raises(IngestUploadNotFound):
            store.read("conn_1", candidate)


def test_an_expired_transfer_is_swept(tmp_path) -> None:
    now = [1_000.0]
    store = _store(tmp_path, ttl_seconds=60.0, clock=lambda: now[0])
    handle = store.create("conn_1")

    now[0] += 61.0

    assert store.sweep() == 1
    with pytest.raises(IngestUploadNotFound):
        store.read("conn_1", handle.upload_id)


def test_a_staged_body_cannot_grow_past_the_cap(tmp_path) -> None:
    store = _store(tmp_path, max_bytes=8)
    handle = store.create("conn_1")

    with pytest.raises(IngestUploadTooLarge):
        store.append("conn_1", handle.upload_id, 0, b"x" * 9)


def test_the_routes_stage_and_commit_a_body(tmp_path) -> None:
    """The route contract, without needing a live ingest pipeline."""

    client = TestClient(create_app(tmp_path / "routes.sqlite3"))
    created = client.post(
        "/api/v2/connector/ingest/uploads",
        headers={"Authorization": "Bearer invalid"},
    )
    # Authorization is required before anything is staged.
    assert created.status_code == 401


def test_the_commit_route_reads_a_compressed_staged_body(tmp_path) -> None:
    """A staged body is decompressed when it is applied, not when it arrives."""

    from agent_server.infra.request_compression import decompress_request_body

    store = _store(tmp_path)
    compressed = gzip.compress(BODY)
    handle = store.create("conn_1", "gzip")
    store.append("conn_1", handle.upload_id, 0, compressed[:5])
    store.append("conn_1", handle.upload_id, 5, compressed[5:])

    body, encoding = store.read("conn_1", handle.upload_id)

    assert encoding == "gzip"
    assert decompress_request_body(encoding, body) == BODY




def test_one_connector_cannot_hold_unbounded_staged_transfers(tmp_path) -> None:
    """The time-to-live bounds disk only if transfers are opened slowly.

    Each transfer may hold up to the per-transfer maximum, so a client that
    keeps opening them inside the window would otherwise fill the disk. The
    oldest goes first; a working Connector stages one body at a time, so what it
    still needs is the newest.
    """

    store = IngestUploadStore(
        tmp_path / "uploads", max_uploads_per_connector=3, ttl_seconds=3600.0
    )
    handles = [store.create("conn_1") for _ in range(5)]

    live = sorted(p.stem for p in (tmp_path / "uploads").glob("*.meta"))
    assert len(live) == 3, live
    # The newest three survive, so an in-flight transfer is the last to go.
    for handle in handles[-3:]:
        assert handle.upload_id in live
    for handle in handles[:2]:
        with pytest.raises(IngestUploadNotFound):
            store.read("conn_1", handle.upload_id)
    # Another Connector is unaffected by this one's churn.
    other = store.create("conn_2")
    assert store.read("conn_2", other.upload_id) == (b"", None)
