package com.agentsanywhere.app.feature.sessiondetail

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Covers the mapping from the platform's capability verdict to the reason the
 * composer reports.
 *
 * The composer used to show one generic string for every failure, which blamed
 * the runtime even when the real cause was a disabled takeover, an offline
 * connector, or a runtime that cannot send at all. These cases pin the reason
 * that each of those situations must produce.
 */
class SendUnavailableReasonTest {

    private fun capability(
        id: String,
        supported: Boolean = true,
        available: Boolean = true,
        allowed: Boolean = true,
        reason: String? = null,
        runtimeId: String? = "rt1",
    ) = EffectiveCapability(
        capabilityId = id,
        version = "1",
        scope = "session",
        runtime = "dsh",
        sessionId = "s1",
        supported = supported,
        available = available,
        allowed = allowed,
        unavailableReason = reason,
        parameters = emptyMap(),
        runtimeId = runtimeId,
        runtimeType = "dsh",
    )

    private fun capabilities(vararg items: EffectiveCapability) = EffectiveCapabilities(
        revision = 1,
        capabilities = items.toList(),
        isLoaded = true,
    )

    @Test
    fun `reports the server reason when takeover has not been enabled`() {
        val facts = capabilities(
            capability(SESSION_SEND_MESSAGE_CAPABILITY, allowed = false, reason = "session_not_taken_over"),
        )
        assertEquals("session_not_taken_over", sendUnavailableReason(facts, "rt1", "dsh"))
    }

    @Test
    fun `reports an offline connector rather than a runtime problem`() {
        val facts = capabilities(
            capability(SESSION_SEND_MESSAGE_CAPABILITY, available = false, reason = "connector_offline"),
        )
        assertEquals("connector_offline", sendUnavailableReason(facts, "rt1", "dsh"))
    }

    @Test
    fun `reports an unsupported runtime when the runtime cannot send`() {
        val facts = capabilities(
            capability(SESSION_SEND_MESSAGE_CAPABILITY, supported = false, reason = "runtime_capability_unsupported"),
        )
        assertEquals("runtime_capability_unsupported", sendUnavailableReason(facts, "rt1", "dsh"))
    }

    @Test
    fun `prefers the send capability reason over steering`() {
        val facts = capabilities(
            capability(SESSION_SEND_MESSAGE_CAPABILITY, allowed = false, reason = "session_not_taken_over"),
            capability(SESSION_STEER_CAPABILITY, available = false, reason = "connector_offline"),
        )
        assertEquals("session_not_taken_over", sendUnavailableReason(facts, "rt1", "dsh"))
    }

    @Test
    fun `falls back to steering when send is usable but steering is not`() {
        val facts = capabilities(
            capability(SESSION_SEND_MESSAGE_CAPABILITY),
            capability(SESSION_STEER_CAPABILITY, available = false, reason = "runtime_capability_unavailable"),
        )
        assertEquals("runtime_capability_unavailable", sendUnavailableReason(facts, "rt1", "dsh"))
    }

    @Test
    fun `returns null when sending is usable`() {
        val facts = capabilities(capability(SESSION_SEND_MESSAGE_CAPABILITY))
        assertNull(sendUnavailableReason(facts, "rt1", "dsh"))
    }

    @Test
    fun `returns null when no capabilities have arrived`() {
        assertNull(sendUnavailableReason(EffectiveCapabilities(), "rt1", "dsh"))
    }

    @Test
    fun `blank reasons are ignored rather than shown as empty text`() {
        val facts = capabilities(
            capability(SESSION_SEND_MESSAGE_CAPABILITY, available = false, reason = "   "),
        )
        assertNull(sendUnavailableReason(facts, "rt1", "dsh"))
    }

    @Test
    fun `uses another unusable capability's reason when the send one has none`() {
        val facts = capabilities(
            capability(SESSION_SEND_MESSAGE_CAPABILITY, available = false, reason = null),
            capability(SESSION_COMMANDS_CAPABILITY, available = false, reason = "connector_offline"),
        )
        assertEquals("connector_offline", sendUnavailableReason(facts, "rt1", "dsh"))
    }
}
