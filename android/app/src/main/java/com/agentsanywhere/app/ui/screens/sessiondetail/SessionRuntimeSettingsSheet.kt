package com.agentsanywhere.app.ui.screens.sessiondetail

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.agentsanywhere.app.R
import com.agentsanywhere.app.feature.sessiondetail.RuntimeSelectionOption
import com.agentsanywhere.app.ui.designsystem.AABottomSheet
import com.agentsanywhere.app.ui.designsystem.AABottomSheetColors
import com.agentsanywhere.app.ui.designsystem.AABottomSheetDefaults
import com.agentsanywhere.app.ui.designsystem.AABottomSheetItem
import com.agentsanywhere.app.ui.designsystem.DownGlyph
import com.agentsanywhere.app.ui.designsystem.ForwardGlyph
import com.agentsanywhere.app.ui.designsystem.noRippleClickable

private enum class RuntimeSettingsPage {
    Model,
    ModeEffort,
}

internal data class ModelOptionGroup(
    val label: String,
    val options: List<RuntimeSelectionOption>,
)

/**
 * One provider and the models it serves.
 *
 * OpenScience serves 146 models from a dozen providers, and the same model name
 * appears under several of them, so a flat list of model names cannot answer
 * "which provider offers this?". A runtime that names no provider keeps the
 * flat list, because a header there would repeat what every label already says.
 */
internal data class ProviderOptionGroup(
    val label: String?,
    val models: List<ModelOptionGroup>,
)

internal val RuntimeSelectionOption.modelLabel: String
    get() = modelName.ifBlank { label.substringBefore(" · ") }.ifBlank { label }

internal val RuntimeSelectionOption.effortLabelText: String?
    get() = effortLabel?.takeIf(String::isNotBlank)
        ?: label.substringAfter(" · ", "").takeIf(String::isNotBlank)

internal fun RuntimeSelectionOption.effortDisplayLabel(defaultLabel: String): String =
    effortLabelText ?: defaultLabel

internal fun List<RuntimeSelectionOption>.groupByModelLabel(): List<ModelOptionGroup> =
    groupBy(RuntimeSelectionOption::modelLabel).map { (label, options) -> ModelOptionGroup(label, options) }

internal fun List<RuntimeSelectionOption>.groupByProvider(): List<ProviderOptionGroup> {
    val providers = mapNotNull(RuntimeSelectionOption::providerLabel).distinct()
    if (providers.size <= 1) return listOf(ProviderOptionGroup(null, groupByModelLabel()))
    // Providers first, then the models inside each one: grouping by model name
    // across the whole catalog would merge two providers that serve the same
    // model into one row, which is the confusion this is here to remove.
    return groupBy(RuntimeSelectionOption::providerLabel)
        .map { (provider, options) -> ProviderOptionGroup(provider, options.groupByModelLabel()) }
}

@Composable
internal fun SessionRuntimeSettingsSheet(
    runtimeLabel: String,
    modelOptions: List<RuntimeSelectionOption>,
    permissionOptions: List<RuntimeSelectionOption>,
    selectedModelId: String?,
    selectedPermissionId: String?,
    modelLoading: Boolean,
    permissionLoading: Boolean,
    modelErrorMessage: String?,
    permissionErrorMessage: String?,
    busy: Boolean,
    onDismiss: () -> Unit,
    onRetryModels: () -> Unit,
    onRetryPermissions: () -> Unit,
    onSelectModel: (String) -> Unit,
    onSelectPermission: (String) -> Unit,
) {
    var page by remember(runtimeLabel) { mutableStateOf(RuntimeSettingsPage.Model) }
    val palette = AABottomSheetDefaults.colors()
    val groupedModels = remember(modelOptions) { modelOptions.groupByModelLabel() }
    val providerGroups = remember(modelOptions) { modelOptions.groupByProvider() }
    val selectedModelGroup = groupedModels.firstOrNull { group ->
        group.options.any { it.selectionId == selectedModelId }
    } ?: groupedModels.firstOrNull()
    val selectedProviderLabel = remember(selectedModelId, modelOptions) {
        modelOptions.firstOrNull { it.selectionId == selectedModelId }?.providerLabel
    }
    val collapsedProviders = remember(runtimeLabel) { mutableStateMapOf<String, Boolean>() }
    val providerExpanded: (String?) -> Boolean = { provider ->
        provider == null || collapsedProviders[provider]?.let { !it } ?: (provider == selectedProviderLabel)
    }
    val selectedPermissionLabel = permissionOptions
        .firstOrNull { it.selectionId == selectedPermissionId }
        ?.label
    val defaultEffortLabel = stringResource(R.string.session_runtime_effort_default)
    val selectedModelOption = selectedModelGroup
        ?.options
        ?.firstOrNull { it.selectionId == selectedModelId }
    val selectedEffortLabel = selectedModelOption?.effortDisplayLabel(defaultEffortLabel)

    AABottomSheet(
        title = stringResource(
            if (page == RuntimeSettingsPage.Model) R.string.session_runtime_select_model else R.string.session_runtime_mode_effort,
        ),
        onDismissRequest = onDismiss,
        dismissEnabled = !busy,
        onBack = if (page == RuntimeSettingsPage.ModeEffort) ({ page = RuntimeSettingsPage.Model }) else null,
    ) {
        when (page) {
            RuntimeSettingsPage.Model -> ModelPage(
                groups = groupedModels,
                providerGroups = providerGroups,
                providerExpanded = providerExpanded,
                onToggleProvider = { provider ->
                    // The map is the override, so a first tap on a folded
                    // provider must record "collapsed = false", not repeat the
                    // default that folded it.
                    collapsedProviders[provider] = providerExpanded(provider)
                },
                selectedId = selectedModelId,
                permissionLabel = selectedPermissionLabel,
                effortLabel = selectedEffortLabel,
                loading = modelLoading,
                errorMessage = modelErrorMessage,
                busy = busy,
                palette = palette,
                onRetry = onRetryModels,
                onOpenModeEffort = { page = RuntimeSettingsPage.ModeEffort },
                onSelect = onSelectModel,
            )
            RuntimeSettingsPage.ModeEffort -> ModeEffortPage(
                runtimeLabel = runtimeLabel,
                selectedModelGroup = selectedModelGroup,
                selectedModelId = selectedModelId,
                permissionOptions = permissionOptions,
                selectedPermissionId = selectedPermissionId,
                modelLoading = modelLoading,
                permissionLoading = permissionLoading,
                permissionErrorMessage = permissionErrorMessage,
                busy = busy,
                palette = palette,
                onRetryPermissions = onRetryPermissions,
                onSelectModel = onSelectModel,
                onSelectPermission = onSelectPermission,
            )
        }
    }
}

@Composable
private fun ModelPage(
    groups: List<ModelOptionGroup>,
    providerGroups: List<ProviderOptionGroup>,
    providerExpanded: (String?) -> Boolean,
    onToggleProvider: (String) -> Unit,
    selectedId: String?,
    permissionLabel: String?,
    effortLabel: String?,
    loading: Boolean,
    errorMessage: String?,
    busy: Boolean,
    palette: AABottomSheetColors,
    onRetry: () -> Unit,
    onOpenModeEffort: () -> Unit,
    onSelect: (String) -> Unit,
) {
    ModelOptions(
        groups = groups,
        providerGroups = providerGroups,
        providerExpanded = providerExpanded,
        onToggleProvider = onToggleProvider,
        selectedId = selectedId,
        loading = loading,
        errorMessage = errorMessage,
        busy = busy,
        palette = palette,
        onRetry = onRetry,
        onSelect = onSelect,
    )

    DividerLine(palette.divider)
    AABottomSheetItem(
        text = stringResource(R.string.session_runtime_mode_effort),
        supportingText = listOfNotNull(permissionLabel, effortLabel).joinToString(" · ")
            .ifBlank { stringResource(R.string.session_runtime_no_settings) },
        enabled = !busy,
        showChevron = true,
        onClick = onOpenModeEffort,
    )
}

@Composable
private fun ModelOptions(
    groups: List<ModelOptionGroup>,
    providerGroups: List<ProviderOptionGroup>,
    providerExpanded: (String?) -> Boolean,
    onToggleProvider: (String) -> Unit,
    selectedId: String?,
    loading: Boolean,
    errorMessage: String?,
    busy: Boolean,
    palette: AABottomSheetColors,
    onRetry: () -> Unit,
    onSelect: (String) -> Unit,
) {
    val rows = groups.map { group ->
        val current = group.options.firstOrNull { it.selectionId == selectedId }
        val option = current
            ?: group.options.firstOrNull { it.enabled && it.default }
            ?: group.options.firstOrNull { it.enabled }
            ?: group.options.first()
        group to option.copy(label = option.modelLabel)
    }
    val selectedIds = groups
        .firstOrNull { group -> group.options.any { option -> option.selectionId == selectedId } }
        ?.options
        ?.mapTo(mutableSetOf()) { it.selectionId }
        .orEmpty()
    val modelRow: @Composable (RuntimeSelectionOption, Modifier) -> Unit = { option, modifier ->
        AABottomSheetItem(
            text = option.label,
            supportingText = option.disabledReason,
            selected = option.selectionId == selectedId || option.selectionId in selectedIds,
            enabled = !busy && !loading && option.enabled,
            modifier = modifier,
            onClick = { onSelect(option.selectionId) },
        )
    }
    val grouped = providerGroups.any { it.label != null }

    when {
        loading && rows.isEmpty() -> SheetLoading(palette = palette)
        errorMessage != null && rows.isEmpty() -> SheetError(
            message = errorMessage,
            retryEnabled = !busy && !loading,
            onRetry = onRetry,
        )
        rows.isEmpty() -> SheetEmpty(palette = palette)
        else -> Column(
            modifier = Modifier.fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            if (!grouped) {
                rows.forEach { (_, option) -> modelRow(option, Modifier) }
                return@Column
            }
            providerGroups.forEach { provider ->
                val label = provider.label
                if (label == null) {
                    // A runtime that named some providers but not others keeps
                    // the unnamed ones visible, just without a header to fold.
                    rows.filter { (group, _) -> provider.models.any { it === group } }
                        .forEach { (_, option) -> modelRow(option, Modifier) }
                    return@forEach
                }
                val expanded = providerExpanded(label)
                ProviderSectionHeader(
                    label = label,
                    count = provider.models.sumOf { it.options.size },
                    expanded = expanded,
                    palette = palette,
                    onClick = { onToggleProvider(label) },
                )
                if (!expanded) return@forEach
                rows.filter { (group, _) -> provider.models.any { it === group } }
                    .forEach { (_, option) -> modelRow(option, Modifier.padding(start = 12.dp)) }
            }
        }
    }
}

/**
 * The row that folds one provider's models away.
 *
 * The count is the number of model rows behind it, so the list says how much it
 * is hiding; the chevron says which way it will move.
 */
@Composable
private fun ProviderSectionHeader(
    label: String,
    count: Int,
    expanded: Boolean,
    palette: AABottomSheetColors,
    onClick: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = 48.dp)
            .clip(RoundedCornerShape(14.dp))
            .noRippleClickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 6.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (expanded) {
            DownGlyph(color = palette.content.copy(alpha = 0.7f))
        } else {
            ForwardGlyph(color = palette.content.copy(alpha = 0.7f))
        }
        Text(
            text = label,
            color = palette.content,
            fontSize = 14.sp,
            fontWeight = FontWeight.SemiBold,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Text(
            text = count.toString(),
            color = palette.secondaryContent,
            fontSize = 12.sp,
        )
    }
}

@Composable
private fun ModeEffortPage(
    runtimeLabel: String,
    selectedModelGroup: ModelOptionGroup?,
    selectedModelId: String?,
    permissionOptions: List<RuntimeSelectionOption>,
    selectedPermissionId: String?,
    modelLoading: Boolean,
    permissionLoading: Boolean,
    permissionErrorMessage: String?,
    busy: Boolean,
    palette: AABottomSheetColors,
    onRetryPermissions: () -> Unit,
    onSelectModel: (String) -> Unit,
    onSelectPermission: (String) -> Unit,
) {
    val modelLabel = selectedModelGroup?.label ?: runtimeLabel
    val effortOptions = selectedModelGroup?.options.orEmpty().takeIf { it.size > 1 }

    PermissionSection(
        options = permissionOptions,
        selectedId = selectedPermissionId,
        loading = permissionLoading,
        errorMessage = permissionErrorMessage,
        busy = busy,
        palette = palette,
        onRetry = onRetryPermissions,
        onSelect = onSelectPermission,
    )

    if (effortOptions != null) {
        DividerLine(palette.divider)
        EffortSelectionSection(
            modelLabel = modelLabel,
            options = effortOptions,
            selectedId = selectedModelId,
            enabled = !busy && !modelLoading,
            palette = palette,
            onSelect = onSelectModel,
        )
    }
}

@Composable
private fun PermissionSection(
    options: List<RuntimeSelectionOption>,
    selectedId: String?,
    loading: Boolean,
    errorMessage: String?,
    busy: Boolean,
    palette: AABottomSheetColors,
    onRetry: () -> Unit,
    onSelect: (String) -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        SectionLabel(stringResource(R.string.session_runtime_permission_mode), palette)
        when {
            loading && options.isEmpty() -> SheetLoading(palette = palette, height = 72.dp)
            errorMessage != null && options.isEmpty() -> SheetError(
                message = errorMessage,
                height = 96.dp,
                retryEnabled = !busy && !loading,
                onRetry = onRetry,
            )
            options.isEmpty() -> SheetEmpty(palette = palette)
            else -> Column(
                modifier = Modifier.fillMaxWidth(),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                options.forEach { option ->
                    AABottomSheetItem(
                        text = option.label,
                        supportingText = option.disabledReason ?: option.description,
                        selected = option.selectionId == selectedId,
                        enabled = !busy && !loading && option.enabled,
                        onClick = { onSelect(option.selectionId) },
                    )
                }
            }
        }
    }
}

@Composable
private fun EffortSelectionSection(
    modelLabel: String,
    options: List<RuntimeSelectionOption>,
    selectedId: String?,
    enabled: Boolean,
    palette: AABottomSheetColors,
    onSelect: (String) -> Unit,
) {
    val defaultEffortLabel = stringResource(R.string.session_runtime_effort_default)
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SectionLabel(stringResource(R.string.session_runtime_effort_for, modelLabel), palette)
        EffortSegments(
            options = options,
            selectedId = selectedId,
            enabled = enabled,
            defaultEffortLabel = defaultEffortLabel,
            palette = palette,
            onSelect = onSelect,
        )
        options.filter { !it.enabled }.forEach { option ->
            option.disabledReason?.takeIf(String::isNotBlank)?.let { reason ->
                Text(
                    text = "${option.effortDisplayLabel(defaultEffortLabel)}: $reason",
                    color = palette.secondaryContent,
                    fontSize = 11.5.sp,
                )
            }
        }
    }
}

@Composable
private fun EffortSegments(
    options: List<RuntimeSelectionOption>,
    selectedId: String?,
    enabled: Boolean,
    defaultEffortLabel: String,
    palette: AABottomSheetColors,
    onSelect: (String) -> Unit,
) {
    var trackWidthPx by remember { mutableFloatStateOf(0f) }
    var dragPositionPx by remember { mutableFloatStateOf(0f) }
    var dragSelectionId by remember { mutableStateOf<String?>(null) }

    fun selectAt(positionPx: Float) {
        if (!enabled || trackWidthPx <= 0f || options.isEmpty()) return
        val index = ((positionPx / trackWidthPx) * options.size)
            .toInt()
            .coerceIn(0, options.lastIndex)
        val option = options[index]
        if (option.enabled) dragSelectionId = option.selectionId
    }

    val dragState = rememberDraggableState { delta ->
        dragPositionPx = (dragPositionPx + delta).coerceIn(0f, trackWidthPx)
        selectAt(dragPositionPx)
    }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .height(48.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(palette.selectedContainer)
            .onSizeChanged { trackWidthPx = it.width.toFloat() }
            .draggable(
                state = dragState,
                orientation = Orientation.Horizontal,
                enabled = enabled,
                onDragStarted = { position ->
                    dragPositionPx = position.x.coerceIn(0f, trackWidthPx)
                    dragSelectionId = null
                    selectAt(dragPositionPx)
                },
                onDragStopped = {
                    val selection = dragSelectionId
                    dragSelectionId = null
                    if (selection != null && selection != selectedId) onSelect(selection)
                },
            )
            .padding(4.dp),
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        options.forEach { option ->
            val selected = (dragSelectionId ?: selectedId) == option.selectionId
            val optionEnabled = enabled && option.enabled
            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .shadow(
                        elevation = if (selected) 3.dp else 0.dp,
                        shape = RoundedCornerShape(12.dp),
                        ambientColor = palette.shadow,
                        spotColor = palette.shadow,
                    )
                    .clip(RoundedCornerShape(12.dp))
                    .background(if (selected) palette.container else Color.Transparent)
                    .noRippleClickable(enabled = optionEnabled) { onSelect(option.selectionId) },
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = option.effortDisplayLabel(defaultEffortLabel),
                    color = (if (selected) palette.content else palette.secondaryContent)
                        .copy(alpha = if (option.enabled || selected) 1f else 0.45f),
                    fontSize = 10.sp,
                    lineHeight = 11.sp,
                    fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
                    maxLines = 2,
                )
            }
        }
    }
}

@Composable
private fun SheetLoading(
    palette: AABottomSheetColors,
    height: Dp = 210.dp,
) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(height),
        contentAlignment = Alignment.Center,
    ) {
        CircularProgressIndicator(
            color = palette.content,
            strokeWidth = 2.dp,
            modifier = Modifier.size(24.dp),
        )
    }
}

@Composable
private fun SheetError(
    message: String,
    height: Dp = 210.dp,
    retryEnabled: Boolean,
    onRetry: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .height(height),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        AABottomSheetItem(
            text = stringResource(R.string.common_retry),
            errorMessage = message,
            enabled = retryEnabled,
            onClick = onRetry,
        )
    }
}

@Composable
private fun SheetEmpty(palette: AABottomSheetColors) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(72.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = stringResource(R.string.session_runtime_no_settings),
            color = palette.secondaryContent,
            fontSize = 13.sp,
        )
    }
}

@Composable
private fun SectionLabel(text: String, palette: AABottomSheetColors) {
    Text(text, modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
        color = palette.secondaryContent, fontSize = 12.sp, fontWeight = FontWeight.Medium)
}

@Composable
private fun DividerLine(color: Color) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(1.dp)
            .background(color),
    )
}
