"use client"

import { History, MessageSquare } from "lucide-react"
import * as React from "react"
import { useTranslations } from "next-intl"

import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"
import {
  hasOlderRequests,
  requestHistoryWindow,
  REQUEST_HISTORY_PAGE_SIZE,
  type TimelineRequestEntry,
} from "@/components/session/timeline-turns"

/**
 * Navigator for the user requests in one session.
 *
 * Long agent sessions accumulate many turns, and reaching an earlier request
 * previously meant scrolling through every folded process block in between.
 * This panel lists the requests and scrolls the timeline straight to the one
 * that is picked.
 */
export function TimelineRequestHistory({
  open,
  onOpenChange,
  entries,
  activeId,
  onSelect,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  entries: TimelineRequestEntry[]
  activeId: string | null
  onSelect: (entry: TimelineRequestEntry) => void
}) {
  const tSession = useTranslations("dashboard.session")
  // Requests are listed newest first so the reader opens on the most recent
  // turn instead of scrolling the transcript to find it. Older ones are
  // revealed by scrolling this panel, never by scrolling the conversation.
  const [visibleCount, setVisibleCount] = React.useState(REQUEST_HISTORY_PAGE_SIZE)

  // Reset the window when the panel is reopened or the session's requests
  // change identity, so a stale count cannot hide the newest entries.
  React.useEffect(() => {
    if (!open) setVisibleCount(REQUEST_HISTORY_PAGE_SIZE)
  }, [open])
  const firstEntryId = entries[0]?.id ?? null
  React.useEffect(() => {
    setVisibleCount(REQUEST_HISTORY_PAGE_SIZE)
  }, [firstEntryId])

  const visible = requestHistoryWindow(entries, visibleCount)
  const older = hasOlderRequests(entries, visibleCount)

  // Reveal the next page when the reader scrolls to the older end (the bottom,
  // since the list runs newest first).
  const handleScroll = React.useCallback((event: React.UIEvent<HTMLDivElement>) => {
    const node = event.currentTarget
    if (node.scrollTop + node.clientHeight < node.scrollHeight - 24) return
    setVisibleCount((current) => (current < entries.length ? current + REQUEST_HISTORY_PAGE_SIZE : current))
  }, [entries.length])

  if (!open) return null

  return (
    <aside
      data-slot="timeline-request-history"
      aria-label={tSession("requestHistory.title")}
      className="flex h-full w-64 min-w-0 shrink-0 flex-col overflow-hidden border-l border-border bg-background"
    >
      <div className="flex h-11 shrink-0 items-center justify-between gap-2 border-b border-border pl-3 pr-2">
        <span className="min-w-0 truncate text-xs font-medium text-muted-foreground">
          {tSession("requestHistory.title")}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={tSession("requestHistory.close")}
          onClick={() => onOpenChange(false)}
        >
          <History className="size-4" />
        </Button>
      </div>
      {entries.length === 0 ? (
        <p className="px-3 py-4 text-xs text-muted-foreground">{tSession("requestHistory.empty")}</p>
      ) : (
        <ScrollArea className="min-h-0 flex-1" viewportProps={{ onScroll: handleScroll }}>
          <ul className="flex flex-col gap-0.5 p-1.5">
            {visible.map((entry) => (
              <li key={entry.id}>
                <button
                  type="button"
                  data-request-id={entry.id}
                  aria-current={entry.id === activeId ? "true" : undefined}
                  onClick={() => onSelect(entry)}
                  className={cn(
                    "flex w-full min-w-0 items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors",
                    entry.id === activeId
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                  )}
                >
                  <span className="mt-px w-5 shrink-0 text-right tabular-nums opacity-60">
                    {entry.index}
                  </span>
                  <MessageSquare className="mt-0.5 size-3 shrink-0 opacity-50" />
                  <span className="min-w-0 flex-1 break-words leading-snug">
                    {entry.text || tSession("requestHistory.untitled")}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {older ? (
            // A count rather than a bare hint: it says how much is left, which
            // is what makes scrolling this panel worth doing instead of the
            // transcript.
            <p className="px-3 pb-2 pt-1 text-center text-[10px] text-muted-foreground/70">
              {tSession("requestHistory.older", { count: entries.length - visible.length })}
            </p>
          ) : null}
        </ScrollArea>
      )}
    </aside>
  )
}
