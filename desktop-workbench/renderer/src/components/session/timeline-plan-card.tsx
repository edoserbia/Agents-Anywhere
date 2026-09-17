"use client"

import { Check, ChevronDown, ListChecks, Loader2 } from "lucide-react"
import { useTranslations } from "next-intl"
import * as React from "react"

import { formatTimelineTimestamp } from "@/components/session/timeline-timestamp"
import type { PlanStep, TimelinePlan } from "@/components/session/timeline-plan"
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
 * The agent's plan for the current work.
 *
 * Agents keep a checklist and rewrite it as steps finish, which is the only
 * place the intended shape of a long run is written down. Rendering it as an
 * ordinary tool call buried that: the reader could see that tools ran but not
 * what remained.
 *
 * While the run is in progress the plan is open, because its whole purpose is
 * to answer "what is left". Once the run ends it collapses to a one-line
 * summary, since a finished plan is history rather than status.
 */
export function TimelinePlanCard({
  plan,
  running,
  className,
}: {
  plan: TimelinePlan
  running: boolean
  className?: string
}) {
  const tSession = useTranslations("dashboard.session")
  const [open, setOpen] = React.useState(running)

  // Follow the run: open when work starts, fold away when it finishes. A manual
  // choice is kept, so a reader who opened a finished plan is not overruled.
  const touchedRef = React.useRef(false)
  React.useEffect(() => {
    if (touchedRef.current) return
    setOpen(running)
  }, [running])

  const allDone = plan.total > 0 && plan.completed === plan.total
  const startedAt = formatTimelineTimestamp(plan.createdAt)

  return (
    <Collapsible
      open={open}
      onOpenChange={(next) => {
        touchedRef.current = true
        setOpen(next)
      }}
    >
      <div
        data-slot="timeline-plan"
        className={cn(
          "rounded-lg border border-border bg-card/40 text-card-foreground",
          className,
        )}
      >
        <CollapsibleTrigger asChild>
          <button
            type="button"
            data-timeline-plan-toggle
            className="flex w-full min-w-0 items-center gap-2 rounded-lg px-3 py-2 text-left transition-colors hover:bg-accent/40"
          >
            <ChevronDown
              className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", !open && "-rotate-90")}
            />
            <ListChecks className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="flex min-w-0 flex-1 flex-col gap-0.5">
              {startedAt ? (
                <span data-timeline-timestamp className="text-[10px] tabular-nums text-muted-foreground/70">
                  {startedAt}
                </span>
              ) : null}
              <span className="truncate text-xs font-medium">
                {allDone
                  ? tSession("plan.allDone", { count: plan.total })
                  : tSession("plan.progress", { completed: plan.completed, total: plan.total })}
              </span>
            </span>
            {running && !allDone ? (
              <Loader2 className="size-3.5 shrink-0 animate-spin text-sky-500" />
            ) : null}
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <ul className="flex flex-col border-t border-border px-3 py-2">
            {plan.steps.map((step, index) => (
              <PlanStepRow key={`${index}:${step.content}`} step={step} />
            ))}
          </ul>
        </CollapsibleContent>
      </div>
    </Collapsible>
  )
}
