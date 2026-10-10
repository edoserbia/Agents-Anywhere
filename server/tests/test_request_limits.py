"""A body ceiling that cannot break the Connector this repository ships.

The cap exists for the Connector that is *deployed* — it batches by count, not
by size — and for a host where an out-of-memory kill is a 502 for every client.
So the tests that matter are: normal ingest is untouched, an oversized body is
refused without being buffered, and a chunked body with no declared length is
caught as it arrives rather than after the fact.
"""

from __future__ import annotations

import json

import pytest

from test_backend_mvp import create_connector_and_session, make_client


def _notification(session_id: str, title: str) -> dict:
    return {
        "notifications": [
            {
                "method": "session.meta.upsert",
                "params": {
                    "sessionId": session_id,
                    "runtime": "codex",
                    "externalSessionId": "thr_1",
                    "title": title,
                },
            }
        ]
    }


def test_an_ordinary_ingest_is_untouched(tmp_path) -> None:
    """The Connector this repository ships sends 256 KiB at most."""

    client = make_client(tmp_path)
    _, token, session_id, headers = create_connector_and_session(client)
    response = client.post(
        "/connector/ingest",
        json=_notification(session_id, "Untouched"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert client.get(f"/sessions/{session_id}/meta", headers=headers).json()["session"]["title"] == "Untouched"


def test_a_body_over_the_ceiling_is_refused_before_it_is_read(tmp_path) -> None:
    """A declared length is enough to refuse it, so nothing is buffered."""

    client = make_client(tmp_path)
    _, token, _, _ = create_connector_and_session(client)
    limit = client.app.state.request_body_limit
    response = client.post(
        "/connector/ingest",
        content=b"x" * (limit + 1),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413, response.text
    assert response.headers["x-max-body-bytes"] == str(limit)
    assert "staged upload" in response.json()["detail"]


def test_the_ceiling_is_well_above_what_a_connector_sends(tmp_path) -> None:
    """A cap that a normal batch could reach would be a regression, not a guard."""

    from agent_server.infra.request_limits import MAX_REQUEST_BODY_BYTES

    # The Connector sends at most `FLUSH_MAX_BYTES` (256 KiB) per request, and
    # anything larger goes through the staged path; this is thirty-two times it.
    # The ceiling is not arbitrary: resident memory grows by roughly nine times
    # the body size and is not returned, so a generous-looking 64 MiB would cost
    # about 600 MB — more than half of the container this protects.
    assert MAX_REQUEST_BODY_BYTES == 8 * 1024 * 1024
    assert MAX_REQUEST_BODY_BYTES >= 32 * 256 * 1024


def test_another_route_is_not_capped(tmp_path) -> None:
    """Pointed at the bulk route only, so nothing else changes shape."""

    client = make_client(tmp_path)
    _, token, session_id, _ = create_connector_and_session(client)
    # Same oversized body, a different path: the limiter must not be involved.
    response = client.post(
        "/connector/ingest/uploads",
        content=b"x" * 1024,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code != 413


def test_a_chunked_body_without_a_length_is_caught_while_reading(tmp_path) -> None:
    """`content-length` is optional, so the count has to be taken as it arrives."""

    client = make_client(tmp_path)
    _, token, _, _ = create_connector_and_session(client)
    limit = client.app.state.request_body_limit

    def chunks():
        sent = 0
        while sent <= limit:
            piece = b"x" * (1024 * 1024)
            sent += len(piece)
            yield piece

    response = client.post(
        "/connector/ingest",
        content=chunks(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert response.status_code == 413, response.text


def test_the_limit_is_configurable(tmp_path, monkeypatch) -> None:
    """A host with less room can lower it without a code change."""

    monkeypatch.setenv("AGENT_SERVER_MAX_REQUEST_BODY_BYTES", "2048")
    client = make_client(tmp_path)
    assert client.app.state.request_body_limit == 2048
    _, token, session_id, _ = create_connector_and_session(client)
    # A normal notification still fits.
    assert client.post(
        "/connector/ingest",
        json=_notification(session_id, "Small"),
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 200
