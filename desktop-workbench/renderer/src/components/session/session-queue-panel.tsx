"use client"

import * as React from "react"
import { useTranslations } from "next-intl"
import { Pencil, Trash2, Clock } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import type { QueuedMessage } from "@/features/dashboard"

/**
 * Messages waiting for the session's current turn to finish.
 *
 * Shown directly above the composer so a message accepted during a run is
 * visibly held rather than looking lost. A pending row can be edited or
 * removed; an item the runtime already took cannot, so the controls are hidden
 * once it leaves the queue.
 */
export function SessionQueuePanel({
  items,
  onUpdate,
  onDelete,
  className,
}: {
  items: QueuedMessage[]
  onUpdate: (item: QueuedMessage, content: string) => Promise<void> | void
  onDelete: (item: QueuedMessage) => Promise<void> | void
  className?: string
}) {
  const t = useTranslations("dashboard.session")
  const [editingId, setEditingId] = React.useState<string | null>(null)
  const [draft, setDraft] = React.useState("")

  if (items.length === 0) return null

  const beginEdit = (item: QueuedMessage) => {
    setEditingId(item.id)
    setDraft(item.content)
  }

  const commit = async (item: QueuedMessage) => {
    setEditingId(null)
    await onUpdate(item, draft)
  }

  return (
    <div
      className={cn(
        "rounded-xl border border-border bg-muted/40 px-3 py-2",
        className,
      )}
      data-slot="session-queue-panel"
    >
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold text-muted-foreground">
        <Clock className="size-3" aria-hidden />
        <span>{t("queueTitle", { count: items.length })}</span>
      </div>
      <ul className="space-y-1.5">
        {items.map((item) => {
          const isPending = item.status === "queued"
          const isEditing = editingId === item.id
          return (
            <li
              key={item.id}
              className="flex items-start gap-2"
              data-queue-item-id={item.id}
              data-queue-item-status={item.status}
            >
              {isEditing ? (
                <div className="flex w-full items-center gap-2">
                  <Input
                    value={draft}
                    autoFocus
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !event.shiftKey) {
                        event.preventDefault()
                        void commit(item)
                      }
                      if (event.key === "Escape") setEditingId(null)
                    }}
                    className="h-8 text-xs"
                  />
                  <Button
                    type="button"
                    size="sm"
                    className="h-8 px-2 text-xs"
                    disabled={draft.trim().length === 0}
                    onClick={() => void commit(item)}
                  >
                    {t("queueSave")}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className="h-8 px-2 text-xs"
                    onClick={() => setEditingId(null)}
                  >
                    {t("queueCancel")}
                  </Button>
                </div>
              ) : (
                <>
                  <span
                    className={cn(
                      "min-w-0 flex-1 whitespace-pre-wrap break-words text-xs leading-5",
                      item.status === "failed"
                        ? "text-destructive"
                        : "text-foreground/90",
                    )}
                  >
                    {item.content.trim() ||
                      item.errorMessage ||
                      t("queueItemFailed")}
                  </span>
                  {isPending ? (
                    <Button
                      type="button"
                      size="icon"
                      variant="ghost"
                      className="size-6 shrink-0"
                      aria-label={t("queueEdit")}
                      title={t("queueEdit")}
                      onClick={() => beginEdit(item)}
                    >
                      <Pencil className="size-3.5" aria-hidden />
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    className="size-6 shrink-0"
                    aria-label={t("queueDelete")}
                    title={t("queueDelete")}
                    onClick={() => void onDelete(item)}
                  >
                    <Trash2 className="size-3.5" aria-hidden />
                  </Button>
                </>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
