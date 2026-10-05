package com.agentsanywhere.app.ui.screens.home

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** In-memory stand-in for `SharedPreferences`, so the rules run on the JVM. */
private class FakeStore : HomePreferenceStore {
    private val strings = mutableMapOf<String, String>()
    private val stringSets = mutableMapOf<String, Set<String>>()
    private val booleans = mutableMapOf<String, Boolean>()

    override fun getStringSet(key: String, fallback: Set<String>): Set<String> = stringSets[key] ?: fallback

    override fun putStringSet(key: String, value: Set<String>) {
        stringSets[key] = value
    }

    override fun getBoolean(key: String, fallback: Boolean): Boolean = booleans[key] ?: fallback

    override fun putBoolean(key: String, value: Boolean) {
        booleans[key] = value
    }

    override fun getString(key: String): String? = strings[key]

    override fun putString(key: String, value: String) {
        strings[key] = value
    }
}

/**
 * Covers which parts of the home list survive leaving the screen.
 *
 * The home screen is a single destination in an `AnimatedContent`, so opening a
 * session disposes it and returning rebuilds it. Device sections were folded
 * with a plain `remember`, so every device came back expanded and the user had
 * to fold them again to reach the device they were working in. These cases pin
 * the state to storage instead.
 */
class HomeProjectPreferencesTest {
    private val key = """["https://closex.cc","usr_1"]"""

    private fun reopened(store: HomePreferenceStore) = HomeProjectPreferences(store, key)

    @Test
    fun `collapsed devices survive returning from a session`() {
        val store = FakeStore()
        reopened(store).setDeviceCollapsed("conn_4", true)
        reopened(store).setDeviceCollapsed("conn_1", true)

        assertEquals(setOf("conn_1", "conn_4"), reopened(store).collapsedDeviceIds)
    }

    @Test
    fun `devices start expanded and a new device never inherits a fold`() {
        val store = FakeStore()
        assertTrue(reopened(store).collapsedDeviceIds.isEmpty())

        reopened(store).setDeviceCollapsed("conn_1", true)
        assertFalse("conn_2" in reopened(store).collapsedDeviceIds)

        reopened(store).setDeviceCollapsed("conn_1", false)
        assertTrue(reopened(store).collapsedDeviceIds.isEmpty())
    }

    @Test
    fun `expanded projects and the pinned section survive returning from a session`() {
        val store = FakeStore()
        reopened(store).setProjectExpanded("proj_1", true)
        reopened(store).setProjectExpanded("proj_2", true)
        reopened(store).setProjectExpanded("proj_1", false)
        reopened(store).togglePinnedSection()

        val preferences = reopened(store)
        assertEquals(setOf("proj_2"), preferences.expandedIds)
        assertFalse(preferences.pinnedExpanded)
    }

    @Test
    fun `the projects section keeps its own fold`() {
        val store = FakeStore()
        reopened(store).toggleSection()

        assertFalse(reopened(store).projectsExpanded)
    }

    @Test
    fun `each server and account keeps its own folds`() {
        val store = FakeStore()
        reopened(store).setDeviceCollapsed("conn_1", true)
        reopened(store).toggleSection()

        val other = HomeProjectPreferences(store, """["https://other.example","usr_1"]""")
        assertTrue(other.collapsedDeviceIds.isEmpty())
        assertTrue(other.projectsExpanded)
    }
}
