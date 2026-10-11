/**
 * The Desktop's client against the real endpoint, over a real socket.
 *
 * Everything else about local-direct is tested with the caller injected, which
 * proves the routing and the fallbacks but not the wire. This starts the actual
 * Python endpoint — the same class the Connector runs — and drives it through
 * the same WebSocket client the app uses, so the handshake, the token, the
 * request/response shape and the refusal path are all exercised for real.
 */

import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { LocalDirect } from "./local-direct";

const REPO = path.resolve(__dirname, "..", "..", "..");
const PYTHON = path.join(REPO, "connector", ".venv", "bin", "python");

const STARTER = `
import asyncio, json, os, sys
sys.path.insert(0, ".")
from connector.server.local_endpoint import LocalEndpoint

async def dispatch(method, params):
    if method == "session.state":
        return {"status": "idle", "method": method}
    if method == "session.send_message":
        return {"turnId": "turn-from-python", "method": method, "params": params}
    raise RuntimeError("unexpected " + method)

async def main():
    endpoint = LocalEndpoint(dispatch, path=os.path.join(os.environ["AA_TEST_DIR"], "local-endpoint.json"))
    await endpoint.start()
    print("READY", flush=True)
    await asyncio.sleep(120)

asyncio.run(main())
`;

function startEndpoint(directory: string): ChildProcess {
  return spawn(PYTHON, ["-c", STARTER], {
    cwd: path.join(REPO, "connector"),
    env: { ...process.env, AA_TEST_DIR: directory },
    stdio: ["ignore", "pipe", "pipe"],
  });
}

async function waitForDescriptor(directory: string, child: ChildProcess) {
  const descriptorPath = path.join(directory, "local-endpoint.json");
  for (let i = 0; i < 200; i += 1) {
    if (child.exitCode !== null) throw new Error(`the endpoint exited with ${child.exitCode}`);
    if (existsSync(descriptorPath)) {
      const descriptor = JSON.parse(readFileSync(descriptorPath, "utf8"));
      if (typeof descriptor.port === "number" && descriptor.port > 0) return descriptorPath;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("the endpoint never published a descriptor");
}

test("the Desktop's client talks to the real endpoint", async () => {
  // The repository venv is how the Connector is run in development. Its absence
  // is a failure, not a skip: a test that quietly does nothing is worse than no
  // test, because it reads as proof.
  assert.ok(
    existsSync(PYTHON),
    `the Connector's interpreter is missing at ${PYTHON}; this test needs it to run`,
  );
  const directory = mkdtempSync(path.join(tmpdir(), "aa-local-direct-"));
  const child = startEndpoint(directory);
  try {
    const descriptorPath = await waitForDescriptor(directory, child);
    const local = new LocalDirect({
      descriptorPath,
      serverOrigin: () => "https://server.test",
      fetcher: (async () =>
        new Response(JSON.stringify({ session: { runtime: "codex", runtimeId: "rti_x" } }), {
          status: 200,
        })) as unknown as typeof fetch,
    });

    // A read.
    const stateRequest = new Request(
      "https://server.test/api/v2/sessions/sess_1/runtime/state",
      { method: "GET" },
    );
    const state = await local.handle(stateRequest, new URL(stateRequest.url));
    assert.ok(state, "expected the real endpoint to answer a read");
    const stateBody = (await state.json()) as { state: { status: string } };
    assert.equal(stateBody.state.status, "idle");
    assert.ok(Date.parse((stateBody as unknown as { serverTime: string }).serverTime) > 0);

    // A write, carrying what the Desktop built.
    const sendRequest = new Request(
      "https://server.test/api/v2/sessions/sess_1/runtime/messages",
      {
        method: "POST",
        body: JSON.stringify({ content: "hello" }),
        headers: { "content-type": "application/json" },
      },
    );
    const sent = await local.handle(sendRequest, new URL(sendRequest.url));
    assert.ok(sent, "expected the real endpoint to answer a write");
    const sentBody = (await sent.json()) as {
      ok: boolean;
      result: { turnId: string; params: Record<string, unknown> };
    };
    assert.equal(sentBody.ok, true);
    assert.equal(sentBody.result.turnId, "turn-from-python");
    assert.equal(sentBody.result.params.sessionId, "sess_1");
    assert.equal(sentBody.result.params.runtime, "codex");
    assert.equal(sentBody.result.params.content, "hello");

    // A method the endpoint does not serve must come back as a fallback, not as
    // an error the renderer would see.
    const refused = new LocalDirect({
      descriptorPath,
      serverOrigin: () => "https://server.test",
      fetcher: (async () =>
        new Response(JSON.stringify({ session: { runtime: "codex" } }), { status: 200 })) as unknown as typeof fetch,
      callLocal: undefined,
    });
    const timeline = new Request("https://server.test/api/v2/sessions/sess_1/timeline");
    assert.equal(await refused.handle(timeline, new URL(timeline.url)), null);
  } finally {
    child.kill("SIGKILL");
    rmSync(directory, { recursive: true, force: true });
  }
});
