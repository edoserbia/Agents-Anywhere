from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from connector.runtime_protocol import timeline_content_hash
from connector.runtimes.dsh.bridge.models import model_catalog, timeline_item
from connector.runtimes.dsh.identity import (
    decode_model_selection_id,
    decode_permission_selection_id,
    model_selection_id,
    permission_selection_id,
    timeline_item_id,
)
from connector.runtimes.session_identity import stable_runtime_session_id

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "dsh-bridge" / "1.0"


@pytest.mark.parametrize(
    "schema_path",
    sorted((CONTRACT / "schemas").glob("*.schema.json")),
    ids=lambda path: path.stem,
)
def test_dsh_bridge_schemas_are_valid(schema_path: Path) -> None:
    Draft202012Validator.check_schema(json.loads(schema_path.read_text()))


def test_dsh_request_fixtures() -> None:
    schema = json.loads((CONTRACT / "schemas" / "request.schema.json").read_text())
    validator = Draft202012Validator(schema)
    for path in sorted((CONTRACT / "fixtures" / "valid").glob("*.json")):
        validator.validate(json.loads(path.read_text()))
    for path in sorted((CONTRACT / "fixtures" / "invalid").glob("*.json")):
        assert not validator.is_valid(json.loads(path.read_text()))


def test_dsh_identity_fixture_matches_connector_implementations() -> None:
    fixture = json.loads((CONTRACT / "fixtures" / "identity.json").read_text())
    for item in fixture["sessionIds"]:
        assert (
            stable_runtime_session_id(
                item["connectorId"], "dsh", item["externalSessionId"]
            )
            == item["sessionId"]
        )
    for item in fixture["modelSelections"]:
        assert (
            model_selection_id(item["provider"], item["model"], item["effort"])
            == item["selectionId"]
        )
        assert decode_model_selection_id(item["selectionId"]) == (
            item["provider"],
            item["model"],
            item["effort"],
        )
    for item in fixture["permissionSelections"]:
        assert permission_selection_id(item["preset"]) == item["selectionId"]
        assert decode_permission_selection_id(item["selectionId"]) == item["preset"]
    for item in fixture["timelineIds"]:
        assert (
            timeline_item_id(
                item["externalSessionId"],
                item["projectionKind"],
                item["businessId"],
            )
            == item["itemId"]
        )
    for item in fixture["contentHashes"]:
        assert (
            timeline_content_hash(
                item["type"], item["status"], item["role"], item["content"]
            )
            == item["contentHash"]
        )


@pytest.mark.parametrize(
    "selection_id",
    [
        "wrong:model:e30",
        "dsh:model:====",
        "dsh:model:W10",
        "dsh:model:WyJhIiwiYiIsImMiLCJkIl0",
    ],
)
def test_invalid_model_selection_ids_are_rejected(selection_id: str) -> None:
    with pytest.raises(ValueError):
        decode_model_selection_id(selection_id)


def test_native_dsh_payload_is_rejected_instead_of_projected_in_python() -> None:
    with pytest.raises(ValueError, match="native payload"):
        timeline_item(
            {"type": "message", "payload": {"role": "assistant", "text": "hello"}}
        )


def test_canonical_item_preserves_turn_and_validates_content_hash() -> None:
    content = {"kind": "markdown", "text": "你好"}
    value = {
        "id": "dsh-item",
        "sessionId": "a",
        "turnId": "turn-1",
        "type": "message",
        "status": "done",
        "role": "assistant",
        "content": content,
        "source": {"runtime": "dsh"},
        "orderSeq": 4,
        "revision": 7,
        "contentHash": timeline_content_hash("message", "done", "assistant", content),
    }
    item = timeline_item(value)
    assert item.turn_id == "turn-1"
    assert item.revision == 7
    value["contentHash"] = "sha256:incorrect"
    with pytest.raises(ValueError, match="contentHash"):
        timeline_item(value)


def _model_item(
    *,
    model_id: str,
    title: str,
    selection_id: str,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """One bridge model entry in the shape the live DSH payload uses."""

    item: dict[str, object] = {
        "id": model_id,
        "title": title,
        "selectionId": selection_id,
        "description": None,
        "reasoningItems": [],
        "enabled": True,
        "metadata": metadata if metadata is not None else {},
    }
    return item


def test_dsh_model_catalog_titles_name_the_serving_provider() -> None:
    """The bridge carries the provider in metadata; the picker has to show it.

    The live DSH catalog serves 75 models through three providers, and the
    same model id is reachable through more than one of them. Without the
    provider in the title those rows read identically even though the base64
    route id behind each one decodes to a different backend.
    """

    catalog = model_catalog(
        {
            "runtime": "dsh",
            "revision": 31,
            "models": [
                _model_item(
                    model_id="WyJjYy1wcm94eSIsImNsYXVkZS1zb25uZXQtNSJd",
                    title="claude-sonnet-5",
                    selection_id="sel_model_cc",
                    metadata={
                        "provider": "cc-proxy",
                        "providerName": "Command Code Proxy",
                        "model": "claude-sonnet-5",
                        "modelName": "claude-sonnet-5",
                        "reasoningEffort": None,
                        "enabled": True,
                    },
                ),
                _model_item(
                    model_id="WyJvcGVuY29kZS1nbyIsImNsYXVkZS1zb25uZXQtNSJd",
                    title="claude-sonnet-5",
                    selection_id="sel_model_go",
                    metadata={
                        "provider": "opencode-go",
                        "providerName": "OpenCode Go",
                        "model": "claude-sonnet-5",
                        "modelName": "claude-sonnet-5",
                    },
                ),
            ],
        }
    )
    assert catalog.revision == 31
    assert [model.title for model in catalog.models] == [
        "claude-sonnet-5 · Command Code Proxy",
        "claude-sonnet-5 · OpenCode Go",
    ]
    # Only the title is qualified: the ids stay the bridge's own route tuples.
    assert [model.id for model in catalog.models] == [
        "WyJjYy1wcm94eSIsImNsYXVkZS1zb25uZXQtNSJd",
        "WyJvcGVuY29kZS1nbyIsImNsYXVkZS1zb25uZXQtNSJd",
    ]
    assert catalog.models[0].selection_id == "sel_model_cc"
    assert catalog.models[0].metadata["provider"] == "cc-proxy"


@pytest.mark.parametrize(
    "metadata",
    [
        None,  # no metadata object at all
        {},  # metadata without a provider name
        {"provider": "cc-proxy"},  # provider id but no display name
        {"providerName": ""},  # blank display name
        {"providerName": None},
    ],
)
def test_dsh_model_catalog_keeps_the_raw_title_without_a_provider_name(
    metadata: dict[str, object] | None,
) -> None:
    """An absent provider must never drop or blank a model."""

    catalog = model_catalog(
        {
            "runtime": "dsh",
            "revision": 1,
            "models": [
                _model_item(
                    model_id="gpt-plain",
                    title="gpt-plain",
                    selection_id="sel_model_plain",
                    metadata=metadata,
                )
            ],
        }
    )
    assert [model.title for model in catalog.models] == ["gpt-plain"]
    assert catalog.models[0].id == "gpt-plain"
    assert catalog.models[0].selection_id == "sel_model_plain"
