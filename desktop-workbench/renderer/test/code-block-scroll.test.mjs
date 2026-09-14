import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"

const scrollAreaSource = readFileSync(
  new URL("../src/components/ui/scroll-area.tsx", import.meta.url),
  "utf8",
)
const markdownSource = readFileSync(
  new URL("../src/components/markdown-text.tsx", import.meta.url),
  "utf8",
)

// Regression coverage for tall fenced code blocks that clipped their content.
//
// Radix renders the viewport with `height: 100%`, which resolves against a
// parent that has no definite height and therefore falls back to `auto`. A
// `max-h-*` set only on the root bounded the root box but left the viewport at
// full content height, so nothing overflowed it: `scrollHeight` equalled
// `clientHeight`, no thumb was mounted, and the tail of the snippet was
// unreachable. The viewport must inherit the root cap for the block to scroll.

function viewportClassName() {
  const viewport = scrollAreaSource.slice(
    scrollAreaSource.indexOf("<ScrollAreaPrimitive.Viewport"),
    scrollAreaSource.indexOf("</ScrollAreaPrimitive.Viewport>"),
  )
  assert.notEqual(viewport, "", "viewport markup must exist")
  return viewport
}

test("scroll area viewport inherits the root max-height so capped areas scroll", () => {
  const viewport = viewportClassName()
  assert.match(
    viewport,
    /max-h-\[inherit\]/,
    "viewport needs max-h-[inherit] or a max-h-* root will clip instead of scroll",
  )
})

test("code block keeps a bounded height and an always-mounted scrollbar", () => {
  const block = markdownSource.slice(
    markdownSource.indexOf("function MarkdownCodeBlock"),
    markdownSource.indexOf("function stripLineSuffix"),
  )
  assert.notEqual(block, "", "MarkdownCodeBlock must exist")
  assert.match(block, /max-h-96/, "code blocks must stay height-bounded")
  // `type="always"` keeps the thumb mounted whenever the content overflows,
  // rather than only while the pointer hovers the block.
  assert.match(
    block,
    /<ScrollArea[^>]*type="always"/,
    'code blocks must use type="always" so tall snippets show a scrollbar',
  )
})

test("the shared scroll area renders a vertical scrollbar by default", () => {
  // MarkdownCodeBlock only adds the horizontal bar; vertical scrolling relies
  // on the default ScrollBar that ScrollArea mounts itself. If that default is
  // ever dropped, tall code blocks would silently lose their scrollbar again.
  const root = scrollAreaSource.slice(
    scrollAreaSource.indexOf("</ScrollAreaPrimitive.Viewport>"),
    scrollAreaSource.indexOf("</ScrollAreaPrimitive.Root>"),
  )
  assert.match(
    root,
    /<ScrollBar\s*\/>/,
    "ScrollArea must mount a default vertical ScrollBar",
  )
})
