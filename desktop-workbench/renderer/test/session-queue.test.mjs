/**
 * The composer must stay usable while a runtime is mid-turn.
 *
 * A runtime that cannot steer (DSH declares `steerTurn: false`) still accepts a
 * message: the server holds it in the queue and sends it when the turn ends.
 * The composer previously treated "running and cannot steer" as "cannot type",
 * which locked the input for the whole run. These cases pin the corrected rule
 * against the composer source, because that is where the gate lives.
 */

import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

const here = path.dirname(fileURLToPath(import.meta.url))
const composerSource = readFileSync(
  path.join(here, "..", "src", "components", "session", "session-composer.tsx"),
  "utf8",
)
const detailSource = readFileSync(
  path.join(here, "..", "src", "components", "session-detail.tsx"),
  "utf8",
)
const panelSource = readFileSync(
  path.join(here, "..", "src", "components", "session", "session-queue-panel.tsx"),
  "utf8",
)
const apiSource = readFileSync(
  path.join(here, "..", "src", "features", "dashboard", "api.ts"),
  "utf8",
)

/** Pull the source of the composer's derived-value block. */
function composerGateBlock() {
  const start = composerSource.indexOf("const queuesWhileRunning")
  const end = composerSource.indexOf("const hasInput")
  assert.ok(start > -1 && end > start, "composer gate block not found")
  return composerSource.slice(start, end)
}

test("a running session stays editable when it can send but not steer", () => {
  const accepts = composerGateBlock()
  // `isRunning` must not veto input on its own; the queue path keeps it open.
  assert.match(
    accepts,
    /!isRunning \|\| queuesWhileRunning/,
    "a running, queueable session must still accept input",
  )
})

test("all running send-capable runtimes use the Agents Anywhere queue", () => {
  const block = composerGateBlock()
  assert.match(block, /const queuesWhileRunning = isRunning && canUseSendMessage/)
  assert.match(block, /const acceptsUserInput/)
})

test("a running steer-capable runtime still queues through the server", () => {
  const block = composerGateBlock()
  assert.match(block, /const queuesWhileRunning = isRunning && canUseSendMessage/)
})

test("the queueing submit asks the server to hold the message", () => {
  assert.match(
    composerSource,
    /queueWhenBusy: queuesWhileRunning/,
    "the composer must pass queueWhenBusy when it decided to queue",
  )
  assert.match(
    apiSource,
    /\.\.\.\(queueWhenBusy \? \{ queueWhenBusy: true \} : \{\}\)/,
    "the API must forward queueWhenBusy to the server",
  )
})

test("the busy placeholder is replaced by a queue-aware one", () => {
  assert.match(
    composerSource,
    /queuesWhileRunning\s*\n?\s*\?\s*tSession\("queuedPlaceholder"\)/,
    "a queueable running session must not show the blocking placeholder",
  )
})

test("the queue panel offers edit and delete for pending items", () => {
  assert.match(panelSource, /data-slot="session-queue-panel"/)
  assert.match(panelSource, /t\("queueEdit"\)/)
  assert.match(panelSource, /t\("queueDelete"\)/)
  assert.match(panelSource, /t\("queueSave"\)/)
  assert.match(panelSource, /t\("queueInsert"\)/)
  assert.match(panelSource, /aria-expanded={!collapsed}/, "the panel can be collapsed")
  assert.match(panelSource, /const canMutate = item\.status === "queued" \|\| item\.status === "failed"/)
})

test("the session reads and mutates the queue through the API", () => {
  for (const call of [
    "getSessionQueue",
    "updateQueuedMessage",
    "deleteQueuedMessage",
    "insertQueuedMessage",
  ]) {
    assert.ok(apiSource.includes(call), `api must expose ${call}`)
    assert.ok(detailSource.includes(`dashboardApi.${call}`), `session must call ${call}`)
  }
  assert.match(
    detailSource,
    /<SessionQueuePanel/,
    "the queue panel must be rendered in the session view",
  )
  assert.match(
    detailSource,
    /if \(options\?\.queueWhenBusy\) void refreshQueue\(\)/,
    "a queued send must refresh the queue so the row appears",
  )
  assert.match(detailSource, /removeOptimisticMessage\(clientMessageId\)/, "queued messages must not remain in the transcript")
  assert.match(detailSource, /response\.items\?\.some\(\(item\) => item\.clientMessageId === clientMessageId\)/, "a lost send response is reconciled against the server queue")
})
