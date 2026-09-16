package com.agentsanywhere.app.feature.sessiondetail

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId

/**
 * Timestamp formatting for timeline rows.
 *
 * A transcript can span hours, and without a visible time a reader cannot tell
 * when a reply or an action began. These cases pin the exact rendered shape,
 * because the value is read as data: two timestamps must be comparable at a
 * glance regardless of the device language.
 */
class TimelineTimestampTest {
    private val utc = ZoneId.of("UTC")

    @Test
    fun `formats an instant as a bracketed month, day and time`() {
        assertEquals(
            "[09-16 14:05]",
            formatTimelineTimestamp("2026-09-16T14:05:09Z", utc),
        )
    }

    @Test
    fun `omits seconds and the year`() {
        val formatted = formatTimelineTimestamp("2026-09-16T14:05:09Z", utc)
        assertTrue("the year must not appear", formatted?.contains("2026") != true)
        assertEquals("only hours and minutes", 2, formatted!!.removePrefix("[").removeSuffix("]").split(":").size)
    }

    @Test
    fun `pads every field to a fixed width`() {
        assertEquals(
            "[01-02 03:04]",
            formatTimelineTimestamp("2026-01-02T03:04:05Z", utc),
        )
    }

    @Test
    fun `renders the instant in the supplied zone`() {
        // 14:05 UTC is 22:05 in Beijing, which is what a reader there expects.
        assertEquals(
            "[09-16 22:05]",
            formatTimelineTimestamp("2026-09-16T14:05:09Z", ZoneId.of("Asia/Shanghai")),
        )
    }

    @Test
    fun `renders nothing for a missing timestamp`() {
        assertNull(formatTimelineTimestamp(null, utc))
        assertNull(formatTimelineTimestamp("", utc))
        assertNull(formatTimelineTimestamp("   ", utc))
    }

    @Test
    fun `renders nothing rather than an unparseable value`() {
        assertNull(formatTimelineTimestamp("not-a-time", utc))
        assertNull(formatTimelineTimestamp("2026-13-45T99:99:99Z", utc))
    }

    @Test
    fun `uses the same width for every timestamp so they line up`() {
        val first = formatTimelineTimestamp("2026-09-16T09:00:00Z", utc)
        val second = formatTimelineTimestamp("2026-09-16T17:30:00Z", utc)
        assertTrue(first != null && second != null)
        assertEquals(first!!.length, second!!.length)
        // Short enough to sit above a row without dominating it.
        assertTrue("stamp is too long: $first", first.length <= 16)
    }
}
