/**
 * Local-first has to be invisible when it fails.
 *
 * The value of this module is one saved round trip; the cost of getting it wrong
 * is a broken send. So the tests are mostly about the ways it must *not* answer:
 * a route it does not serve, a session the Server does not know, a Connector
 * that refuses, a socket that dies, a message carrying attachments the Server
 * would have resolved. Every one of those has to come back null so the caller
 * forwards to the Server exactly as it did before this existed.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { LocalDirect } from "./local-direct";

const SERVER = "https://server.test";

function meta(session: Record<string, unknown> = { runtime: "codex", runtimeId: "rti_1" }) {
  return async () => new Response(JSON.stringify({ session }), { status: 200 });
}

function make(
  options: {
    callLocal?: (method: string, params: Record<string, unknown>) => Promise<unknown>;
    fetcher?: typeof fetch;
    origin?: string | null;
    now?: () => number;
  } = {},
) {
  return new LocalDirect({
    descriptorPath: "/nonexistent/local-endpoint.json",
    serverOrigin: () => (options.origin === undefined ? SERVER : options.origin),
    fetcher: options.fetcher ?? (meta() as unknown as typeof fetch),
    callLocal: options.callLocal,
    now: options.now,
  });
}

const SEND = (id = "sess_1") =>
  new Request(`${SERVER}/api/v2/sessions/${id}/runtime/messages`, {
    method: "POST",
    body: JSON.stringify({ content: "hello" }),
    headers: { "content-type": "application/json" },
  });

const STATE = (id = "sess_1") =>
  new Request(`${SERVER}/api/v2/sessions/${id}/runtime/state`, { method: "GET" });

const url = (request: Request) => new URL(request.url);

test("a route this does not serve falls back", async () => {
  const local = make();
  const request = new Request(`${SERVER}/api/v2/sessions/sess_1/timeline`);
  assert.equal(await local.handle(request, url(request)), null);
});

test("a send the Connector accepts is answered locally, in the Server's shape", async () => {
  const seen: Array<[string, Record<string, unknown>]> = [];
  const local = make({
    callLocal: async (method, params) => {
      seen.push([method, params]);
      return { turnId: "t1" };
    },
  });
  const response = await local.handle(SEND(), url(SEND()));
  assert.ok(response, "expected a local answer");
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, result: { turnId: "t1" }, error: null });
  assert.equal(seen.length, 1);
  assert.equal(seen[0][0], "session.send_message");
  assert.equal(seen[0][1].sessionId, "sess_1");
  assert.equal(seen[0][1].runtime, "codex");
  assert.equal(seen[0][1].runtimeId, "rti_1");
  assert.equal(seen[0][1].content, "hello");
});

test("a state read is answered locally, in the Server's shape", async () => {
  const local = make({ callLocal: async () => ({ status: "idle" }) });
  const response = await local.handle(STATE(), url(STATE()));
  assert.ok(response);
  const body = (await response.json()) as { state: unknown; serverTime: string };
  assert.deepEqual(body.state, { status: "idle" });
  assert.ok(Date.parse(body.serverTime) > 0, body.serverTime);
});

test("a refusal from the Connector falls back rather than failing the request", async () => {
  const local = make({
    callLocal: async () => {
      throw new Error("session is running");
    },
  });
  assert.equal(await local.handle(SEND(), url(SEND())), null);
});

test("a dead socket falls back", async () => {
  const local = make({
    callLocal: async () => {
      throw new Error("local endpoint closed");
    },
  });
  assert.equal(await local.handle(STATE(), url(STATE())), null);
});

test("a session the Server does not know falls back", async () => {
  const local = make({
    fetcher: (async () => new Response("nope", { status: 404 })) as unknown as typeof fetch,
    callLocal: async () => {
      throw new Error("must not be called");
    },
  });
  assert.equal(await local.handle(SEND("sess_missing"), url(SEND("sess_missing"))), null);
});

test("a session with no runtime falls back", async () => {
  const local = make({
    fetcher: (async () =>
      new Response(JSON.stringify({ session: { id: "s" } }), { status: 200 })) as unknown as typeof fetch,
    callLocal: async () => {
      throw new Error("must not be called");
    },
  });
  assert.equal(await local.handle(SEND(), url(SEND())), null);
});

test("no Server origin falls back", async () => {
  const local = make({ origin: null });
  assert.equal(await local.handle(SEND(), url(SEND())), null);
});

test("a message with attachments goes the normal way", async () => {
  // The Server resolves file ids into payloads and checks the capability. A
  // local send cannot, so it must not quietly send the message without them.
  const local = make({
    callLocal: async () => {
      throw new Error("must not be called");
    },
  });
  const request = new Request(`${SERVER}/api/v2/sessions/sess_1/runtime/messages`, {
    method: "POST",
    body: JSON.stringify({ content: "look", attachments: [{ fileId: "f1" }] }),
    headers: { "content-type": "application/json" },
  });
  assert.equal(await local.handle(request, url(request)), null);
});

test("a body without content falls back", async () => {
  const local = make({
    callLocal: async () => {
      throw new Error("must not be called");
    },
  });
  const request = new Request(`${SERVER}/api/v2/sessions/sess_1/runtime/messages`, {
    method: "POST",
    body: JSON.stringify({}),
    headers: { "content-type": "application/json" },
  });
  assert.equal(await local.handle(request, url(request)), null);
});

test("the session's runtime is read once and then remembered", async () => {
  let reads = 0;
  const local = make({
    fetcher: (async () => {
      reads += 1;
      return new Response(JSON.stringify({ session: { runtime: "dsh", runtimeId: "rti_d" } }), {
        status: 200,
      });
    }) as unknown as typeof fetch,
    callLocal: async () => ({ status: "idle" }),
  });
  for (let i = 0; i < 5; i += 1) {
    const request = STATE();
    assert.ok(await local.handle(request, url(request)));
  }
  assert.equal(reads, 1, "the metadata read must not repeat for every call");
});

test("the cache expires so a moved session is noticed", async () => {
  let reads = 0;
  let clock = 0;
  const local = make({
    now: () => clock,
    fetcher: (async () => {
      reads += 1;
      return new Response(JSON.stringify({ session: { runtime: "codex" } }), { status: 200 });
    }) as unknown as typeof fetch,
    callLocal: async () => ({ status: "idle" }),
  });
  const first = STATE();
  await local.handle(first, url(first));
  clock += 6 * 60_000;
  const second = STATE();
  await local.handle(second, url(second));
  assert.equal(reads, 2);
});

test("reset drops what it remembered", async () => {
  let reads = 0;
  const local = make({
    fetcher: (async () => {
      reads += 1;
      return new Response(JSON.stringify({ session: { runtime: "codex" } }), { status: 200 });
    }) as unknown as typeof fetch,
    callLocal: async () => ({ status: "idle" }),
  });
  const first = STATE();
  await local.handle(first, url(first));
  local.reset();
  const second = STATE();
  await local.handle(second, url(second));
  assert.equal(reads, 2);
});
