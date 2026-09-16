package com.agentsanywhere.app.feature.sessiondetail

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression guard built from the live server payload for a DSH session.
 *
 * Captured from `/sessions/{id}/snapshot` on the deployed server: takeover is
 * on, `session.send_message` is supported/available/allowed, and
 * `session.steer` is unsupported. Before the fix the composer showed
 * "This runtime cannot send messages" for exactly this payload.
 */
class LivePayloadTest {
    private val runtimeId = "rti_O9YH4FA7EMhem7R_"

    private fun capability(
        id: String,
        supported: Boolean,
        available: Boolean,
        allowed: Boolean,
        reason: String?,
    ) = EffectiveCapability(
        capabilityId = id,
        version = "1",
        scope = "session",
        runtime = "dsh",
        sessionId = "sess_dsh_c4a783dd2e08b7d0eb020e31",
        supported = supported,
        available = available,
        allowed = allowed,
        unavailableReason = reason,
        parameters = emptyMap(),
        runtimeId = runtimeId,
        runtimeType = "dsh",
    )

    private val live = EffectiveCapabilities(
        revision = 5,
        capabilities = listOf(
            capability("session.send_message", true, true, true, null),
            capability("session.steer", false, false, false, "runtime_capability_unsupported"),
            capability("session.interrupt", true, true, true, null),
        ),
        connectorId = "conn_mcFSUxwkhrkT8g",
        serverTime = "2026-09-16T05:00:00Z",
        isLoaded = true,
    )

    @Test
    fun `the composer can send on the live DSH payload`() {
        assertTrue(live.isUsable("session.send_message", runtimeId, "dsh"))
    }

    @Test
    fun `the composer is not blocked on an idle live DSH session`() {
        assertFalse(
            runtimeBlocksComposerSubmission(
                SessionRuntimeStatus.Idle,
                canSteer = false,
                canSendMessage = true,
            ),
        )
    }

    @Test
    fun `the composer is not blocked on a running live DSH session`() {
        // DSH cannot steer, but it can send, so the message is queued.
        assertFalse(
            runtimeBlocksComposerSubmission(
                SessionRuntimeStatus.Running,
                canSteer = false,
                canSendMessage = true,
            ),
        )
    }

    @Test
    fun `a running live DSH session queues rather than steers`() {
        assertTrue(runtimeQueuesWhileRunning(SessionRuntimeStatus.Running, canSteer = false, canSendMessage = true))
    }

    @Test
    fun `the reported reason names the steer gap, not send`() {
        // The only reason carried by this payload belongs to session.steer.
        assertEquals("runtime_capability_unsupported", sendUnavailableReason(live, runtimeId, "dsh"))
    }

    @Test
    fun `the message action on a running live DSH session is Send`() {
        assertEquals(RuntimeMessageAction.Send, live.messageAction(runtimeId, SessionRuntimeStatus.Running, "dsh"))
    }
}
