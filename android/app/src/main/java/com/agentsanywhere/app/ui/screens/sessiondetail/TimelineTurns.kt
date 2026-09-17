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
 * Split grouped items into the visible request and answer of each turn, with
 * everything the runtime did in between folded into a single block.
 *
 * A turn is one user request and its outcome, but the runtime narrates as it
 * goes: it emits a reply, runs tools, emits another reply, and so on. Treating
 * every one of those replies as an answer splits a single turn into many
 * process blocks and makes the reader scroll past intermediate commentary to
 * reach the conclusion.
 *
 * So a turn renders as exactly three things: the request, one folded block
 * holding all the process *and* every intermediate reply, and the turn's final
 * reply. The final reply is the only message left outside the fold, because it
 * is the answer the reader came for.
 *
 * The final reply is identified by looking ahead to the next user request (or
 * the end of the list). While a turn is still running the newest reply stays
 * visible as the current progress report, and the fold grows behind it.
 */
internal fun buildTimelineBlocks(items: List<TimelineRenderItem>): List<TimelineBlock> {
    // Which item holds each turn's final reply. Computed up front because the
    // answer is only knowable from what follows it.
    val finalReplyIndex = mutableSetOf<Int>()
    fun closeTurn(endExclusive: Int, startInclusive: Int) {
        for (i in endExclusive - 1 downTo startInclusive) {
            val item = items.getOrNull(i) ?: continue
            if (item.messages.any { it.isAgentReply() }) {
                finalReplyIndex += i
                return
            }
        }
    }
    var turnStart = 0
    items.forEachIndexed { index, item ->
        if (item.messages.any { it.isUserRequest() }) {
            if (index > turnStart) closeTurn(index, turnStart)
            turnStart = index
        }
    }
    closeTurn(items.size, turnStart)

    val blocks = mutableListOf<TimelineBlock>()
    val pending = mutableListOf<TimelineRenderItem>()

    fun flush() {
        if (pending.isEmpty()) return
        val messages = pending.flatMap { it.messages }
        blocks += TimelineBlock.Process(messages = messages, items = pending.toList())
        pending.clear()
    }

    items.forEachIndexed { index, item ->
        // The request opens a turn; the turn's final reply closes it. Both stay
        // visible. Every other item — including intermediate replies — folds.
        val visible = item.messages.any { it.isUserRequest() } || index in finalReplyIndex
        if (visible) {
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
 * Keys of the process blocks belonging to the turn currently being produced.
 *
 * The running turn's work stays open so its progress is visible as it happens.
 * Every block of that turn qualifies, not just the last one: a turn narrates as
 * reply, tools, reply, tools, and each intermediate reply closes the block
 * before it. Returning only the final block hid the work already done, leaving
 * a reader with "the agent is working" and no way to tell what it was doing or
 * whether it had stalled.
 *
 * Blocks from finished turns are excluded, because an earlier turn is complete
 * once a newer request exists.
 */
internal fun activeProcessBlockKeys(blocks: List<TimelineBlock>): Set<String> {
    val lastRequestIndex = blocks.indexOfLast { block ->
        block is TimelineBlock.Entry && block.messages.any { it.isUserRequest() }
    }
    return blocks
        .drop(lastRequestIndex + 1)
        .filterIsInstance<TimelineBlock.Process>()
        .map { it.key }
        .toSet()
}

/** Key of the most recent live process block, or null when none is live. */
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
