package com.agentsanywhere.app.app

import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasScrollToIndexAction
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToIndex
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.agentsanywhere.app.feature.sessions.ProjectSessionStatusFilter
import com.agentsanywhere.app.model.AgentDevice
import com.agentsanywhere.app.model.AgentProject
import com.agentsanywhere.app.navigation.AppDestination
import com.agentsanywhere.app.ui.screens.home.HomePreferenceStore
import com.agentsanywhere.app.ui.screens.home.HomeProjectList
import com.agentsanywhere.app.ui.screens.home.HomeProjectPreferences
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** In-memory `SharedPreferences` stand-in, so the test needs no device storage. */
private class FakeStore : HomePreferenceStore {
    private val booleans = mutableMapOf<String, Boolean>()
    private val sets = mutableMapOf<String, Set<String>>()

    override fun getStringSet(key: String, fallback: Set<String>): Set<String> = sets[key] ?: fallback

    override fun putStringSet(key: String, value: Set<String>) {
        sets[key] = value
    }

    override fun getBoolean(key: String, fallback: Boolean): Boolean = booleans[key] ?: fallback

    override fun putBoolean(key: String, value: Boolean) {
        booleans[key] = value
    }

    override fun getString(key: String): String? = null

    override fun putString(key: String, value: String) = Unit
}

/**
 * Drives the real project list through the app's navigation pattern.
 *
 * The home screen is disposed whenever a session is opened, so both the folds
 * and the scroll offset must be restored by something outside it. The folds live
 * in `HomeProjectPreferences`; the scroll offset is a `rememberSaveable` inside
 * the list and needs the `SaveableStateHolder` the nav host now provides. Before
 * that holder existed every device came back expanded and the list came back at
 * the top.
 */
@RunWith(AndroidJUnit4::class)
class DestinationStateRestorationTest {
    @get:Rule
    val compose = createComposeRule()

    private val store = FakeStore()
    private val preferencesKey = """["https://closex.cc","usr_1"]"""

    private fun project(id: String, name: String, connectorId: String) = AgentProject(
        id = id,
        userId = "usr_1",
        connectorId = connectorId,
        name = name,
        workspacePath = "/workspace/$id",
        pinned = false,
        pinnedAt = null,
        activeSessionCount = 0,
        lastActivityAt = "2026-10-05T10:00:00Z",
        createdAt = "2026-10-01T10:00:00Z",
        updatedAt = "2026-10-05T10:00:00Z",
    )

    private fun device(id: String, name: String) = AgentDevice(
        id = id,
        name = name,
        online = true,
        createdAt = "2026-10-01T00:00:00Z",
    )

    /**
     * `destination` is read inside the composition, so the test swaps screens
     * exactly the way the nav host's `AnimatedContent` does.
     */
    private fun showDestination(
        destination: () -> AppDestination,
        projects: List<AgentProject>,
        devices: List<AgentDevice>,
    ) {
        compose.setContent {
            val current = destination()
            val holder = rememberSaveableStateHolder()
            RestorableDestination(current, null, null, holder) {
                if (current == AppDestination.Sessions) {
                    HomeProjectList(
                        projects = projects,
                        hasProjectsInOtherStatuses = false,
                        allSessions = emptyList(),
                        // Mirrors `HomeScreen`: the preferences are rebuilt from
                        // storage on every return, which is what makes the fold
                        // outlive the destination.
                        projectPreferences = remember { HomeProjectPreferences(store, preferencesKey) },
                        projectSessionStatus = ProjectSessionStatusFilter.Active,
                        onProjectSessionStatusChange = {},
                        projectErrors = emptyMap(),
                        onRetryProject = {},
                        onCreateProject = {},
                        pinnedSessions = emptyList(),
                        sessionsByProject = emptyMap(),
                        loadingProjectIds = emptySet(),
                        expandedProjectIds = emptySet(),
                        onProjectExpandedChange = { _, _ -> },
                        onProjectMenu = {},
                        onNewSession = {},
                        onSessionLongPress = { _, _ -> },
                        onOpenSession = {},
                        devices = devices,
                    )
                } else {
                    Text("session detail")
                }
            }
        }
    }

    @Test
    fun collapsedDeviceStaysCollapsedAfterOpeningASession() {
        var destination by mutableStateOf(AppDestination.Sessions)
        showDestination(
            destination = { destination },
            projects = listOf(
                project("p1", "Alpha project", "conn_1"),
                project("p2", "Beta project", "conn_2"),
            ),
            devices = listOf(device("conn_1", "Device One"), device("conn_2", "Device Two")),
        )

        compose.onNodeWithText("Alpha project").assertIsDisplayed()
        compose.onNodeWithText("Beta project").assertIsDisplayed()

        // Fold the first device, as the user does before opening a session.
        compose.onNodeWithText("Device One").performClick()
        compose.onNodeWithText("Alpha project").assertDoesNotExist()
        compose.onNodeWithText("Beta project").assertIsDisplayed()

        compose.runOnIdle { destination = AppDestination.SessionDetail }
        compose.onNodeWithText("session detail").assertIsDisplayed()
        compose.runOnIdle { destination = AppDestination.Sessions }

        // Before the fix every device came back expanded and this failed.
        compose.onNodeWithText("Beta project").assertIsDisplayed()
        compose.onNodeWithText("Alpha project").assertDoesNotExist()
    }

    @Test
    fun projectListKeepsItsScrollPositionAfterOpeningASession() {
        var destination by mutableStateOf(AppDestination.Sessions)
        showDestination(
            destination = { destination },
            projects = (0 until 40).map { index ->
                project("p$index", "Project %02d".format(index), "conn_1")
            },
            devices = listOf(device("conn_1", "Device One")),
        )

        compose.onNode(hasScrollToIndexAction()).performScrollToIndex(35)
        compose.onNodeWithText("Project 35").assertIsDisplayed()
        compose.onNodeWithText("Project 00").assertDoesNotExist()

        compose.runOnIdle { destination = AppDestination.SessionDetail }
        compose.onNodeWithText("session detail").assertIsDisplayed()
        compose.runOnIdle { destination = AppDestination.Sessions }

        // Without the holder the list is rebuilt at the top, so "Project 00"
        // would be visible again and "Project 35" would be gone.
        compose.onNodeWithText("Project 35").assertIsDisplayed()
        compose.onNodeWithText("Project 00").assertDoesNotExist()
    }
}
