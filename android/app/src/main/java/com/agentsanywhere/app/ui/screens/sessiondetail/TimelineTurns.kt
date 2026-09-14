package com.agentsanywhere.app.ui.screens.sessiondetail

import com.agentsanywhere.app.feature.sessiondetail.MessageAuthor
import com.agentsanywhere.app.feature.sessiondetail.TimelineMessage

/**
 * Render planning for the session timeline.
 *
 * One turn is a user request plus everything the runtime did to answer it:
 * reasoning, tool calls, file changes, sub-agent calls and reconnect noise.
 * Rendering each of those as its own row pushes a lot of process detail
 * between the question and the answer, so a turn's process is folded into a
 * single collapsible row that the reader can open on demand.
 */
internal sealed interface TimelineBlock {
    val key: String
    val messages: List<TimelineMessage>

    /** A request, an answer, or any single item that must stay visible. */
    data class Entry(val item: TimelineRenderItem) : TimelineBlock {
        override val key: String = item.key
        override val messages: List<TimelineMessage> = item.messages
    }

    /** The folded process of one turn. */
    data class Process(
        override val messages: List<TimelineMessage>,
        val items: List<TimelineRenderItem>,
    ) : TimelineBlock {
        // Anchor to the first folded item so the key stays stable while the
        // runtime appends more process rows to the same turn.
        override val key: String = "process:${messages.firstOrNull()?.id ?: "unknown"}"
    }
}

internal fun TimelineMessage.isUserRequest(): Boolean =
    type == "message" && author == MessageAuthor.User

internal fun TimelineMessage.isAgentReply(): Boolean =
    type == "message" && author != MessageAuthor.User

/**
 * Split grouped items into visible requests/answers and the folded process
 * between them. Anything that is neither a request nor an answer is process.
 */
internal fun buildTimelineBlocks(items: List<TimelineRenderItem>): List<TimelineBlock> {
    val blocks = mutableListOf<TimelineBlock>()
    val pending = mutableListOf<TimelineRenderItem>()

    fun flush() {
        if (pending.isEmpty()) return
        val messages = pending.flatMap { it.messages }
        blocks += TimelineBlock.Process(messages = messages, items = pending.toList())
        pending.clear()
    }

    for (item in items) {
        val isVisibleTurnItem = item.messages.any { it.isUserRequest() || it.isAgentReply() }
        if (isVisibleTurnItem) {
            flush()
            blocks += TimelineBlock.Entry(item)
        } else {
            pending += item
        }
    }
    flush()
    return blocks
}

/**
 * Key of the process block that is still being produced. Only the last block
 * can be live, because an earlier turn is finished once a newer request exists.
 */
internal fun activeProcessBlockKey(blocks: List<TimelineBlock>): String? =
    blocks.filterIsInstance<TimelineBlock.Process>().lastOrNull()?.key

/** One navigable request in the session. */
internal data class TimelineRequestEntry(
    /** Id used to scroll the timeline to this request. */
    val id: String,
    /** Position in the session, counting from one, in reading order. */
    val index: Int,
    val text: String,
    val status: String,
)

/** Collapse whitespace so a request reads as one line in the navigator. */
internal fun requestPreview(text: String, maxLength: Int = 120): String {
    val collapsed = text.replace(Regex("\\s+"), " ").trim()
    if (collapsed.length <= maxLength) return collapsed
    return collapsed.take(maxLength - 1).trimEnd() + "…"
}

/**
 * Build the navigable list of past requests, oldest first, so the numbering
 * matches how a reader counts requests down the timeline.
 */
internal fun buildTimelineRequestEntries(
    blocks: List<TimelineBlock>,
    maxLength: Int = 120,
): List<TimelineRequestEntry> {
    val entries = mutableListOf<TimelineRequestEntry>()
    for (block in blocks) {
        if (block !is TimelineBlock.Entry) continue
        for (message in block.messages) {
            if (!message.isUserRequest()) continue
            entries += TimelineRequestEntry(
                id = message.id,
                index = entries.size + 1,
                text = requestPreview(message.text, maxLength),
                status = message.status,
            )
        }
    }
    return entries
}
