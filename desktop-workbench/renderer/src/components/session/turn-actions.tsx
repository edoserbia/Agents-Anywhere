"use client"

import * as React from "react"
import { Check, Copy, Loader2, SquareArrowOutUpRight, Trash2 } from "lucide-react"
import { useTranslations } from "next-intl"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { dashboardApi } from "@/features/dashboard/api"
import type { SessionShareScope } from "@/features/dashboard/types"
import { cn } from "@/lib/utils"
import { copyText } from "@/lib/clipboard"

export type TurnAction = {
  copyText: string
  itemIds: string[]
  /** The request that opened this turn, when the reader may delete it. */
  requestItemId?: string
}

export function TurnActions({
  token,
  sessionId,
  action,
  onDeleted,
}: {
  token: string
  sessionId: string
  action: TurnAction
  /** Called after the server confirms, so the transcript can drop the item. */
  onDeleted?: (itemId: string) => void
}) {
  const t = useTranslations("dashboard.session")
  const [copied, setCopied] = React.useState(false)
  const [dialogOpen, setDialogOpen] = React.useState(false)
  const [scope, setScope] = React.useState<SessionShareScope>("message")
  const [sharing, setSharing] = React.useState(false)
  const [confirmDelete, setConfirmDelete] = React.useState(false)
  const [deleting, setDeleting] = React.useState(false)

  const copyReply = React.useCallback(async () => {
    if (!action.copyText) return
    setCopied(false)
    try {
      await copyText(action.copyText)
      setCopied(true)
      toast.success(t("replyCopied"))
      window.setTimeout(() => setCopied(false), 1200)
    } catch {
      toast.error(t("copyReplyFailed"))
    }
  }, [action.copyText, t])

  const deleteRequest = React.useCallback(async () => {
    if (!action.requestItemId || deleting) return
    setDeleting(true)
    try {
      await dashboardApi.deleteTimelineItem(token, sessionId, action.requestItemId)
      toast.success(t("messageDeleted"))
      setConfirmDelete(false)
      onDeleted?.(action.requestItemId)
    } catch {
      toast.error(t("deleteMessageFailed"))
    } finally {
      setDeleting(false)
    }
  }, [action.requestItemId, deleting, onDeleted, sessionId, t, token])

  const createShare = React.useCallback(async () => {
    if (sharing) return
    setSharing(true)
    try {
      const result = await dashboardApi.createSessionShare(token, sessionId, {
        scope,
        itemIds: scope === "message" ? action.itemIds : [],
      })
      if (typeof navigator.share === "function") {
        try {
          await navigator.share({ url: result.shareUrl })
          setDialogOpen(false)
          return
        } catch (error) {
          if (error instanceof DOMException && error.name === "AbortError") return
        }
      }
      await copyText(result.shareUrl)
      toast.success(t("shareLinkCopied"))
      setDialogOpen(false)
    } catch {
      toast.error(t("shareFailed"))
    } finally {
      setSharing(false)
    }
  }, [action.itemIds, scope, sessionId, sharing, t, token])

  return (
    <>
      <div className="mt-1">
        <div className="flex items-center gap-0.5">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={t("copyReply")}
            title={t("copyReply")}
            disabled={!action.copyText}
            onClick={copyReply}
            className="text-muted-foreground hover:text-foreground"
          >
            {copied ? <Check /> : <Copy />}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={t("share")}
            title={t("share")}
            onClick={() => setDialogOpen(true)}
            className="text-muted-foreground hover:text-foreground"
          >
            <SquareArrowOutUpRight />
          </Button>
          {action.requestItemId ? (
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={t("deleteMessage")}
              title={t("deleteMessage")}
              onClick={() => setConfirmDelete(true)}
              className="text-muted-foreground hover:text-destructive"
            >
              <Trash2 />
            </Button>
          ) : null}
        </div>
      </div>

      <AlertDialog open={confirmDelete} onOpenChange={(open) => { if (!deleting) setConfirmDelete(open) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteMessageTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("deleteMessageDescription")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>{t("cancel")}</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleting}
              onClick={(event) => { event.preventDefault(); void deleteRequest() }}
            >
              {deleting ? <Loader2 className="animate-spin" data-icon="inline-start" /> : <Trash2 data-icon="inline-start" />}
              {t("deleteMessage")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog open={dialogOpen} onOpenChange={(open) => { if (!sharing) setDialogOpen(open) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("shareDialogTitle")}</DialogTitle>
            <DialogDescription>{t("shareDialogDescription")}</DialogDescription>
          </DialogHeader>
          <div className="grid gap-2">
            <ShareScopeOption
              selected={scope === "message"}
              title={t("shareCurrentReply")}
              description={t("shareCurrentReplyDescription")}
              onClick={() => setScope("message")}
            />
            <ShareScopeOption
              selected={scope === "session"}
              title={t("shareEntireSession")}
              description={t("shareEntireSessionDescription")}
              onClick={() => setScope("session")}
            />
          </div>
          <DialogFooter>
            <Button type="button" onClick={createShare} disabled={sharing}>
              {sharing ? <Loader2 className="animate-spin" data-icon="inline-start" /> : <SquareArrowOutUpRight data-icon="inline-start" />}
              {t("createShareLink")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function ShareScopeOption({
  selected,
  title,
  description,
  onClick,
}: {
  selected: boolean
  title: string
  description: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        "rounded-lg border px-3 py-3 text-left transition-colors",
        selected ? "border-foreground/35 bg-accent" : "border-border hover:bg-accent/50",
      )}
    >
      <span className="block text-sm font-medium">{title}</span>
      <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">{description}</span>
    </button>
  )
}
