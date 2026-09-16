/**
 * Timestamp formatting for timeline entries.
 *
 * A transcript can span hours, and without a visible time a reader cannot tell
 * when a reply or an action began. These cases pin the exact rendered shape,
 * because the value is read as data: two timestamps must be comparable at a
 * glance regardless of the reader's locale.
 */

import assert from "node:assert/strict"
import test from "node:test"

import {
  formatTimelineTimestamp,
  localDateTime,
} from "../src/components/session/timeline-timestamp.ts"

test("formats an instant as a bracketed local date and time", () => {
  const date = new Date(2026, 8, 16, 14, 5, 9) // 2026-09-16 14:05:09 local
  const formatted = localDateTime(date)
  assert.equal(formatted, "2026-09-16 14:05:09")
})

test("pads every field to a fixed width", () => {
  const date = new Date(2026, 0, 2, 3, 4, 5) // 2026-01-02 03:04:05 local
  assert.equal(localDateTime(date), "2026-01-02 03:04:05")
})

test("wraps the timestamp in square brackets", () => {
  const iso = new Date(2026, 8, 16, 14, 5, 9).toISOString()
  const formatted = formatTimelineTimestamp(iso)
  assert.ok(formatted, "a valid instant must format")
  assert.match(formatted, /^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]$/)
})

test("renders nothing for a missing timestamp", () => {
  assert.equal(formatTimelineTimestamp(null), null)
  assert.equal(formatTimelineTimestamp(undefined), null)
  assert.equal(formatTimelineTimestamp(""), null)
})

test("renders nothing rather than an invalid date", () => {
  // A placeholder must not become the literal text "Invalid Date".
  assert.equal(formatTimelineTimestamp("not-a-time"), null)
  assert.equal(formatTimelineTimestamp("2026-13-45T99:99:99Z"), null)
})

test("uses the same format for every timestamp so they line up", () => {
  const first = formatTimelineTimestamp(new Date(2026, 8, 16, 9, 0, 0).toISOString())
  const second = formatTimelineTimestamp(new Date(2026, 8, 16, 17, 30, 0).toISOString())
  assert.ok(first && second)
  assert.equal(first.length, second.length)
})
