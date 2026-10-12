package com.agentsanywhere.app.ui.screens.home

import android.content.SharedPreferences
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.agentsanywhere.app.feature.sessions.ProjectSessionStatusFilter
import org.json.JSONArray

/**
 * The slice of `SharedPreferences` these preferences use.
 *
 * Keeping storage behind an interface lets the persistence rules be unit tested
 * on the JVM, where `SharedPreferences` is unavailable.
 */
internal interface HomePreferenceStore {
    fun getStringSet(key: String, fallback: Set<String>): Set<String>
    fun putStringSet(key: String, value: Set<String>)
    fun getBoolean(key: String, fallback: Boolean): Boolean
    fun putBoolean(key: String, value: Boolean)
    fun getString(key: String): String?
    fun putString(key: String, value: String)
    fun remove(key: String)
}

private class SharedPreferenceStore(private val storage: SharedPreferences) : HomePreferenceStore {
    override fun getStringSet(key: String, fallback: Set<String>): Set<String> =
        storage.getStringSet(key, null)?.toSet() ?: fallback

    override fun putStringSet(key: String, value: Set<String>) {
        storage.edit().putStringSet(key, value).apply()
    }

    override fun getBoolean(key: String, fallback: Boolean): Boolean = storage.getBoolean(key, fallback)

    override fun putBoolean(key: String, value: Boolean) {
        storage.edit().putBoolean(key, value).apply()
    }

    override fun getString(key: String): String? = storage.getString(key, null)

    override fun putString(key: String, value: String) {
        storage.edit().putString(key, value).apply()
    }

    override fun remove(key: String) {
        storage.edit().remove(key).apply()
    }
}

/**
 * How the home lists are folded, remembered per server and account.
 *
 * Every flag here outlives the composable on purpose. The home screen is one
 * destination in an `AnimatedContent`, so opening a session disposes it
 * completely and coming back builds it again from scratch; anything held in a
 * plain `remember` silently returns to its default. Device sections used to be
 * exactly that, so returning from a session re-expanded every device and the
 * user had to fold them again to reach the one they were working in.
 */
internal class HomeProjectPreferences(private val storage: HomePreferenceStore, private val key: String) {
    var expandedIds by mutableStateOf(storage.getStringSet("$key:ids", emptySet()))
        private set
    var projectsExpanded by mutableStateOf(storage.getBoolean("$key:section", true))
        private set
    /**
     * Devices start expanded, so only the collapsed ids are stored. A device
     * that appears later then starts expanded instead of inheriting a fold the
     * user never asked for.
     */
    var collapsedDeviceIds by mutableStateOf(storage.getStringSet("$key:devices", emptySet()))
        private set
    var pinnedExpanded by mutableStateOf(storage.getBoolean("$key:pinned", true))
        private set
    /**
     * The flat session list folds its own pinned and recent sections. They are
     * separate keys from the project list's pinned section: the two views show
     * different rows under the same label.
     */
    var sessionListPinnedExpanded by mutableStateOf(storage.getBoolean("$key:sessions-pinned", true))
        private set
    var sessionListRecentExpanded by mutableStateOf(storage.getBoolean("$key:sessions-recent", true))
        private set
    var sessionStatus by mutableStateOf(
        ProjectSessionStatusFilter.entries.firstOrNull { it.name == storage.getString("$key:status") }
            ?: ProjectSessionStatusFilter.Active,
    )
        private set
    var selectedDeviceId by mutableStateOf(storage.getString("$key:device")?.takeIf(String::isNotBlank))
        private set
    var selectedAgentRuntime by mutableStateOf(storage.getString("$key:agent")?.takeIf(String::isNotBlank))
        private set

    fun selectSessionStatus(status: ProjectSessionStatusFilter) {
        sessionStatus = status
        storage.putString("$key:status", status.name)
    }

    fun selectDevice(id: String?) {
        selectedDeviceId = id
        if (id == null) storage.remove("$key:device") else storage.putString("$key:device", id)
    }

    fun selectAgent(runtime: String?) {
        selectedAgentRuntime = runtime
        if (runtime == null) storage.remove("$key:agent") else storage.putString("$key:agent", runtime)
    }

    fun clearFilters() {
        sessionStatus = ProjectSessionStatusFilter.Active
        selectedDeviceId = null
        selectedAgentRuntime = null
        storage.putString("$key:status", ProjectSessionStatusFilter.Active.name)
        storage.remove("$key:device")
        storage.remove("$key:agent")
    }

    fun setProjectExpanded(id: String, expanded: Boolean) {
        expandedIds = if (expanded) expandedIds + id else expandedIds - id
        storage.putStringSet("$key:ids", expandedIds)
    }

    fun setDeviceCollapsed(connectorId: String, collapsed: Boolean) {
        collapsedDeviceIds = if (collapsed) collapsedDeviceIds + connectorId else collapsedDeviceIds - connectorId
        storage.putStringSet("$key:devices", collapsedDeviceIds)
    }

    fun togglePinnedSection() {
        pinnedExpanded = !pinnedExpanded
        storage.putBoolean("$key:pinned", pinnedExpanded)
    }

    fun toggleSessionListPinned() {
        sessionListPinnedExpanded = !sessionListPinnedExpanded
        storage.putBoolean("$key:sessions-pinned", sessionListPinnedExpanded)
    }

    fun toggleSessionListRecent() {
        sessionListRecentExpanded = !sessionListRecentExpanded
        storage.putBoolean("$key:sessions-recent", sessionListRecentExpanded)
    }

    fun toggleSection() {
        projectsExpanded = !projectsExpanded
        storage.putBoolean("$key:section", projectsExpanded)
    }
}

@Composable
internal fun rememberHomeProjectPreferences(serverUrl: String, userId: String): HomeProjectPreferences {
    val context = LocalContext.current
    return remember(context, serverUrl, userId) {
        HomeProjectPreferences(
            SharedPreferenceStore(context.getSharedPreferences("project_sidebar", 0)),
            JSONArray(listOf(serverUrl.trim().trimEnd('/'), userId)).toString(),
        )
    }
}
