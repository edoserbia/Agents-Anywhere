/**
 * The Desktop shell is not a WebSocket-capable origin.
 *
 * It serves the renderer from `aa-workbench://web`, and Electron refuses to
 * construct a WebSocket for that scheme — "The URL's scheme must be either
 * 'http', 'https', 'ws', or 'wss'". Building the stream URL from
 * `window.location.origin` therefore threw on every attempt, both live streams
 * retried forever, and the session view never showed activity after its first
 * snapshot.
 *
 * So these tests are about *which origin* the URL comes from. A browser must
 * keep using its own origin; a shell that answers must be believed; a shell that
 * cannot answer must not stop streaming.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://closex.cc/dashboard",
});
Object.defineProperty(globalThis, "window", { configurable: true, value: dom.window });

const { webSocketUrl } = await import("../src/lib/api/stream-url.ts");

test("a browser keeps using its own origin", async () => {
  delete window.desktopWorkbench;
  assert.equal(
    await webSocketUrl("/api/v2/sessions/s1/ws?ticket=t"),
    "wss://closex.cc/api/v2/sessions/s1/ws?ticket=t",
  );
});

test("the Desktop shell's origin is never used for a stream", async () => {
  // What the renderer actually sees there. Chromium would throw on this URL.
  const shell = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "aa-workbench://web/dashboard",
  });
  const previous = globalThis.window;
  Object.defineProperty(globalThis, "window", { configurable: true, value: shell.window });
  try {
    shell.window.desktopWorkbench = {
      stream: { baseUrl: async () => "https://closex.cc" },
    };
    const url = await webSocketUrl("/api/v2/sessions/s1/ws?ticket=t");
    assert.equal(url, "wss://closex.cc/api/v2/sessions/s1/ws?ticket=t");
    assert.ok(!url.includes("aa-workbench"), url);
  } finally {
    Object.defineProperty(globalThis, "window", { configurable: true, value: previous });
  }
});

test("an http shell yields ws, and an https shell yields wss", async () => {
  const previous = globalThis.window;
  const insecure = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "http://127.0.0.1:5184/",
  });
  const secure = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "https://self.example/",
  });
  try {
    Object.defineProperty(globalThis, "window", { configurable: true, value: insecure.window });
    insecure.window.desktopWorkbench = { stream: { baseUrl: async () => "http://127.0.0.1:43120" } };
    assert.equal(
      await webSocketUrl("/api/v2/dashboard/ws?ticket=t"),
      "ws://127.0.0.1:43120/api/v2/dashboard/ws?ticket=t",
    );

    Object.defineProperty(globalThis, "window", { configurable: true, value: secure.window });
    secure.window.desktopWorkbench = { stream: { baseUrl: async () => "https://self.example" } };
    assert.equal(
      await webSocketUrl("/api/v2/dashboard/ws?ticket=t"),
      "wss://self.example/api/v2/dashboard/ws?ticket=t",
    );
  } finally {
    Object.defineProperty(globalThis, "window", { configurable: true, value: previous });
  }
});

test("a shell that cannot answer does not stop streaming", async () => {
  const previous = globalThis.window;
  const shell = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "https://closex.cc/",
  });
  try {
    Object.defineProperty(globalThis, "window", { configurable: true, value: shell.window });
    // Throws.
    shell.window.desktopWorkbench = {
      stream: { baseUrl: async () => { throw new Error("no server configured"); } },
    };
    assert.equal(
      await webSocketUrl("/api/v2/dashboard/ws?ticket=t"),
      "wss://closex.cc/api/v2/dashboard/ws?ticket=t",
    );

    // Answers with nothing.
    shell.window.desktopWorkbench = { stream: { baseUrl: async () => null } };
    assert.equal(
      await webSocketUrl("/api/v2/dashboard/ws?ticket=t"),
      "wss://closex.cc/api/v2/dashboard/ws?ticket=t",
    );

    // Has no stream section at all, which is every build before this one.
    shell.window.desktopWorkbench = {};
    assert.equal(
      await webSocketUrl("/api/v2/dashboard/ws?ticket=t"),
      "wss://closex.cc/api/v2/dashboard/ws?ticket=t",
    );
  } finally {
    Object.defineProperty(globalThis, "window", { configurable: true, value: previous });
  }
});

test("the ticket is carried through and escaped", async () => {
  delete window.desktopWorkbench;
  const url = await webSocketUrl("/api/v2/sessions/s%2F1/ws?ticket=a%2Bb%3D");
  assert.equal(url, "wss://closex.cc/api/v2/sessions/s%2F1/ws?ticket=a%2Bb%3D");
});

test("a configured API origin still wins over the shell", async () => {
  const previous = globalThis.window;
  const shell = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "aa-workbench://web/",
  });
  try {
    Object.defineProperty(globalThis, "window", { configurable: true, value: shell.window });
    shell.window.desktopWorkbench = { stream: { baseUrl: async () => "https://closex.cc" } };
    assert.equal(
      await webSocketUrl("/api/v2/x/ws?ticket=t", "https://fixed.example"),
      "wss://fixed.example/api/v2/x/ws?ticket=t",
    );
  } finally {
    Object.defineProperty(globalThis, "window", { configurable: true, value: previous });
  }
});
