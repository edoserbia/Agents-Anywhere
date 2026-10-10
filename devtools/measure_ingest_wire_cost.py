#!/usr/bin/env python3
"""Measure what an ingest actually costs on the wire.

The release notes quote byte counts for a compressed snapshot and a staged
upload. Those were measured by hand, which means nobody can check them and two
runs disagree in the notes without either being wrong — the payloads were not
byte-identical.

This script is the method, so the numbers can be reproduced instead of
believed. It starts the real Server under real uvicorn on a real socket, drives
it with the real Connector client, and reports the bytes each request carried.

    server/.venv/bin/python devtools/measure_ingest_wire_cost.py

The Connector client comes from the repository, so run it with an interpreter
that can import both packages — the Server venv plus the repository root on the
path is enough, and the script says so if the import fails.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import socket
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]

for candidate in (ROOT, ROOT / "server", ROOT / "connector"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# This machine's `NO_PROXY` ends in `[::1]`, which httpx cannot parse, and an
# httpx client built with it raises before sending anything. The Connector
# normalises it at startup; a script that builds its own clients has to do the
# same or it fails for a reason that has nothing to do with what it measures.
try:
    from connector.core.proxy_env import normalize_no_proxy

    normalize_no_proxy()
except ImportError:
    pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(database: pathlib.Path) -> tuple[str, object]:
    """Run the real app under uvicorn and return its base URL and the server."""
    import uvicorn

    from agent_server.app import create_app

    port = free_port()
    app = create_app(database)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="measure-uvicorn", daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.05)
    if not server.started:
        raise RuntimeError("the server did not start")
    # A fresh database needs the first-run setup token, which the app holds in
    # memory. Running uvicorn in a thread of this process is what makes it
    # reachable; the tests read it the same way.
    return f"http://127.0.0.1:{port}", server, app


def migrated_database(destination: pathlib.Path) -> pathlib.Path:
    """A private copy of the migrated template, the way the tests do it."""
    sys.path.insert(0, str(ROOT / "server" / "tests"))
    from conftest import migrated_template_db

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(migrated_template_db(), destination)
    return destination


def create_session(base_url: str, connector_id: str, user_token: str) -> str:
    """A real session, because the Server rejects ingest for one that is not there."""
    import httpx

    with httpx.Client(base_url=f"{base_url}/api/v2", timeout=30) as client:
        headers = {"Authorization": f"Bearer {user_token}"}
        # The session needs a project, and the project needs the Connector.
        project = client.post(
            "/projects",
            headers=headers,
            json={
                "name": f"measure {connector_id}",
                "connectorId": connector_id,
                "workspacePath": "/repo",
            },
        )
        project.raise_for_status()
        project_id = project.json()["project"]["id"]

        created = client.post(
            "/sessions",
            headers=headers,
            json={
                "connectorId": connector_id,
                "projectId": project_id,
                "runtime": "codex",
                "externalSessionId": f"thr_{connector_id}_measure",
                "title": "measure",
                "cwd": "/repo",
            },
        )
        created.raise_for_status()
        return created.json()["session"]["id"]


def register_connector(base_url: str, app) -> tuple[str, str, str]:
    """Create a user, a Connector and an access token; return the last two."""
    import httpx

    with httpx.Client(base_url=f"{base_url}/api/v2", timeout=30) as client:
        login = client.post(
            "/auth/login",
            json={"email": "user1@example.com", "displayName": "user1", "password": "secret"},
        )
        if login.status_code == 401:
            login = client.post(
                "/auth/register",
                json={"email": "user1@example.com", "displayName": "user1", "password": "secret"},
            )
        if login.status_code == 401:
            body: dict = {
                "email": "user1@example.com",
                "displayName": "user1",
                "password": "secret",
            }
            if client.get("/auth/config").json().get("needsBootstrap"):
                body["setupToken"] = app.state.setup_token.peek()
            login = client.post("/auth/register", json=body)
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}

        created = client.post("/connectors", headers=headers, json={"name": "measure"})
        created.raise_for_status()
        body = created.json()
        connector_id = body["connector"]["id"]

        auth = client.post(
            "/connector/auth",
            headers={"Authorization": f"Connector {connector_id}:{body['connectorToken']}"},
        )
        auth.raise_for_status()
        return connector_id, auth.json()["accessToken"], login.json()["accessToken"]


class RecordingTransport:
    """An httpx transport that remembers how many bytes each request carried."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.sizes: list[tuple[str, int, int]] = []

    async def handle_async_request(self, request):
        response = await self._inner.handle_async_request(request)
        self.sizes.append(
            (request.url.path, len(request.content or b""), response.status_code)
        )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._inner.__aexit__(*exc_info)


def report(label: str, sizes: list[tuple[str, int, int]], payload: int) -> None:
    total = sum(size for _, size, _ in sizes)
    requests = len(sizes)
    print(
        f"  {label:<26} requests={requests:<3} bytes={total:<9,} "
        f"payload={payload:<9,} ratio={total / payload:.3f}"
    )
    for path, size, status in sizes:
        print(f"      {path:<48} {size:>9,}  HTTP {status}")


async def measure(base_url: str, access_token: str, session_id: str) -> None:
    import httpx

    from connector.server.ingest import ConnectorIngestClient

    transport = RecordingTransport(httpx.AsyncHTTPTransport())
    async with httpx.AsyncClient(transport=transport) as http:
        async def token(force: bool = False) -> str:
            return access_token

        ingest = ConnectorIngestClient(
            base_url, token, lambda: http, lambda timeout: http
        )
        # The flush loop is what actually puts bytes on the wire; the barrier
        # only waits for the result.
        flusher = asyncio.create_task(ingest.flush_loop())

        # 1. Text that repeats, which is what an uplink actually runs out of.
        text = "the same sentence over and over. " * 700
        big = {
            "method": "timeline.itemUpsert",
            "params": {
                "sessionId": session_id,
                "externalSessionId": "thr_measure",
                "item": {
                    "id": "item-reply",
                    "sessionId": session_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "done",
                    "orderSeq": 1,
                    "revision": 1,
                    "content": {"text": text, "format": "markdown"},
                    "source": {"runtime": "codex"},
                    "contentHash": "0" * 64,
                },
            },
        }
        payload = len(json.dumps({"notifications": [big]}).encode())
        transport.sizes.clear()
        await ingest.submit_notifications([big])
        try:
            await ingest.flush_notifications()
        except Exception as exc:  # noqa: BLE001 - reported, then measured anyway
            print(f"      (the barrier did not settle: {type(exc).__name__}: {exc})")
        report("repetitive 20 KB notification", transport.sizes, payload)

        # 2. Something that cannot be compressed at all, which is what the
        #    staged path exists for.
        import os

        blob = os.urandom(900 * 1024)
        snapshot = {
            "method": "timeline.sync",
            "params": {
                "sessionId": session_id,
                "complete": True,
                "items": [{"id": "blob", "data": blob.hex()}],
            },
        }
        raw = len(json.dumps({"notifications": [snapshot]}).encode())
        transport.sizes.clear()
        await ingest.submit_notifications([snapshot])
        try:
            await ingest.flush_notifications()
        except Exception as exc:  # noqa: BLE001 - reported, then measured anyway
            print(f"      (the barrier did not settle: {type(exc).__name__}: {exc})")
        report("900 KB incompressible", transport.sizes, raw)

        flusher.cancel()
        await asyncio.gather(flusher, return_exceptions=True)


def main() -> int:
    try:
        import agent_server  # noqa: F401
        import connector  # noqa: F401
    except ImportError as exc:
        print(f"cannot import both packages: {exc}", file=sys.stderr)
        print(
            "Run with an interpreter that sees the repository root and the "
            "Server, e.g. PYTHONPATH=. server/.venv/bin/python " + __file__,
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory() as directory:
        database = migrated_database(pathlib.Path(directory) / "measure.sqlite3")
        base_url, server, app = start_server(database)
        print(f"  real uvicorn on {base_url}")
        connector_id, access_token, user_token = register_connector(base_url, app)
        session_id = create_session(base_url, connector_id, user_token)
        print(f"  session {session_id}")
        try:
            asyncio.run(measure(base_url, access_token, session_id))
        finally:
            server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
