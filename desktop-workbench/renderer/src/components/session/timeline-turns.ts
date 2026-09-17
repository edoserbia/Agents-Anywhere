import type { TimelineGroup } from "@/components/session-detail"
import type { TimelineItem } from "@/features/dashboard/types"

/**
 * Render planning for the session timeline.
 *
 * A turn is one user request plus everything the runtime did to answer it:
 * reasoning, tool calls, file changes, sub-agent calls and any reconnect
 * noise. Previously every one of those pieces rendered as its own row, so a
 * single request could push dozens of rows of process detail between the
 * question and the answer.
 *
 * These helpers fold the process of a turn into a single collapsible block so
 * the conversation reads as request -> answer, with the detail available on
 * demand.
 */

export type TimelineProcessBlock = {
  kind: "process"
  key: string
  groups: TimelineGroup[]
  items: TimelineItem[]
}

export type TimelineEntryBlock = {
  kind: "entry"
  key: string
  group: TimelineGroup
}

export type TimelineRenderBlock = TimelineProcessBlock | TimelineEntryBlock

export function timelineGroupItemsOf(group: TimelineGroup): TimelineItem[] {
  return group.kind === "single" ? [group.item] : group.items
}

export function timelineGroupKeyOf(group: TimelineGroup): string {
  return group.kind === "single" ? group.item.id : group.key
}

export function isUserRequestItem(item: TimelineItem): boolean {
  return item.type === "message" && item.role === "user"
}

function isAssistantReplyItem(item: TimelineItem): boolean {
  return item.type === "message" && item.role !== "user"
}

/** The first assistant reply inside a group, if it has one. */
function firstAssistantReplyId(items: TimelineItem[]): string | null {
  const reply = items.find(isAssistantReplyItem)
  return reply ? reply.id : null
}

/**
 * Split groups into the visible request and answer of each turn, with
 * everything the runtime did in between folded into a single block.
 *
 * A turn is one user request and its outcome, but the runtime narrates as it
 * goes: it emits a reply, runs tools, emits another reply, and so on. Treating
 * every one of those replies as an answer split a single turn into many
 * process blocks and left the reader scrolling past intermediate commentary to
 * reach the conclusion.
 *
 * So a turn renders as exactly three things: the request, one folded block
 * holding all the process *and* every intermediate reply, and the turn's final
 * reply. The final reply is the only assistant message left outside the fold,
 * because it is the answer the reader came for.
 *
 * A turn's final reply is identified by looking ahead: the reply immediately
 * before the next user request (or the end of the timeline). While a turn is
 * still running there is no such reply yet, so nothing is promoted and the
 * fold grows until the runtime answers.
 */
export function buildTimelineRenderBlocks(groups: TimelineGroup[]): TimelineRenderBlock[] {
  // Which group holds each turn's final reply. Computed up front because the
  // answer is only knowable from what follows it.
  const finalReplyGroupKey = new Map<number, string>()
  let turnStart = 0
  const closeTurn = (endExclusive: number) => {
    for (let i = endExclusive - 1; i >= turnStart; i -= 1) {
      const group = groups[i]
      if (!group) continue
      if (firstAssistantReplyId(timelineGroupItemsOf(group))) {
        finalReplyGroupKey.set(i, timelineGroupKeyOf(group))
        return
      }
    }
  }
  for (let i = 0; i < groups.length; i += 1) {
    const group = groups[i]
    if (group && timelineGroupItemsOf(group).some(isUserRequestItem)) {
      if (i > turnStart) closeTurn(i)
      turnStart = i
    }
  }
  closeTurn(groups.length)

  const blocks: TimelineRenderBlock[] = []
  let pendingProcess: TimelineGroup[] = []

  const flushProcess = () => {
    if (pendingProcess.length === 0) return
    const items = pendingProcess.flatMap(timelineGroupItemsOf)
    const first = items[0]
    const firstGroup = pendingProcess[0]
    blocks.push({
      kind: "process",
      // Anchor the key to the first folded item so it stays stable while the
      // runtime appends more process rows to the same turn.
      key: `process:${first ? first.id : firstGroup ? timelineGroupKeyOf(firstGroup) : "unknown"}`,
      groups: pendingProcess,
      items,
    })
    pendingProcess = []
  }

  for (let i = 0; i < groups.length; i += 1) {
    const group = groups[i]
    if (!group) continue
    const items = timelineGroupItemsOf(group)
    // The request opens a turn; the turn's final reply closes it. Both stay
    // visible. Every other group — including intermediate replies — is folded.
    const visible = items.some(isUserRequestItem) || finalReplyGroupKey.get(i) === timelineGroupKeyOf(group)
    if (visible) {
      flushProcess()
      blocks.push({ kind: "entry", key: timelineGroupKeyOf(group), group })
      continue
    }
    pendingProcess.push(group)
  }
  flushProcess()
  return blocks
}

/**
 * Keys of the process blocks belonging to the turn currently being produced.
 *
 * The running turn's work is open, so its progress is visible as it happens.
 * Every block of that turn qualifies, not just the last one: a turn narrates as
 * reply, tools, reply, tools, and each intermediate reply closes the block
 * before it. Returning only the final block hid everything the runtime had
 * already done, so a reader saw "the agent is working" with no way to tell what
 * it was doing or whether it had stalled.
 *
 * Blocks from finished turns are excluded, because an earlier turn is complete
 * once a newer request exists.
 */
export function activeProcessBlockKeys(blocks: TimelineRenderBlock[]): string[] {
  const lastRequestIndex = blocks.reduce(
    (found, block, index) => (block.kind === "entry" && isUserRequestBlock(block) ? index : found),
    -1,
  )
  return blocks
    .slice(lastRequestIndex + 1)
    .filter((block): block is TimelineProcessBlock => block.kind === "process")
    .map((block) => block.key)
}

function isUserRequestBlock(block: TimelineEntryBlock): boolean {
  return timelineGroupItemsOf(block.group).some(isUserRequestItem)
}

/**
 * The single most recent live process block, or null.
 *
 * Retained for callers that need one anchor rather than the whole set.
 */
export function activeProcessBlockKey(blocks: TimelineRenderBlock[]): string | null {
  const keys = activeProcessBlockKeys(blocks)
  return keys.length > 0 ? (keys[keys.length - 1] ?? null) : null
}

export type TimelineRequestEntry = {
  /** Id of the user message item, used to scroll the timeline to it. */
  id: string
  /** Key of the render block that contains the request. */
  blockKey: string
  /** Position of the request in the session, oldest first. */
  index: number
  text: string
  status: TimelineItem["status"]
  createdAt: string
}

/** Collapse whitespace so a request renders as a single readable line. */
export function requestPreview(text: string, maxLength = 120): string {
  const collapsed = text.replace(/\s+/g, " ").trim()
  if (collapsed.length <= maxLength) return collapsed
  return `${collapsed.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`
}

/**
 * Build the navigable list of past requests. Requests are ordered oldest
 * first so the numbering matches how a reader counts them in the timeline.
 */
export function buildTimelineRequestEntries(
  blocks: TimelineRenderBlock[],
  readText: (item: TimelineItem) => string,
  maxLength = 120,
): TimelineRequestEntry[] {
  const entries: TimelineRequestEntry[] = []
  for (const block of blocks) {
    if (block.kind !== "entry") continue
    for (const item of timelineGroupItemsOf(block.group)) {
      if (!isUserRequestItem(item)) continue
      entries.push({
        id: item.id,
        blockKey: block.key,
        index: entries.length + 1,
        text: requestPreview(readText(item), maxLength),
        status: item.status,
        createdAt: item.createdAt,
      })
    }
  }
  return entries
}

/** How many requests the navigator shows before older ones are revealed. */
export const REQUEST_HISTORY_PAGE_SIZE = 10

/**
 * The slice of requests the navigator currently shows, newest first.
 *
 * Requests are listed newest first so the reader starts at the most recent
 * turn — the one they are most likely to want — instead of having to scroll the
 * transcript to find it. Scrolling the panel reveals older requests, which is
 * why the window grows downward from the newest entry.
 *
 * `entries` arrives in reading order (oldest first) because every other caller
 * numbers them that way, so the reversal happens here rather than at each call
 * site.
 */
export function requestHistoryWindow(
  entries: readonly TimelineRequestEntry[],
  visibleCount: number,
): TimelineRequestEntry[] {
  if (visibleCount <= 0) return []
  return [...entries].reverse().slice(0, visibleCount)
}

/** Whether more requests exist beyond the ones currently shown. */
export function hasOlderRequests(
  entries: readonly TimelineRequestEntry[],
  visibleCount: number,
): boolean {
  return entries.length > visibleCount
}
