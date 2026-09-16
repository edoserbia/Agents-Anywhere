package com.agentsanywhere.app.feature.sessiondetail

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Timestamp rendering for timeline rows.
 *
 * Every timeline item carries the moment the server first saw it. Showing that
 * lets a reader tell when a reply or an action began, which is otherwise
 * impossible to reconstruct from a transcript that can span hours.
 *
 * The value renders as `[YYYY-MM-DD HH:mm:ss]` in the device's own timezone, in
 * the same bracketed form the timeline uses for other metadata.
 */
private val timelineTimestampFormatter: DateTimeFormatter =
    DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")

/**
 * Format an ISO-8601 instant as `[YYYY-MM-DD HH:mm:ss]`, or null when unusable.
 *
 * The fields are assembled from a formatter rather than a locale-aware style so
 * the numeric form stays stable regardless of device language: a reader
 * comparing two timestamps should not have to contend with a locale-specific
 * field order.
 */
internal fun formatTimelineTimestamp(
    value: String?,
    zone: ZoneId = ZoneId.systemDefault(),
): String? {
    if (value.isNullOrBlank()) return null
    val instant = runCatching { Instant.parse(value) }.getOrNull() ?: return null
    return "[${timelineTimestampFormatter.format(instant.atZone(zone))}]"
}
