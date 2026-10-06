import { isApiError } from "../../lib/api/errors.ts"
import type { DeviceRuntimeView, ProjectCreateRequest, ProjectView } from "./types"

/**
 * True when a runtime generates project directories itself.
 *
 * The flag is the runtime's own promise, published as `createProject` in its
 * inventory capabilities: a client must not ask the user for a path the runtime
 * is going to choose anyway.
 */
export function runtimeCreatesProject(
  runtime: Pick<DeviceRuntimeView, "capabilities"> | null | undefined,
): boolean {
  return runtime?.capabilities?.createProject === true
}

/**
 * The runtime that will create this project's directory, if one will.
 *
 * A named runtime decides on its own: a device may host both a runtime that
 * generates paths and one that needs a path, and the form has to follow the
 * runtime the session will actually use. With no named runtime the device's
 * active path-creating runtime answers instead, which is the only honest answer
 * for a form that has no agent picker — and it must already be active, because
 * a stopped runtime cannot create anything.
 */
export function projectCreatingRuntime(
  runtimes: DeviceRuntimeView[] | undefined,
  runtimeId?: string | null,
): DeviceRuntimeView | null {
  const candidates = (runtimes ?? []).filter(runtimeCreatesProject)
  if (runtimeId) {
    return candidates.find((runtime) => runtime.runtimeId === runtimeId) ?? null
  }
  return candidates.find((runtime) => runtime.active) ?? null
}

/**
 * Build the create request for whichever side owns the workspace.
 *
 * Exactly one source is sent: a runtime id when the runtime generates the path,
 * otherwise the path the user chose. Sending both is rejected by the server, so
 * this is the single place that decides.
 */
export function projectCreateRequest({
  name,
  connectorId,
  workspacePath,
  runtimeId,
}: {
  name: string
  connectorId: string
  workspacePath?: string | null
  runtimeId?: string | null
}): ProjectCreateRequest {
  if (runtimeId) return { name, connectorId, runtimeId }
  return { name, connectorId, workspacePath: (workspacePath ?? "").trim() }
}

export function isWindowsWorkspace(path: string, deviceOs?: string | null): boolean {
  return deviceOs === "windows" || /^[a-z]:[\\/]/i.test(path) || path.startsWith("\\\\")
}

export function workspacePathKey(path: string, deviceOs?: string | null): string {
  const trimmed = path.trim()
  const windows = isWindowsWorkspace(trimmed, deviceOs)
  const slashes = windows ? trimmed.replaceAll("\\", "/") : trimmed
  const parts: string[] = []
  for (const part of slashes.split("/")) {
    if (!part || part === ".") continue
    if (!windows && part === "..") parts.pop()
    else parts.push(part)
  }
  const prefix = slashes.startsWith("//") && !slashes.startsWith("///") ? "//" : slashes.startsWith("/") ? "/" : ""
  const key = prefix + parts.join("/")
  return windows ? key.toLowerCase() : key
}

export function workspaceName(path: string, fallback = "Workspace"): string {
  const trimmed = path.trim()
  const windows = isWindowsWorkspace(trimmed)
  const normalized = windows ? trimmed.replaceAll("\\", "/") : trimmed
  const clean = normalized.replace(/\/+$/, "")
  if (windows && (/^[a-z]:$/i.test(clean) || /^\/\/[^/]+\/[^/]+$/.test(clean))) return fallback
  return clean.split("/").at(-1) || fallback
}

export function availableProjectName(name: string, projects: Pick<ProjectView, "id" | "name">[], ignoreId?: string): string {
  const base = Array.from(name.trim() || "Workspace").slice(0, 255).join("")
  const names = new Set(projects.filter((project) => project.id !== ignoreId).map((project) => project.name))
  let candidate = base
  for (let suffix = 1; names.has(candidate); suffix++) {
    const ending = ` (${suffix})`
    candidate = Array.from(base).slice(0, 255 - ending.length).join("") + ending
  }
  return candidate
}

export function findWorkspaceProject(
  projects: ProjectView[], connectorId: string, path: string, deviceOs?: string | null,
): ProjectView | undefined {
  const key = workspacePathKey(path, deviceOs)
  if (!key) return undefined
  return projects.find((project) => project.connectorId === connectorId
    && workspacePathKey(project.workspacePath, deviceOs) === key)
}

export async function resolveWorkspaceProject({
  projects, connectorId, path, deviceOs, list, create,
}: {
  projects: ProjectView[]
  connectorId: string
  path: string
  deviceOs?: string | null
  list: () => Promise<ProjectView[]>
  create: (payload: ProjectCreateRequest) => Promise<ProjectView>
}): Promise<ProjectView> {
  if (!connectorId || !path.trim()) throw new Error("A device and workspace are required")
  const existing = findWorkspaceProject(projects, connectorId, path, deviceOs)
  if (existing) return existing

  let currentProjects = await list()
  for (let attempt = 0; ; attempt++) {
    const current = findWorkspaceProject(currentProjects, connectorId, path, deviceOs)
    if (current) return current
    try {
      return await create({
        name: availableProjectName(workspaceName(path), currentProjects),
        connectorId,
        workspacePath: path.trim(),
        manuallyCreated: false,
      })
    } catch (error) {
      if (attempt >= 2 || !isApiError(error) || error.status !== 409 || error.code !== "project_name_conflict") throw error
      // Another client may have created this workspace or claimed its name.
      currentProjects = await list()
    }
  }
}
