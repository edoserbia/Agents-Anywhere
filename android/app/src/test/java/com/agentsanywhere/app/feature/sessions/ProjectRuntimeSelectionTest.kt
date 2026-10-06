package com.agentsanywhere.app.feature.sessions

import com.agentsanywhere.app.feature.devices.DeviceRuntime
import com.agentsanywhere.app.feature.devices.DeviceRuntimeList
import com.agentsanywhere.app.feature.devices.DeviceRuntimeStatus
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Which engine a device's runtime inventory selects.
 *
 * "New project" only means something for an engine that can create one: for
 * every other engine the directory *is* the project, so the user is really just
 * picking an existing folder. The project form therefore opens on a
 * project-creating runtime when the device has one, while a plain new session
 * keeps defaulting to the engine it always did — and an engine the user picked
 * by hand still wins in both flows.
 */
class ProjectRuntimeSelectionTest {
    private fun runtime(
        id: String,
        type: String = id,
        name: String = id.replaceFirstChar(Char::uppercase),
        createProject: Boolean? = null,
        status: DeviceRuntimeStatus = DeviceRuntimeStatus.Running,
    ) = DeviceRuntime(
        connectorId = "device",
        id = id,
        type = type,
        displayName = name,
        present = true,
        configured = true,
        active = true,
        status = status,
        discovery = emptyMap(),
        capabilities = createProject?.let { mapOf(PROJECT_CREATE_INVENTORY_FLAG to it) }.orEmpty(),
        schema = null,
        uiSchema = emptyMap(),
        config = emptyMap(),
        error = null,
        lastDiscoveredAt = null,
        updatedAt = null,
    )

    /** The 2080ti device: Codex first in inventory order, OpenScience able to create. */
    private fun device() = DeviceRuntimeList(
        connectorId = "device",
        runtimes = listOf(
            runtime("rti_codex", type = "codex", name = "Codex"),
            runtime("rti_dsh", type = "dsh", name = "DSH"),
            runtime("rti_openscience", type = "openscience", name = "OpenScience", createProject = true),
        ),
        serverTime = null,
    )

    private fun selection(
        runtimes: DeviceRuntimeList = device(),
        selectedRuntimeId: String? = null,
    ) = NewSessionRuntimeSelectionState(
        connectorId = "device",
        selectedRuntimeId = selectedRuntimeId,
    )

    @Test
    fun `creating a project opens on the runtime that owns project creation`() {
        val state = selection().replaceRuntimeInventory(device(), creatingProject = true)

        assertEquals("rti_openscience", state.selectedRuntimeId)
    }

    @Test
    fun `creating a project replaces an engine that merely came first`() {
        val state = selection(selectedRuntimeId = "rti_codex")
            .replaceRuntimeInventory(device(), creatingProject = true)

        assertEquals("rti_openscience", state.selectedRuntimeId)
    }

    @Test
    fun `an engine the user picked by hand still wins`() {
        val state = selection(selectedRuntimeId = "rti_codex")
            .replaceRuntimeInventory(device(), preferredRuntimeId = "rti_codex", creatingProject = true)

        assertEquals("rti_codex", state.selectedRuntimeId)
    }

    @Test
    fun `a plain session keeps its remembered engine and default`() {
        val remembered = selection(selectedRuntimeId = "rti_dsh")
            .replaceRuntimeInventory(device(), preferredRuntimeId = "rti_dsh")
        val defaulted = selection().replaceRuntimeInventory(device())

        assertEquals("rti_dsh", remembered.selectedRuntimeId)
        assertEquals("rti_codex", defaulted.selectedRuntimeId)
    }

    @Test
    fun `several project-creating runtimes are taken in inventory order`() {
        val inventory = DeviceRuntimeList(
            connectorId = "device",
            runtimes = listOf(
                runtime("rti_openscience_a", type = "openscience", name = "OpenScience A", createProject = true),
                runtime("rti_openscience_b", type = "openscience", name = "OpenScience B", createProject = true),
            ),
            serverTime = null,
        )

        val state = selection().replaceRuntimeInventory(inventory, creatingProject = true)

        assertEquals("rti_openscience_a", state.selectedRuntimeId)
    }

    @Test
    fun `a device without a project-creating runtime keeps today's default`() {
        val inventory = DeviceRuntimeList(
            connectorId = "device",
            runtimes = listOf(
                runtime("rti_codex", type = "codex", name = "Codex"),
                runtime("rti_dsh", type = "dsh", name = "DSH"),
            ),
            serverTime = null,
        )

        val state = selection().replaceRuntimeInventory(inventory, creatingProject = true)

        assertEquals("rti_codex", state.selectedRuntimeId)
    }

    @Test
    fun `a stopped project-creating runtime is not selected`() {
        val inventory = DeviceRuntimeList(
            connectorId = "device",
            runtimes = listOf(
                runtime("rti_codex", type = "codex", name = "Codex"),
                runtime("rti_openscience", type = "openscience", createProject = true, status = DeviceRuntimeStatus.Stopped),
            ),
            serverTime = null,
        )

        val state = selection().replaceRuntimeInventory(inventory, creatingProject = true)

        assertEquals("rti_codex", state.selectedRuntimeId)
    }

    @Test
    fun `only the inventory flag marks a runtime as a project creator`() {
        assertTrue(runtime("rti_openscience", createProject = true).createsProject)
        assertFalse(runtime("rti_codex", createProject = false).createsProject)
        assertFalse(runtime("rti_codex").createsProject)
    }

    @Test
    fun `another device's inventory is ignored`() {
        val other = DeviceRuntimeList(connectorId = "other", runtimes = emptyList(), serverTime = null)

        val state = selection(selectedRuntimeId = "rti_codex").replaceRuntimeInventory(other, creatingProject = true)

        assertEquals("rti_codex", state.selectedRuntimeId)
        assertEquals("device", state.connectorId)
    }
}
