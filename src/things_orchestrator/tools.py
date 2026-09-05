"""Canonical advertised MCP tool definitions shared by server and hashes."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from mcp.types import Tool, ToolAnnotations
from pydantic import BaseModel

ITEM_ID = r"^(task|project|area|heading):[^\s:]+$"

CLIENT_BUNDLE_PATH = "/client/bundle"
CLIENT_BUNDLE_FORMAT_VERSION = 1

_READ_NAMES = frozenset(("things_view", "things_find", "things_get", "things_receipt"))
_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


def advertised_output_schema(model: type[BaseModel] | None = None) -> dict[str, Any]:
    """Flattened PublicResult schema that tolerates additive object properties."""

    from .v2 import PublicResult, flat_schema

    return cast(dict[str, Any], _allow_additional_object_properties(flat_schema(model or PublicResult)))


def advertised_tools() -> tuple[Tool, ...]:
    """Exact tools/list contract: discovery inputs, additive outputs, annotations."""

    from .v2 import DESCRIPTIONS, DISCOVERY_MODELS, MODELS, flat_schema

    output_schema = advertised_output_schema()
    return tuple(
        Tool(
            name=name,
            description=DESCRIPTIONS[name],
            input_schema=flat_schema(DISCOVERY_MODELS[name]),
            output_schema=output_schema,
            annotations=_READ_ONLY if name in _READ_NAMES else _IDEMPOTENT_WRITE,
        )
        for name in MODELS
    )


def advertised_tool_payload(tool: Tool) -> dict[str, object]:
    annotations = tool.annotations
    payload: dict[str, object] = {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.input_schema,
        "outputSchema": tool.output_schema,
    }
    if annotations is not None:
        payload["annotations"] = annotations.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    return payload


def hash_payload(value: object) -> str:
    blob = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:24]


def content_sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def tool_schema_hash(tools: tuple[Tool, ...] | None = None) -> str:
    selected = tools if tools is not None else advertised_tools()
    return hash_payload(
        {
            "version": "v2",
            "inputs": {tool.name: tool.input_schema for tool in selected},
            "output": selected[0].output_schema if selected else {},
        }
    )


def tool_contract_hash(tools: tuple[Tool, ...] | None = None) -> str:
    selected = tools if tools is not None else advertised_tools()
    return hash_payload(
        {
            "version": "v2",
            "inputs": {tool.name: tool.input_schema for tool in selected},
            "output": selected[0].output_schema if selected else {},
            "descriptions": {tool.name: tool.description for tool in selected},
        }
    )


def tool_discovery_hash(tools: tuple[Tool, ...] | None = None) -> str:
    selected = tools if tools is not None else advertised_tools()
    return hash_payload([advertised_tool_payload(tool) for tool in selected])


def _allow_additional_object_properties(value: object) -> object:
    if isinstance(value, list):
        return [_allow_additional_object_properties(item) for item in value]
    if not isinstance(value, dict):
        return value
    mapped = {
        key: _allow_additional_object_properties(item) for key, item in value.items()
    }
    if "properties" in mapped and _declares_object_type(mapped):
        additional = mapped.get("additionalProperties")
        if additional is False:
            mapped["additionalProperties"] = True
    return mapped


def _declares_object_type(schema: dict[str, object]) -> bool:
    type_value = schema.get("type")
    if type_value == "object":
        return True
    return isinstance(type_value, list) and "object" in type_value
