package com.agentsanywhere.app.feature.sessiondetail


/** Whether the history sheet should ask the server for another timeline page. */
internal fun shouldLoadRequestHistory(
    open: Boolean,
    entryCount: Int,
    visibleCount: Int,
    hasMore: Boolean,
    loading: Boolean,
    failed: Boolean,
): Boolean = open && entryCount < visibleCount && hasMore && !loading && !failed

/** Stops a stale endpoint from making the history loader request the same page forever. */
internal fun timelinePageMadeProgress(items: List<TimelineOrderingItem>, beforeOrderSeq: Int): Boolean =
    items.any { it.orderSeq < beforeOrderSeq }
