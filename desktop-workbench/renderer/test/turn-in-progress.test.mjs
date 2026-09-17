import assert from "node:assert/strict"
import test from "node:test"
import fs from "node:fs"

/**
 * `sessionTurnInProgress` decides whether a running turn's process stays open.
 *
 * It is not exported for testing, so this asserts the rule against the source:
 * both status sources must be consulted. That is deliberate — the bug was that
 * one source was trusted alone, and a behavioural test would need the whole
 * session view mounted to reproduce the disagreement.
 */
const SOURCE = fs.readFileSync(
  new URL("../src/components/session-detail.tsx", import.meta.url),
  "utf8",
)

test("turn activity is read from both the runtime and the session", () => {
  const start = SOURCE.indexOf("function sessionTurnInProgress(")
  assert.notEqual(start, -1, "the helper exists")

  const body = SOURCE.slice(start, start + 900)
  // The runtime's answer alone is what used to hide a live turn.
  assert.match(body, /effectiveRuntimeStatus\(/, "consults the runtime status")
  assert.match(
    body,
    /session\?\.status/,
    "also consults the session status, which flips first when work starts",
  )
  assert.match(body, /ACTIVE_STATUSES\.has\(/, "compares against the active set")
})

test("every working status counts as active", () => {
  const start = SOURCE.indexOf("const ACTIVE_STATUSES = new Set<string>([")
  assert.notEqual(start, -1, "the active set exists")
  const body = SOURCE.slice(start, SOURCE.indexOf("])", start))

  for (const status of ["waiting", "pending", "running", "stopping", "waiting_approval"]) {
    assert.ok(body.includes(`"${status}"`), `${status} keeps the fold open`)
  }
})

test("the fold uses the helper rather than a single status check", () => {
  assert.match(
    SOURCE,
    /const turnInProgress = sessionTurnInProgress\(runtimeState, session\)/,
    "turnInProgress is derived from both sources",
  )
})
