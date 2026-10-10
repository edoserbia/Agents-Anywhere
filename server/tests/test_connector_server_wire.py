"""The Connector's own ingest client against the real Server.

Every other test around this feature checks one half: the Connector's client
against a stand-in transport, or the Routes against hand-built requests. The
failures that survive both are the ones where the halves disagree — a header the
other side consumes, an offset convention, a status the sender does not expect.
Those only appear on a deployed pair, which is the worst place to find them.

Skipped where only the Server is present (the deployed image does not contain
the Connector), so this never becomes a dependency of the container build.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

import httpx
import pytest
from test_backend_mvp import create_connector_and_session, make_client

REPO_ROOT = Path(__file__).resolve().parents[2]
CONNECTOR_ROOT = REPO_ROOT / "connector"
if CONNECTOR_ROOT.is_dir() and str(CONNECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(CONNECTOR_ROOT))

pytest.importorskip(
    "connector.server.ingest",
    reason="the Connector source is not part of the Server image",
)

from connector.runtime_protocol import timeline_content_hash  # noqa: E402
from connector.server import ingest as connector_ingest  # noqa: E402
from connector.server.ingest import ConnectorIngestClient  # noqa: E402


def _notification(session_id: str, title: str) -> list[dict]:
    return [
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


class _RecordingTransport(httpx.AsyncBaseTransport):
    """The real app, plus a note of every route the Connector actually used."""

    def __init__(self, inner: httpx.AsyncTransport, seen: list[tuple[str, str]]) -> None:
        self._inner = inner
        self._seen = seen

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._seen.append((request.method, request.url.path))
        return await self._inner.handle_async_request(request)


def _run_connector(
    client,
    access_token: str,
    notifications: list[dict],
    seen: list[tuple[str, str]] | None = None,
) -> None:
    """Push one batch through the Connector's real client, in one event loop."""

    async def token(force: bool) -> str:
        return access_token

    async def exercise() -> None:
        transport = _RecordingTransport(
            httpx.ASGITransport(app=client.app), seen if seen is not None else []
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://server.test"
        ) as http:
            ingest = ConnectorIngestClient(
                "http://server.test", token, lambda: http, lambda timeout: http
            )
            await ingest.submit_notifications(notifications)
            flusher = asyncio.create_task(ingest.flush_loop())
            barrier = asyncio.create_task(ingest.flush_notifications())
            try:
                await asyncio.wait_for(barrier, 30)
            finally:
                flusher.cancel()
                await asyncio.gather(flusher, return_exceptions=True)

    asyncio.run(exercise())


def _title(client, session_id: str, headers) -> str:
    response = client.get(f"/sessions/{session_id}/meta", headers=headers)
    assert response.status_code == 200
    return response.json()["session"]["title"]


def test_the_connector_client_and_the_server_agree_on_the_wire(tmp_path) -> None:
    client = make_client(tmp_path)
    _, access_token, session_id, headers = create_connector_and_session(client)
    assert client.get("/api/v2/health").json()["ingest"]["resumable"] is True

    _run_connector(client, access_token, _notification(session_id, "Cross-component"))

    assert _title(client, session_id, headers) == "Cross-component"


def test_the_connector_stages_a_body_the_server_can_commit(tmp_path) -> None:
    """The resumable path, driven by the sender that will actually use it."""

    client = make_client(tmp_path)
    _, access_token, session_id, headers = create_connector_and_session(client)
    # High-entropy text: a repetitive body compresses to a few dozen bytes and
    # would never reach the staging threshold, which is exactly the trap that
    # makes a "large upload" test quietly exercise the plain path instead.
    text = secrets.token_hex(2000)
    content = {"text": text, "format": "markdown"}
    bulk = {
        "method": "timeline.sync",
        "params": {
            "sessionId": session_id,
            "externalSessionId": "thr_1",
            "complete": True,
            "items": [
                {
                    "id": "item-1",
                    "sessionId": session_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "done",
                    "orderSeq": 1,
                    "revision": 1,
                    "content": content,
                    "contentHash": timeline_content_hash(
                        "message", "done", "assistant", content
                    ),
                    "source": {"runtime": "codex"},
                }
            ],
        },
    }

    seen: list[tuple[str, str]] = []
    # Small thresholds so a modest body still spans several pieces; the point is
    # the protocol between the halves, not the size that triggers it.
    original = (
        connector_ingest.RESUMABLE_UPLOAD_MIN_BYTES,
        connector_ingest.RESUMABLE_UPLOAD_CHUNK_BYTES,
    )
    connector_ingest.RESUMABLE_UPLOAD_MIN_BYTES = 256
    connector_ingest.RESUMABLE_UPLOAD_CHUNK_BYTES = 512
    try:
        _run_connector(client, access_token, [bulk], seen)
    finally:
        (
            connector_ingest.RESUMABLE_UPLOAD_MIN_BYTES,
            connector_ingest.RESUMABLE_UPLOAD_CHUNK_BYTES,
        ) = original

    staged = [path for method, path in seen if method == "PUT" and "/ingest/uploads/" in path]
    assert staged, f"the body never went through staging: {seen}"
    assert any("/commit" in path for _, path in seen)

    timeline = client.get(f"/sessions/{session_id}/timeline", headers=headers)
    assert timeline.status_code == 200
    assert text[:200] in json.dumps(timeline.json()), (
        "the staged snapshot never reached the timeline"
    )
    puts = [path for method, path in seen if method == "PUT"]
    assert len(puts) > 1, f"the body travelled in one piece: {seen}"


def test_a_refused_body_surfaces_instead_of_looking_delivered(tmp_path) -> None:
    """A batch the Server refuses must not look delivered to its producer.

    The producer is a sync feed that advances a checkpoint once its data is
    stored; a refusal that looked like success would let that checkpoint move
    past data the Server never kept.
    """

    client = make_client(tmp_path)
    _, access_token, session_id, headers = create_connector_and_session(client)
    before = _title(client, session_id, headers)
    content = {"text": "kept", "format": "markdown"}
    del content["format"]
    refused = {
        "method": "timeline.itemUpsert",
        "params": {
            "sessionId": session_id,
            "externalSessionId": "thr_1",
            # `contentHash` is required; the Server validates and refuses this.
            "item": {
                "id": "item-bad",
                "sessionId": session_id,
                "type": "message",
                "role": "assistant",
                "status": "done",
                "orderSeq": 1,
                "revision": 1,
                "content": {"text": "kept", "format": "markdown"},
                "source": {"runtime": "codex"},
            },
        },
    }

    with pytest.raises(connector_ingest.ConnectorIngestRejectedError):
        _run_connector(client, access_token, [refused])

    assert _title(client, session_id, headers) == before
