"""Deployed package identity for health, logs, and MCP initialize."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version

from .cloud import _CACHE_VERSION

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
}


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


def git_commit() -> str | None:
    value = os.environ.get("THINGS_ORCHESTRATOR_COMMIT", "").strip()
    return value or None


def health_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "version": package_version(),
        "cache_version": CACHE_VERSION,
        "capabilities": dict(CAPABILITIES),
    }
    commit = git_commit()
    if commit is not None:
        payload["commit"] = commit
    return payload
