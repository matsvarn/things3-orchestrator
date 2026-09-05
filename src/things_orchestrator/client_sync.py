"""Client-only host bundle fetch, instruction sync, and connection checks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Literal, TextIO

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ListToolsResult, Tool

from .client_bundle import (
    CATALOG_EPOCH,
    CATALOG_POLICY,
    RESERVED_PREFIX,
    BundleError,
    ClientBundle,
    ComponentHashes,
    parse_client_bundle,
)
from .config import ConfigError, McpBearer, McpUrl, normalize_mcp_url
from .tools import (
    CLIENT_BUNDLE_PATH,
    ITEM_ID,
    advertised_tool_payload,
    content_sha256,
    hash_payload,
    tool_discovery_hash,
)

MARKER_NAME = ".things-orchestrator-client.json"
PENDING_NAME = ".things-orchestrator-client.pending.json"
STAGING_NAME = ".things-orchestrator-staging"
LOCK_NAME = ".things-orchestrator-client.lock"
DEFAULT_TOKEN_ENV = "THINGS_MCP_TOKEN"
MAX_BUNDLE_BYTES = 1_048_576
_STATE_NAMES = frozenset({MARKER_NAME, PENDING_NAME, STAGING_NAME, LOCK_NAME})

BundleFetcher = Callable[[McpUrl, McpBearer], bytes]
DiscoveryProbe = Callable[[McpUrl, McpBearer], tuple[Tool, ...]]
ReadProbe = Callable[[McpUrl, McpBearer, str], dict[str, object]]
ApplyInterrupt = Callable[[str], None]
CatalogVerdict = Literal[
    "match", "recommended_refresh", "required_refresh", "required_review"
]


class ClientSyncError(RuntimeError):
    """The client sync cannot apply the host bundle safely."""

    def __init__(self, message: str, *, kind: str = "sync") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class SyncReport:
    server: dict[str, object]
    managed_files: dict[str, object]
    client_cache: dict[str, object]
    read: dict[str, object]
    required_actions: tuple[str, ...]
    recommended_actions: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": (
                "action_required" if self.required_actions
                else "files_synced_client_unverified"
            ),
            "client_cache": dict(self.client_cache),
            "managed_files": dict(self.managed_files),
            "read": dict(self.read),
            "recommended_actions": list(self.recommended_actions),
            "required_actions": list(self.required_actions),
            "server": dict(self.server),
        }


@dataclass(frozen=True, slots=True)
class _StoredState:
    files: dict[str, str] | None
    version: str | None
    components: ComponentHashes | None


def resolve_client_token(
    token_env: str,
    *,
    environ: dict[str, str] | None = None,
    prompt: Callable[[str], str] | None = None,
    tty: bool | None = None,
) -> McpBearer:
    if not token_env or token_env.startswith("-"):
        raise ConfigError("token-env needs an environment variable name")
    source = os.environ if environ is None else environ
    value = source.get(token_env, "").strip()
    if value:
        return McpBearer(value)
    interactive = sys.stdin.isatty() if tty is None else tty
    if not interactive:
        raise ConfigError(
            f"Set {token_env} or run client-sync in a private terminal"
        )
    reader = prompt or getpass
    secret = reader("MCP bearer: ").strip()
    if not secret:
        raise ConfigError("The MCP bearer is empty")
    return McpBearer(secret)


def run_client_sync(
    *,
    url: McpUrl,
    directory: Path,
    bearer: McpBearer,
    observed_tools: Path | None = None,
    read_id: str | None = None,
    fetch_bundle: BundleFetcher | None = None,
    discover: DiscoveryProbe | None = None,
    read_item: ReadProbe | None = None,
    apply_interrupt: ApplyInterrupt | None = None,
) -> SyncReport:
    if read_id is not None:
        _validate_read_id(read_id)
    try:
        raw = (fetch_bundle or fetch_client_bundle)(url, bearer)
        bundle = parse_client_bundle(raw)
        tools = (discover or discover_tools)(url, bearer)
    except BundleError as error:
        raise ClientSyncError(str(error)) from error
    except ClientSyncError:
        raise
    except Exception as error:
        raise ClientSyncError(
            f"{url}: authenticated client fetch failed: {_message(error)}"
        ) from None

    live_hash = tool_discovery_hash(tools)
    bundle_hash = bundle.fingerprints["tool_discovery_hash"]
    server: dict[str, object] = {
        "bundle_path": CLIENT_BUNDLE_PATH,
        "commit": bundle.package.commit,
        "discovery_hash": live_hash,
        "bundle_discovery_hash": bundle_hash,
        "match": live_hash == bundle_hash,
        "version": bundle.package.version,
    }
    if live_hash != bundle_hash:
        raise ClientSyncError(
            "fresh MCP discovery does not match the host client bundle"
        )

    cache, cache_required, cache_recommended = _client_cache(
        observed_tools, tools, url, bundle.client_impact
    )
    files = apply_managed_files(
        directory, bundle, interrupt=apply_interrupt
    )
    read, read_required = _read_result(
        url, bearer, read_id, read_item=read_item
    )
    file_recommended = _file_recommendations(files.get("changed"))
    required = tuple(cache_required + read_required)
    recommended = tuple(cache_recommended + file_recommended)
    return SyncReport(
        server=server,
        managed_files=files,
        client_cache=cache,
        read=read,
        required_actions=required,
        recommended_actions=recommended,
    )


def fetch_client_bundle(url: McpUrl, bearer: McpBearer) -> bytes:
    headers = {"Authorization": f"Bearer {bearer.reveal()}"}
    target = f"{url.origin}{CLIENT_BUNDLE_PATH}"
    try:
        with httpx2.Client(
            headers=headers,
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream("GET", target) as response:
                return _read_bounded_body(url, response)
    except ClientSyncError:
        raise
    except httpx2.HTTPError as error:
        raise ClientSyncError(
            f"{url}: client bundle fetch failed: {_message(error)}"
        ) from None


def discover_tools(url: McpUrl, bearer: McpBearer) -> tuple[Tool, ...]:
    return anyio.run(_discover_tools, url, bearer)


def fetch_fresh_read(
    url: McpUrl, bearer: McpBearer, item_id: str
) -> dict[str, object]:
    return anyio.run(_fresh_read, url, bearer, item_id)


def apply_managed_files(
    directory: Path,
    bundle: ClientBundle,
    *,
    interrupt: ApplyInterrupt | None = None,
) -> dict[str, object]:
    try:
        root = _prepare_root(directory)
        with _directory_lock(root):
            return _apply_managed_files(root, bundle, interrupt=interrupt)
    except OSError:
        raise ClientSyncError(
            "Cannot update the managed directory. Check permissions and free disk space, "
            "then rerun client-sync to resume."
        ) from None


@contextmanager
def _directory_lock(root: Path) -> Iterator[None]:
    path = root / LOCK_NAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ClientSyncError("managed lock is not a regular file")
    with path.open("a+b") as lock:
        try:
            if sys.platform == "win32":
                import msvcrt

                if path.stat().st_size == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ClientSyncError("Another client-sync owns this directory; retry after it finishes.") from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _apply_managed_files(
    root: Path, bundle: ClientBundle, *, interrupt: ApplyInterrupt | None
) -> dict[str, object]:
    marker_path = root / MARKER_NAME
    pending_path = root / PENDING_NAME
    staging = root / STAGING_NAME
    existing = _existing_entries(root)
    marker = _load_state(marker_path, existing, MARKER_NAME)
    pending = _load_state(pending_path, existing, PENDING_NAME)
    owned = _owned_files(marker, pending)
    previous_version = marker.version
    planned = {item.path: item for item in bundle.files}
    _reject_on_disk_collisions(root, planned)

    extras = sorted(
        path
        for path in existing
        if path not in planned and path not in _STATE_NAMES
    )
    removals: list[str] = []
    unmanaged: list[str] = []
    for path in extras:
        previous = None if owned is None else owned.get(path)
        destination = _safe_destination(root, path)
        if previous is None:
            unmanaged.append(path)
            continue
        if destination.is_symlink() or not destination.is_file():
            raise ClientSyncError(f"refusing to replace non-file path: {path}")
        current = content_sha256(destination.read_bytes())
        if current not in previous:
            raise ClientSyncError(f"refusing to remove edited file: {path}")
        removals.append(path)
    if unmanaged:
        raise ClientSyncError(
            "directory contains unmanaged files: " + ", ".join(unmanaged)
        )
    if owned is None and existing - _STATE_NAMES:
        raise ClientSyncError(
            "directory is not empty and is not a managed client tree"
        )

    for path, item in planned.items():
        destination = _safe_destination(root, path)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                raise ClientSyncError(f"refusing to replace non-file path: {path}")
            current = content_sha256(destination.read_bytes())
            known = {item.sha256}
            if owned is not None and path in owned:
                known.update(owned[path])
            if current not in known:
                raise ClientSyncError(f"refusing to overwrite customized file: {path}")

    changed = _component_changes(marker.components, bundle.component_hashes)
    _write_state(pending_path, bundle)
    if interrupt is not None:
        interrupt("after_pending")

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        for item in bundle.files:
            staged = _safe_destination(staging, item.path)
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(item.content.encode("utf-8"))
            if content_sha256(staged.read_bytes()) != item.sha256:
                raise ClientSyncError(f"staged file checksum failed: {item.path}")
        unchanged = True
        for item in bundle.files:
            destination = _safe_destination(root, item.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file() and not destination.is_symlink():
                if content_sha256(destination.read_bytes()) == item.sha256:
                    continue
            unchanged = False
            _safe_destination(staging, item.path).replace(destination)
        for path in removals:
            _safe_destination(root, path).unlink()
            _remove_empty_parents(root, path)
        if interrupt is not None:
            interrupt("before_marker")
        _write_state(marker_path, bundle)
        if pending_path.exists() or pending_path.is_symlink():
            pending_path.unlink()
        status = "unchanged" if unchanged and not removals else "synced"
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "changed": changed,
        "directory": str(root),
        "files": sorted(planned),
        "host_version": bundle.package.version,
        "previous_version": previous_version,
        "status": status,
    }


def load_observed_tools(path: Path) -> tuple[Tool, ...]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ClientSyncError("observed-tools JSON is unreadable") from error
    if isinstance(raw, dict) and "tools" not in raw and isinstance(raw.get("result"), dict):
        raw = raw["result"]
    try:
        parsed = ListToolsResult.model_validate(raw)
    except Exception as error:
        raise ClientSyncError("observed-tools JSON is not a tools/list result") from error
    return tuple(parsed.tools)


def classify_catalog_delta(
    observed: tuple[Tool, ...],
    live: tuple[Tool, ...],
    impact: Mapping[str, object],
) -> CatalogVerdict:
    observed_hash = hash_payload(
        [advertised_tool_payload(tool) for tool in observed]
    )
    live_hash = tool_discovery_hash(live)
    if observed_hash == live_hash:
        return "match"
    policy = impact.get("catalog_policy")
    epoch = impact.get("catalog_epoch")
    known_contract = policy == CATALOG_POLICY and epoch == CATALOG_EPOCH
    observed_names = tuple(tool.name for tool in observed)
    live_names = tuple(tool.name for tool in live)
    if observed_names != live_names:
        return "required_review"
    if not known_contract:
        return "required_review"
    closed_to_additive = False
    live_by_name = {tool.name: tool for tool in live}
    for observed_tool in observed:
        live_tool = live_by_name[observed_tool.name]
        if (
            observed_tool.input_schema != live_tool.input_schema
            or observed_tool.annotations != live_tool.annotations
        ):
            return "required_review"
        delta = _output_schema_delta(observed_tool.output_schema, live_tool.output_schema)
        if delta == "required_review":
            return delta
        closed_to_additive |= delta == "required_refresh"
    if closed_to_additive:
        return "required_refresh"
    return "recommended_refresh"


def _client_cache(
    observed_tools: Path | None,
    server_tools: tuple[Tool, ...],
    url: McpUrl,
    impact: Mapping[str, object],
) -> tuple[dict[str, object], list[str], list[str]]:
    refresh = _catalog_refresh_action(url)
    if observed_tools is None:
        return (
            {
                "status": "unknown",
                "note": (
                    "No observed-tools snapshot. Export this client's tools/list "
                    "JSON and rerun with --observed-tools PATH. Downloaded "
                    "instruction files do not activate a provider prompt or "
                    "application skill."
                ),
            },
            [],
            [
                (
                    "Export this client's tools/list JSON and rerun with "
                    "--observed-tools PATH."
                ),
                refresh,
            ],
        )
    observed = load_observed_tools(observed_tools)
    observed_hash = hash_payload(
        [advertised_tool_payload(tool) for tool in observed]
    )
    server_hash = tool_discovery_hash(server_tools)
    if observed_hash == server_hash:
        return (
            {
                "status": "matches_snapshot",
                "note": (
                    "Observed tools/list snapshot matches server discovery. "
                    "This is not proof of the current connection."
                ),
                "observed_hash": observed_hash,
            },
            [],
            [],
        )
    verdict = classify_catalog_delta(observed, server_tools, impact)
    cache: dict[str, object] = {
        "status": "stale",
        "note": (
            "Observed tools/list snapshot differs from server discovery. "
            "This is not proof of the current connection."
        ),
        "observed_hash": observed_hash,
        "server_hash": server_hash,
        "catalog_delta": verdict,
    }
    if verdict == "recommended_refresh":
        return cache, [], [refresh]
    if verdict == "required_refresh":
        return cache, [refresh], []
    return (
        cache,
        ["Review this client's tools/list against the host catalog."],
        [],
    )


def _file_recommendations(changed: object) -> list[str]:
    if not isinstance(changed, dict):
        return []
    actions: list[str] = []
    if changed.get("skill") is True:
        actions.append(
            "Reapply the Things skill from the synced directory. "
            "Downloaded files do not activate an application skill."
        )
    if changed.get("routines_receiver") is True:
        actions.append(
            "Review the saved routine receiver prompt. "
            "Downloaded files do not activate it."
        )
    templates = changed.get("routine_templates")
    if isinstance(templates, list):
        for path in templates:
            if isinstance(path, str) and path:
                actions.append(
                    f"Review the saved routine template {path}. "
                    "Downloaded files do not activate it."
                )
    return actions


def _component_changes(
    previous: ComponentHashes | None, current: ComponentHashes
) -> dict[str, object]:
    previous_templates = {} if previous is None else previous.routine_templates
    changed_templates = sorted(
        {
            path
            for path, digest in current.routine_templates.items()
            if previous_templates.get(path) != digest
        }
        | (set(previous_templates) - set(current.routine_templates))
    )
    return {
        "routine_templates": changed_templates,
        "routines_receiver": previous is None
        or previous.routines_receiver != current.routines_receiver,
        "skill": previous is None or previous.skill != current.skill,
    }


def _read_result(
    url: McpUrl,
    bearer: McpBearer,
    read_id: str | None,
    *,
    read_item: ReadProbe | None,
) -> tuple[dict[str, object], list[str]]:
    if read_id is None:
        return ({"status": "skipped", "source": "fresh_connection"}, [])
    probe = read_item or fetch_fresh_read
    try:
        payload = probe(url, bearer, read_id)
    except Exception as error:
        return (
            {
                "status": "failed",
                "category": (
                    error.kind if isinstance(error, ClientSyncError)
                    else _transport_failure_kind(error)
                ),
                "source": "fresh_connection",
                "note": "Bounded things_get on the fresh connection failed.",
            },
            ["Retry things_get on this HTTP connection; do not treat this as activation success."],
        )
    state = payload.get("state")
    if state == "ok":
        return (
            {
                "status": "ok",
                "source": "fresh_connection",
                "code": payload.get("code"),
                "note": "Fresh connection read only. This is not provider skill activation.",
            },
            [],
        )
    return (
        {
            "status": "not_ok",
            "category": "application",
            "source": "fresh_connection",
            "state": state,
            "code": payload.get("code"),
            "note": "Fresh connection read was not ok. This is not activation success.",
        },
        ["Inspect the fresh-connection things_get result before retrying a write."],
    )


def _catalog_refresh_action(url: McpUrl) -> str:
    if url.origin.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:")):
        return (
            "Reconnect the HTTP MCP session so the client repeats tools/list. "
            "Same-host stdio serve is a separate server; restart that process "
            "if that is the connection in use."
        )
    return (
        "Reconnect the HTTP MCP connector so the client repeats tools/list "
        "against this host. Hosted connectors and local stdio bridges are "
        "separate runtimes."
    )


def _validate_read_id(read_id: str) -> None:
    import re

    if re.fullmatch(ITEM_ID, read_id) is None:
        raise ConfigError("read-id needs one exact typed Things ID")


def _prepare_root(directory: Path) -> Path:
    root = directory.expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root = Path(os.path.normpath(root))
    if root.is_symlink():
        raise ClientSyncError("managed directory is a symlink")
    if root.exists() and not root.is_dir():
        raise ClientSyncError("directory is not a directory")
    # The caller chooses the root. macOS itself aliases /var and /tmp.
    # Bundle paths are checked against this canonical root without following links.
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ClientSyncError("managed directory is a symlink")
    return root


def _existing_entries(root: Path) -> set[str]:
    names: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        relative_dir = current.relative_to(root).as_posix()
        if relative_dir == STAGING_NAME or relative_dir.startswith(STAGING_NAME + "/"):
            dirnames[:] = []
            continue
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            relative = child.relative_to(root).as_posix()
            if relative == STAGING_NAME:
                continue
            if child.is_symlink():
                names.add(relative)
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            relative = (current / name).relative_to(root).as_posix()
            first = relative.split("/", 1)[0]
            if first.startswith(RESERVED_PREFIX) and first.endswith(".tmp"):
                continue
            names.add(relative)
    return names


def _load_state(path: Path, existing: set[str], name: str) -> _StoredState:
    if name not in existing:
        return _StoredState(files=None, version=None, components=None)
    if path.is_symlink() or not path.is_file():
        raise ClientSyncError("managed marker is not a regular file")
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ClientSyncError("managed marker is unreadable") from error
    if not isinstance(payload, dict):
        raise ClientSyncError("managed marker is unreadable")
    if payload.get("package_name") != "things-orchestrator":
        raise ClientSyncError("directory contains foreign managed state")
    files = payload.get("files")
    version = payload.get("package_version")
    if not isinstance(files, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in files.items()
    ):
        raise ClientSyncError("managed marker is unreadable")
    if not isinstance(version, str) or not version:
        raise ClientSyncError("managed marker is unreadable")
    components = _stored_components(payload.get("component_hashes"))
    return _StoredState(
        files={str(key): str(value) for key, value in files.items()},
        version=version,
        components=components,
    )


def _stored_components(value: object) -> ComponentHashes | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ClientSyncError("managed marker is unreadable")
    templates = value.get("routine_templates")
    tools = value.get("tools")
    skill = value.get("skill")
    receiver = value.get("routines_receiver")
    if set(value) != {"routine_templates", "routines_receiver", "skill", "tools"}:
        raise ClientSyncError("managed marker is unreadable")
    if not isinstance(templates, dict) or not isinstance(tools, str):
        raise ClientSyncError("managed marker is unreadable")
    if not isinstance(skill, str) or not isinstance(receiver, str):
        raise ClientSyncError("managed marker is unreadable")
    parsed_templates: dict[str, str] = {}
    for key, digest in templates.items():
        if not isinstance(key, str) or not isinstance(digest, str):
            raise ClientSyncError("managed marker is unreadable")
        parsed_templates[key] = digest
    return ComponentHashes(
        tools=tools,
        skill=skill,
        routines_receiver=receiver,
        routine_templates=parsed_templates,
    )


def _owned_files(marker: _StoredState, pending: _StoredState) -> dict[str, set[str]] | None:
    if marker.files is None and pending.files is None:
        return None
    owned: dict[str, set[str]] = {}
    for state in (marker, pending):
        for path, digest in (state.files or {}).items():
            owned.setdefault(path, set()).add(digest)
    return owned


def _write_state(path: Path, bundle: ClientBundle) -> None:
    payload: dict[str, object] = {
        "commit": bundle.package.commit,
        "component_hashes": bundle.component_hashes.as_dict(),
        "files": {item.path: item.sha256 for item in bundle.files},
        "format_version": 1,
        "package_name": bundle.package.name,
        "package_version": bundle.package.version,
    }
    _atomic_write_json(path, payload)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f"{RESERVED_PREFIX}state-", suffix=".tmp", delete=False
    ) as temporary:
        tmp = Path(temporary.name)
        temporary.write(text.encode("utf-8"))
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _safe_destination(root: Path, relative: str) -> Path:
    if _is_reserved_path(relative):
        raise ClientSyncError("client bundle path collides with managed state")
    candidate = root
    for part in relative.split("/"):
        candidate = candidate / part
        if candidate.is_symlink():
            raise ClientSyncError(f"refusing to replace non-file path: {relative}")
    if not _is_within(candidate, root):
        raise ClientSyncError(f"path escapes the managed directory: {relative}")
    return candidate


def _is_reserved_path(path: str) -> bool:
    return any(part.startswith(RESERVED_PREFIX) for part in path.split("/"))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_on_disk_collisions(
    root: Path, planned: Mapping[str, object]
) -> None:
    for path in planned:
        destination = _safe_destination(root, path)
        if destination.exists() and destination.is_dir() and not destination.is_symlink():
            raise ClientSyncError(f"refusing to replace non-file path: {path}")
        parent = destination.parent
        while parent != root and _is_within(parent, root):
            if parent.exists() and parent.is_file():
                raise ClientSyncError(
                    f"client bundle path collides with a directory: {path}"
                )
            parent = parent.parent


def _remove_empty_parents(root: Path, relative: str) -> None:
    parent = (_safe_destination(root, relative)).parent
    while parent != root and _is_within(parent, root):
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _read_bounded_body(url: McpUrl, response: httpx2.Response) -> bytes:
    if response.status_code == 401:
        raise ClientSyncError(f"{url}: stored bearer was rejected")
    if response.status_code == 404:
        raise ClientSyncError(
            "This host does not provide client bundles. Upgrade the host to a "
            "bundle-capable release or follow its release-specific client setup."
        )
    if response.status_code != 200:
        raise ClientSyncError(
            f"{url}: client bundle fetch failed: HTTP {response.status_code}"
        )
    length = response.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            declared = MAX_BUNDLE_BYTES + 1
        if declared > MAX_BUNDLE_BYTES:
            raise ClientSyncError("client bundle exceeds the size bound")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        incoming = len(chunk)
        if total + incoming > MAX_BUNDLE_BYTES:
            raise ClientSyncError("client bundle exceeds the size bound")
        total += incoming
        chunks.append(chunk)
    return b"".join(chunks)


def _output_schema_delta(observed: object, live: object) -> CatalogVerdict:
    """Recognize output additions only; leave all other changes for review."""
    if observed == live:
        return "match"
    if isinstance(observed, list) and isinstance(live, list):
        if len(observed) != len(live):
            return "required_review"
        deltas = [_output_schema_delta(old, new) for old, new in zip(observed, live)]
    elif isinstance(observed, dict) and isinstance(live, dict):
        old = dict(observed)
        new = dict(live)
        for descriptive in ("description", "title", "examples"):
            old.pop(descriptive, None)
            new.pop(descriptive, None)
        deltas = []
        if "properties" in old and "properties" in new:
            old_properties = old.pop("properties")
            new_properties = new.pop("properties")
            if not isinstance(old_properties, dict) or not isinstance(new_properties, dict):
                return "required_review"
            if old_properties.keys() - new_properties.keys():
                return "required_review"
            old_additional = old.get("additionalProperties", True)
            new_additional = new.get("additionalProperties", True)
            if old_additional is False and new_additional is True:
                deltas.append("required_refresh")
                old["additionalProperties"] = True
            added = new_properties.keys() - old_properties.keys()
            if added and old_additional is not True:
                if new_additional is not True or old_additional is not False:
                    return "required_review"
                deltas.append("required_refresh")
            old_required = old.get("required", [])
            new_required = new.get("required", [])
            if not isinstance(old_required, list) or not isinstance(new_required, list):
                return "required_review"
            # Newly added output fields may be required by the new server.
            # Existing required fields must still be present in every result.
            new["required"] = [name for name in new_required if name not in added]
            old["required"] = old_required
            deltas.extend(
                _output_schema_delta(value, new_properties[name])
                for name, value in old_properties.items()
            )
        if old.keys() != new.keys():
            return "required_review"
        deltas.extend(_output_schema_delta(value, new[name]) for name, value in old.items())
    else:
        return "required_review"
    if "required_review" in deltas:
        return "required_review"
    if "required_refresh" in deltas:
        return "required_refresh"
    return "recommended_refresh"


async def _with_session(
    url: McpUrl,
    bearer: McpBearer,
    operation: Callable[[ClientSession], Awaitable[object]],
) -> object:
    headers = {"Authorization": f"Bearer {bearer.reveal()}"}
    try:
        async with httpx2.AsyncClient(
            headers=headers,
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with streamable_http_client(
                str(url), http_client=client
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await operation(session)
    except ClientSyncError:
        raise
    except Exception as error:
        raise ClientSyncError(
            f"{url}: authenticated MCP round trip failed: {_message(error)}",
            kind=_transport_failure_kind(error),
        ) from None


async def _discover_tools(url: McpUrl, bearer: McpBearer) -> tuple[Tool, ...]:
    listed = await _with_session(url, bearer, ClientSession.list_tools)
    if not isinstance(listed, ListToolsResult):
        raise ClientSyncError("fresh MCP discovery returned no tool list")
    return tuple(listed.tools)


async def _fresh_read(
    url: McpUrl, bearer: McpBearer, item_id: str
) -> dict[str, object]:
    async def call(session: ClientSession) -> dict[str, object]:
        result = await session.call_tool("things_get", {"ids": [item_id]})
        structured = result.structured_content
        if not isinstance(structured, dict):
            raise ClientSyncError(
                "fresh connection things_get returned no JSON object"
            )
        return dict(structured)

    payload = await _with_session(url, bearer, call)
    if not isinstance(payload, dict):
        raise ClientSyncError("fresh connection things_get returned no JSON object")
    return payload


def _message(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(_message(item) for item in error.exceptions)
    return str(error) or type(error).__name__


def _transport_failure_kind(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        kinds = {_transport_failure_kind(item) for item in error.exceptions}
        return next(
            (kind for kind in ("authentication", "reachability") if kind in kinds),
            "protocol",
        )
    if isinstance(error, httpx2.HTTPStatusError) and error.response.status_code in {401, 403}:
        return "authentication"
    if isinstance(error, (httpx2.TimeoutException, httpx2.ConnectError, ConnectionError)):
        return "reachability"
    return "protocol"


def failed_sync_report(message: str) -> SyncReport:
    return SyncReport(
        server={"status": "not_verified", "error": message},
        managed_files={"status": "not_verified"},
        client_cache={"status": "unknown"},
        read={"status": "skipped", "source": "fresh_connection"},
        required_actions=(
            "Fix authentication, reachability, or the host bundle, then rerun client-sync.",
        ),
        recommended_actions=(),
    )


def write_report(report: SyncReport, stream: TextIO | None = None) -> None:
    print(json.dumps(report.as_dict(), sort_keys=True), file=stream or sys.stdout)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        required=True,
        help="HTTPS origin, loopback HTTP origin, or /mcp URL of the serving host",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="directory that will hold the managed instruction tree",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help="environment variable that holds the MCP bearer (default: THINGS_MCP_TOKEN)",
    )
    parser.add_argument(
        "--observed-tools",
        type=Path,
        default=None,
        help="tools/list JSON exported from the actual client catalog",
    )
    parser.add_argument(
        "--read-id",
        default=None,
        help="exact typed Things ID for a bounded fresh-connection things_get",
    )


def run_command(args: argparse.Namespace) -> None:
    try:
        url = normalize_mcp_url(args.url)
        bearer = resolve_client_token(args.token_env)
        report = run_client_sync(
            url=url,
            directory=args.directory,
            bearer=bearer,
            observed_tools=args.observed_tools,
            read_id=args.read_id,
        )
    except (ConfigError, ClientSyncError) as error:
        write_report(failed_sync_report(str(error)))
        raise SystemExit(1) from None
    write_report(report)
    if report.required_actions:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="things-orchestrator client-sync", allow_abbrev=False)
    add_arguments(parser)
    run_command(parser.parse_args(argv))
