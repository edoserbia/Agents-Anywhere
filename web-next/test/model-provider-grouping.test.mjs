import assert from "node:assert/strict"
import test from "node:test"

import {
  groupModelsByProvider,
  hasModelProviderGrouping,
  modelProvider,
} from "../src/components/session/catalog-selection.ts"

/**
 * A picker that lists every model the way the runtime serves it is unusable.
 *
 * OpenScience serves 146 models from a dozen providers, and the same model name
 * appears under several of them — "GPT-6.1 Sol" is served by both Command Code
 * Proxy and the AI98 Pro relay. These assertions pin the rule that turns that
 * flat list into provider groups, and pins that a runtime which names no
 * provider (DSH, Codex) keeps the flat list it has always had.
 */
function model(id, providerID, providerName) {
  return {
    id,
    displayName: id,
    metadata: providerID
      ? { providerID, providerName, source: "openscience.config.providers" }
      : { source: "dsh.catalog" },
  }
}

const MODELS = [
  model("cc-proxy/gpt-6.1-sol", "cc-proxy", "Command Code Proxy"),
  model("cc-proxy/gpt-5.6-luna", "cc-proxy", "Command Code Proxy"),
  model("rayinai/gpt-6.1-sol", "rayinai", "AI98 Pro (codex relay)"),
  model("opencode-go/gpt-5.6-luna", "opencode-go", "OpenCode Go"),
]

test("a model reports the provider the runtime published for it", () => {
  assert.deepEqual(modelProvider(MODELS[0]), {
    id: "cc-proxy",
    label: "Command Code Proxy",
  })
  assert.deepEqual(modelProvider(MODELS[2]), {
    id: "rayinai",
    label: "AI98 Pro (codex relay)",
  })
})

test("a model with no provider metadata reports nothing rather than guessing", () => {
  assert.deepEqual(modelProvider(model("dsh:model:x", null, null)), { id: "", label: "" })
})

test("models group by provider, keeping catalog order inside each group", () => {
  const groups = groupModelsByProvider(MODELS.map((item) => ({ provider: modelProvider(item), id: item.id })))
  assert.deepEqual(
    groups.map((group) => [group.id, group.label, group.items.map((item) => item.id)]),
    [
      ["cc-proxy", "Command Code Proxy", ["cc-proxy/gpt-6.1-sol", "cc-proxy/gpt-5.6-luna"]],
      ["rayinai", "AI98 Pro (codex relay)", ["rayinai/gpt-6.1-sol"]],
      ["opencode-go", "OpenCode Go", ["opencode-go/gpt-5.6-luna"]],
    ],
  )
})

test("grouping appears only when it distinguishes something", () => {
  assert.equal(hasModelProviderGrouping(MODELS.map((item) => ({ provider: modelProvider(item) }))), true)
  assert.equal(
    hasModelProviderGrouping(
      [model("a", "dsh", "DSH"), model("b", "dsh", "DSH")].map((item) => ({
        provider: modelProvider(item),
      })),
    ),
    false,
    "one provider needs no header",
  )
  assert.equal(
    hasModelProviderGrouping(
      [model("dsh:model:x", null, null)].map((item) => ({ provider: modelProvider(item) })),
    ),
    false,
    "no provider at all keeps the flat list",
  )
})

test("the composer and both pickers group instead of listing every model flat", async () => {
  const fs = await import("node:fs")
  const read = (relative) =>
    fs.readFileSync(new URL(relative, import.meta.url), "utf8")

  for (const path of [
    "../src/components/session/session-composer.tsx",
    "../src/components/task-composer.tsx",
  ]) {
    const source = read(path)
    assert.match(source, /hasModelProviderGrouping\(/, `${path} decides whether to group`)
    assert.match(source, /groupModelsByProvider\(/, `${path} renders provider groups`)
    assert.match(source, /provider: modelProvider\(item\)/, `${path} carries the provider`)
  }

  const drawer = read("../src/components/session/selection-settings-drawer.tsx")
  assert.match(drawer, /groupModelsByProvider\(modelItems\)/, "the drawer groups its models")
  assert.match(drawer, /aria-expanded=\{expanded\}/, "a provider group folds")
  assert.match(
    drawer,
    /providerToggles\[id\] \?\? id === selectedProviderId/,
    "the group holding the current choice opens itself",
  )
})
