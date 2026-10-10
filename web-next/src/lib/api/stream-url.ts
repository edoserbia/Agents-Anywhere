/**
 * Which origin a live stream should connect to.
 *
 * `window.location.origin` is only usable when it is itself a WebSocket scheme.
 * In a browser it always is. In the Desktop shell the renderer is served from
 * `aa-workbench://web`, and Electron refuses to construct a WebSocket for that
 * scheme — "The URL's scheme must be either 'http', 'https', 'ws', or 'wss'" —
 * so building the URL from the current origin threw on every attempt. Both live
 * streams (the session view and the dashboard) retried forever and neither had
 * an ongoing polling fallback, so the Desktop showed the first snapshot and
 * never the activity after it.
 *
 * The shell therefore answers with the origin that does work. Everything else
 * keeps the old behaviour, including a shell that is too old to answer.
 */

type StreamBridge = {
  stream?: { baseUrl?: () => Promise<string | null> };
};

function streamBridge(): StreamBridge["stream"] | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as unknown as { desktopWorkbench?: StreamBridge }).desktopWorkbench?.stream;
}

export async function streamBaseUrl(): Promise<string | null> {
  const bridge = streamBridge();
  if (!bridge?.baseUrl) return null;
  try {
    const resolved = await bridge.baseUrl();
    return typeof resolved === "string" && resolved ? resolved : null;
  } catch {
    // A shell that cannot answer is not a reason to stop streaming.
    return null;
  }
}

/**
 * Build a stream URL. `explicitBase` wins when a build configured one, so an
 * install that points the client at a fixed API origin keeps doing that.
 */
export async function webSocketUrl(path: string, explicitBase?: string): Promise<string> {
  if (typeof window === "undefined") return path;
  const base = explicitBase || (await streamBaseUrl()) || window.location.origin;
  const url = new URL(path, base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

/**
 * The address other people can use to reach this deployment.
 *
 * Not a stream concern, but the same question: `window.location.origin` is only
 * an address the outside world can reach in a browser. In the Desktop shell it
 * is `aa-workbench://web`, so anything built from it — the endpoint shown on the
 * service page, the OAuth callback derived from it — means nothing outside the
 * shell. Falls back to `fallback` when nothing can answer.
 */
export async function publicOrigin(fallback: string): Promise<string> {
  if (typeof window === "undefined") return fallback;
  const resolved = (await streamBaseUrl()) || window.location.origin;
  return resolved.replace(/\/$/, "") || fallback;
}
