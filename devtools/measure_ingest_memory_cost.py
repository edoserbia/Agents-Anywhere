#!/usr/bin/env python3
"""How much resident memory does one ingest request cost, and is it returned?

The body ceiling in `agent_server/infra/request_limits.py` is set from this
measurement rather than from taste. Parsing a JSON body costs resident memory at
roughly nine times the body size, and the allocator does not give it back — so a
ceiling that looks generous can be larger than the container it is meant to
protect.

    server/.venv/bin/python devtools/measure_ingest_memory_cost.py

Run it with an interpreter that sees the repository root and the Server.
"""
import json, os, pathlib, shutil, socket, sys, tempfile, threading, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
for c in (ROOT, ROOT / "server"):
    sys.path.insert(0, str(c))
try:
    sys.path.insert(0, str(ROOT / "connector"))
    from connector.core.proxy_env import normalize_no_proxy
    normalize_no_proxy()
except ImportError:
    pass

import httpx, uvicorn
from agent_server.app import create_app

sys.path.insert(0, str(ROOT / "server" / "tests"))
from conftest import migrated_template_db


def rss_mb(pid):
    for line in open(f"/proc/{pid}/status") if os.path.isdir("/proc") else []:
        pass
    import subprocess
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip() or 0) / 1024


with tempfile.TemporaryDirectory() as d:
    db = pathlib.Path(d) / "m.sqlite3"
    shutil.copyfile(migrated_template_db(), db)
    app = create_app(db)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started: break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}/api/v2"

    with httpx.Client(base_url=base, timeout=120) as c:
        r = c.post("/auth/register", json={"email": "u@e.com", "displayName": "u", "password": "s"})
        if r.status_code != 200:
            r = c.post("/auth/login", json={"email": "u@e.com", "displayName": "u", "password": "s"})
        if r.status_code != 200:
            r = c.post("/auth/register", json={"email": "u@e.com", "displayName": "u", "password": "s",
                                              "setupToken": app.state.setup_token.peek()})
        tok = r.json()["accessToken"]
        H = {"Authorization": f"Bearer {tok}"}
        con = c.post("/connectors", headers=H, json={"name": "m"}).json()
        cid, ctok = con["connector"]["id"], con["connectorToken"]
        caccess = c.post("/connector/auth", headers={"Authorization": f"Connector {cid}:{ctok}"}).json()["accessToken"]
        proj = c.post("/projects", headers=H, json={"name": "p", "connectorId": cid, "workspacePath": "/r"}).json()["project"]["id"]
        sess = c.post("/sessions", headers=H, json={"connectorId": cid, "projectId": proj, "runtime": "codex",
                                                    "externalSessionId": "thr_m", "title": "m", "cwd": "/r"}).json()["session"]["id"]
        CH = {"Authorization": f"Bearer {caccess}"}

        pid = os.getpid()
        print(f"  baseline rss: {rss_mb(pid):.0f} MB")

        for size_mb in (1, 4, 16):
            text = "x" * (size_mb * 1024 * 1024)
            payload = {"notifications": [{"method": "timeline.itemUpsert", "params": {
                "sessionId": sess, "externalSessionId": "thr_m",
                "item": {"id": f"i{size_mb}", "sessionId": sess, "type": "message", "role": "assistant",
                         "status": "done", "orderSeq": size_mb, "revision": 1,
                         "content": {"text": text, "format": "markdown"},
                         "source": {"runtime": "codex"}, "contentHash": "0" * 64}}}]}
            body = json.dumps(payload)
            before = rss_mb(pid)
            resp = c.post("/connector/ingest", content=body, headers={**CH, "Content-Type": "application/json"})
            after = rss_mb(pid)
            print(f"  body {len(body)/1048576:6.1f} MB -> HTTP {resp.status_code}   rss {before:.0f} -> {after:.0f} MB "
                  f"(+{after-before:.0f})")
        # Now the other half: a body over the ceiling must be refused without
        # being parsed, so the floor does not move. This is the production
        # failure mode — the deployed Connector batches by count, so it can
        # send a body of any size, and each one raises the floor until the
        # container is killed.
        limit = app.state.request_body_limit
        text = "x" * (limit + 1024 * 1024)
        payload = {"notifications": [{"method": "timeline.itemUpsert", "params": {
            "sessionId": sess, "externalSessionId": "thr_m",
            "item": {"id": "oversized", "sessionId": sess, "type": "message",
                     "role": "assistant", "status": "done", "orderSeq": 99,
                     "revision": 1, "content": {"text": text, "format": "markdown"},
                     "source": {"runtime": "codex"}, "contentHash": "0" * 64}}}]}
        body = json.dumps(payload)
        before = rss_mb(pid)
        resp = c.post("/connector/ingest", content=body,
                      headers={**CH, "Content-Type": "application/json"})
        after = rss_mb(pid)
        print(f"  body {len(body)/1048576:6.1f} MB -> HTTP {resp.status_code}   "
              f"rss {before:.0f} -> {after:.0f} MB ({after-before:+.0f})   "
              f"[ceiling {limit/1048576:.0f} MiB]")
        print(f"  final rss: {rss_mb(pid):.0f} MB")
    server.should_exit = True
    time.sleep(0.5)
