"use client"

import type {
  ProtocolModelCatalog,
  ProtocolModelItem,
  ProtocolPermissionCatalog,
  ProtocolPermissionItem,
  ProtocolReasoningItem,
} from "@/features/dashboard/types"

type CatalogItem = ProtocolModelItem | ProtocolPermissionItem | ProtocolReasoningItem

export function catalogItemEnabled(item: CatalogItem): boolean {
  if (typeof item.enabled === "boolean") return item.enabled
  return typeof item.metadata?.enabled === "boolean" ? item.metadata.enabled : true
}

export function catalogItemDisabledReason(item: CatalogItem): string | null {
  if (typeof item.disabledReason === "string" && item.disabledReason.trim()) {
    return item.disabledReason.trim()
  }
  const metadataReason = item.metadata?.disabledReason
  return typeof metadataReason === "string" && metadataReason.trim()
    ? metadataReason.trim()
    : null
}

const dshPermissionLabelKeys: Record<string, string> = {
  "read-only": "permissionModes.dsh.readOnly.label",
  "workspace-write": "permissionModes.dsh.workspaceWrite.label",
  "danger-full-access": "permissionModes.dsh.fullAccess.label",
}

export function catalogI18nText(
  translate: (key: string) => string,
  metadata: Record<string, unknown> | null | undefined,
  field: "labelKey" | "descriptionKey",
  fallback: string | null | undefined,
): string {
  const i18n = metadata?.i18n
  const preset = metadata?.preset
  const rawKey = (isRecord(i18n) ? i18n[field] : undefined)
    ?? (field === "labelKey" && typeof preset === "string" ? dshPermissionLabelKeys[preset] : undefined)
  if (typeof rawKey !== "string" || !rawKey) return fallback ?? ""
  const key = rawKey.startsWith("dashboard.new.")
    ? rawKey.slice("dashboard.new.".length)
    : rawKey
  try {
    return translate(key)
  } catch {
    return fallback ?? ""
  }
}

export function modelCatalogDisplayName(
  item: ProtocolModelItem,
  models: readonly ProtocolModelItem[],
  label: string,
  defaultReasoningLabel: string,
): string {
  const provider = metadataString(item.metadata, "provider")
  const model = metadataString(item.metadata, "model")
  if (!provider || !model || item.metadata.reasoningEffort !== null) return label

  const hasExplicitReasoningVariant = models.some(
    (candidate) =>
      metadataString(candidate.metadata, "provider") === provider &&
      metadataString(candidate.metadata, "model") === model &&
      typeof candidate.metadata.reasoningEffort === "string" &&
      candidate.metadata.reasoningEffort.length > 0,
  )
  if (!hasExplicitReasoningVariant || label.endsWith(` · ${defaultReasoningLabel}`)) return label
  return `${label} · ${defaultReasoningLabel}`
}

export function selectionIdForModelCatalog(
  catalog: ProtocolModelCatalog | null,
  modelId: string,
  reasoningId: string,
): string | null {
  if (!catalog || !modelId) return null
  const model = catalog.models.find((item) => item.id === modelId)
  if (!model || !catalogItemEnabled(model)) return null
  if (reasoningId) {
    const reasoning = model.reasoningItems.find((item) => item.id === reasoningId)
    return reasoning && catalogItemEnabled(reasoning) ? reasoning.selectionId : null
  }
  return model.selectionId
    ?? model.reasoningItems.find((item) => item.default && catalogItemEnabled(item))?.selectionId
    ?? null
}

export function modelIdsForSelectionId(
  catalog: ProtocolModelCatalog | null,
  selectionId: string | null | undefined,
  includeDisabled = false,
): { modelId: string; reasoningId: string } | null {
  if (!catalog || !selectionId) return null
  for (const model of catalog.models) {
    if (!includeDisabled && !catalogItemEnabled(model)) continue
    if (model.selectionId === selectionId) return { modelId: model.id, reasoningId: "" }
    const reasoning = model.reasoningItems.find(
      (item) => item.selectionId === selectionId && (includeDisabled || catalogItemEnabled(item)),
    )
    if (reasoning) return { modelId: model.id, reasoningId: reasoning.id }
  }
  return null
}

export function selectionIdForPermissionCatalog(
  catalog: ProtocolPermissionCatalog | null,
  permissionId: string,
): string | null {
  if (!catalog || !permissionId) return null
  return catalog.permissions.find(
    (item) => item.id === permissionId && catalogItemEnabled(item),
  )?.selectionId ?? null
}

export function permissionIdForSelectionId(
  catalog: ProtocolPermissionCatalog | null,
  selectionId: string | null | undefined,
  includeDisabled = false,
): string {
  if (!catalog || !selectionId) return ""
  return catalog.permissions.find(
    (item) => item.selectionId === selectionId && (includeDisabled || catalogItemEnabled(item)),
  )?.id ?? ""
}

export type ModelProvider = {
  id: string
  label: string
}

/**
 * The provider serving one model, as the runtime's own catalog reports it.
 *
 * OpenScience serves 146 models from a dozen providers and repeats model names
 * across them — the same "GPT-6.1 Sol" is served by both Command Code Proxy and
 * the AI98 Pro relay. Without the provider a picker cannot tell the two apart,
 * and a flat list of every model is unusable for finding one. A runtime that
 * reports no provider (DSH, Codex) yields an empty id, which callers render as
 * a single ungrouped list.
 */
export function modelProvider(item: ProtocolModelItem): ModelProvider {
  const metadata = isRecord(item.metadata) ? item.metadata : {}
  const id = metadataString(metadata, "providerID") ?? ""
  return { id, label: metadataString(metadata, "providerName") ?? id }
}

export type ModelProviderGroup<T> = {
  id: string
  label: string
  items: T[]
}

/**
 * Group models by provider, keeping catalog order inside each group.
 *
 * Group order follows the catalog too, which already lists every model of one
 * provider together, so a picker reads the same way on every open. A model
 * without a provider falls into its own unlabelled group rather than being
 * mixed into a named one.
 */
export function groupModelsByProvider<T extends { provider: ModelProvider }>(
  items: readonly T[],
): ModelProviderGroup<T>[] {
  const groups: ModelProviderGroup<T>[] = []
  const byId = new Map<string, ModelProviderGroup<T>>()
  for (const item of items) {
    const id = item.provider.id
    let group = byId.get(id)
    if (!group) {
      group = { id, label: item.provider.label, items: [] }
      byId.set(id, group)
      groups.push(group)
    }
    group.items.push(item)
  }
  return groups
}

/**
 * Whether grouping would tell the reader anything.
 *
 * One named provider — or none at all — means every label already carries the
 * whole story, so callers keep the flat list and skip the header.
 */
export function hasModelProviderGrouping<T extends { provider: ModelProvider }>(
  items: readonly T[],
): boolean {
  const named = new Set(
    items.map((item) => item.provider.id).filter((id) => id.length > 0),
  )
  return named.size > 1
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function metadataString(metadata: Record<string, unknown>, key: string): string | null {
  const value = metadata[key]
  return typeof value === "string" && value.length > 0 ? value : null
}
