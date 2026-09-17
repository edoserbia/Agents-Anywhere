package com.agentsanywhere.app.ui.screens.sessiondetail

import com.agentsanywhere.app.feature.sessiondetail.TimelineMessage
import org.json.JSONArray
import org.json.JSONObject

/** One step of an agent's plan. */
internal data class PlanStep(
    /** What the step is; the agent writes this text itself. */
    val content: String,
    val status: PlanStatus,
)

internal enum class PlanStatus {
    PENDING,
    IN_PROGRESS,
    COMPLETED;

    companion object {
        /**
         * Maps a wire status.
         *
         * An unrecognised status becomes [PENDING] rather than dropping the
         * step, so a runtime that adds a status can never make a plan item
         * silently disappear.
         */
        fun from(raw: String?): PlanStatus = when (raw) {
            "completed" -> COMPLETED
            "in_progress" -> IN_PROGRESS
            else -> PENDING
        }
    }
}

/** The plan an agent is following, as of its most recent revision. */
internal data class TimelinePlanState(
    /** The item that produced this revision, used as a stable key. */
    val id: String,
    val steps: List<PlanStep>,
    val createdAt: String,
) {
    val total: Int get() = steps.size
    val completed: Int get() = steps.count { it.status == PlanStatus.COMPLETED }
}

/**
 * Tool names whose calls carry a plan.
 *
 * DSH rewrites the whole list on every call (`todo_write`), and Codex uses
 * `update_plan`; both send the same shape, so one reader covers them. Matching
 * on the name rather than on a positional field keeps this working when a
 * runtime adds or reorders other fields.
 */
private val PLAN_TOOL_NAMES = setOf("todo_write", "todowrite", "update_plan", "updateplan")

/**
 * Reads the steps out of a plan-carrying message, or null when it carries none.
 *
 * The message's `rawContent` is the item's content object as JSON, which is the
 * only place the full list survives projection — the typed fields on
 * [TimelineMessage] keep a display string, not the steps.
 */
private fun planStepsFromMessage(message: TimelineMessage): List<PlanStep>? {
    if (message.rawContent.isBlank()) return null
    val content = runCatching { JSONObject(message.rawContent) }.getOrNull() ?: return null

    val toolName = content.optString("toolName").lowercase()
    val title = content.optString("title").lowercase()
    if (toolName !in PLAN_TOOL_NAMES && title !in PLAN_TOOL_NAMES) return null

    // The list sits under `input` for a tool call, but some runtimes put it at
    // the top level of the payload.
    val holder = content.optJSONObject("input") ?: content
    val array: JSONArray = holder.optJSONArray("todos")
        ?: holder.optJSONArray("plan")
        ?: holder.optJSONArray("steps")
        ?: return null

    val steps = ArrayList<PlanStep>(array.length())
    for (index in 0 until array.length()) {
        val record = array.optJSONObject(index) ?: continue
        val text = record.optString("content").ifBlank { record.optString("step") }
        if (text.isBlank()) continue
        steps += PlanStep(
            content = text.trim(),
            status = PlanStatus.from(record.optString("status").ifBlank { null }),
        )
    }
    return steps.ifEmpty { null }
}

/**
 * The plan currently in force for a session, or null when the agent never made
 * one.
 *
 * The agent rewrites the entire list on every call, so only the newest call
 * describes the present state; earlier calls are history and are used here just
 * to pick the newest revision.
 */
internal fun buildTimelinePlan(messages: List<TimelineMessage>): TimelinePlanState? {
    var latest: TimelineMessage? = null
    var steps: List<PlanStep>? = null
    for (message in messages) {
        val found = planStepsFromMessage(message) ?: continue
        val previous = latest
        if (previous == null || message.orderSeq >= previous.orderSeq) {
            latest = message
            steps = found
        }
    }
    val item = latest ?: return null
    return TimelinePlanState(
        id = item.id,
        steps = steps ?: return null,
        createdAt = item.createdAt,
    )
}
