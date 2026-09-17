const deviceNameOrder = new Intl.Collator("zh-Hans-CN-u-co-pinyin", {
  usage: "sort",
  sensitivity: "base",
  numeric: true,
})

/** Fixed locale and an ID tie-breaker keep polling and presence changes from moving devices. */
export function sortDevicesByName<T extends { id: string; name: string }>(devices: readonly T[]): T[] {
  return [...devices].sort((left, right) => (
    deviceNameOrder.compare(left.name.trim(), right.name.trim())
    || (left.id < right.id ? -1 : left.id > right.id ? 1 : 0)
  ))
}

/** Millisecond timestamp, or null when the value is missing or unparsable. */
function parseTimestamp(value: string | null | undefined): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

/**
 * Orders devices by when they were added, oldest first, and never by activity.
 *
 * The server returns connectors sorted by `updated_at`, so a device that
 * reports presence, runs a session or reconnects jumps to the top and the list
 * reshuffles under the user. Sorting on `createdAt` instead pins each device to
 * the position it had when it was paired.
 *
 * `createdAt` is the tie-breaker of last resort because the server sorts by
 * `created_at DESC` when `updated_at` ties; reversing it restores addition
 * order. The id keeps the result total and deterministic, so devices can never
 * swap places between polls.
 */
export function sortDevicesByCreation<
  T extends { id: string; createdAt?: string | null },
>(devices: readonly T[]): T[] {
  const createdAt = new Map<T, number | null>()
  for (const device of devices) createdAt.set(device, parseTimestamp(device.createdAt))

  return [...devices].sort((left, right) => {
    const leftAt = createdAt.get(left) ?? null
    const rightAt = createdAt.get(right) ?? null
    // A device with a usable timestamp always outranks one without, so missing
    // data cannot interleave with real positions.
    if (leftAt !== null && rightAt !== null && leftAt !== rightAt) return leftAt - rightAt
    if (leftAt === null && rightAt !== null) return 1
    if (leftAt !== null && rightAt === null) return -1
    return left.id < right.id ? -1 : left.id > right.id ? 1 : 0
  })
}
