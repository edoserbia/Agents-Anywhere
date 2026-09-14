// Relative imports keep this module loadable by the plain-Node test runner,
// which has no knowledge of the `@/` path alias. The `.ts` extension is
// required because that runner resolves ESM specifiers literally.
import type { ProjectView } from "../../features/dashboard/types"
import { sortDevicesByCreation } from "./device-list-order.ts"

/** Device metadata used to label each project group. */
export type ProjectDeviceInfo = {
  id: string
  name: string
  deviceOs?: string | null
  status?: string
  /** Pairing time; decides the group order so it never follows device activity. */
  createdAt?: string | null
}

export type DeviceProjectGroup = {
  connectorId: string
  deviceName: string
  online: boolean
  projects: ProjectView[]
}

/**
 * Groups projects by their owning device so same-named projects on different
 * machines stay distinguishable.
 *
 * Groups are ordered by when each device was added (see
 * {@link sortDevicesByCreation}), never by activity, so a device that comes
 * online or runs a session keeps its position instead of jumping to the top.
 *
 * Projects whose device is unknown are collected into a trailing group rather
 * than dropped, and input order inside a group is preserved so callers keep
 * their own sorting.
 *
 * Returns an empty list when there are no devices, which lets callers fall back
 * to an ungrouped list.
 */
export function groupProjectsByDevice(
  projects: ProjectView[],
  devices: ProjectDeviceInfo[],
): DeviceProjectGroup[] {
  if (devices.length === 0) return []

  const orderedDevices = sortDevicesByCreation(devices)
  const deviceById = new Map(devices.map((device) => [device.id, device]))
  const groups = new Map<string, DeviceProjectGroup>()

  for (const project of projects) {
    const existing = groups.get(project.connectorId)
    if (existing) {
      existing.projects.push(project)
      continue
    }
    groups.set(project.connectorId, {
      connectorId: project.connectorId,
      // An unknown device has no name, so the raw id is the only label.
      deviceName: deviceById.get(project.connectorId)?.name ?? project.connectorId,
      online: deviceById.get(project.connectorId)?.status === "online",
      projects: [project],
    })
  }

  const ordered = orderedDevices
    .map((device) => groups.get(device.id))
    .filter((group): group is DeviceProjectGroup => Boolean(group))
  const knownIds = new Set(devices.map((device) => device.id))
  const unknown = [...groups.values()].filter((group) => !knownIds.has(group.connectorId))

  return [...ordered, ...unknown]
}
