import assert from "node:assert/strict"
import test from "node:test"

import {
  activeProcessBlockKey,
  buildTimelineRenderBlocks,
  buildTimelineRequestEntries,
  requestPreview,
} from "../src/components/session/timeline-turns.ts"

function item(overrides = {}) {
  return {
    id: "item-1",
    sessionId: "session-1",
    type: "message",
    status: "done",
    role: "assistant",
    content: { text: "hello" },
    source: {},
    orderSeq: 1,
    revision: 1,
    contentHash: "hash",
    updatedSeq: 1,
    createdAt: "2026-08-28T00:00:00Z",
    updatedAt: "2026-08-28T00:00:00Z",
    completedAt: null,
    ...overrides,
  }
}

const user = (id, text) => item({ id, type: "message", role: "user", content: { text } })
const reply = (id, text) => item({ id, role: "assistant", content: { text } })
const tool = (id) => item({ id, type: "tool", role: null, content: { kind: "command" } })
const reasoning = (id) => item({ id, type: "system", role: null, content: { kind: "reasoning" } })

const single = (it) => ({ kind: "single", item: it })
const toolRun = (key, items) => ({ kind: "tool-run", key, items })

const readText = (it) => String(it.content?.text ?? "")

test("folds a turn's process into one block between the request and the reply", () => {
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "do the thing")),
    single(reasoning("r1")),
    toolRun("tool-run:t1", [tool("t1"), tool("t2")]),
    single(reply("a1", "done")),
  ])

  assert.deepEqual(
    blocks.map((block) => block.kind),
    ["entry", "process", "entry"],
    "request and reply stay visible with the process folded between them",
  )
  assert.equal(blocks[0].group.item.id, "u1")
  assert.equal(blocks[2].group.item.id, "a1")
  assert.deepEqual(
    blocks[1].items.map((it) => it.id),
    ["r1", "t1", "t2"],
    "reasoning and tool rows fold into the process block",
  )
})

test("keeps process blocks separate per turn so an old turn cannot absorb a new one", () => {
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "first")),
    single(reasoning("r1")),
    single(reply("a1", "answer one")),
    single(user("u2", "second")),
    single(tool("t9")),
    single(reply("a2", "answer two")),
  ])

  assert.deepEqual(
    blocks.map((block) => block.kind),
    ["entry", "process", "entry", "entry", "process", "entry"],
  )
  const processes = blocks.filter((block) => block.kind === "process")
  assert.equal(processes.length, 2)
  assert.deepEqual(processes[0].items.map((it) => it.id), ["r1"])
  assert.deepEqual(processes[1].items.map((it) => it.id), ["t9"])
})

test("folds process that arrives before any request, without losing it", () => {
  const blocks = buildTimelineRenderBlocks([
    single(reasoning("r0")),
    single(user("u1", "hi")),
    single(reply("a1", "hello")),
  ])

  assert.deepEqual(blocks.map((block) => block.kind), ["process", "entry", "entry"])
  assert.deepEqual(blocks[0].items.map((it) => it.id), ["r0"])
})

test("only the last process block is considered live", () => {
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "a")),
    single(reasoning("r1")),
    single(reply("a1", "b")),
    single(user("u2", "c")),
    single(reasoning("r2")),
  ])

  const live = activeProcessBlockKey(blocks)
  const processes = blocks.filter((block) => block.kind === "process")
  assert.equal(live, processes[processes.length - 1].key)
  assert.notEqual(live, processes[0].key)
})

test("no process block means nothing is live", () => {
  const blocks = buildTimelineRenderBlocks([single(user("u1", "a")), single(reply("a1", "b"))])
  assert.equal(activeProcessBlockKey(blocks), null)
})

test("process block key stays stable as more process rows arrive", () => {
  const before = buildTimelineRenderBlocks([single(user("u1", "a")), single(reasoning("r1"))])
  const after = buildTimelineRenderBlocks([
    single(user("u1", "a")),
    single(reasoning("r1")),
    single(tool("t1")),
    single(tool("t2")),
  ])
  const keyOf = (blocks) => blocks.find((block) => block.kind === "process").key
  assert.equal(keyOf(before), keyOf(after), "expansion state must survive new process rows")
})

test("lists past requests oldest-first with stable numbering", () => {
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "first request")),
    single(reasoning("r1")),
    single(reply("a1", "one")),
    single(user("u2", "second request")),
    single(reply("a2", "two")),
  ])

  const entries = buildTimelineRequestEntries(blocks, readText)
  assert.deepEqual(entries.map((entry) => entry.id), ["u1", "u2"])
  assert.deepEqual(entries.map((entry) => entry.index), [1, 2])
  assert.deepEqual(entries.map((entry) => entry.text), ["first request", "second request"])
})

test("assistant replies are never listed as requests", () => {
  const blocks = buildTimelineRenderBlocks([
    single(reply("a0", "unprompted")),
    single(user("u1", "real request")),
    single(reply("a1", "reply")),
  ])
  const entries = buildTimelineRequestEntries(blocks, readText)
  assert.deepEqual(entries.map((entry) => entry.id), ["u1"])
})

test("request previews collapse whitespace and truncate", () => {
  assert.equal(requestPreview("  hello   world \n next "), "hello world next")
  const long = "x".repeat(300)
  const preview = requestPreview(long, 40)
  assert.equal(preview.length, 40)
  assert.ok(preview.endsWith("…"))
})

test("empty request text yields an empty preview rather than throwing", () => {
  assert.equal(requestPreview(""), "")
  assert.equal(requestPreview("   \n  "), "")
})
