"""End-to-end ingest transport: compression and staged uploads.

The unit tests around each half prove the pieces work; these prove the halves
agree. Every one of them goes through the real route, the real access-token
check and the real ingest service, and asserts an effect on the stored session
rather than a status code — which is what a deployment actually depends on.
"""

from __future__ import annotations

import gzip
import json

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


def _title(client, session_id: str, headers) -> str:
    response = client.get(f"/sessions/{session_id}/meta", headers=headers)
    assert response.status_code == 200
    return response.json()["session"]["title"]


def test_a_gzipped_ingest_has_the_same_effect_as_a_plain_one(tmp_path) -> None:
    client = make_client(tmp_path)
    _, token, session_id, headers = create_connector_and_session(client)
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    plain = client.post(
        "/connector/ingest", json=_notification(session_id, "Plain"), headers=auth
    )
    assert plain.status_code == 200
    assert _title(client, session_id, headers) == "Plain"

    compressed = client.post(
        "/connector/ingest",
        content=gzip.compress(json.dumps(_notification(session_id, "Gzipped")).encode()),
        headers={**auth, "Content-Encoding": "gzip"},
    )
    assert compressed.status_code == 200
    assert _title(client, session_id, headers) == "Gzipped"


def test_a_staged_upload_has_the_same_effect_as_a_plain_one(tmp_path) -> None:
    client = make_client(tmp_path)
    _, token, session_id, headers = create_connector_and_session(client)
    auth = {"Authorization": f"Bearer {token}"}
    body = json.dumps(_notification(session_id, "Staged")).encode("utf-8")

    created = client.post("/connector/ingest/uploads", headers=auth)
    assert created.status_code == 200
    upload_id = created.json()["uploadId"]
    assert created.json()["receivedBytes"] == 0

    # Two pieces, as a slow link would send them.
    half = len(body) // 2
    for offset, piece in ((0, body[:half]), (half, body[half:])):
        appended = client.put(
            f"/connector/ingest/uploads/{upload_id}",
            content=piece,
            headers={**auth, "X-Upload-Offset": str(offset)},
        )
        assert appended.status_code == 200, appended.text
        assert appended.json()["receivedBytes"] == offset + len(piece)

    committed = client.post(f"/connector/ingest/uploads/{upload_id}/commit", headers=auth)
    assert committed.status_code == 200, committed.text
    assert _title(client, session_id, headers) == "Staged"

    # Committing consumed the staging.
    assert client.get(f"/connector/ingest/uploads/{upload_id}", headers=auth).status_code == 404


def test_a_staged_compressed_upload_is_applied_after_decoding(tmp_path) -> None:
    client = make_client(tmp_path)
    _, token, session_id, headers = create_connector_and_session(client)
    auth = {"Authorization": f"Bearer {token}"}
    body = gzip.compress(json.dumps(_notification(session_id, "Staged+Gzip")).encode())

    upload_id = client.post(
        "/connector/ingest/uploads",
        headers={**auth, "X-Upload-Encoding": "gzip"},
    ).json()["uploadId"]
    assert client.put(
        f"/connector/ingest/uploads/{upload_id}",
        content=body,
        headers={**auth, "X-Upload-Offset": "0"},
    ).status_code == 200

    assert client.post(
        f"/connector/ingest/uploads/{upload_id}/commit", headers=auth
    ).status_code == 200
    assert _title(client, session_id, headers) == "Staged+Gzip"


def test_a_resumed_piece_reports_where_to_continue(tmp_path) -> None:
    client = make_client(tmp_path)
    _, token, _, _ = create_connector_and_session(client)
    auth = {"Authorization": f"Bearer {token}"}

    upload_id = client.post("/connector/ingest/uploads", headers=auth).json()["uploadId"]
    assert client.put(
        f"/connector/ingest/uploads/{upload_id}",
        content=b"abcdef",
        headers={**auth, "X-Upload-Offset": "0"},
    ).json()["receivedBytes"] == 6

    # A sender that lost track asks rather than restarting.
    assert client.get(
        f"/connector/ingest/uploads/{upload_id}", headers=auth
    ).json()["receivedBytes"] == 6
    # And a sender that guesses wrong is told where to continue.
    conflict = client.put(
        f"/connector/ingest/uploads/{upload_id}",
        content=b"xyz",
        headers={**auth, "X-Upload-Offset": "3"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["receivedBytes"] == 6


def test_a_discarded_upload_is_never_applied(tmp_path) -> None:
    client = make_client(tmp_path)
    _, token, session_id, headers = create_connector_and_session(client)
    auth = {"Authorization": f"Bearer {token}"}
    before = _title(client, session_id, headers)
    body = json.dumps(_notification(session_id, "Discarded")).encode()

    upload_id = client.post("/connector/ingest/uploads", headers=auth).json()["uploadId"]
    client.put(
        f"/connector/ingest/uploads/{upload_id}",
        content=body,
        headers={**auth, "X-Upload-Offset": "0"},
    )

    assert client.delete(f"/connector/ingest/uploads/{upload_id}", headers=auth).status_code == 204
    assert _title(client, session_id, headers) == before
    assert client.post(
        f"/connector/ingest/uploads/{upload_id}/commit", headers=auth
    ).status_code == 404


def test_another_connectors_staged_upload_is_not_reachable(tmp_path) -> None:
    client = make_client(tmp_path)
    _, token, _, _ = create_connector_and_session(client)
    _, other_token, _, _ = create_connector_and_session(client)

    upload_id = client.post(
        "/connector/ingest/uploads", headers={"Authorization": f"Bearer {token}"}
    ).json()["uploadId"]
    other = {"Authorization": f"Bearer {other_token}"}

    assert client.get(f"/connector/ingest/uploads/{upload_id}", headers=other).status_code == 404
    assert client.put(
        f"/connector/ingest/uploads/{upload_id}",
        content=b"x",
        headers={**other, "X-Upload-Offset": "0"},
    ).status_code == 404
    assert client.post(
        f"/connector/ingest/uploads/{upload_id}/commit", headers=other
    ).status_code == 404


def test_a_half_received_snapshot_is_never_applied(tmp_path) -> None:
    """The dangerous case: a truncated body must not reach the timeline.

    A complete snapshot replaces a session's whole timeline, so applying half of
    one — or failing to parse it and letting something else through — would
    destroy history rather than merely lag behind.
    """

    client = make_client(tmp_path)
    _, token, session_id, headers = create_connector_and_session(client)
    auth = {"Authorization": f"Bearer {token}"}
    before = _title(client, session_id, headers)
    body = json.dumps(_notification(session_id, "Truncated")).encode("utf-8")

    upload_id = client.post("/connector/ingest/uploads", headers=auth).json()["uploadId"]
    # Only the first half ever arrives, then the sender commits anyway.
    assert client.put(
        f"/connector/ingest/uploads/{upload_id}",
        content=body[: len(body) // 2],
        headers={**auth, "X-Upload-Offset": "0"},
    ).status_code == 200

    committed = client.post(f"/connector/ingest/uploads/{upload_id}/commit", headers=auth)

    assert committed.status_code == 422
    assert _title(client, session_id, headers) == before
