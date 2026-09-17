"use client"

import { ChevronRight, Monitor, MoreHorizontal, Plus } from "lucide-react"
import * as React from "react"
import { ProjectSidebarItem } from "@/components/sidebar/project-sidebar-item"
import {
  groupProjectsByDevice,
  type DeviceProjectGroup,
  type ProjectDeviceInfo,
} from "@/components/sidebar/project-device-groups"
import { SidebarLoadingItem } from "@/components/sidebar/sidebar-loading-item"
import { SidebarSectionTrigger } from "@/components/sidebar/sidebar-section-trigger"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
} from "@/components/ui/sidebar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { WorkspaceSessionView } from "@/components/workspace-context"
import type { ProjectSessionStatusFilter } from "@/components/sidebar/sidebar-selectors"
import type { ProjectView } from "@/features/dashboard/types"
import { cn } from "@/lib/utils"
import { useTranslations } from "next-intl"

export type ProjectListController = {
  sessionsForProject: (
    projectId: string,
    status?: ProjectSessionStatusFilter,
  ) => WorkspaceSessionView[]
  expandedProjectIds: string[]
  activeSessionId: string | null
  onExpandedChange: (projectId: string, open: boolean) => void
  onOpenSession: (sessionId: string) => void
  onNewSession: (projectId: string) => void
  onEdit: (project: ProjectView) => void
  onTogglePin: (project: ProjectView) => void
  onArchiveAll: (project: ProjectView) => void
  onToggleSessionPin: (sessionId: string) => void
  onToggleSessionArchive: (sessionId: string) => void
  onRenameSession: (sessionId: string, title: string) => Promise<boolean>
}

export function ProjectList({
  projects,
  controller,
  sessionStatus = "active",
}: {
  projects: ProjectView[]
  controller: ProjectListController
  sessionStatus?: ProjectSessionStatusFilter
}) {
  return (
    <>
      {projects.map((project) => (
        <ProjectSidebarItem
          key={project.id}
          project={project}
          sessions={controller.sessionsForProject(project.id, sessionStatus)}
          expanded={controller.expandedProjectIds.includes(project.id)}
          activeSessionId={controller.activeSessionId}
          onExpandedChange={(open) => controller.onExpandedChange(project.id, open)}
          onOpenSession={controller.onOpenSession}
          onNewSession={() => controller.onNewSession(project.id)}
          onEdit={() => controller.onEdit(project)}
          onTogglePin={() => controller.onTogglePin(project)}
          onArchiveAll={() => controller.onArchiveAll(project)}
          onToggleSessionPin={controller.onToggleSessionPin}
          onToggleSessionArchive={controller.onToggleSessionArchive}
          onRenameSession={controller.onRenameSession}
        />
      ))}
    </>
  )
}

type DeviceGroup = DeviceProjectGroup

/**
 * One device's projects, under a heading naming the device.
 *
 * Same-named projects on different machines are otherwise indistinguishable, so
 * the device is the grouping key rather than an attribute of each row. The
 * heading carries presence and a project count so a collapsed group still says
 * how much is inside it.
 */
function DeviceGroupHeader({
  group,
  expanded,
  controller,
  sessionStatus,
  onExpandedChange,
}: {
  group: DeviceGroup
  expanded: boolean
  controller: ProjectListController
  sessionStatus: ProjectSessionStatusFilter
  onExpandedChange: (expanded: boolean) => void
}) {
  return (
    <Collapsible open={expanded} onOpenChange={onExpandedChange}>
      <CollapsibleTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-xs font-medium",
            "text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
          )}
        >
          <ChevronRight
            className={cn("size-3 shrink-0 transition-transform", expanded && "rotate-90")}
          />
          <Monitor className="size-3.5 shrink-0" />
          <span className="truncate">{group.deviceName}</span>
          <span
            className={cn(
              "size-1.5 shrink-0 rounded-full",
              group.online ? "bg-emerald-500" : "bg-muted-foreground/40",
            )}
            aria-hidden
          />
          <span className="ml-auto shrink-0 tabular-nums text-[10px] text-muted-foreground/70">
            {group.projects.length}
          </span>
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ProjectList projects={group.projects} controller={controller} sessionStatus={sessionStatus} />
      </CollapsibleContent>
    </Collapsible>
  )
}

type ProjectsSectionProps = {
  projects: ProjectView[]
  isLoading: boolean
  expanded: boolean
  controller: ProjectListController
  sessionStatus: ProjectSessionStatusFilter
  onExpandedChange: (expanded: boolean) => void
  onSessionStatusChange: (status: ProjectSessionStatusFilter) => void
  onAddProject: () => void
  /** Devices used to group projects; grouping is skipped when omitted or empty. */
  devices?: ProjectDeviceInfo[]
}

export function ProjectsSection({
  projects,
  isLoading,
  expanded,
  controller,
  sessionStatus,
  onExpandedChange,
  onSessionStatusChange,
  onAddProject,
  devices = [],
}: ProjectsSectionProps) {
  const t = useTranslations("dashboard")
  // Devices start expanded so a project stays one click away, matching the flat
  // list this section showed before grouping.
  const [collapsedDeviceIds, setCollapsedDeviceIds] = React.useState<string[]>([])

  const groups = React.useMemo(
    () => (devices.length > 0 ? groupProjectsByDevice(projects, devices) : []),
    [devices, projects],
  )
  const useGroups = groups.length > 0
  const [filterOpen, setFilterOpen] = React.useState(false)

  return (
    <SidebarGroup>
      <Collapsible open={expanded} onOpenChange={onExpandedChange}>
        <SidebarGroupLabel
          className="group/projects-label flex items-center justify-between pr-1"
          role="heading"
          aria-level={2}
        >
          <SidebarSectionTrigger label={t("sections.projects")} expanded={expanded} />
          <div className="flex items-center gap-0.5">
            <DropdownMenu open={filterOpen} onOpenChange={setFilterOpen}>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  aria-label={t("projects.filterSessions")}
                  className={cn(
                    "rounded p-0.5 text-muted-foreground opacity-0 transition-[color,background-color,opacity]",
                    "hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                    "group-hover/projects-label:opacity-100",
                    filterOpen && "bg-sidebar-accent text-sidebar-accent-foreground opacity-100",
                  )}
                >
                  <MoreHorizontal className="size-3.5" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" side="right" className="w-56">
                <DropdownMenuLabel className="text-xs text-muted-foreground">
                  {t("projects.sessionStatus")}
                </DropdownMenuLabel>
                <DropdownMenuRadioGroup
                  value={sessionStatus}
                  onValueChange={(value) => {
                    if (value === "active" || value === "archived" || value === "all") {
                      onSessionStatusChange(value)
                    }
                  }}
                >
                  <DropdownMenuRadioItem value="active">
                    <span className="truncate">{t("projects.statusActive")}</span>
                  </DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="archived">
                    <span className="truncate">{t("projects.statusArchived")}</span>
                  </DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="all">
                    <span className="truncate">{t("projects.statusAll")}</span>
                  </DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>

            <TooltipProvider delayDuration={300}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    aria-label={t("projects.add")}
                    onClick={onAddProject}
                    className="rounded p-0.5 text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
                  >
                    <Plus className="size-3.5" />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="top">{t("projects.add")}</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        </SidebarGroupLabel>
        <CollapsibleContent>
          <SidebarGroupContent>
            <SidebarMenu>
              {isLoading ? (
                <SidebarLoadingItem label={t("status.loadingProjects")} />
              ) : projects.length === 0 ? (
                <p className="px-3 py-2 text-xs text-muted-foreground">{t("projects.empty")}</p>
              ) : useGroups ? (
                <div className="flex flex-col gap-0.5">
                  {groups.map((group) => (
                    <DeviceGroupHeader
                      key={group.connectorId}
                      group={group}
                      expanded={!collapsedDeviceIds.includes(group.connectorId)}
                      controller={controller}
                      sessionStatus={sessionStatus}
                      onExpandedChange={(open) => {
                        setCollapsedDeviceIds((current) => open
                          ? current.filter((id) => id !== group.connectorId)
                          : current.includes(group.connectorId)
                            ? current
                            : [...current, group.connectorId])
                      }}
                    />
                  ))}
                </div>
              ) : (
                <ProjectList
                  projects={projects}
                  controller={controller}
                  sessionStatus={sessionStatus}
                />
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </CollapsibleContent>
      </Collapsible>
    </SidebarGroup>
  )
}
