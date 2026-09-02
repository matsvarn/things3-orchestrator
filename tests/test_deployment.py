from __future__ import annotations

import json
from pathlib import Path

import pytest

from things_orchestrator.deployment import (
    installed_identity,
    skill_path,
    tool_contract_hash,
    tool_schema_hash,
)
from things_orchestrator.v2 import MODELS

ROOT = Path(__file__).parents[1]
EXPECTED_TOOL_SCHEMA_HASH = "sha256:bc72f9c4434e853ef6646342"
EXPECTED_TOOL_CONTRACT_HASH = "sha256:99a4dbd45f064c5a7307d431"
EXPECTED_TOOLS = (
    "things_view",
    "things_find",
    "things_get",
    "things_capture",
    "things_update",
    "things_complete",
    "things_trash",
    "things_receipt",
)


def test_onboarding_changes_preserve_the_v080_tool_contract() -> None:
    assert tuple(MODELS) == EXPECTED_TOOLS
    assert tool_schema_hash() == EXPECTED_TOOL_SCHEMA_HASH
    assert tool_contract_hash() == EXPECTED_TOOL_CONTRACT_HASH


def test_skill_path_is_packaged_and_matches_the_codex_plugin() -> None:
    packaged = skill_path()
    plugin = ROOT / "plugin/skills/things-orchestrator"
    packaged_files = sorted(path.relative_to(packaged) for path in packaged.rglob("*") if path.is_file())
    plugin_files = sorted(path.relative_to(plugin) for path in plugin.rglob("*") if path.is_file())

    assert packaged_files == plugin_files
    for relative in packaged_files:
        assert (packaged / relative).read_bytes() == (plugin / relative).read_bytes()


def test_installed_identity_prefers_pep610_git_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40

    class Distribution:
        def read_text(self, filename: str) -> str | None:
            assert filename == "direct_url.json"
            return json.dumps(
                {
                    "url": "https://github.com/matsvarn/things3-orchestrator.git",
                    "vcs_info": {
                        "vcs": "git",
                        "commit_id": commit,
                        "requested_revision": "v0.9.0",
                    },
                }
            )

    monkeypatch.setattr(
        "things_orchestrator.deployment.distribution", lambda _name: Distribution()
    )
    monkeypatch.setattr(
        "things_orchestrator.deployment.package_version", lambda: "0.9.0"
    )
    monkeypatch.setenv("THINGS_ORCHESTRATOR_COMMIT", "b" * 40)

    identity = installed_identity()

    assert identity.version == "0.9.0"
    assert identity.commit == commit
    assert identity.requested_revision == "v0.9.0"
    assert identity.source == "pep610"


def test_installed_identity_rejects_malformed_pep610_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Distribution:
        def read_text(self, _filename: str) -> str | None:
            return json.dumps(
                {"vcs_info": {"vcs": "git", "commit_id": "not-a-commit"}}

            )

    monkeypatch.setattr(
        "things_orchestrator.deployment.distribution", lambda _name: Distribution()
    )
    monkeypatch.setattr(
        "things_orchestrator.deployment._checkout_commit", lambda: None
    )

    identity = installed_identity()

    assert identity.commit is None
    assert identity.source == "unknown"
