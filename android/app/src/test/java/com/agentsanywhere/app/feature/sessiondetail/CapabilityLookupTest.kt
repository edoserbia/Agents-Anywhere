package com.agentsanywhere.app.feature.sessiondetail

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Capability lookup must survive a session whose runtime instance has no
 * dedicated facts yet.
 *
 * The live connector publishes session-scoped capabilities carrying the active
 * `runtimeId`. When the client asks with a *stale* id — or with an id the
 * payload does not carry — the lookup used to fall through to `null`, which the
 * UI reports as "This runtime cannot send messages" even though the runtime
 * plainly supports sending. That left the composer disabled on an idle session.
 */
class CapabilityLookupTest {
    private fun cap(
        id: String,
        supported: Boolean = true,
        available: Boolean = true,
        allowed: Boolean = true,
        reason: String? = null,
        runtimeId: String? = "rti_live",
        runtimeType: String? = "dsh",
        scope: String = "session",
    ) = EffectiveCapability(
        capabilityId = id,
        version = "1",
        scope = scope,
        runtime = runtimeType,
        sessionId = "s1",
        supported = supported,
        available = available,
        allowed = allowed,
        unavailableReason = reason,
        parameters = emptyMap(),
        runtimeId = runtimeId,
        runtimeType = runtimeType,
    )

    private fun caps(vararg items: EffectiveCapability) = EffectiveCapabilities(
        revision = 1,
        capabilities = items.toList(),
        connectorId = "c1",
        serverTime = null,
        isLoaded = true,
    )

    @Test
    fun `matching runtime id resolves the capability`() {
        val set = caps(cap("session.send_message"))
        assertTrue(set.isUsable("session.send_message", "rti_live", "dsh"))
    }

    @Test
    fun `a stale runtime id still resolves the session capability`() {
        // The session moved to a new instance; the facts may lag one revision.
        val set = caps(cap("session.send_message", runtimeId = "rti_previous"))
        assertTrue(
            "a session capability must not vanish because the instance id moved",
            set.isUsable("session.send_message", "rti_live", "dsh"),
        )
    }

    @Test
    fun `a session scoped capability applies even when ids disagree`() {
        val set = caps(cap("session.send_message", runtimeId = "rti_other", scope = "session"))
        assertTrue(set.isUsable("session.send_message", "rti_live", "dsh"))
    }

    @Test
    fun `explicitly unsupported stays unusable`() {
        val set = caps(cap("session.steer", supported = false, available = false, allowed = false))
        assertFalse(set.isUsable("session.steer", "rti_live", "dsh"))
    }

    @Test
    fun `an unsupported capability explains itself`() {
        val set = caps(
            cap("session.send_message"),
            cap("session.steer", supported = false, available = false, allowed = false, reason = "runtime_capability_unsupported"),
        )
        assertEquals("runtime_capability_unsupported", sendUnavailableReason(set, "rti_live", "dsh"))
    }

    @Test
    fun `an idle session with a usable send offers Send`() {
        val set = caps(cap("session.send_message"), cap("session.steer", supported = false, available = false, allowed = false))
        assertEquals(RuntimeMessageAction.Send, set.messageAction("rti_live", SessionRuntimeStatus.Idle, "dsh"))
    }

    @Test
    fun `a running session that cannot steer still offers Send`() {
        val set = caps(cap("session.send_message"), cap("session.steer", supported = false, available = false, allowed = false))
        assertEquals(RuntimeMessageAction.Send, set.messageAction("rti_live", SessionRuntimeStatus.Running, "dsh"))
    }
}

/**
 * A capability set that reports nothing usable is the server's
 * "could not reach the connector" placeholder, not a statement about the
 * runtime. It must never outrank facts that name something usable, no matter
 * what revision each carries — a placeholder revision is a session timestamp
 * and is therefore always larger, which previously made the placeholder win
 * forever and left the composer disabled on a healthy session.
 */
class CapabilityRevisionTest {
    private fun cap(
        supported: Boolean,
        revision: Long,
        runtimeId: String = "rti_live",
    ): Pair<EffectiveCapabilities, EffectiveCapability> {
        val c = EffectiveCapability(
            capabilityId = "session.send_message",
            version = "1",
            scope = "session",
            runtime = "dsh",
            sessionId = "s1",
            supported = supported,
            available = supported,
            allowed = true,
            unavailableReason = if (supported) null else "runtime_capability_unsupported",
            parameters = emptyMap(),
            runtimeId = runtimeId,
            runtimeType = "dsh",
        )
        return EffectiveCapabilities(
            revision = revision,
            capabilities = listOf(c),
            connectorId = "c1",
            serverTime = null,
            isLoaded = true,
        ) to c
    }

    @Test
    fun `healthier facts are recognized as usable`() {
        val (healthy, _) = cap(supported = true, revision = 100)
        assertTrue(healthy.isUsable("session.send_message", "rti_live", "dsh"))
    }

    @Test
    fun `the placeholder reports nothing usable`() {
        val (placeholder, _) = cap(supported = false, revision = 1789534940969771L)
        assertTrue(
            "the placeholder must be detectable as 'nothing usable'",
            placeholder.capabilities.none { it.usable },
        )
    }

    @Test
    fun `a placeholder revision dwarfs a real counter`() {
        // This is why revision alone cannot decide which facts to keep.
        val (_, _) = cap(supported = true, revision = 46_930)
        val placeholderRevision = 1_789_534_940_969_771L
        assertTrue(placeholderRevision > 46_930)
    }
}
