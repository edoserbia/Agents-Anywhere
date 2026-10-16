/** History pages count timeline events; the navigator pages user requests. */
export function shouldLoadRequestHistory(options: {
  open: boolean
  entryCount: number
  visibleCount: number
  hasMore: boolean
  loading: boolean
  failed: boolean
}): boolean {
  return options.open && options.entryCount < options.visibleCount && options.hasMore
    && !options.loading && !options.failed
}

/** Stop even if a broken/stale endpoint keeps claiming that it has more. */
export function timelinePageMadeProgress(items: { orderSeq: number }[], beforeOrderSeq: number): boolean {
  return items.some((item) => item.orderSeq < beforeOrderSeq)
}
