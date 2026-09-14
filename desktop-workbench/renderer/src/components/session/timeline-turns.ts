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

/**
 * Split groups into visible requests/replies and the folded process between
 * them. Anything that is neither a user request nor an assistant reply is
 * process detail and joins the surrounding process block.
 */
export function buildTimelineRenderBlocks(groups: TimelineGroup[]): TimelineRenderBlock[] {
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

  for (const group of groups) {
    const items = timelineGroupItemsOf(group)
    if (items.some(isUserRequestItem) || items.some(isAssistantReplyItem)) {
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
 * Process blocks that are still being produced. Only the last block can be
 * live: an earlier turn's process is finished by definition once a newer
 * request exists.
 */
export function activeProcessBlockKey(blocks: TimelineRenderBlock[]): string | null {
  const processes = blocks.filter((block): block is TimelineProcessBlock => block.kind === "process")
  const last = processes[processes.length - 1]
  return last ? last.key : null
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
