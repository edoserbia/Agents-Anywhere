/**
 * Timestamp rendering for timeline entries.
 *
 * Every timeline item carries the moment the server first saw it. Showing that
 * lets a reader tell when a reply or an action began, which is otherwise
 * impossible to reconstruct from a transcript that can span hours.
 *
 * The value is rendered as `[YYYY-MM-DD HH:mm:ss]` in the reader's own timezone,
 * matching the bracketed form the timeline uses for other metadata.
 */

/** Format an ISO timestamp as `[YYYY-MM-DD HH:mm:ss]`, or null when unusable. */
export function formatTimelineTimestamp(value: string | null | undefined): string | null {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return null
  return `[${localDateTime(parsed)}]`
}

/**
 * Format a date in local time.
 *
 * Built from the individual fields rather than `toLocaleString`, because the
 * numeric form is stable across locales and the transcript is read as data
 * rather than prose — a reader comparing two timestamps should not have to
 * contend with a locale-specific ordering.
 */
export function localDateTime(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0")
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    ` ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}
