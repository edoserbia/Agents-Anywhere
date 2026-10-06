import assert from "node:assert/strict"
import test from "node:test"
import { readFileSync } from "node:fs"
import { setTimeout as delay } from "node:timers/promises"
import { JSDOM } from "jsdom"
import { registerSource } from "./helpers/onboarding-source.mjs"

/**
 * "New project" opens on an engine that can create a project.
 *
 * The project form asks for a directory only when the engine it is about to use
 * cannot create one itself. A caller that already has an engine must therefore
 * hand it over only when the user picked it by hand: the engine a composer
 * happens to sit on is usually just the first one the device listed, and that
 * default must not drag the form back into asking for a path the device's own
 * project-creating engine would have chosen anyway. An engine the user did pick
 * still decides for the rest of the flow.
 */
const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "https://app.example.test/", pretendToBeVisual: true })
for (const name of ["window", "document", "navigator", "HTMLElement", "HTMLInputElement", "HTMLButtonElement", "Element", "Node", "NodeFilter", "Event", "CustomEvent", "MutationObserver", "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame"]) {
  Object.defineProperty(globalThis, name, { configurable: true, value: dom.window[name] })
}
// The dialog's select and form primitives reach for DOM constructors beyond the
// handful above; jsdom owns them all.
for (const name of Object.getOwnPropertyNames(dom.window)) {
  if (!/^[A-Z]/.test(name) || name in globalThis) continue
  const value = dom.window[name]
  if (typeof value !== "function") continue
  Object.defineProperty(globalThis, name, { configurable: true, value })
}
window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} })
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const { createElement: h, act } = await import("react")
const { createRoot } = await import("react-dom/client")
const { NextIntlClientProvider } = await import("next-intl")
const hooks = registerSource()
const { AuthProvider } = await import("../src/components/auth/auth-context.tsx")
const { ProjectEditorDialog } = await import("../src/components/sidebar/project-editor-dialog.tsx")
const { authApi } = await import("../src/features/auth/api.ts")
const { dashboardApi } = await import("../src/features/dashboard/api.ts")
hooks.deregister()

const account = { userId: "user1", displayName: "Test User", role: "admin" }
const connector = { id: "conn_2080ti", name: "2080ti-server", status: "online", deviceOs: "linux" }
const runtime = (runtimeId, displayName, capabilities) => ({
  connectorId: connector.id,
  runtimeId,
  runtimeType: runtimeId,
  displayName,
  typeDisplayName: displayName,
  present: true,
  available: true,
  reason: null,
  configured: true,
  active: true,
  status: "running",
  discovery: {},
  metadata: {},
  capabilities,
  schema: null,
  uiSchema: {},
  defaults: {},
  config: {},
  error: null,
  lastDiscoveredAt: null,
  createdAt: null,
  updatedAt: null,
})

async function render(t, { preferredRuntimeId } = {}) {
  window.localStorage.clear()
  window.localStorage.setItem("aa.session.v1", JSON.stringify({ ...account, accessToken: "test-session" }))
  t.mock.method(authApi, "config", async () => ({ needsBootstrap: false, registrationOpen: true }))
  t.mock.method(authApi, "me", async () => account)
  t.mock.method(dashboardApi, "getConnectorRuntimes", async () => ({
    connectorId: connector.id,
    runtimes: [
      runtime("codex", "Codex", { modelCatalog: true }),
      runtime("openscience", "OpenScience", { createProject: true }),
    ],
    serverTime: null,
  }))
  const container = document.createElement("div")
  document.body.append(container)
  const root = createRoot(container)
  await act(async () => root.render(
    h(NextIntlClientProvider, {
      locale: "en",
      messages: JSON.parse(readFileSync(new URL("../messages/en.json", import.meta.url), "utf8")),
      timeZone: "UTC",
    }, h(AuthProvider, null, h(ProjectEditorDialog, {
      editor: { mode: "create" },
      connectors: [connector],
      preferredConnectorId: connector.id,
      preferredRuntimeId,
      projects: [],
      onOpenChange: () => {},
      onCreate: async () => null,
      onUpdate: async () => null,
    }))),
  ))
  t.after(async () => { await act(async () => root.unmount()); container.remove() })
  return container
}

async function until(check) {
  for (let count = 0; count < 100; count++) {
    if (check()) return
    await act(async () => { await delay(15) })
  }
  assert.fail("Expected UI state did not arrive: " + document.body.textContent)
}

test("an engine the caller picked by hand still asks for a directory", async (t) => {
  await render(t, { preferredRuntimeId: "codex" })
  await until(() => document.querySelector("#project-workspace"))
  assert.ok(document.querySelector("#project-workspace"), "the path field must come back for a hand-picked engine that cannot create projects")
})

test("with no engine of the caller's own, the device's project-creating runtime owns the path", async (t) => {
  await render(t)
  await until(() => document.body.textContent.includes("The location is chosen by OpenScience."))
  assert.equal(document.querySelector("#project-workspace"), null, "the form must not ask for a path the runtime generates itself")
})

// ── The caller's side of the contract ──────────────────────────

const COMPOSER = readFileSync(new URL("../src/components/task-composer.tsx", import.meta.url), "utf8")

test("the composer hands the form an engine only when the user picked it", () => {
  const start = COMPOSER.indexOf("<ProjectEditorDialog")
  assert.notEqual(start, -1, "the composer renders the project form")
  const props = COMPOSER.slice(start, COMPOSER.indexOf("/>", start))
  assert.match(
    props,
    /preferredRuntimeId=\{agentChosenExplicitly\s*\?\s*selectedRuntime\?\.runtimeId\s*:\s*undefined\}/,
    "a defaulted engine must not be handed to the project form",
  )
})

test("the engine choice is recorded where the user makes it, and dropped with the device", () => {
  const pick = COMPOSER.indexOf("const handleAgentChange")
  assert.notEqual(pick, -1, "the composer has an engine picker")
  assert.match(
    COMPOSER.slice(pick, COMPOSER.indexOf("}, [", pick)),
    /setAgentChosenExplicitly\(true\)/,
    "picking an engine by hand is a choice",
  )

  const device = COMPOSER.indexOf("const handleDeviceChange")
  assert.notEqual(device, -1, "the composer has a device picker")
  assert.match(
    COMPOSER.slice(device, COMPOSER.indexOf("}, [", device)),
    /setAgentChosenExplicitly\(false\)/,
    "an engine chosen for one device is not a choice for the next",
  )
})
