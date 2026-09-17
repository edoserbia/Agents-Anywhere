// Relative imports keep this module loadable by the plain-Node test runner,
// which has no knowledge of the `@/` path alias. The `.ts` extension is
// required because that runner resolves ESM specifiers literally.
import type { TimelineItem } from "../../features/dashboard/types"
import { isUserRequestItem } from "./timeline-turns.ts"

/** One step of an agent's plan. */
export type PlanStep = {
  /** What the step is; the agent writes this text itself. */
  content: string
  status: "pending" | "in_progress" | "completed"
}

/** The plan an agent is following, as of its most recent revision. */
export type TimelinePlan = {
  /** The tool call that produced this revision, used as a stable list key. */
  id: string
  steps: PlanStep[]
  /** Id of the item this plan should be rendered at. */
  anchorId: string
  orderSeq: number
  createdAt: string
  completed: number
  total: number
}

/**
 * Tool names whose calls carry a plan.
 *
 * DSH rewrites the whole list on every call (`todo_write`), and Codex uses
 * `update_plan`; both send the same shape, so one reader covers them. Matching
 * on the name rather than on a positional argument keeps this working when a
 * runtime adds or reorders other fields.
 */
const PLAN_TOOL_NAMES = new Set(["todo_write", "todowrite", "update_plan", "updateplan"])

/** Item types a plan can hide inside, since runtimes differ in how they tag it. */
function planStepsFromItem(item: TimelineItem): PlanStep[] | null {
  const content = item.content ?? {}
  const toolName = typeof content.toolName === "string" ? content.toolName : ""
  const title = typeof content.title === "string" ? content.title : ""
  if (!PLAN_TOOL_NAMES.has(toolName.toLowerCase()) && !PLAN_TOOL_NAMES.has(title.toLowerCase())) {
    return null
  }
  // The list is carried under `input` for a tool call, but some runtimes put it
  // at the top level of the payload.
  const holder = (content.input ?? content) as Record<string, unknown>
  const raw = holder.todos ?? holder.plan ?? holder.steps
  if (!Array.isArray(raw)) return null

  const steps: PlanStep[] = []
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue
    const record = entry as Record<string, unknown>
    const text = typeof record.content === "string"
      ? record.content
      : typeof record.step === "string"
        ? record.step
        : ""
    if (!text.trim()) continue
    const status = record.status
    steps.push({
      content: text.trim(),
      // An unrecognised status is treated as not-started rather than dropped,
      // so a new runtime status can never silently remove a step from view.
      status: status === "completed" || status === "in_progress" ? status : "pending",
    })
  }
  return steps.length > 0 ? steps : null
}

/**
 * The plan in force for the session's **current** turn, or null when the current
 * turn has no plan.
 *
 * Scoped to the latest request because a plan describes the work in hand, not
 * the session's history: showing the previous turn's finished checklist while a
 * new request runs would describe work that is already over. As soon as a newer
 * request exists, any plan written before it belongs to the earlier turn and is
 * no longer current — if that newer turn writes no plan, there is nothing to
 * show, which is the intended outcome rather than a fallback to the old one.
 *
 * Within the current turn only the newest revision counts, because the agent
 * rewrites the whole list on every call.
 */
export function buildTimelinePlan(items: readonly TimelineItem[]): TimelinePlan | null {
  // The last request opens the turn the reader is currently in.
  let turnStart = -1
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index]
    if (item && isUserRequestItem(item)) turnStart = index
  }

  let latest: { item: TimelineItem; steps: PlanStep[] } | null = null
  for (let index = turnStart + 1; index < items.length; index += 1) {
    const item = items[index]
    if (!item) continue
    const steps = planStepsFromItem(item)
    if (!steps) continue
    if (!latest || item.orderSeq >= latest.item.orderSeq) latest = { item, steps }
  }
  if (!latest) return null
  const completed = latest.steps.filter((step) => step.status === "completed").length
  return {
    id: latest.item.id,
    steps: latest.steps,
    anchorId: latest.item.id,
    orderSeq: latest.item.orderSeq,
    createdAt: latest.item.createdAt,
    completed,
    total: latest.steps.length,
  }
}

/**
 * Ids of every item that carries a plan, so callers can keep those rows out of
 * the folded process block and avoid showing the same information twice.
 */
export function planItemIds(items: readonly TimelineItem[]): Set<string> {
  const ids = new Set<string>()
  for (const item of items) {
    if (planStepsFromItem(item)) ids.add(item.id)
  }
  return ids
}
