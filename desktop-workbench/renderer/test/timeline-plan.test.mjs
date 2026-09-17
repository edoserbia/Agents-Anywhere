import assert from "node:assert/strict"
import test from "node:test"

import {
  buildTimelinePlan,
  planItemIds,
} from "../src/components/session/timeline-plan.ts"

/** Minimal timeline item; only the fields the plan reader inspects. */
function item(overrides = {}) {
  return {
    id: "i1",
    sessionId: "s1",
    type: "tool",
    status: "done",
    role: null,
    content: {},
    source: {},
    orderSeq: 1,
    revision: 1,
    contentHash: "h",
    updatedSeq: 1,
    createdAt: "2026-09-16T10:00:00Z",
    updatedAt: "2026-09-16T10:00:00Z",
    completedAt: null,
    ...overrides,
  }
}

/** A `todo_write` call, the shape DSH actually emits on the timeline. */
function todoCall(id, orderSeq, todos, createdAt = "2026-09-16T10:00:00Z") {
  return item({
    id,
    orderSeq,
    createdAt,
    content: { kind: "tool_call", title: "todo_write", toolName: "todo_write", input: { todos } },
  })
}

test("reads the plan out of a todo_write tool call", () => {
  const plan = buildTimelinePlan([
    item({ id: "u1", type: "message", role: "user", orderSeq: 1 }),
    todoCall("p1", 2, [
      { content: "First step", status: "completed" },
      { content: "Second step", status: "in_progress" },
      { content: "Third step", status: "pending" },
    ]),
  ])

  assert.ok(plan, "a plan is found")
  assert.equal(plan.total, 3)
  assert.equal(plan.completed, 1)
  assert.deepEqual(plan.steps.map((step) => step.status), ["completed", "in_progress", "pending"])
})

test("the newest revision wins, because each call replaces the whole list", () => {
  // The agent rewrites the entire list on every call, so an earlier call is
  // history: reading it would show steps that have since been reworded or
  // dropped.
  const plan = buildTimelinePlan([
    todoCall("p1", 2, [{ content: "Old wording", status: "pending" }]),
    todoCall("p2", 5, [
      { content: "Old wording", status: "completed" },
      { content: "Newly discovered step", status: "in_progress" },
    ]),
  ])

  assert.ok(plan)
  assert.equal(plan.id, "p2", "anchored to the latest call")
  assert.equal(plan.total, 2)
  assert.equal(plan.completed, 1)
  assert.deepEqual(
    plan.steps.map((step) => step.content),
    ["Old wording", "Newly discovered step"],
  )
})

test("a plan that finishes and is rewritten still reports the latest state", () => {
  const plan = buildTimelinePlan([
    todoCall("p1", 1, [{ content: "Only step", status: "in_progress" }]),
    todoCall("p2", 9, [{ content: "Only step", status: "completed" }]),
  ])
  assert.ok(plan)
  assert.equal(plan.completed, plan.total, "all steps done")
})

test("no plan means no card, rather than an empty one", () => {
  assert.equal(buildTimelinePlan([]), null)
  assert.equal(
    buildTimelinePlan([
      item({ id: "u1", type: "message", role: "user" }),
      item({ id: "t1", content: { kind: "command", title: "ls" } }),
    ]),
    null,
  )
})

test("Codex's update_plan is read the same way", () => {
  const plan = buildTimelinePlan([
    item({
      id: "c1",
      orderSeq: 3,
      content: { kind: "tool_call", toolName: "update_plan", input: { plan: [
        { step: "Investigate the bug", status: "completed" },
        { step: "Write the fix", status: "pending" },
      ] } },
    }),
  ])
  assert.ok(plan, "the plan is recognised by tool name")
  assert.deepEqual(plan.steps.map((step) => step.content), ["Investigate the bug", "Write the fix"])
})

test("an unrecognised status is kept as not-started, never dropped", () => {
  // A runtime that adds a status must never make a step vanish from the plan.
  const plan = buildTimelinePlan([
    todoCall("p1", 1, [
      { content: "Known", status: "completed" },
      { content: "Brand new status", status: "blocked" },
    ]),
  ])
  assert.ok(plan)
  assert.equal(plan.total, 2, "both steps survive")
  assert.equal(plan.steps[1].status, "pending")
})

test("blank steps are skipped and a plan of only blanks is no plan", () => {
  const plan = buildTimelinePlan([
    todoCall("p1", 1, [{ content: "   ", status: "pending" }, { content: "Real", status: "pending" }]),
  ])
  assert.ok(plan)
  assert.equal(plan.total, 1)

  assert.equal(buildTimelinePlan([todoCall("p2", 1, [{ content: "  ", status: "pending" }])]), null)
})

test("plan items are reported so the fold can avoid showing them twice", () => {
  const items = [
    todoCall("p1", 1, [{ content: "A", status: "pending" }]),
    item({ id: "t2", content: { kind: "command" } }),
    todoCall("p3", 3, [{ content: "B", status: "completed" }]),
  ]
  const ids = planItemIds(items)
  assert.deepEqual([...ids].sort(), ["p1", "p3"])
  assert.equal(ids.has("t2"), false)
})
