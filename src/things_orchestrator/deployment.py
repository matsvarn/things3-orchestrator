"""Deployed package identity for health, logs, and MCP initialize."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from . import PACKAGE_NAME as PACKAGE_NAME
from .cloud import _CACHE_VERSION
from .tools import (
    CLIENT_BUNDLE_FORMAT_VERSION,
    CLIENT_BUNDLE_PATH,
    tool_contract_hash,
    tool_discovery_hash,
    tool_schema_hash,
)

CACHE_VERSION = _CACHE_VERSION
_GIT_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
CAPABILITIES = {
    "bounded_v2": True,
    "shared_bearer_write_authority": True,
    "default_eight": True,
    "immutable_operations": True,
    "account_outcome_fence": True,
    "signed_legacy_recovery": True,
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


@dataclass(frozen=True)
class DeploymentIdentity:
    version: str
    commit: str | None
    requested_revision: str | None
    source: Literal["pep610", "checkout", "unknown"]


def _git_commit_at(root: Path) -> str | None:
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
    if completed.returncode != 0 or _GIT_COMMIT.fullmatch(commit) is None:
        return None
    return commit.lower()


def _checkout_commit() -> str | None:
    return _git_commit_at(Path(__file__).resolve().parents[2])


def _direct_url() -> dict[str, object]:
    try:
        raw = distribution(PACKAGE_NAME).read_text("direct_url.json")
    except PackageNotFoundError:
        return {}
    if raw is None:
        return {}
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _direct_url_checkout_commit(payload: dict[str, object]) -> str | None:
    if not isinstance(payload.get("dir_info"), dict):
        return None
    raw_url = payload.get("url")
    if not isinstance(raw_url, str):
        return None
    parsed = urlsplit(raw_url)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    return _git_commit_at(Path(unquote(parsed.path)))


def installed_identity() -> DeploymentIdentity:
    payload = _direct_url()
    vcs = payload.get("vcs_info")
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        commit = vcs.get("commit_id")
        requested = vcs.get("requested_revision")
        if isinstance(commit, str) and _GIT_COMMIT.fullmatch(commit):
            return DeploymentIdentity(
                version=package_version(),
                commit=commit.lower(),
                requested_revision=requested if isinstance(requested, str) else None,
                source="pep610",
            )
    commit = _direct_url_checkout_commit(payload) or _checkout_commit()
    return DeploymentIdentity(
        version=package_version(),
        commit=commit,
        requested_revision=None,
        source="checkout" if commit is not None else "unknown",
    )


def git_commit() -> str | None:
    return installed_identity().commit


def skill_path() -> Path:
    path = Path(__file__).resolve().with_name("skills") / "things-orchestrator"
    if not (path / "SKILL.md").is_file():
        raise RuntimeError("The installed package does not contain the Things skill")
    return path


def health_payload(*, authenticated: bool = False) -> dict[str, object]:
    if not authenticated:
        return {"ok": True}
    payload: dict[str, object] = {
        "ok": True,
        "version": package_version(),
        "cache_version": CACHE_VERSION,
        "tool_schema_hash": tool_schema_hash(),
        "tool_contract_hash": tool_contract_hash(),
        "tool_discovery_hash": tool_discovery_hash(),
        "client_bundle": {
            "format_version": CLIENT_BUNDLE_FORMAT_VERSION,
            "path": CLIENT_BUNDLE_PATH,
        },
        "capabilities": dict(CAPABILITIES),
    }
    commit = git_commit()
    if commit is not None:
        payload["commit"] = commit
    return payload
