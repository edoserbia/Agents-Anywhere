package com.agentsanywhere.app.ui.screens.sessiondetail

import androidx.compose.runtime.Composable
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
    onSelect: (TimelineRequestEntry) -> Unit,
    onDismissRequest: () -> Unit,
) {
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
        entries.forEach { entry ->
            AABottomSheetItem(
                text = entry.text.ifBlank { stringResource(R.string.session_request_history_untitled) },
                supportingText = stringResource(R.string.session_request_history_index, entry.index),
                icon = Lucide.MessageSquare,
                onClick = { onSelect(entry) },
            )
        }
    }
}
