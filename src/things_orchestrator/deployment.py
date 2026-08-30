"""Deployed package identity for health, logs, and MCP initialize."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .cloud import _CACHE_VERSION
from .v2 import DESCRIPTIONS, MODELS, PublicResult

PACKAGE_NAME = "things-orchestrator"
CACHE_VERSION = _CACHE_VERSION
CAPABILITIES = {
    "owner_safe_v2": True,
    "default_eight": True,
    "immutable_operations": True,
    "account_outcome_fence": True,
    "signed_host_authorization": True,
    "hmac_receipt_cursors": True,
    "legacy_cutover_report": True,
    "seven_day_retention": True,
    "permanent_tombstones": True,
    "taint_marked_things_text": True,
    "rt1_task_recurrence": True,
    "rt1_project_recurrence": True,
    "repeat_create_next": True,
    "rt2_recurrence_read_only": True,
    "advanced_scopes": False,
    "mutation_coach": False,
    "permanent_delete": False,
}


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


def git_commit() -> str | None:
    value = os.environ.get("THINGS_ORCHESTRATOR_COMMIT", "").strip()
    if value:
        return value
    root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = completed.stdout.strip()
    return commit if completed.returncode == 0 and commit else None


def _hash_payload(value: object) -> str:
    blob = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:24]


def tool_schema_hash() -> str:
    return _hash_payload(
        {
            "version": "v2",
            "inputs": {name: model.model_json_schema() for name, model in MODELS.items()},
            "output": PublicResult.model_json_schema(),
        }
    )


def tool_contract_hash() -> str:
    return _hash_payload(
        {
            "version": "v2",
            "inputs": {name: model.model_json_schema() for name, model in MODELS.items()},
            "output": PublicResult.model_json_schema(),
            "descriptions": DESCRIPTIONS,
        }
    )


def health_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "version": package_version(),
        "cache_version": CACHE_VERSION,
        "tool_schema_hash": tool_schema_hash(),
        "tool_contract_hash": tool_contract_hash(),
        "capabilities": dict(CAPABILITIES),
    }
    commit = git_commit()
    if commit is not None:
        payload["commit"] = commit
    return payload
