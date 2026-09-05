from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from things_orchestrator.tools import (
    advertised_output_schema,
    advertised_tools,
    tool_contract_hash,
    tool_discovery_hash,
    tool_schema_hash,
)
from things_orchestrator.v2 import (
    MODELS,
    GetCall,
    PublicItem,
    PublicResult,
    TaintedText,
    flat_schema,
)

ROOT = Path(__file__).parents[1]
CLOSED_V0105 = ROOT / "tests/fixtures/v0.10.5-closed-public-result.schema.json"


def _ok_item_result() -> dict[str, object]:
    result = PublicResult(
        state="ok",
        instruction="Read current items.",
        code="ok",
        next_action="none",
        items=[
            PublicItem(
                id="task:sample",
                kind="task",
                title=TaintedText(value="Sample"),
                status="open",
                notes_state="available",
            )
        ],
    )
    return result.model_dump(mode="json", exclude_none=True)


def test_v0105_closed_schema_rejects_nested_additions_that_advertised_schema_accepts() -> None:
    old_schema = json.loads(CLOSED_V0105.read_text())
    new_schema = advertised_output_schema()
    payload = _ok_item_result()
    payload["items"][0]["future_note_flag"] = True
    payload["items"][0]["title"]["future_mark"] = "nested"

    with pytest.raises(JsonSchemaError, match="future_note_flag"):
        Draft202012Validator(old_schema).validate(payload)
    Draft202012Validator(new_schema).validate(payload)

    closed_item = _ok_item_result()
    Draft202012Validator(old_schema).validate(closed_item)
    Draft202012Validator(new_schema).validate(closed_item)


def test_advertised_output_schema_keeps_known_field_and_outcome_constraints() -> None:
    schema = advertised_output_schema()
    bad_kind = _ok_item_result()
    bad_kind["items"][0]["kind"] = "widget"
    with pytest.raises(JsonSchemaError):
        Draft202012Validator(schema).validate(bad_kind)

    bad_state = _ok_item_result()
    bad_state["state"] = "activated"
    with pytest.raises(JsonSchemaError):
        Draft202012Validator(schema).validate(bad_state)

    extra = _ok_item_result()
    extra["future_note_flag"] = True
    with pytest.raises(ValidationError):
        PublicResult.model_validate(extra)


def test_input_and_runtime_result_validation_remain_strict() -> None:
    with pytest.raises(ValidationError):
        GetCall.model_validate({"ids": ["task:sample"], "unexpected": True})
    extra = _ok_item_result()
    extra["items"][0]["future_note_flag"] = True
    with pytest.raises(ValidationError):
        PublicResult.model_validate(extra)
    tools = {tool.name: tool for tool in advertised_tools()}
    assert tools["things_get"].input_schema.get("additionalProperties") is False


def test_discovery_hash_includes_discovery_inputs_descriptions_and_annotations() -> None:
    advertised = advertised_tools()
    capture = next(tool for tool in advertised if tool.name == "things_capture")
    runtime_input = capture.model_copy(
        update={"input_schema": flat_schema(MODELS["things_capture"])}
    )
    runtime_tools = tuple(
        runtime_input if tool.name == "things_capture" else tool for tool in advertised
    )
    assert tool_discovery_hash(runtime_tools) != tool_discovery_hash(advertised)
    assert tool_schema_hash(runtime_tools) != tool_schema_hash(advertised)

    renamed = advertised[0].model_copy(update={"description": "Changed discovery text."})
    described = (renamed, *advertised[1:])
    assert tool_discovery_hash(described) != tool_discovery_hash(advertised)
    assert tool_contract_hash(described) != tool_contract_hash(advertised)
    assert tool_schema_hash(described) == tool_schema_hash(advertised)

    annotated = advertised[0].model_copy(
        update={
            "annotations": ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=False,
            )
        }
    )
    annotated_tools = (annotated, *advertised[1:])
    assert tool_discovery_hash(annotated_tools) != tool_discovery_hash(advertised)
    assert tool_schema_hash(annotated_tools) == tool_schema_hash(advertised)
    assert tool_contract_hash(annotated_tools) == tool_contract_hash(advertised)
