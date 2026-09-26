import assert from "node:assert/strict"
import test from "node:test"
import fs from "node:fs"

/**
 * A running turn must offer a way to queue the next message.
 *
 * The composer derives `showInterrupt` from the runtime being busy, which made
 * the submit button an interrupt and left Enter inert — so a message typed
 * during a run could not be queued at all, even though the runtime accepts one
 * and the server queues it. These assertions pin the rule that decides it.
 */
const SOURCE = fs.readFileSync(
  new URL("../src/components/session/session-composer.tsx", import.meta.url),
  "utf8",
)

test("the interrupt button yields to a send whenever something can be sent", () => {
  const start = SOURCE.indexOf("const canQueueWhileRunning")
  assert.notEqual(start, -1, "the queue affordance is computed")

  const block = SOURCE.slice(start, start + 400)
  assert.match(
    block,
    /queuesWhileRunning\s*&&/,
    "a running send-capable runtime uses the AA queue",
  )
  assert.match(
    block,
    /!canQueueWhileRunning/,
    "showInterrupt is suppressed while a queued send is available",
  )
})

test("Enter submits rather than being inert while a turn runs", () => {
  const start = SOURCE.indexOf('event.key === "Enter"')
  assert.notEqual(start, -1, "Enter is handled")
  const block = SOURCE.slice(start, start + 400)
  assert.match(block, /void submit\(\)/, "Enter reaches submit, which queues")
})
