"""Deployed package identity for health, logs, and MCP initialize."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .cloud import _CACHE_VERSION
from .interface import (
    APPROVE_DESC,
    APPROVE_IN,
    APPROVE_OUT,
    COMMIT_DESC,
    COMMIT_IN,
    COMMIT_OUT,
    READ_DESC,
    READ_IN,
    READ_OUT,
    RESULT_OUT,
)

PACKAGE_NAME = "things-orchestrator"
CACHE_VERSION = _CACHE_VERSION
CAPABILITIES = {
    "repair_inbox_placement": True,
    "clear_someday": True,
    "area_view": True,
    "audit_view": True,
    "diagnostics_view": True,
    "bulk_ids": True,
    "trash_view": True,
    "same_batch_today_after": True,
    "project_teardown": True,
    "neighborhood_reads": True,
    "in_band_validation": True,
    "review_context": True,
    "native_heading_delete": True,
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
        [READ_IN, COMMIT_IN, APPROVE_IN, READ_OUT, COMMIT_OUT, APPROVE_OUT]
    )


def tool_contract_hash() -> str:
    return _hash_payload(
        [
            READ_IN,
            COMMIT_IN,
            APPROVE_IN,
            READ_OUT,
            COMMIT_OUT,
            APPROVE_OUT,
            READ_DESC,
            COMMIT_DESC,
            APPROVE_DESC,
            RESULT_OUT,
        ]
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
