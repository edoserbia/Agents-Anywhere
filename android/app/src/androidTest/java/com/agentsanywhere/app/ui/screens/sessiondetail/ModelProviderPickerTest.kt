package com.agentsanywhere.app.ui.screens.sessiondetail

import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotSelected
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.agentsanywhere.app.MainActivity
import com.agentsanywhere.app.feature.sessiondetail.RuntimeSelectionOption
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Offline UI fixtures: no account, network catalog, or production session is needed. */
@RunWith(AndroidJUnit4::class)
class ModelProviderPickerTest {
    @get:Rule
    val compose = createEmptyComposeRule()

    private lateinit var scenario: ActivityScenario<MainActivity>
    private var selectedId by mutableStateOf<String?>(null)
    private var receivedSelection: String? = null

    @Before
    fun launch() {
        scenario = ActivityScenario.launch(MainActivity::class.java)
    }

    @After
    fun close() {
        scenario.close()
    }

    private fun option(id: String, model: String, provider: String?, effort: String? = null, default: Boolean = false) =
        RuntimeSelectionOption(
            selectionId = id,
            label = model,
            description = null,
            default = default,
            modelName = model,
            providerLabel = provider,
            effortLabel = effort,
        )

    private fun show(options: List<RuntimeSelectionOption>, initialSelection: String? = null) {
        selectedId = initialSelection
        scenario.onActivity { activity ->
            activity.setContent {
                SessionRuntimeSettingsSheet(
                    runtimeLabel = "Fixture runtime",
                    modelOptions = options,
                    permissionOptions = emptyList(),
                    selectedModelId = selectedId,
                    selectedPermissionId = null,
                    modelLoading = false,
                    permissionLoading = false,
                    modelErrorMessage = null,
                    permissionErrorMessage = null,
                    busy = false,
                    onDismiss = {},
                    onRetryModels = {},
                    onRetryPermissions = {},
                    onSelectModel = { receivedSelection = it; selectedId = it },
                    onSelectPermission = {},
                )
            }
        }
    }

    @Test
    fun expandingProviderShowsItsModelsAndSelectionReachesCallback() {
        show(listOf(option("alpha-a", "Alpha model", "Alpha"), option("beta-b", "Beta model", "Beta")))
        compose.onNodeWithText("Alpha model").assertDoesNotExist()
        compose.onNodeWithText("Alpha").performClick()
        compose.onNodeWithText("Alpha model").assertIsDisplayed().performClick().assertIsSelected()
        compose.runOnIdle { assertEquals("alpha-a", receivedSelection) }
        compose.onNodeWithText("Alpha").performClick()
        compose.onNodeWithText("Alpha model").assertDoesNotExist()
        compose.onNodeWithText("Beta").performClick()
        compose.onNodeWithText("Beta model").performScrollTo().assertIsDisplayed().performClick().assertIsSelected()
        compose.runOnIdle { assertEquals("beta-b", receivedSelection) }
    }

    @Test
    fun sameNameModelsKeepProviderIdentityAndOnlySelectedProvidersEfforts() {
        show(
            listOf(
                option("alpha-low", "Shared model", "Alpha", "Alpha low", default = true),
                option("alpha-high", "Shared model", "Alpha", "Alpha high"),
                option("beta-low", "Shared model", "Beta", "Beta low", default = true),
                option("beta-high", "Shared model", "Beta", "Beta high"),
            ),
            initialSelection = "alpha-high",
        )
        compose.onNodeWithText("Shared model").assertIsDisplayed().assertIsSelected()
        compose.onNodeWithText("Beta").performScrollTo().performClick()
        compose.onAllNodesWithText("Shared model")[1].performScrollTo().assertIsNotSelected().performClick()
        compose.runOnIdle { assertEquals("beta-low", receivedSelection) }
        // The formerly selected provider folds automatically; reopen it to check both rows.
        compose.onNodeWithText("Alpha").performScrollTo().performClick()
        compose.onAllNodesWithText("Shared model")[0].assertIsNotSelected()
        compose.onAllNodesWithText("Shared model")[1].assertIsSelected()
        // The settings row contains the selected reasoning label even when localized.
        compose.onNodeWithText("Beta low").performScrollTo().performClick()
        compose.onNodeWithText("Beta high").performScrollTo().assertIsDisplayed().performClick()
        compose.runOnIdle { assertEquals("beta-high", receivedSelection) }
        compose.onNodeWithText("Alpha high").assertDoesNotExist()
    }

    @Test
    fun unnamedModelsRemainVisibleAlongsideCollapsibleProviders() {
        show(listOf(option("plain", "Plain model", null), option("a", "A model", "Alpha"), option("b", "B model", "Beta")))
        compose.onNodeWithText("Plain model").assertIsDisplayed().performClick()
        compose.runOnIdle { assertEquals("plain", receivedSelection) }
        compose.onNodeWithText("Alpha").performClick()
        compose.onNodeWithText("A model").assertIsDisplayed()
    }

    @Test
    fun longProviderCatalogScrollsToAndSelectsLastModelOnPhone() {
        show((1..40).map { option("alpha-$it", "Model $it", "Alpha") } + option("b", "B model", "Beta"))
        compose.onNodeWithText("Alpha").performClick()
        compose.onNodeWithText("Model 40").performScrollTo().assertIsDisplayed().performClick().assertIsSelected()
        compose.runOnIdle { assertEquals("alpha-40", receivedSelection) }
    }

    @Test
    fun oneProviderKeepsFlatSelectableModels() {
        show(listOf(option("a", "A model", "Alpha"), option("b", "B model", "Alpha")))
        compose.onNodeWithText("Alpha").assertDoesNotExist()
        compose.onNodeWithText("B model").assertIsDisplayed().performClick().assertIsSelected()
        compose.runOnIdle { assertEquals("b", receivedSelection) }
    }
}
