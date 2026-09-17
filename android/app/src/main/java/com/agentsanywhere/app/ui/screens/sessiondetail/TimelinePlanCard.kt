package com.agentsanywhere.app.ui.screens.sessiondetail

import androidx.compose.foundation.background
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.agentsanywhere.app.feature.sessiondetail.formatTimelineTimestamp
import com.agentsanywhere.app.ui.designsystem.LocalAAColors
import com.agentsanywhere.app.ui.designsystem.noRippleClickable
import com.composables.icons.lucide.Circle
import com.composables.icons.lucide.ChevronRight
import com.composables.icons.lucide.Circle
import com.composables.icons.lucide.CircleCheck
import com.composables.icons.lucide.ListChecks
import com.composables.icons.lucide.LoaderCircle
import com.composables.icons.lucide.Lucide

/**
 * The agent's plan for the current work.
 *
 * Agents keep a checklist and rewrite it as steps finish, which is the only
 * place the intended shape of a long run is written down. Rendering it as an
 * ordinary tool row buried that: the reader could see that tools ran but not
 * what remained.
 *
 * While the run is in progress the plan is open, because its whole purpose is
 * to answer "what is left". Once the run ends it collapses to a one-line
 * summary, since a finished plan is history rather than status.
 */
@Composable
internal fun TimelinePlanCard(
    plan: TimelinePlanState,
    running: Boolean,
    modifier: Modifier = Modifier,
) {
    val colors = LocalAAColors.current
    val muted = colors.muted
    val surface = colors.sessionTimelineActivitySurface
    val haptic = LocalHapticFeedback.current
    var open by remember(plan.id) { mutableStateOf(running) }

    // Follow the run, but never overrule a choice the reader made.
    var touched by remember(plan.id) { mutableStateOf(false) }
    if (!touched && open != running) open = running

    val allDone = plan.total > 0 && plan.completed == plan.total
    val startedAt = formatTimelineTimestamp(plan.createdAt)

    Column(modifier = modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 34.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(surface)
                .noRippleClickable {
                    haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                    touched = true
                    open = !open
                }
                .padding(horizontal = 6.dp, vertical = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                imageVector = Lucide.ChevronRight,
                contentDescription = null,
                tint = muted,
                modifier = Modifier
                    .rotate(if (open) 90f else 0f)
                    .size(14.dp),
            )
            Icon(
                imageVector = Lucide.ListChecks,
                contentDescription = null,
                tint = muted,
                modifier = Modifier.size(16.dp),
            )
            Column(modifier = Modifier.weight(1f)) {
                if (startedAt != null) {
                    Text(
                        text = startedAt,
                        color = muted,
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Medium,
                    )
                }
                Text(
                    text = if (allDone) {
                        "计划 · ${plan.total} 项已全部完成"
                    } else {
                        "计划 · 已完成 ${plan.completed}/${plan.total}"
                    },
                    color = colors.ink,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium,
                )
            }
        }

        if (open) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(start = 26.dp, top = 4.dp, end = 6.dp, bottom = 2.dp),
                verticalArrangement = Arrangement.spacedBy(2.dp),
            ) {
                plan.steps.forEach { step ->
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.Top,
                    ) {
                        Icon(
                            imageVector = when (step.status) {
                                PlanStatus.COMPLETED -> Lucide.CircleCheck
                                PlanStatus.IN_PROGRESS -> Lucide.LoaderCircle
                                PlanStatus.PENDING -> Lucide.Circle
                            },
                            contentDescription = null,
                            tint = when (step.status) {
                                PlanStatus.COMPLETED -> colors.noticeSuccess
                                PlanStatus.IN_PROGRESS -> colors.sessionStatusAccent
                                PlanStatus.PENDING -> muted
                            },
                            modifier = Modifier
                                .padding(top = 2.dp)
                                .size(14.dp),
                        )
                        Text(
                            text = step.content,
                            color = when (step.status) {
                                PlanStatus.COMPLETED -> muted
                                PlanStatus.IN_PROGRESS -> colors.sessionStatusAccent
                                PlanStatus.PENDING -> muted
                            },
                            fontSize = 12.sp,
                            lineHeight = 16.sp,
                            textDecoration = if (step.status == PlanStatus.COMPLETED) {
                                TextDecoration.LineThrough
                            } else {
                                TextDecoration.None
                            },
                            modifier = Modifier.weight(1f),
                        )
                    }
                }
            }
        }
    }
}
