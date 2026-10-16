import assert from "node:assert/strict"
import test from "node:test"
import { shouldLoadRequestHistory, timelinePageMadeProgress } from "../src/components/session/request-history-pagination.ts"

const options = { open: true, entryCount: 2, visibleCount: 10, hasMore: true, loading: false, failed: false }

test("opening history fills ten requests even across pages containing only tools", () => {
  assert.equal(shouldLoadRequestHistory(options), true)
  assert.equal(shouldLoadRequestHistory({ ...options, entryCount: 0 }), true)
  assert.equal(shouldLoadRequestHistory({ ...options, entryCount: 9 }), true)
  assert.equal(shouldLoadRequestHistory({ ...options, entryCount: 10 }), false)
  assert.equal(shouldLoadRequestHistory({ ...options, entryCount: 10, visibleCount: 20 }), true)
  assert.equal(shouldLoadRequestHistory({ ...options, entryCount: 20, visibleCount: 20 }), false)
})

test("closing, exhaustion, in-flight loading and errors stop automatic paging", () => {
  for (const override of [{ open: false }, { hasMore: false }, { loading: true }, { failed: true }]) {
    assert.equal(shouldLoadRequestHistory({ ...options, ...override }), false)
  }
})

test("empty or overlapping history pages cannot spin forever", () => {
  assert.equal(timelinePageMadeProgress([], 100), false)
  assert.equal(timelinePageMadeProgress([{ orderSeq: 100 }, { orderSeq: 101 }], 100), false)
  assert.equal(timelinePageMadeProgress([{ orderSeq: 99 }, { orderSeq: 100 }], 100), true)
})
