package com.agentsanywhere.app.ui.screens.sessiondetail

import com.agentsanywhere.app.feature.sessiondetail.MessageAuthor
import com.agentsanywhere.app.feature.sessiondetail.TimelineMessage
import com.agentsanywhere.app.feature.sessiondetail.TimelineMessageKind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Coverage for reading an agent's plan off the timeline. */
class TimelinePlanTest {

    private fun message(
        id: String,
        orderSeq: Int,
        rawContent: String,
        createdAt: String = "2026-09-16T10:00:00Z",
    ) = TimelineMessage(
        id = id,
        author = MessageAuthor.Agent,
        text = "",
        kind = TimelineMessageKind.ToolCall,
        type = "tool",
        createdAt = createdAt,
        orderSeq = orderSeq,
        rawContent = rawContent,
    )

    /** A `todo_write` call, the shape DSH actually emits. */
    private fun todoCall(id: String, orderSeq: Int, todos: String) = message(
        id = id,
        orderSeq = orderSeq,
        rawContent = """{"kind":"tool_call","title":"todo_write","toolName":"todo_write","input":{"todos":$todos}}""",
    )

    @Test
    fun `reads the plan out of a todo_write tool call`() {
        val plan = buildTimelinePlan(
            listOf(
                todoCall(
                    "p1",
                    2,
                    """[{"content":"First step","status":"completed"},
                        {"content":"Second step","status":"in_progress"},
                        {"content":"Third step","status":"pending"}]""",
                ),
            ),
        )

        requireNotNull(plan) { "a plan is found" }
        assertEquals(3, plan.total)
        assertEquals(1, plan.completed)
        assertEquals(
            listOf(PlanStatus.COMPLETED, PlanStatus.IN_PROGRESS, PlanStatus.PENDING),
            plan.steps.map { it.status },
        )
    }

    @Test
    fun `the newest revision wins because each call replaces the whole list`() {
        // The agent rewrites the entire list on every call, so an earlier call is
        // history: reading it would show steps that have since been reworded.
        val plan = buildTimelinePlan(
            listOf(
                todoCall("p1", 2, """[{"content":"Old wording","status":"pending"}]"""),
                todoCall(
                    "p2",
                    5,
                    """[{"content":"Old wording","status":"completed"},
                        {"content":"Newly discovered step","status":"in_progress"}]""",
                ),
            ),
        )

        requireNotNull(plan)
        assertEquals("p2", plan.id)
        assertEquals(2, plan.total)
        assertEquals(1, plan.completed)
    }

    @Test
    fun `no plan means no card rather than an empty one`() {
        assertNull(buildTimelinePlan(emptyList()))
        assertNull(buildTimelinePlan(listOf(message("t1", 1, """{"kind":"command","title":"ls"}"""))))
    }

    @Test
    fun `Codex's update_plan is read the same way`() {
        val plan = buildTimelinePlan(
            listOf(
                message(
                    "c1",
                    3,
                    """{"kind":"tool_call","toolName":"update_plan","input":{"plan":[
                        {"step":"Investigate the bug","status":"completed"},
                        {"step":"Write the fix","status":"pending"}]}}""",
                ),
            ),
        )
        requireNotNull(plan) { "the plan is recognised by tool name" }
        assertEquals(listOf("Investigate the bug", "Write the fix"), plan.steps.map { it.content })
    }

    @Test
    fun `an unrecognised status is kept as not-started never dropped`() {
        // A runtime that adds a status must never make a step vanish.
        val plan = buildTimelinePlan(
            listOf(
                todoCall(
                    "p1",
                    1,
                    """[{"content":"Known","status":"completed"},
                        {"content":"Brand new status","status":"blocked"}]""",
                ),
            ),
        )
        requireNotNull(plan)
        assertEquals(2, plan.total)
        assertEquals(PlanStatus.PENDING, plan.steps[1].status)
    }

    @Test
    fun `blank steps are skipped and a plan of only blanks is no plan`() {
        val plan = buildTimelinePlan(
            listOf(
                todoCall(
                    "p1",
                    1,
                    """[{"content":"   ","status":"pending"},{"content":"Real","status":"pending"}]""",
                ),
            ),
        )
        requireNotNull(plan)
        assertEquals(1, plan.total)

        assertNull(buildTimelinePlan(listOf(todoCall("p2", 1, """[{"content":"  ","status":"pending"}]"""))))
    }

    @Test
    fun `malformed payloads are ignored instead of throwing`() {
        // The raw content is wire data, so anything can arrive; a bad payload
        // must not take the transcript down with it.
        assertNull(buildTimelinePlan(listOf(message("bad", 1, "not json at all"))))
        assertNull(buildTimelinePlan(listOf(message("bad2", 1, """{"toolName":"todo_write"}"""))))
        assertNull(
            buildTimelinePlan(
                listOf(message("bad3", 1, """{"toolName":"todo_write","input":{"todos":"nope"}}""")),
            ),
        )
    }
}
