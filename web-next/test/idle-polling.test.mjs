import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"

for (const [name, base] of [
  ["Web", "../src/components/"],
  ["Desktop", "../../desktop-workbench/renderer/src/components/"],
]) {
  const detail = readFileSync(new URL(`${base}session-detail.tsx`, import.meta.url), "utf8")
  const composer = readFileSync(new URL(`${base}task-composer.tsx`, import.meta.url), "utf8")

  test(`${name} queue polling is slow, foreground-only, and cleans up resume listeners`, () => {
    const start = detail.indexOf("let cancelled = false", detail.indexOf("const refreshQueue"))
    const end = detail.indexOf("const handleUpdateQueued", start)
    const block = detail.slice(start, end)
    assert.ok(start >= 0 && end > start)
    assert.match(block, /document\.visibilityState !== "visible"/)
    assert.match(block, /addEventListener\("visibilitychange", onVisible\)/)
    assert.match(block, /addEventListener\("focus", onVisible\)/)
    assert.match(block, /removeEventListener\("visibilitychange", onVisible\)/)
    assert.match(block, /removeEventListener\("focus", onVisible\)/)
    assert.match(block, /clearInterval\(timer\)/)
    assert.match(block, /setInterval\(tick, (QUEUE_POLL_INTERVAL_MS|8_000)\)/)
    if (block.includes("QUEUE_POLL_INTERVAL_MS")) {
      assert.match(detail, /const QUEUE_POLL_INTERVAL_MS = 8_000/)
    }
  })

  test(`${name} permission catalog uses a visible-only 30 second fallback`, () => {
    assert.match(composer, /setInterval\(\(\) => \{\s*if \(document\.visibilityState === "visible"\) void refresh\(\)\s*\}, 30_000\)/)
    assert.doesNotMatch(composer, /setInterval\(\(\) => \{ void refresh\(\) \}, 5_000\)/)
  })
}

test("Android foreground queue refreshes immediately and polls serially every eight seconds", () => {
  const source = readFileSync(new URL("../../android/app/src/main/java/com/agentsanywhere/app/ui/screens/sessiondetail/SessionDetailScreen.kt", import.meta.url), "utf8")
  const start = source.indexOf("val activeSessionId = sessionId")
  const end = source.indexOf("LaunchedEffect(", start)
  const block = source.slice(start, end)
  assert.ok(start >= 0 && end > start)
  assert.match(block, /!appVisible\) break/)
  assert.match(block, /delay\(8_000\)/)
  assert.doesNotMatch(block, /delay\(2_000\)/)
  assert.ok(block.indexOf("controller.loadQueue") < block.indexOf("delay(8_000)"))
})
