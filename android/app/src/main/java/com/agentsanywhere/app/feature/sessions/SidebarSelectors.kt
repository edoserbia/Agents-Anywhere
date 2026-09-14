package com.agentsanywhere.app.feature.sessions

import com.agentsanywhere.app.model.AgentDevice
import com.agentsanywhere.app.model.AgentProject
import com.agentsanywhere.app.model.AgentSession
import com.agentsanywhere.app.model.SessionStatus
import java.time.Instant

internal fun timestampMillis(value: String?): Long =
    value?.let { runCatching { Instant.parse(it).toEpochMilli() }.getOrDefault(0L) } ?: 0L

fun sessionListComparator(now: Long = System.currentTimeMillis()): Comparator<AgentSession> = Comparator { left, right ->
    val pinned = right.pinned.compareTo(left.pinned)
    val leftRunning = left.status == SessionStatus.Running || left.optimisticTopUntil > now
    val rightRunning = right.status == SessionStatus.Running || right.optimisticTopUntil > now
    when {
        pinned != 0 -> pinned
        leftRunning != rightRunning -> if (leftRunning) -1 else 1
        leftRunning -> left.id.compareTo(right.id)
        else -> timestampMillis(right.sortKey).compareTo(timestampMillis(left.sortKey))
            .takeIf { it != 0 } ?: right.id.compareTo(left.id)
    }
}

fun archivedSessionComparator(): Comparator<AgentSession> =
    compareByDescending<AgentSession> { timestampMillis(it.archivedAt ?: it.sortKey) }.thenBy { it.id }

enum class ProjectSessionStatusFilter(val archiveStates: List<Boolean>) {
    Active(listOf(false)),
    Archived(listOf(true)),
    All(listOf(false, true)),
}

data class ProjectSessionLoadKey(val projectId: String, val archived: Boolean)

fun projectSessionMatchesStatus(session: AgentSession, status: ProjectSessionStatusFilter): Boolean = when (status) {
    ProjectSessionStatusFilter.Active -> !session.archived && !session.pinned
    ProjectSessionStatusFilter.Archived -> session.archived
    ProjectSessionStatusFilter.All -> session.archived || !session.pinned
}

fun projectHasVisibleSessions(
    project: AgentProject,
    sessions: Collection<AgentSession>,
    status: ProjectSessionStatusFilter,
): Boolean = project.manuallyCreated || (project.sidebarSessionCounts?.let { counts ->
    when (status) {
        ProjectSessionStatusFilter.Active -> counts.active > 0
        ProjectSessionStatusFilter.Archived -> counts.archived > 0
        ProjectSessionStatusFilter.All -> counts.active + counts.archived > 0
    }
} ?: sessions.any { it.projectId == project.id && projectSessionMatchesStatus(it, status) })

fun projectHasActiveSessions(project: AgentProject, sessions: Collection<AgentSession>): Boolean =
    projectHasVisibleSessions(project, sessions, ProjectSessionStatusFilter.Active)

fun sortProjectsByActivity(projects: List<AgentProject>, sessions: Collection<AgentSession>): List<AgentProject> {
    val activity = sessions.filter { !it.projectId.isNullOrBlank() }.groupBy { it.projectId }
        .mapValues { (_, items) -> items.maxOf { timestampMillis(it.sortKey) } }
    fun empty(project: AgentProject) = project.manuallyCreated && project.lastActivityAt.isNullOrBlank()
        && project.id !in activity && project.activeSessionCount == 0
        && (project.sidebarSessionCounts?.active ?: 0) == 0 && (project.sidebarSessionCounts?.archived ?: 0) == 0
    return projects.sortedWith(
        compareByDescending<AgentProject> { empty(it) }
            .thenByDescending { maxOf(timestampMillis(it.lastActivityAt), activity[it.id] ?: 0L) }
            .thenByDescending { timestampMillis(it.createdAt) }
            .thenBy { it.name }.thenBy { it.id },
    )
}

/** Projects that live on one device, in the order the project list already uses. */
data class DeviceProjectGroup(
    val connectorId: String,
    val deviceName: String,
    val deviceOs: String?,
    val online: Boolean,
    val projects: List<AgentProject>,
)

/**
 * Orders devices by when they were paired, oldest first, and never by activity.
 *
 * The server lists connectors by `updated_at`, so a device that reports
 * presence, runs a session or reconnects jumps to the top and the list
 * reshuffles. Sorting on [AgentDevice.createdAt] instead pins each device to the
 * position it had when it was added.
 *
 * Devices without a usable timestamp sort last, and the id breaks ties, so the
 * result is total and identical between refreshes.
 */
fun sortDevicesByCreation(devices: List<AgentDevice>): List<AgentDevice> {
    // A missing or unparsable timestamp yields 0, which is pushed to the end so
    // incomplete records never interleave with known positions.
    fun pairingOrder(device: AgentDevice): Long =
        timestampMillis(device.createdAt).takeIf { millis -> millis > 0L } ?: Long.MAX_VALUE

    return devices.sortedWith(
        compareBy<AgentDevice> { pairingOrder(it) }.thenBy { it.id },
    )
}

/**
 * Groups projects by their owning device so same-named projects on different
 * devices stay distinguishable.
 *
 * Groups are ordered by pairing time via [sortDevicesByCreation], never by
 * activity, so a device that comes online keeps its position. Projects whose
 * device is unknown keep a trailing group instead of being dropped. Input order
 * inside each group is preserved, so callers keep their activity-based sorting.
 */
fun groupProjectsByDevice(
    projects: List<AgentProject>,
    devices: List<AgentDevice>,
): List<DeviceProjectGroup> {
    if (projects.isEmpty()) return emptyList()
    val deviceById = devices.associateBy(AgentDevice::id)
    val grouped = LinkedHashMap<String, MutableList<AgentProject>>()
    projects.forEach { project ->
        grouped.getOrPut(project.connectorId) { mutableListOf() }.add(project)
    }

    val ordered = sortDevicesByCreation(devices).mapNotNull { device ->
        grouped[device.id]?.let { members ->
            DeviceProjectGroup(
                connectorId = device.id,
                deviceName = device.name,
                deviceOs = device.deviceOs,
                online = device.online,
                projects = members.toList(),
            )
        }
    }
    val knownIds = devices.mapTo(mutableSetOf(), AgentDevice::id)
    val unknown = grouped.filterKeys { it !in knownIds }.map { (connectorId, members) ->
        DeviceProjectGroup(
            connectorId = connectorId,
            // Fall back to the raw id so an unknown device is still identifiable.
            deviceName = deviceById[connectorId]?.name ?: connectorId,
            deviceOs = null,
            online = false,
            projects = members.toList(),
        )
    }
    return ordered + unknown
}
