import assert from "node:assert/strict"
import test from "node:test"

import {
  activeProcessBlockKey,
  activeProcessBlockKeys,
  hasOlderRequests,
  requestHistoryWindow,
  REQUEST_HISTORY_PAGE_SIZE,
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

test("a turn renders as request, one fold, and the final reply", () => {
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "do the thing")),
    single(reasoning("r1")),
    toolRun("tool-run:t1", [tool("t1"), tool("t2")]),
    single(reply("a1", "done")),
  ])

  assert.deepEqual(
    blocks.map((block) => block.kind),
    ["entry", "process", "entry"],
    "request and final reply stay visible with the process folded between them",
  )
  assert.equal(blocks[0].group.item.id, "u1")
  assert.equal(blocks[2].group.item.id, "a1")
  assert.deepEqual(
    blocks[1].items.map((it) => it.id),
    ["r1", "t1", "t2"],
    "reasoning and tool rows fold into the process block",
  )
})

test("intermediate replies fold away, leaving only the turn's final reply", () => {
  // A runtime narrates as it works: reply, tools, reply, tools, conclusion.
  // Only the conclusion is the answer, so everything before it folds.
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "investigate")),
    single(reply("a1", "let me look")),
    single(tool("t1")),
    single(reply("a2", "found something")),
    single(tool("t2")),
    single(reply("a3", "here is the answer")),
  ])

  assert.deepEqual(
    blocks.map((block) => block.kind),
    ["entry", "process", "entry"],
    "one fold per turn, not one per intermediate reply",
  )
  assert.equal(blocks[0].group.item.id, "u1", "the request stays at the top")
  assert.equal(blocks[2].group.item.id, "a3", "only the final reply stays visible")
  assert.deepEqual(
    blocks[1].items.map((it) => it.id),
    ["a1", "t1", "a2", "t2"],
    "intermediate replies fold in with the process, in order",
  )
})

test("while a turn runs, the latest reply stays visible and earlier ones fold", () => {
  // The newest reply is the runtime's current progress report, so it stays on
  // screen where the reader is already looking. Everything before it — earlier
  // commentary and the tools run so far — folds away.
  const first = buildTimelineRenderBlocks([
    single(user("u1", "long task")),
    single(reply("a1", "starting")),
    single(tool("t1")),
  ])
  assert.deepEqual(first.map((block) => block.kind), ["entry", "entry", "process"])
  assert.deepEqual(first[2].items.map((it) => it.id), ["t1"])

  // When the next update arrives, the previous one folds in behind it.
  const second = buildTimelineRenderBlocks([
    single(user("u1", "long task")),
    single(reply("a1", "starting")),
    single(tool("t1")),
    single(reply("a2", "still going")),
  ])
  assert.deepEqual(second.map((block) => block.kind), ["entry", "process", "entry"])
  assert.deepEqual(second[1].items.map((it) => it.id), ["a1", "t1"])
  assert.equal(second[2].group.item.id, "a2", "the newest update stays visible")
})

test("each turn keeps its own fold", () => {
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "first")),
    single(reply("a1", "working")),
    single(tool("t1")),
    single(reply("a2", "answer one")),
    single(user("u2", "second")),
    single(reply("a3", "working again")),
    single(tool("t2")),
    single(reply("a4", "answer two")),
  ])

  const kinds = blocks.map((block) => block.kind)
  assert.deepEqual(kinds, ["entry", "process", "entry", "entry", "process", "entry"])
  const processes = blocks.filter((block) => block.kind === "process")
  assert.deepEqual(processes[0].items.map((it) => it.id), ["a1", "t1"])
  assert.deepEqual(processes[1].items.map((it) => it.id), ["a3", "t2"])
  // Each turn's own conclusion stays out of its fold.
  assert.deepEqual(
    blocks.filter((b) => b.kind === "entry").map((b) => b.group.item.id),
    ["u1", "a2", "u2", "a4"],
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

test("a turn with no reply yet, followed by a new request, still separates", () => {
  // The first turn was abandoned without an answer; the fold must not swallow
  // the second request.
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "first")),
    single(tool("t1")),
    single(user("u2", "second")),
    single(reply("a2", "answer two")),
  ])

  assert.deepEqual(blocks.map((block) => block.kind), ["entry", "process", "entry", "entry"])
  assert.deepEqual(blocks[1].items.map((it) => it.id), ["t1"])
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

test("every process block of the running turn is live, not just the last", () => {
  // A turn narrates as reply, tools, reply, tools. Each intermediate reply
  // closes the block before it, so keeping only the final block open would hide
  // most of the work and leave no way to tell whether the run had stalled.
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "long task")),
    single(reply("a1", "looking")),
    single(tool("t1")),
    single(reply("a2", "still going")),
    single(tool("t2")),
  ])

  const live = activeProcessBlockKeys(blocks)
  const processes = blocks.filter((block) => block.kind === "process")
  assert.equal(processes.length, 2, "the turn folds into two process blocks")
  assert.deepEqual(
    live,
    processes.map((block) => block.key),
    "both blocks belong to the running turn, so both stay open",
  )
})

test("a finished turn stops being live once a newer request arrives", () => {
  const blocks = buildTimelineRenderBlocks([
    single(user("u1", "first")),
    single(reply("a1", "working")),
    single(tool("t1")),
    single(user("u2", "second")),
    single(tool("t2")),
  ])

  const live = activeProcessBlockKeys(blocks)
  const processes = blocks.filter((block) => block.kind === "process")
  assert.equal(processes.length, 2, "one fold per turn")
  assert.deepEqual(
    live,
    [processes[1].key],
    "only the newest turn is live; the earlier turn folds",
  )
})

test("before any request, the only block is live so the view is not empty", () => {
  // Reconnect noise or runtime chatter can precede the first request. It is the
  // entire transcript at that point, so it stays open; collapsing it would show
  // an empty screen.
  const blocks = buildTimelineRenderBlocks([single(reasoning("r1")), single(tool("t1"))])
  assert.equal(blocks.length, 1)
  assert.deepEqual(activeProcessBlockKeys(blocks), [blocks[0].key])
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

test("the request navigator opens on the newest requests, not the oldest", () => {
  // Reaching an earlier request must not require scrolling the transcript, so
  // the panel starts at the recent end and grows downward into the past.
  const entries = Array.from({ length: 25 }, (_, i) => ({
    id: `u${i + 1}`,
    blockKey: `b${i + 1}`,
    index: i + 1,
    text: `request ${i + 1}`,
    status: "done",
    createdAt: "2026-09-16T00:00:00Z",
  }))

  const firstPage = requestHistoryWindow(entries, REQUEST_HISTORY_PAGE_SIZE)
  assert.equal(firstPage.length, 10, "ten requests are shown by default")
  assert.equal(firstPage[0].id, "u25", "the newest request is first")
  assert.equal(firstPage[9].id, "u16", "the page reaches back ten turns")
  assert.ok(hasOlderRequests(entries, REQUEST_HISTORY_PAGE_SIZE), "15 remain")

  // Scrolling reveals the next page without losing the newest ones.
  const secondPage = requestHistoryWindow(entries, REQUEST_HISTORY_PAGE_SIZE * 2)
  assert.equal(secondPage.length, 20)
  assert.equal(secondPage[0].id, "u25", "the newest entry never moves")
  assert.equal(secondPage[19].id, "u6")

  // Past the end the window is the whole list, with no phantom entries.
  const all = requestHistoryWindow(entries, 100)
  assert.equal(all.length, 25)
  assert.equal(all[24].id, "u1", "the oldest request is last")
  assert.equal(hasOlderRequests(entries, 100), false)

  assert.deepEqual(requestHistoryWindow(entries, 0), [], "nothing shown before the window is sized")
})
