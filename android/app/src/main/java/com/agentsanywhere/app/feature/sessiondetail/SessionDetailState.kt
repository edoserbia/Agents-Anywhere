package com.agentsanywhere.app.feature.sessiondetail

import com.agentsanywhere.app.model.AgentSession

data class SessionDetailState(
    val meta: SessionMeta = SessionMeta(),
    val timeline: SessionTimelineState = SessionTimelineState(),
    val runtime: SessionRuntimeState = SessionRuntimeState(),
    val capabilities: EffectiveCapabilities = EffectiveCapabilities(),
    val runtimeCapabilities: RuntimeCapabilities = RuntimeCapabilities(),
    val notices: RuntimeNotices = RuntimeNotices(),
    val catalogs: RuntimeCatalogs = RuntimeCatalogs(),
    val commands: RuntimeCommands = RuntimeCommands(),
    val realtime: SessionRealtimeState = SessionRealtimeState(),
    val initialized: Boolean = false,
    val actionError: String? = null,
    val takeoverInFlight: Boolean = false,
    val sending: Boolean = false,
    val interrupting: Boolean = false,
    val selectionUpdating: Boolean = false,
    val commandExecuting: Boolean = false,
    val respondingNoticeIds: Set<String> = emptySet(),
    val queue: SessionMessageQueue = SessionMessageQueue(),
) {
    val session: AgentSession?
        get() = meta.session

    val messages: List<TimelineMessage>
        get() = timeline.messages

    val nextSeq: Int
        get() = timeline.nextSeq

    val hasMore: Boolean
        get() = timeline.hasMore

    fun withSession(session: AgentSession?): SessionDetailState = copy(meta = meta.copy(session = session))
}

data class SessionRealtimeState(
    val connected: Boolean = false,
    val recovering: Boolean = false,
    val reconnectAttempt: Int = 0,
    val cursor: String = "seq:0",
    val processedEventIds: Set<String> = emptySet(),
    val lastErrorMessage: String? = null,
) {
    fun rememberEvent(eventId: String, cursor: String): SessionRealtimeState {
        val remembered = (processedEventIds + eventId).let { ids ->
            if (ids.size <= MAX_PROCESSED_EVENTS) ids else ids.drop(ids.size - RETAINED_PROCESSED_EVENTS).toSet()
        }
        return copy(cursor = laterEventCursor(this.cursor, cursor), processedEventIds = remembered)
    }

    private companion object {
        const val MAX_PROCESSED_EVENTS = 1_000
        const val RETAINED_PROCESSED_EVENTS = 500
    }
}

internal fun laterEventCursor(current: String, incoming: String): String {
    val currentSequence = current.removePrefix("seq:").toLongOrNull() ?: 0L
    val incomingSequence = incoming.removePrefix("seq:").toLongOrNull() ?: return current
    return if (incomingSequence >= currentSequence) incoming else current
}

data class SessionMeta(
    val session: AgentSession? = null,
    val serverTime: String? = null,
    val isLoading: Boolean = false,
    val errorMessage: String? = null,
)

internal fun SessionDetailState.beginSnapshotLoad(clearErrors: Boolean): SessionDetailState = copy(
    meta = meta.copy(
        isLoading = true,
        errorMessage = if (clearErrors) null else meta.errorMessage,
    ),
    timeline = timeline.copy(
        isLoading = true,
        loadingOlder = false,
        errorMessage = if (clearErrors) null else timeline.errorMessage,
    ),
    runtime = runtime.copy(
        isLoading = true,
        errorMessage = if (clearErrors) null else runtime.errorMessage,
    ),
    capabilities = capabilities.copy(
        isLoading = true,
        errorMessage = if (clearErrors) null else capabilities.errorMessage,
    ),
    notices = notices.copy(
        isLoading = true,
        errorMessage = if (clearErrors) null else notices.errorMessage,
    ),
)

internal fun SessionDetailState.completeSnapshotLoad(): SessionDetailState = copy(
    meta = meta.copy(isLoading = false, errorMessage = null),
    timeline = timeline.copy(isLoading = false, loadingOlder = false, errorMessage = null),
    runtime = runtime.copy(isLoading = false, errorMessage = null),
    capabilities = capabilities.copy(isLoading = false, errorMessage = null),
    notices = notices.copy(isLoading = false, errorMessage = null),
)

internal fun SessionDetailState.failSnapshotLoad(message: String?): SessionDetailState = copy(
    meta = meta.copy(isLoading = false, errorMessage = meta.errorMessage ?: message),
    timeline = timeline.copy(
        isLoading = false,
        loadingOlder = false,
        errorMessage = timeline.errorMessage ?: message,
    ),
    runtime = runtime.copy(isLoading = false, errorMessage = runtime.errorMessage ?: message),
    capabilities = capabilities.copy(isLoading = false, errorMessage = capabilities.errorMessage ?: message),
    notices = notices.copy(isLoading = false, errorMessage = notices.errorMessage ?: message),
)

/**
 * Messages the server is holding until the session's current turn finishes.
 *
 * The queue exists because a runtime can only run one turn at a time. A message
 * sent while one is running is kept here and dispatched automatically, instead
 * of being refused — which is what used to leave the composer dead on runtimes
 * that cannot steer, such as DSH.
 */
data class SessionMessageQueue(
    val items: List<QueuedMessage> = emptyList(),
    val isLoaded: Boolean = false,
    val errorMessage: String? = null,
) {
    /** Items still waiting; only these can be edited or removed. */
    val pending: List<QueuedMessage>
        get() = items.filter(QueuedMessage::pending)

    val isEmpty: Boolean get() = items.isEmpty()
}

data class QueuedMessage(
    val id: String,
    val sessionId: String,
    val position: Int,
    val status: String,
    val content: String,
    val clientMessageId: String? = null,
    val errorCode: String? = null,
    val errorMessage: String? = null,
) {
    val pending: Boolean get() = status == "queued"
    val failed: Boolean get() = status == "failed"
}
