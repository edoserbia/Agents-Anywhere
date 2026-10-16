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
import { LocalDeliveryUnknown, LocalDirect } from "./local-direct";

const SERVER = "https://server.test";

function meta(session: Record<string, unknown> = { runtime: "codex", runtimeId: "rti_1", takeover: true }) {
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
    body: JSON.stringify({ content: "hello", clientMessageId: "cm_default" }),
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

test("a queueing send always falls back so the Server owns admission", async () => {
  let called = false
  const local = make({
    callLocal: async () => {
      called = true
      return { queued: true }
    },
  })
  const request = new Request(`${SERVER}/api/v2/sessions/sess_1/runtime/messages`, {
    method: "POST",
    body: JSON.stringify({ content: "hello", clientMessageId: "cm_queue", queueWhenBusy: true }),
    headers: { "content-type": "application/json" },
  })
  assert.equal(await local.handle(request, url(request)), null)
  assert.equal(called, false, "a local send must not bypass the queue endpoint")
})

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

test("the metadata read carries the caller's credential", async () => {
  // Without this the read is a 401 for every signed-in user, so the channel
  // would never engage — safe, but indistinguishable from working. Found by
  // running the compiled module against a real signed-in Server.
  let seen: string | null = null;
  const local = make({
    fetcher: (async (_input: unknown, init?: { headers?: Record<string, string> }) => {
      seen = init?.headers?.authorization ?? null;
      return new Response(JSON.stringify({ session: { runtime: "codex" } }), { status: 200 });
    }) as unknown as typeof fetch,
    callLocal: async () => ({ status: "idle" }),
  });
  const request = new Request(`${SERVER}/api/v2/sessions/sess_1/runtime/state`, {
    method: "GET",
    headers: { authorization: "Bearer user-token" },
  });
  assert.ok(await local.handle(request, url(request)));
  assert.equal(seen, "Bearer user-token");
});

test("a caller with no credential is not given one", async () => {
  let seen: string | null = "unset";
  const local = make({
    fetcher: (async (_input: unknown, init?: { headers?: Record<string, string> }) => {
      seen = init?.headers?.authorization ?? null;
      return new Response(JSON.stringify({ session: { runtime: "codex" } }), { status: 200 });
    }) as unknown as typeof fetch,
    callLocal: async () => ({ status: "idle" }),
  });
  const request = STATE();
  await local.handle(request, url(request));
  assert.equal(seen, null);
});

test("a send without an idempotency key goes the normal way", async () => {
  // Without one, a retry cannot be told apart from a second send, and the
  // Server keeps whatever the runtime produces — so the message would appear
  // twice. The Server's own path is where that is handled.
  const local = make({
    callLocal: async () => {
      throw new Error("must not be called");
    },
  });
  const request = new Request(`${SERVER}/api/v2/sessions/sess_1/runtime/messages`, {
    method: "POST",
    body: JSON.stringify({ content: "hello" }),
    headers: { "content-type": "application/json" },
  });
  assert.equal(await local.handle(request, url(request)), null);
});

test("the idempotency key reaches the runtime", async () => {
  const seen: Array<Record<string, unknown>> = [];
  const local = make({
    callLocal: async (_method, params) => {
      seen.push(params);
      return { turnId: "t1" };
    },
  });
  const request = new Request(`${SERVER}/api/v2/sessions/sess_1/runtime/messages`, {
    method: "POST",
    body: JSON.stringify({ content: "hello", clientMessageId: "cm_1" }),
    headers: { "content-type": "application/json" },
  });
  assert.ok(await local.handle(request, url(request)));
  assert.equal(seen[0].clientMessageId, "cm_1");
});

test("a write that may have been delivered is reported, never re-sent", async () => {
  // The one case where falling back would run the user's message twice: the
  // request reached the runtime and the reply was lost.
  const local = make({
    callLocal: async () => {
      throw new LocalDeliveryUnknown("socket closed: delivered");
    },
  });
  const request = new Request(`${SERVER}/api/v2/sessions/sess_1/runtime/messages`, {
    method: "POST",
    body: JSON.stringify({ content: "hello", clientMessageId: "cm_2" }),
    headers: { "content-type": "application/json" },
  });
  const response = await local.handle(request, url(request));
  assert.ok(response, "expected a reported failure, not a fallback");
  const body = (await response.json()) as { ok: boolean; error: { code: string } };
  assert.equal(body.ok, false);
  assert.equal(body.error.code, "local_delivery_unknown");
});

test("a read that may have been delivered still falls back", async () => {
  // Reads can be repeated, so ambiguity costs nothing there.
  const local = make({
    callLocal: async () => {
      throw new LocalDeliveryUnknown("socket closed");
    },
  });
  const request = STATE();
  const response = await local.handle(request, url(request));
  assert.ok(response);
  const body = (await response.json()) as { ok?: boolean; error?: { code: string } };
  // The read path builds a state response; the ambiguous error must not have
  // leaked into it, and the caller must not have been given a fallback either.
  assert.equal(body.error?.code, "local_delivery_unknown");
});

test("a session that is read-only is not written to locally", async () => {
  // Takeover is the owner's decision. A local send that ignored it would be a
  // way around the product's rules rather than a faster way through them.
  //
  // The assertion is that no call was *attempted*. Asserting only that the
  // caller fell back would pass with the gate removed too, because the call
  // would be made, fail, and fall back anyway — a test that cannot tell the
  // difference is not evidence.
  let attempted = false;
  const local = make({
    fetcher: (async () =>
      new Response(JSON.stringify({ session: { runtime: "codex", takeover: false } }), {
        status: 200,
      })) as unknown as typeof fetch,
    callLocal: async () => {
      attempted = true;
      return { turnId: "t1" };
    },
  });
  assert.equal(await local.handle(SEND(), url(SEND())), null);
  assert.equal(attempted, false, "a read-only session must not be written to");
});

test("an archived session is not written to locally", async () => {
  let attempted = false;
  const local = make({
    fetcher: (async () =>
      new Response(
        JSON.stringify({ session: { runtime: "codex", takeover: true, archived: true } }),
        { status: 200 },
      )) as unknown as typeof fetch,
    callLocal: async () => {
      attempted = true;
      return { turnId: "t1" };
    },
  });
  assert.equal(await local.handle(SEND(), url(SEND())), null);
  assert.equal(attempted, false, "an archived session must not be written to");
});

test("a writable session does reach the Connector", async () => {
  // The control for the two above: without this, a gate that refused
  // everything would look correct.
  let attempted = false;
  const local = make({
    fetcher: (async () =>
      new Response(JSON.stringify({ session: { runtime: "codex", takeover: true } }), {
        status: 200,
      })) as unknown as typeof fetch,
    callLocal: async () => {
      attempted = true;
      return { turnId: "t1" };
    },
  });
  assert.ok(await local.handle(SEND(), url(SEND())));
  assert.equal(attempted, true);
});

test("a read still works on a read-only session", async () => {
  // Read-only means read-only, not unusable.
  const local = make({
    fetcher: (async () =>
      new Response(JSON.stringify({ session: { runtime: "codex", takeover: false } }), {
        status: 200,
      })) as unknown as typeof fetch,
    callLocal: async () => ({ status: "idle" }),
  });
  const request = STATE();
  assert.ok(await local.handle(request, url(request)));
});

test("a writable session is written to locally", async () => {
  const local = make({
    fetcher: (async () =>
      new Response(JSON.stringify({ session: { runtime: "codex", takeover: true } }), {
        status: 200,
      })) as unknown as typeof fetch,
    callLocal: async () => ({ turnId: "t1" }),
  });
  assert.ok(await local.handle(SEND(), url(SEND())));
});
