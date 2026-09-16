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
