"use client"

import { Check, ChevronDown, ListChecks, Loader2 } from "lucide-react"
import { useTranslations } from "next-intl"
import * as React from "react"

import type { PlanStep, TimelinePlan } from "@/components/session/timeline-plan"
import { formatTimelineTimestamp } from "@/components/session/timeline-timestamp"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

/** One step: a checkbox-like mark plus the agent's own wording. */
function PlanStepRow({ step }: { step: PlanStep }) {
  return (
    <li className="flex min-w-0 items-start gap-2 py-0.5">
      <span className="mt-[3px] flex size-3.5 shrink-0 items-center justify-center">
        {step.status === "completed" ? (
          <Check className="size-3.5 text-emerald-500" />
        ) : step.status === "in_progress" ? (
          <Loader2 className="size-3 animate-spin text-sky-500" />
        ) : (
          // An empty ring reads as "not started" without implying failure, which
          // a cross or a muted check would.
          <span className="size-3 rounded-full border border-muted-foreground/40" aria-hidden />
        )}
      </span>
      <span
        className={cn(
          "min-w-0 flex-1 break-words text-xs leading-snug",
          step.status === "completed"
            ? "text-muted-foreground line-through decoration-muted-foreground/40"
            : step.status === "in_progress"
              ? "text-foreground"
              : "text-muted-foreground",
        )}
      >
        {step.content}
      </span>
    </li>
  )
}

/**
 * The current turn's plan, pinned above the composer.
 *
 * It sits with the composer rather than in the transcript because the plan is an
 * input to reading the run — what is happening and what is left — and the
 * transcript scrolls it away exactly when a long run makes it most useful.
 *
 * Collapsed by default. The one-line summary already carries the progress count,
 * which is what a reader glancing at a running task needs; the steps are one
 * click away for anyone who wants the detail. It follows the run rather than
 * fighting the reader: a manual open or close is kept until the plan itself is
 * replaced.
 *
 * Rendered only when the current turn has a plan. A turn without one shows
 * nothing at all, so the bar never occupies space to say it has nothing to say.
 */
export function TimelinePlanBar({
  plan,
  running,
  className,
}: {
  plan: TimelinePlan
  running: boolean
  className?: string
}) {
  const tSession = useTranslations("dashboard.session")
  const [open, setOpen] = React.useState(false)

  // A new plan is a new turn's checklist, so its own state starts collapsed
  // again; the reader's choice applies to the plan they were looking at.
  const [seenPlanId, setSeenPlanId] = React.useState(plan.id)
  if (seenPlanId !== plan.id) {
    setSeenPlanId(plan.id)
    setOpen(false)
  }

  const allDone = plan.total > 0 && plan.completed === plan.total
  const startedAt = formatTimelineTimestamp(plan.createdAt)

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <div
        data-slot="timeline-plan"
        data-plan-id={plan.id}
        className={cn(
          "overflow-hidden rounded-lg border border-border bg-card/60 text-card-foreground shadow-sm backdrop-blur",
          className,
        )}
      >
        <CollapsibleTrigger asChild>
          <button
            type="button"
            data-timeline-plan-toggle
            aria-expanded={open}
            className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-accent/40"
          >
            <ChevronDown
              className={cn(
                "size-3.5 shrink-0 text-muted-foreground transition-transform",
                !open && "-rotate-90",
              )}
            />
            <ListChecks className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="flex min-w-0 flex-1 items-baseline gap-2">
              <span className="truncate text-xs font-medium">
                {allDone
                  ? tSession("plan.allDone", { count: plan.total })
                  : tSession("plan.progress", { completed: plan.completed, total: plan.total })}
              </span>
              {startedAt ? (
                <span
                  data-timeline-timestamp
                  className="shrink-0 text-[10px] tabular-nums text-muted-foreground/70"
                >
                  {startedAt}
                </span>
              ) : null}
            </span>
            {running && !allDone ? (
              <Loader2 className="size-3.5 shrink-0 animate-spin text-sky-500" />
            ) : null}
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <ul className="flex max-h-56 flex-col overflow-y-auto border-t border-border px-3 py-2">
            {plan.steps.map((step, index) => (
              <PlanStepRow key={`${index}:${step.content}`} step={step} />
            ))}
          </ul>
        </CollapsibleContent>
      </div>
    </Collapsible>
  )
}
