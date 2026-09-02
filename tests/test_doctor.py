from __future__ import annotations

from dataclasses import replace

import pytest
from mcp.types import Implementation

from things_orchestrator.config import normalize_mcp_url
from things_orchestrator.deployment import (
    DeploymentIdentity,
    tool_contract_hash,
    tool_schema_hash,
)
from things_orchestrator.doctor import (
    DoctorFailure,
    TargetReceipt,
    curl_tool_count_command,
    validate_target,
)
from things_orchestrator.v2 import MODELS


def _receipt() -> TargetReceipt:
    return TargetReceipt(
        url=normalize_mcp_url("http://127.0.0.1:8787/mcp"),
        public_health={"ok": True},
        detailed_health={
            "ok": True,
            "version": "0.8.0",
            "commit": "a" * 40,
            "tool_schema_hash": tool_schema_hash(),
            "tool_contract_hash": tool_contract_hash(),
        },
        server_info=Implementation(name="things", version="0.8.0"),
        tool_names=tuple(MODELS),
    )


def _identity() -> DeploymentIdentity:
    return DeploymentIdentity(
        version="0.8.0",
        commit="a" * 40,
        requested_revision="v0.8.0",
        source="pep610",
    )


def test_validate_target_accepts_exact_public_health_identity_and_tools() -> None:
    validate_target(_receipt(), _identity())


@pytest.mark.parametrize(
    ("receipt", "message"),
    [
        (replace(_receipt(), public_health={"ok": True, "version": "0.8.0"}), "public /health"),
        (replace(_receipt(), tool_names=tuple(MODELS)[:-1]), "eight tools"),
        (
            replace(
                _receipt(),
                detailed_health={
                    **_receipt().detailed_health,
                    "tool_schema_hash": "sha256:wrong",
                },
            ),
            "schema hash",
        ),
        (
            replace(
                _receipt(),
                detailed_health={
                    **_receipt().detailed_health,
                    "tool_contract_hash": "sha256:wrong",
                },
            ),
            "contract hash",
        ),
        (
            replace(
                _receipt(),
                detailed_health={
                    **_receipt().detailed_health,
                    "commit": "b" * 40,
                },
            ),
            "stale",
        ),
    ],
)
def test_validate_target_rejects_protocol_and_deployment_drift(
    receipt: TargetReceipt, message: str
) -> None:
    with pytest.raises(DoctorFailure, match=message):
        validate_target(receipt, _identity())


def test_validate_target_rejects_unknown_local_commit() -> None:
    identity = replace(_identity(), commit=None, source="unknown")
    with pytest.raises(DoctorFailure, match="installed commit is unknown"):
        validate_target(_receipt(), identity)


def test_curl_command_uses_environment_bearer_and_returns_tool_count() -> None:
    command = curl_tool_count_command(normalize_mcp_url("https://tasks.example.com/mcp"))
    assert "$THINGS_MCP_TOKEN" in command
    assert "things/list" not in command
    assert '"method":"tools/list"' in command
    assert "jq" in command
    assert "| length" in command
    assert "keep-me" not in command
