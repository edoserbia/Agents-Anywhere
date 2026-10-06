package com.agentsanywhere.app.feature.sessions

import com.agentsanywhere.app.feature.devices.DeviceRuntime
import com.agentsanywhere.app.feature.devices.DeviceRuntimeStatus
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A runtime that creates project directories must be recognised before the
 * project form decides whether to ask for a path.
 *
 * OpenScience generates the workspace itself and publishes `project.create` for
 * exactly that reason. Reading the flag from the runtime's own capability set
 * keeps the form honest: the path field disappears only when the runtime the
 * session will use actually owns the directory.
 */
class ProjectCreateCapabilityTest {
    private fun capability(
        supported: Boolean = true,
        available: Boolean = true,
        allowed: Boolean = true,
        runtimeId: String? = "rti_openscience",
        runtimeType: String? = "openscience",
    ) = NewSessionRuntimeCapability(
        capabilityId = PROJECT_CREATE_CAPABILITY,
        version = "1",
        scope = "runtime",
        runtime = runtimeType,
        sessionId = null,
        supported = supported,
        available = available,
        allowed = allowed,
        unavailableReason = null,
        parameters = emptyMap(),
        runtimeId = runtimeId,
        runtimeType = runtimeType,
    )

    private fun runtime(id: String = "rti_openscience", type: String = "openscience") = DeviceRuntime(
        connectorId = "device",
        id = id,
        type = type,
        displayName = "OpenScience",
        present = true,
        configured = true,
        active = true,
        status = DeviceRuntimeStatus.Running,
        discovery = emptyMap(),
        schema = null,
        uiSchema = emptyMap(),
        config = emptyMap(),
        error = null,
        lastDiscoveredAt = null,
        updatedAt = null,
    )

    private fun selection(
        capabilities: List<NewSessionRuntimeCapability>,
        runtime: DeviceRuntime? = runtime(),
    ) = NewSessionRuntimeSelectionState(
        connectorId = "device",
        runtimes = listOfNotNull(runtime),
        selectedRuntimeId = runtime?.id,
        capabilities = NewSessionRemoteData(
            data = NewSessionRuntimeCapabilities(
                connectorId = "device",
                revision = 1,
                capabilities = capabilities,
                serverTime = null,
            ),
            loaded = true,
        ),
    )

    @Test
    fun `a usable project_create capability means the runtime owns the path`() {
        assertTrue(selection(listOf(capability())).runtimeCreatesProject)
    }

    @Test
    fun `an unsupported or unavailable capability leaves the path to the user`() {
        assertFalse(selection(listOf(capability(supported = false))).runtimeCreatesProject)
        assertFalse(selection(listOf(capability(available = false))).runtimeCreatesProject)
        assertFalse(selection(listOf(capability(allowed = false))).runtimeCreatesProject)
    }

    @Test
    fun `another runtime's capability does not apply to the selected one`() {
        val other = capability(runtimeId = "rti_codex", runtimeType = "codex")

        assertFalse(selection(listOf(other)).runtimeCreatesProject)
    }

    @Test
    fun `capabilities that were never loaded leave the path to the user`() {
        val state = NewSessionRuntimeSelectionState(
            connectorId = "device",
            runtimes = listOf(runtime()),
            selectedRuntimeId = "rti_openscience",
        )

        assertFalse(state.runtimeCreatesProject)
    }
}
