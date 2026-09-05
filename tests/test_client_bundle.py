from __future__ import annotations

from pathlib import Path

import pytest

from things_orchestrator.client_bundle import (
    CATALOG_EPOCH,
    CATALOG_POLICY,
    RECEIVER_INSTRUCTION_PATH,
    encode_client_bundle,
    is_named_routine_template,
    parse_client_bundle,
)
from things_orchestrator.deployment import skill_path
from things_orchestrator.routines_config import ROUTINE_RECEIVER_INSTRUCTION
from things_orchestrator.tools import (
    advertised_tool_payload,
    advertised_tools,
    tool_discovery_hash,
)

ROOT = Path(__file__).parents[1]


def test_client_bundle_is_deterministic_and_complete() -> None:
    first = encode_client_bundle()
    second = encode_client_bundle()
    assert first == second
    bundle = parse_client_bundle(first)
    skill = skill_path()
    expected = {
        path.relative_to(skill).as_posix()
        for path in skill.rglob("*")
        if path.is_file()
    }
    expected.add(RECEIVER_INSTRUCTION_PATH)
    actual = {item.path for item in bundle.files}
    assert actual == expected
    receiver = next(
        item for item in bundle.files if item.path == RECEIVER_INSTRUCTION_PATH
    )
    assert receiver.content == ROUTINE_RECEIVER_INSTRUCTION
    tools = advertised_tools()
    assert [item["name"] for item in bundle.advertised_tools] == [tool.name for tool in tools]
    assert bundle.advertised_tools == tuple(advertised_tool_payload(tool) for tool in tools)
    assert bundle.fingerprints["tool_discovery_hash"] == tool_discovery_hash()
    assert bundle.package.name == "things-orchestrator"
    assert bundle.client_impact["catalog_policy"] == CATALOG_POLICY
    assert bundle.client_impact["catalog_epoch"] == CATALOG_EPOCH
    assert "breaking" not in bundle.client_impact
    templates = {
        item.path: item.sha256
        for item in bundle.files
        if is_named_routine_template(item.path)
    }
    assert bundle.component_hashes.routine_templates == templates
    assert "references/routine-weekly-review.md" in templates
    assert "references/routines.md" not in templates
    assert bundle.package.commit is None or len(bundle.package.commit) in {40, 64}
    assert b"mcp_token" not in first
    assert b"password" not in first


def test_client_bundle_cli_writes_the_same_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from things_orchestrator.cli import main

    destination = tmp_path / "bundle.json"
    main(["client-bundle", "--output", str(destination)])
    assert destination.read_bytes() == encode_client_bundle()
    assert "Wrote" in capsys.readouterr().out
