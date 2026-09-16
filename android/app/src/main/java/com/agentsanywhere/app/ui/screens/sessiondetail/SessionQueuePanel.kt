package com.agentsanywhere.app.ui.screens.sessiondetail

import androidx.compose.foundation.background
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.agentsanywhere.app.R
import com.agentsanywhere.app.feature.sessiondetail.QueuedMessage
import com.agentsanywhere.app.feature.sessiondetail.SessionMessageQueue
import com.agentsanywhere.app.ui.designsystem.LocalAAColors
import com.agentsanywhere.app.ui.designsystem.noRippleClickable
import com.composables.icons.lucide.Clock
import com.composables.icons.lucide.Lucide
import com.composables.icons.lucide.Pencil
import com.composables.icons.lucide.Trash2

/**
 * Messages waiting for the current turn to finish.
 *
 * Shown above the composer so it is clear that a message was accepted rather
 * than lost. Each pending row can be edited or removed; a row that already
 * failed is reported and can only be dismissed.
 */
@Composable
internal fun SessionQueuePanel(
    queue: SessionMessageQueue,
    onUpdate: (QueuedMessage, String) -> Unit,
    onDelete: (QueuedMessage) -> Unit,
    modifier: Modifier = Modifier,
) {
    if (queue.items.isEmpty()) return
    val colors = LocalAAColors.current
    val shape = RoundedCornerShape(18.dp)

    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 20.dp, vertical = 6.dp)
            .clip(shape)
            .background(colors.raisedSurface)
            .border(1.dp, colors.border, shape)
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                imageVector = Lucide.Clock,
                contentDescription = null,
                tint = colors.muted,
                modifier = Modifier.size(14.dp),
            )
            Text(
                text = stringResource(R.string.session_queue_title, queue.items.size),
                color = colors.muted,
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold,
            )
        }
        queue.items.forEach { item ->
            QueueRow(
                item = item,
                onUpdate = onUpdate,
                onDelete = onDelete,
            )
        }
    }
}

@Composable
private fun QueueRow(
    item: QueuedMessage,
    onUpdate: (QueuedMessage, String) -> Unit,
    onDelete: (QueuedMessage) -> Unit,
) {
    val colors = LocalAAColors.current
    var editing by remember(item.id) { mutableStateOf(false) }
    var draft by remember(item.id, item.content) { mutableStateOf(item.content) }

    if (editing) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            BasicTextField(
                value = draft,
                onValueChange = { draft = it },
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(colors.canvas)
                    .border(1.dp, colors.border, RoundedCornerShape(12.dp))
                    .padding(horizontal = 10.dp, vertical = 8.dp),
                textStyle = TextStyle(
                    color = colors.ink,
                    fontSize = 13.sp,
                    lineHeight = 18.sp,
                ),
                cursorBrush = SolidColor(colors.ink),
                maxLines = 6,
            )
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.End,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                TextButton(onClick = {
                    draft = item.content
                    editing = false
                }) {
                    Text(stringResource(R.string.session_queue_cancel))
                }
                TextButton(
                    onClick = {
                        editing = false
                        onUpdate(item, draft)
                    },
                    enabled = draft.isNotBlank(),
                ) {
                    Text(stringResource(R.string.session_queue_save))
                }
            }
        }
        return
    }

    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Text(
            text = item.content.ifBlank { item.errorMessage.orEmpty() },
            modifier = Modifier.weight(1f).heightIn(max = 96.dp),
            color = if (item.failed) colors.errorText else colors.ink,
            fontSize = 13.sp,
            lineHeight = 18.sp,
            maxLines = 4,
            overflow = TextOverflow.Ellipsis,
        )
        if (item.pending) {
            Icon(
                imageVector = Lucide.Pencil,
                contentDescription = stringResource(R.string.session_queue_edit),
                tint = colors.muted,
                modifier = Modifier
                    .size(18.dp)
                    .noRippleClickable { editing = true },
            )
        }
        Icon(
            imageVector = Lucide.Trash2,
            contentDescription = stringResource(R.string.session_queue_delete),
            tint = colors.muted,
            modifier = Modifier
                .size(18.dp)
                .noRippleClickable { onDelete(item) },
        )
    }
}
