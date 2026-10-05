package com.agentsanywhere.app.ui.screens.sessiondetail

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.res.stringResource
import com.agentsanywhere.app.R
import com.agentsanywhere.app.ui.designsystem.AABottomSheet
import com.agentsanywhere.app.ui.designsystem.AABottomSheetItem
import com.composables.icons.lucide.History
import com.composables.icons.lucide.Lucide
import com.composables.icons.lucide.MessageSquare

/**
 * Navigator for the user requests in one session.
 *
 * Long agent sessions accumulate many turns; reaching an earlier request used
 * to mean scrolling past every folded process block in between. This sheet
 * lists the requests and scrolls the timeline to the one that is picked.
 */
@Composable
internal fun SessionRequestHistorySheet(
    entries: List<TimelineRequestEntry>,
    canLoadMore: Boolean,
    loadingMore: Boolean,
    onLoadMore: () -> Unit,
    onSelect: (TimelineRequestEntry) -> Unit,
    onDismissRequest: () -> Unit,
) {
    var visibleCount by remember { mutableIntStateOf(10) }
    val visibleEntries = entries.asReversed().take(visibleCount)
    val hasLocalOlder = entries.size > visibleCount
    val hasOlder = hasLocalOlder || canLoadMore

    AABottomSheet(
        title = stringResource(R.string.session_request_history_title),
        onDismissRequest = onDismissRequest,
    ) {
        if (entries.isEmpty()) {
            AABottomSheetItem(
                text = stringResource(R.string.session_request_history_empty),
                onClick = {},
                enabled = false,
                icon = Lucide.History,
            )
            return@AABottomSheet
        }
        visibleEntries.forEach { entry ->
            AABottomSheetItem(
                text = entry.text.ifBlank { stringResource(R.string.session_request_history_untitled) },
                supportingText = stringResource(R.string.session_request_history_index, entry.index),
                icon = Lucide.MessageSquare,
                onClick = { onSelect(entry) },
            )
        }
        if (hasOlder) {
            AABottomSheetItem(
                text = stringResource(
                    if (loadingMore) R.string.session_request_history_loading
                    else R.string.session_request_history_more,
                ),
                onClick = {
                    visibleCount += 10
                    if (!hasLocalOlder) onLoadMore()
                },
                enabled = !loadingMore,
                icon = Lucide.History,
            )
        }
    }
}
