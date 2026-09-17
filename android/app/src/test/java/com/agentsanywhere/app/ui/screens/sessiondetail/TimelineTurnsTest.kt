package com.agentsanywhere.app.ui.screens.sessiondetail

import com.agentsanywhere.app.feature.sessiondetail.MessageAuthor
import com.agentsanywhere.app.feature.sessiondetail.TimelineMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Covers the timeline turn folding that keeps a request and its answer visible
 * while the runtime's process detail collapses behind one row.
 */
class TimelineTurnsTest {

    private fun user(id: String, text: String = "do it") = TimelineMessage(
        id = id,
        author = MessageAuthor.User,
        text = text,
        type = "message",
    )

    private fun reply(id: String, text: String = "done") = TimelineMessage(
        id = id,
        author = MessageAuthor.Agent,
        text = text,
        type = "message",
    )

    private fun tool(id: String) = TimelineMessage(
        id = id,
        author = MessageAuthor.Tool,
        text = "",
        type = "tool",
        contentKind = "command",
    )

    private fun reasoning(id: String) = TimelineMessage(
        id = id,
        author = MessageAuthor.Agent,
        text = "thinking",
        type = "system",
        kind = com.agentsanywhere.app.feature.sessiondetail.TimelineMessageKind.Reasoning,
        contentKind = "reasoning",
    )

    private fun single(message: TimelineMessage) = TimelineRenderItem.Single(message)

    private fun toolRun(vararg messages: TimelineMessage) = TimelineRenderItem.ToolRun(messages.toList())

    @Test
    fun `folds a turn process into one block between request and reply`() {
        val blocks = buildTimelineBlocks(
            listOf(
                single(user("u1")),
                single(reasoning("r1")),
                toolRun(tool("t1"), tool("t2")),
                single(reply("a1")),
            ),
        )

        assertEquals(
            listOf("Entry", "Process", "Entry"),
            blocks.map { it::class.simpleName },
        )
        assertEquals("u1", blocks[0].messages.single().id)
        assertEquals("a1", blocks[2].messages.single().id)
        // reasoning and tool rows fold into the process block
        assertEquals(listOf("r1", "t1", "t2"), blocks[1].messages.map { it.id })
    }

    @Test
    fun `intermediate replies fold away leaving only the turn's final reply`() {
        // A runtime narrates as it works: reply, tools, reply, tools, conclusion.
        // Only the conclusion is the answer, so everything before it folds.
        val blocks = buildTimelineBlocks(
            listOf(
                single(user("u1")),
                single(reply("a1")),
                single(tool("t1")),
                single(reply("a2")),
                single(tool("t2")),
                single(reply("a3")),
            ),
        )

        assertEquals(
            listOf("Entry", "Process", "Entry"),
            blocks.map { it::class.simpleName },
        )
        assertEquals("u1", blocks[0].messages.single().id)
        assertEquals("a3", blocks[2].messages.single().id)
        // Intermediate replies fold in with the process, in order.
        assertEquals(listOf("a1", "t1", "a2", "t2"), blocks[1].messages.map { it.id })
    }

    @Test
    fun `while a turn runs the newest reply stays visible and earlier ones fold`() {
        val first = buildTimelineBlocks(
            listOf(single(user("u1")), single(reply("a1")), single(tool("t1"))),
        )
        assertEquals(
            listOf("Entry", "Entry", "Process"),
            first.map { it::class.simpleName },
        )
        assertEquals(listOf("t1"), first[2].messages.map { it.id })

        // When the next update arrives, the previous one folds in behind it.
        val second = buildTimelineBlocks(
            listOf(single(user("u1")), single(reply("a1")), single(tool("t1")), single(reply("a2"))),
        )
        assertEquals(
            listOf("Entry", "Process", "Entry"),
            second.map { it::class.simpleName },
        )
        assertEquals(listOf("a1", "t1"), second[1].messages.map { it.id })
        assertEquals("a2", second[2].messages.single().id)
    }

    @Test
    fun `each turn keeps its own fold and its own conclusion`() {
        val blocks = buildTimelineBlocks(
            listOf(
                single(user("u1")),
                single(reply("a1")),
                single(tool("t1")),
                single(reply("a2")),
                single(user("u2")),
                single(reply("a3")),
                single(tool("t2")),
                single(reply("a4")),
            ),
        )

        assertEquals(
            listOf("Entry", "Process", "Entry", "Entry", "Process", "Entry"),
            blocks.map { it::class.simpleName },
        )
        val processes = blocks.filterIsInstance<TimelineBlock.Process>()
        assertEquals(listOf("a1", "t1"), processes[0].messages.map { it.id })
        assertEquals(listOf("a3", "t2"), processes[1].messages.map { it.id })
        assertEquals(
            listOf("u1", "a2", "u2", "a4"),
            blocks.filterIsInstance<TimelineBlock.Entry>().map { it.messages.single().id },
        )
    }

    @Test
    fun `a turn abandoned without a reply still separates from the next`() {
        val blocks = buildTimelineBlocks(
            listOf(single(user("u1")), single(tool("t1")), single(user("u2")), single(reply("a2"))),
        )

        assertEquals(
            listOf("Entry", "Process", "Entry", "Entry"),
            blocks.map { it::class.simpleName },
        )
        assertEquals(listOf("t1"), blocks[1].messages.map { it.id })
    }

    @Test
    fun `every process block of the running turn is live not just the last`() {
        // A turn narrates as reply, tools, reply, tools. Each intermediate reply
        // closes the block before it, so keeping only the final block open would
        // hide most of the work and leave no way to tell whether the run stalled.
        val blocks = buildTimelineBlocks(
            listOf(
                single(user("u1")),
                single(reply("a1")),
                single(tool("t1")),
                single(reply("a2")),
                single(tool("t2")),
            ),
        )
        val processes = blocks.filterIsInstance<TimelineBlock.Process>()
        assertEquals(2, processes.size)
        assertEquals(processes.map { it.key }.toSet(), activeProcessBlockKeys(blocks))
    }

    @Test
    fun `a finished turn stops being live once a newer request arrives`() {
        val blocks = buildTimelineBlocks(
            listOf(
                single(user("u1")),
                single(reply("a1")),
                single(tool("t1")),
                single(user("u2")),
                single(tool("t2")),
            ),
        )
        val processes = blocks.filterIsInstance<TimelineBlock.Process>()
        assertEquals(2, processes.size)
        assertEquals(setOf(processes[1].key), activeProcessBlockKeys(blocks))
    }

    @Test
    fun `keeps a process block per turn so an old turn cannot absorb a new one`() {
        val blocks = buildTimelineBlocks(
            listOf(
                single(user("u1")),
                single(reasoning("r1")),
                single(reply("a1")),
                single(user("u2")),
                single(tool("t9")),
                single(reply("a2")),
            ),
        )

        assertEquals(
            listOf("Entry", "Process", "Entry", "Entry", "Process", "Entry"),
            blocks.map { it::class.simpleName },
        )
        val processes = blocks.filterIsInstance<TimelineBlock.Process>()
        assertEquals(2, processes.size)
        assertEquals(listOf("r1"), processes[0].messages.map { it.id })
        assertEquals(listOf("t9"), processes[1].messages.map { it.id })
    }

    @Test
    fun `folds process that arrives before any request without losing it`() {
        val blocks = buildTimelineBlocks(
            listOf(single(reasoning("r0")), single(user("u1")), single(reply("a1"))),
        )

        assertEquals(listOf("Process", "Entry", "Entry"), blocks.map { it::class.simpleName })
        assertEquals(listOf("r0"), blocks[0].messages.map { it.id })
    }

    @Test
    fun `only the last process block is live`() {
        val blocks = buildTimelineBlocks(
            listOf(
                single(user("u1")),
                single(reasoning("r1")),
                single(reply("a1")),
                single(user("u2")),
                single(reasoning("r2")),
            ),
        )

        val processes = blocks.filterIsInstance<TimelineBlock.Process>()
        assertEquals(processes.last().key, activeProcessBlockKey(blocks))
        assertTrue(activeProcessBlockKey(blocks) != processes.first().key)
    }

    @Test
    fun `no process block means nothing is live`() {
        val blocks = buildTimelineBlocks(listOf(single(user("u1")), single(reply("a1"))))
        assertNull(activeProcessBlockKey(blocks))
    }

    @Test
    fun `process block key stays stable as more process rows arrive`() {
        val before = buildTimelineBlocks(listOf(single(user("u1")), single(reasoning("r1"))))
        val after = buildTimelineBlocks(
            listOf(
                single(user("u1")),
                single(reasoning("r1")),
                single(tool("t1")),
                single(tool("t2")),
            ),
        )
        fun keyOf(blocks: List<TimelineBlock>) =
            blocks.filterIsInstance<TimelineBlock.Process>().first().key

        assertEquals(keyOf(before), keyOf(after)) // expansion state must survive new process rows
    }

    @Test
    fun `lists past requests oldest first with stable numbering`() {
        val blocks = buildTimelineBlocks(
            listOf(
                single(user("u1", "first request")),
                single(reasoning("r1")),
                single(reply("a1")),
                single(user("u2", "second request")),
                single(reply("a2")),
            ),
        )

        val entries = buildTimelineRequestEntries(blocks)
        assertEquals(listOf("u1", "u2"), entries.map { it.id })
        assertEquals(listOf(1, 2), entries.map { it.index })
        assertEquals(listOf("first request", "second request"), entries.map { it.text })
    }

    @Test
    fun `agent replies are never listed as requests`() {
        val blocks = buildTimelineBlocks(
            listOf(single(reply("a0", "unprompted")), single(user("u1")), single(reply("a1"))),
        )
        assertEquals(listOf("u1"), buildTimelineRequestEntries(blocks).map { it.id })
    }

    @Test
    fun `request previews collapse whitespace and truncate`() {
        assertEquals("hello world next", requestPreview("  hello   world \n next "))
        val preview = requestPreview("x".repeat(300), 40)
        assertEquals(40, preview.length)
        assertTrue(preview.endsWith("…"))
    }

    @Test
    fun `empty request text yields an empty preview rather than throwing`() {
        assertEquals("", requestPreview(""))
        assertEquals("", requestPreview("   \n  "))
    }
}
