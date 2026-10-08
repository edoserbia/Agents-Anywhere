package com.agentsanywhere.app.ui.screens.sessiondetail

import com.agentsanywhere.app.api.RemoteRuntimeModel
import com.agentsanywhere.app.api.RemoteRuntimeModelCatalog
import com.agentsanywhere.app.api.RemoteRuntimeReasoning
import com.agentsanywhere.app.feature.sessiondetail.selectionOptions
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A picker has to say which provider serves a model, and it has to fold.
 *
 * OpenScience puts the provider inside the model's `displayName`
 * ("GPT-6.1 Sol · AI98 Pro (codex relay)"), and the Android picker recovered the
 * model name by splitting that string on " · ". That dropped the provider from
 * the row and pushed it into the reasoning label, so the same model name served
 * by two providers was indistinguishable — exactly when the reader needs to
 * tell them apart. These assertions pin the structured fields and the grouping
 * that replaced the string splitting.
 */
class ModelProviderOptionsTest {
    private fun reasoning(id: String, displayName: String, selectionId: String = "sel_$id") =
        RemoteRuntimeReasoning(
            id = id,
            selectionId = selectionId,
            fullModelId = null,
            displayName = displayName,
            description = null,
            default = false,
            metadata = mapOf("variant" to id),
        )

    private fun model(
        id: String,
        displayName: String,
        providerId: String?,
        providerName: String?,
        modelName: String? = null,
        reasoningItems: List<RemoteRuntimeReasoning> = emptyList(),
    ) = RemoteRuntimeModel(
        id = id,
        selectionId = if (reasoningItems.isEmpty()) "sel_$id" else null,
        displayName = displayName,
        description = null,
        default = false,
        reasoningItems = reasoningItems,
        metadata = mapOf(
            "providerID" to providerId,
            "providerName" to providerName,
            "modelName" to modelName,
            "source" to "openscience.config.providers",
        ),
    )

    private fun catalog(vararg models: RemoteRuntimeModel) =
        RemoteRuntimeModelCatalog(runtime = "openscience", revision = 1, models = models.toList())

    @Test
    fun `the provider stays attached to the model instead of leaking into the effort`() {
        val options = catalog(
            model(
                id = "rayinai/gpt-6.1-sol",
                displayName = "GPT-6.1 Sol · AI98 Pro (codex relay)",
                providerId = "rayinai",
                providerName = "AI98 Pro (codex relay)",
                modelName = "GPT-6.1 Sol",
                reasoningItems = listOf(reasoning("xhigh", "Extra high")),
            ),
        ).selectionOptions()

        assertEquals(1, options.size)
        assertEquals("GPT-6.1 Sol", options[0].modelLabel)
        assertEquals("AI98 Pro (codex relay)", options[0].providerLabel)
        assertEquals("Extra high", options[0].effortLabelText)
        assertEquals("Extra high", options[0].effortDisplayLabel("默认"))
    }

    @Test
    fun `a runtime that names no provider keeps a readable model name`() {
        val options = catalog(
            model(
                id = "dsh:model:abc",
                displayName = "DeepSeek V4.1 Flash",
                providerId = null,
                providerName = null,
            ),
        ).selectionOptions()

        assertEquals(1, options.size)
        assertEquals("DeepSeek V4.1 Flash", options[0].modelLabel)
        assertNull(options[0].providerLabel)
    }

    @Test
    fun `models fold into the provider that serves them`() {
        val options = catalog(
            model("cc-proxy/a", "A · Command Code Proxy", "cc-proxy", "Command Code Proxy", "A"),
            model("cc-proxy/b", "B · Command Code Proxy", "cc-proxy", "Command Code Proxy", "B"),
            model("rayinai/a", "A · AI98 Pro (codex relay)", "rayinai", "AI98 Pro (codex relay)", "A"),
        ).selectionOptions()

        val groups = options.groupByProvider()
        assertEquals(listOf("Command Code Proxy", "AI98 Pro (codex relay)"), groups.map { it.label })
        assertEquals(
            listOf(listOf("A", "B"), listOf("A")),
            groups.map { group -> group.models.map { it.label } },
        )
    }

    @Test
    fun `one provider or none keeps the flat list`() {
        val single = catalog(
            model("dsh:model:a", "A", "dsh", "DSH", "A"),
            model("dsh:model:b", "B", "dsh", "DSH", "B"),
        ).selectionOptions()
        assertEquals(null, single.groupByProvider().single().label)

        val none = catalog(
            model("dsh:model:a", "A", null, null),
            model("dsh:model:b", "B", null, null),
        ).selectionOptions()
        assertEquals(null, none.groupByProvider().single().label)
        assertTrue(none.groupByProvider().single().models.size == 2)
    }
}
