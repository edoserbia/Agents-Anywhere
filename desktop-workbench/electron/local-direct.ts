/**
 * Serve a request from the Connector on this machine when it can, and let the
 * Server answer when it cannot.
 *
 * The Desktop already proxies the renderer's API calls to the Server. When the
 * Connector that owns a session is running on this same machine, that hop out to
 * the public internet and back is pure latency: the answer is on the machine
 * already. This module takes that hop out for the calls where it is safe to do
 * so, and falls back for everything else.
 *
 * Two rules make it safe:
 *
 * 1. **A local answer never replaces the Server as the record.** The Connector
 *    watches the runtime it drives and uploads what it observes through the same
 *    ingest path it always uses, so every other machine still learns the
 *    session's state from the Server. Nothing here is the only copy of anything.
 *
 * 2. **Any doubt falls back.** An unknown session, a missing descriptor, a
 *    refused write, a closed socket, a timeout — all of them return null and the
 *    caller forwards to the Server exactly as before. The Server's own pipeline
 *    (ownership, takeover, queue-when-busy, capabilities, attachments) is not
 *    reproduced here, so a caller that needs it must reach the Server; falling
 *    back is how that happens.
 */

export type LocalDirectOptions = {
  /** Where the Connector publishes how to reach it. */
  descriptorPath: string;
  /** The Server origin, for the one metadata read this needs. */
  serverOrigin: () => string | null;
  /** Used for the metadata read. */
  fetcher: typeof fetch;
  /**
   * Talks to the Connector. Injected so tests can drive this without a socket;
   * the default opens the loopback WebSocket the descriptor describes.
   */
  callLocal?: (method: string, params: Record<string, unknown>) => Promise<unknown>;
  /** Injectable clock, so the metadata cache can be tested without waiting. */
  now?: () => number;
  /** How long a session's runtime is remembered before it is re-read. */
  metadataTtlMs?: number;
};

type Descriptor = { host: string; port: number; token: string };

type SessionRuntime = {
  runtime: string;
  runtimeId?: string;
  cwd?: string;
  externalSessionId?: string;
  readAt: number;
};

const DEFAULT_METADATA_TTL_MS = 5 * 60_000;
const LOCAL_CALL_TIMEOUT_MS = 30_000;

/** Routes this module is willing to answer, and what each one maps to. */
const SERVED = [
  { method: "POST", pattern: /^\/api\/v2\/sessions\/([^/]+)\/runtime\/messages$/, kind: "send" },
  { method: "GET", pattern: /^\/api\/v2\/sessions\/([^/]+)\/runtime\/state$/, kind: "state" },
] as const;

export class LocalDirect {
  private readonly options: LocalDirectOptions;
  private readonly metadata = new Map<string, SessionRuntime>();
  private readonly ttl: number;
  private readonly clock: () => number;
  private descriptorCache: { descriptor: Descriptor; readAt: number } | null = null;

  constructor(options: LocalDirectOptions) {
    this.options = options;
    this.ttl = options.metadataTtlMs ?? DEFAULT_METADATA_TTL_MS;
    this.clock = options.now ?? (() => Date.now());
  }

  /**
   * Answer locally, or return null so the caller forwards to the Server.
   *
   * Never throws: a failure here is a reason to use the Server, not a reason for
   * the renderer to see an error.
   */
  async handle(request: Request, url: URL): Promise<Response | null> {
    const route = SERVED.find(
      (candidate) => candidate.method === request.method && candidate.pattern.test(url.pathname),
    );
    if (!route) return null;
    const sessionId = decodeURIComponent(route.pattern.exec(url.pathname)![1]);
    try {
      const session = await this.sessionRuntime(sessionId, request.headers.get("authorization"));
      if (!session) return null;
      const params: Record<string, unknown> = {
        sessionId,
        runtime: session.runtime,
        ...(session.runtimeId ? { runtimeId: session.runtimeId } : {}),
        ...(session.cwd ? { cwd: session.cwd } : {}),
        ...(session.externalSessionId ? { externalSessionId: session.externalSessionId } : {}),
      };

      if (route.kind === "send") {
        const body = (await request.clone().json()) as Record<string, unknown>;
        if (typeof body?.content !== "string") return null;
        params.content = body.content;
        if (typeof body.clientMessageId === "string") params.clientMessageId = body.clientMessageId;
        // Attachments are resolved by the Server from file ids; without that
        // resolution the local call would silently drop them, so a message that
        // carries any goes the normal way.
        if (Array.isArray(body.attachments) && body.attachments.length > 0) return null;
        const result = await this.call("session.send_message", params);
        return json({ ok: true, result, error: null });
      }

      const result = await this.call("session.state", params);
      return json({ state: result, serverTime: new Date(this.clock()).toISOString() });
    } catch {
      // Any failure at all: the Server answers this one.
      return null;
    }
  }

  /** Forget everything cached; used when the Connector restarts. */
  reset(): void {
    this.metadata.clear();
    this.descriptorCache = null;
  }

  /**
   * The session's runtime, from the Server, once per session.
   *
   * Every method on the local channel is scoped by runtime, and only the Server
   * knows which runtime a session belongs to. One small read pays for it; the
   * call that the user is waiting on is the one that then goes local.
   */
  private async sessionRuntime(
    sessionId: string,
    authorization: string | null,
  ): Promise<SessionRuntime | null> {
    const cached = this.metadata.get(sessionId);
    if (cached && this.clock() - cached.readAt < this.ttl) return cached;
    const origin = this.options.serverOrigin();
    if (!origin) return null;
    // Carry the caller's own credential. Without it the read is a 401 for every
    // signed-in user, and this channel would silently never engage — which is
    // safe but useless, and indistinguishable from working.
    const response = await this.options.fetcher(
      `${origin}/api/v2/sessions/${encodeURIComponent(sessionId)}/meta`,
      {
        headers: {
          accept: "application/json",
          ...(authorization ? { authorization } : {}),
        },
      },
    );
    if (!response.ok) return null;
    const payload = (await response.json()) as { session?: Record<string, unknown> };
    const session = payload?.session;
    const runtime = session?.runtime;
    if (typeof runtime !== "string" || !runtime) return null;
    const entry: SessionRuntime = {
      runtime,
      runtimeId: typeof session?.runtimeId === "string" ? session.runtimeId : undefined,
      cwd: typeof session?.cwd === "string" ? session.cwd : undefined,
      externalSessionId:
        typeof session?.externalSessionId === "string" ? session.externalSessionId : undefined,
      readAt: this.clock(),
    };
    this.metadata.set(sessionId, entry);
    return entry;
  }

  private async call(method: string, params: Record<string, unknown>): Promise<unknown> {
    if (this.options.callLocal) return await this.options.callLocal(method, params);
    const descriptor = await this.descriptor();
    if (!descriptor) throw new Error("no local Connector");
    return await callEndpoint(descriptor, method, params);
  }

  private async descriptor(): Promise<Descriptor | null> {
    const cached = this.descriptorCache;
    if (cached && this.clock() - cached.readAt < this.ttl) return cached.descriptor;
    const { readFile } = await import("node:fs/promises");
    const raw = await readFile(this.options.descriptorPath, "utf8");
    const parsed = JSON.parse(raw) as Partial<Descriptor>;
    if (
      typeof parsed.host !== "string" ||
      typeof parsed.port !== "number" ||
      typeof parsed.token !== "string"
    ) {
      return null;
    }
    const descriptor = { host: parsed.host, port: parsed.port, token: parsed.token };
    this.descriptorCache = { descriptor, readAt: this.clock() };
    return descriptor;
  }
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

/** One request per connection: the endpoint answers and the socket closes. */
async function callEndpoint(
  descriptor: Descriptor,
  method: string,
  params: Record<string, unknown>,
): Promise<unknown> {
  const socket = new WebSocket(`ws://${descriptor.host}:${descriptor.port}`);
  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      try {
        socket.close();
      } catch {
        // already gone
      }
      reject(new Error("local endpoint timed out"));
    }, LOCAL_CALL_TIMEOUT_MS);
    const settle = (fn: () => void) => {
      clearTimeout(timer);
      try {
        socket.close();
      } catch {
        // already gone
      }
      fn();
    };
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ type: "initialize", token: descriptor.token }));
    });
    socket.addEventListener("message", (event) => {
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(String(event.data)) as Record<string, unknown>;
      } catch {
        return settle(() => reject(new Error("malformed local reply")));
      }
      if (message.type === "initialized") {
        socket.send(JSON.stringify({ type: "request", id: 1, method, params }));
        return;
      }
      if (message.type !== "response") return;
      if (message.error) {
        const error = message.error as { code?: string; message?: string };
        return settle(() => reject(new Error(error.message ?? error.code ?? "refused")));
      }
      settle(() => resolve(message.result));
    });
    socket.addEventListener("error", () => settle(() => reject(new Error("local endpoint failed"))));
    socket.addEventListener("close", () => settle(() => reject(new Error("local endpoint closed"))));
  });
}
