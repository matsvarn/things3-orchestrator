"""Deterministic client instruction bundle for the installed host release."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from mcp.types import Tool

from . import PACKAGE_NAME
from .routines_config import ROUTINE_RECEIVER_INSTRUCTION
from .tools import (
    CLIENT_BUNDLE_FORMAT_VERSION,
    advertised_tool_payload,
    advertised_tools,
    content_sha256,
    hash_payload,
    tool_contract_hash,
    tool_discovery_hash,
    tool_schema_hash,
)

RECEIVER_INSTRUCTION_PATH = "routines/receiver-instruction.txt"
CATALOG_POLICY = "additive_output_v1"
CATALOG_EPOCH = 1
RESERVED_PREFIX = ".things-orchestrator-"
_KNOWN_KEYS = frozenset(
    {
        "advertised_tools",
        "bundle_checksum",
        "client_impact",
        "component_hashes",
        "files",
        "fingerprints",
        "format_version",
        "package",
    }
)
_PACKAGE_KEYS = frozenset({"commit", "name", "version"})
_FINGERPRINT_KEYS = frozenset(
    {"tool_contract_hash", "tool_discovery_hash", "tool_schema_hash"}
)
_COMPONENT_KEYS = frozenset(
    {"routine_templates", "routines_receiver", "skill", "tools"}
)
_IMPACT_KEYS = frozenset(
    {
        "catalog_epoch",
        "catalog_policy",
        "input_schema",
        "output_schema",
        "unknown_outcomes",
    }
)
_FILE_KEYS = frozenset({"content", "path", "sha256"})
_TOOL_REQUIRED_KEYS = frozenset(
    {"description", "inputSchema", "name", "outputSchema"}
)
_TOOL_OPTIONAL_KEYS = frozenset({"annotations"})
_MAX_FILES = 64
_MAX_FILE_BYTES = 256_000
_MAX_PATH_LENGTH = 200
_COMMIT_HEX = frozenset("0123456789abcdef")


class BundleError(ValueError):
    """The client bundle is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class BundleFile:
    path: str
    sha256: str
    content: str


@dataclass(frozen=True, slots=True)
class PackageIdentity:
    name: str
    version: str
    commit: str | None


@dataclass(frozen=True, slots=True)
class ComponentHashes:
    tools: str
    skill: str
    routines_receiver: str
    routine_templates: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "routine_templates": dict(self.routine_templates),
            "routines_receiver": self.routines_receiver,
            "skill": self.skill,
            "tools": self.tools,
        }


@dataclass(frozen=True, slots=True)
class ClientBundle:
    format_version: int
    package: PackageIdentity
    advertised_tools: tuple[dict[str, object], ...]
    fingerprints: dict[str, str]
    files: tuple[BundleFile, ...]
    component_hashes: ComponentHashes
    client_impact: dict[str, object]
    bundle_checksum: str


def is_named_routine_template(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        len(parts) == 2
        and parts[0] == "references"
        and parts[1].startswith("routine-")
        and parts[1].endswith(".md")
    )


def encode_client_bundle() -> bytes:
    from .deployment import git_commit, package_version

    tools = advertised_tools()
    files = _installed_files()
    return encode_client_bundle_from(
        package=PackageIdentity(
            name=PACKAGE_NAME,
            version=package_version(),
            commit=git_commit(),
        ),
        tools=tools,
        files=files,
    )


def encode_client_bundle_from(
    *,
    package: PackageIdentity,
    tools: tuple[Tool, ...],
    files: tuple[BundleFile, ...],
    client_impact: dict[str, object] | None = None,
) -> bytes:
    _reject_file_path_collisions(item.path for item in files)
    for item in files:
        if not _safe_relative_path(item.path):
            raise BundleError("client bundle file path is unsafe")
    payloads = tuple(advertised_tool_payload(tool) for tool in tools)
    components = component_hashes_for(tools, files)
    document: dict[str, object] = {
        "format_version": CLIENT_BUNDLE_FORMAT_VERSION,
        "package": {
            "name": package.name,
            "version": package.version,
            "commit": package.commit,
        },
        "advertised_tools": list(payloads),
        "fingerprints": {
            "tool_schema_hash": tool_schema_hash(tools),
            "tool_contract_hash": tool_contract_hash(tools),
            "tool_discovery_hash": tool_discovery_hash(tools),
        },
        "component_hashes": components.as_dict(),
        "client_impact": client_impact if client_impact is not None else _client_impact(),
        "files": [
            {"path": item.path, "sha256": item.sha256, "content": item.content}
            for item in files
        ],
    }
    checksum = content_sha256(_canonical_bytes(document))
    document["bundle_checksum"] = checksum
    return _canonical_bytes(document)


def parse_client_bundle(raw: bytes) -> ClientBundle:
    if not raw or len(raw) > 1_048_576:
        raise BundleError("client bundle is empty or exceeds the size bound")
    try:
        payload: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleError("client bundle is not JSON") from error
    if not isinstance(payload, dict):
        raise BundleError("client bundle must be a JSON object")
    keys = set(payload)
    if keys != _KNOWN_KEYS:
        raise BundleError("client bundle has an unknown or incomplete format")
    format_version = payload.get("format_version")
    if format_version != CLIENT_BUNDLE_FORMAT_VERSION:
        raise BundleError("client bundle format is unsupported")
    checksum = _require_sha256(payload.get("bundle_checksum"), field="bundle_checksum")
    unsigned = dict(payload)
    unsigned.pop("bundle_checksum")
    if content_sha256(_canonical_bytes(unsigned)) != checksum:
        raise BundleError("client bundle checksum does not match")
    package = _parse_package(payload.get("package"))
    tools = _parse_advertised_tools(payload.get("advertised_tools"))
    fingerprints = _parse_string_map(payload.get("fingerprints"), _FINGERPRINT_KEYS)
    files = _parse_files(payload.get("files"))
    components = _parse_component_hashes(payload.get("component_hashes"), files)
    impact = _parse_client_impact(payload.get("client_impact"))
    discovery = hash_payload(list(tools))
    if fingerprints["tool_discovery_hash"] != discovery:
        raise BundleError("client bundle discovery hash does not match advertised tools")
    parsed_tools = tuple(Tool.model_validate(tool) for tool in tools)
    if (
        fingerprints["tool_schema_hash"] != tool_schema_hash(parsed_tools)
        or fingerprints["tool_contract_hash"] != tool_contract_hash(parsed_tools)
    ):
        raise BundleError("client bundle fingerprints do not match advertised tools")
    expected = component_hashes_for_payloads(discovery, files)
    if components != expected:
        raise BundleError("client bundle component hashes do not match files")
    return ClientBundle(
        format_version=CLIENT_BUNDLE_FORMAT_VERSION,
        package=package,
        advertised_tools=tools,
        fingerprints=fingerprints,
        files=files,
        component_hashes=components,
        client_impact=impact,
        bundle_checksum=checksum,
    )


def bundle_file(path: str, content: str) -> BundleFile:
    encoded = content.encode("utf-8")
    return BundleFile(path=path, sha256=content_sha256(encoded), content=content)


def component_hashes_for(
    tools: tuple[Tool, ...], files: tuple[BundleFile, ...]
) -> ComponentHashes:
    return component_hashes_for_payloads(tool_discovery_hash(tools), files)


def component_hashes_for_payloads(
    tools_hash: str, files: tuple[BundleFile, ...]
) -> ComponentHashes:
    receiver = next(
        (item for item in files if item.path == RECEIVER_INSTRUCTION_PATH),
        None,
    )
    if receiver is None:
        raise BundleError("client bundle is missing the routines receiver instruction")
    skill_hashes = [
        (item.path, item.sha256)
        for item in files
        if item.path != RECEIVER_INSTRUCTION_PATH
        and not is_named_routine_template(item.path)
    ]
    templates = {
        item.path: item.sha256
        for item in files
        if is_named_routine_template(item.path)
    }
    return ComponentHashes(
        tools=tools_hash,
        skill=hash_payload(skill_hashes),
        routines_receiver=receiver.sha256,
        routine_templates=templates,
    )


def _installed_files() -> tuple[BundleFile, ...]:
    from .deployment import skill_path

    root = skill_path()
    files = [
        bundle_file(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    files.append(bundle_file(RECEIVER_INSTRUCTION_PATH, ROUTINE_RECEIVER_INSTRUCTION))
    files.sort(key=lambda item: item.path)
    return tuple(files)


def _client_impact() -> dict[str, object]:
    return {
        "catalog_epoch": CATALOG_EPOCH,
        "catalog_policy": CATALOG_POLICY,
        "input_schema": "strict",
        "output_schema": "additive_object_properties",
        "unknown_outcomes": "constrained",
    }


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _parse_package(value: object) -> PackageIdentity:
    if not isinstance(value, dict) or set(value) != _PACKAGE_KEYS:
        raise BundleError("client bundle package identity is malformed")
    name = value.get("name")
    version = value.get("version")
    commit = value.get("commit")
    if name != PACKAGE_NAME or not isinstance(version, str) or not version:
        raise BundleError("client bundle package identity is malformed")
    if commit is not None and not _hex_commit(commit):
        raise BundleError("client bundle commit is malformed")
    return PackageIdentity(name=name, version=version, commit=commit)


def _hex_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in _COMMIT_HEX for character in value)
    )


def _parse_advertised_tools(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not value:
        raise BundleError("client bundle advertised tools are malformed")
    parsed: list[dict[str, object]] = []
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise BundleError("client bundle advertised tools are malformed")
        keys = set(item)
        if not _TOOL_REQUIRED_KEYS <= keys or keys - _TOOL_REQUIRED_KEYS - _TOOL_OPTIONAL_KEYS:
            raise BundleError("client bundle advertised tools are malformed")
        name = item.get("name")
        description = item.get("description")
        if not isinstance(name, str) or not name.startswith("things_"):
            raise BundleError("client bundle advertised tools are malformed")
        if not isinstance(description, str) or not description:
            raise BundleError("client bundle advertised tools are malformed")
        if not isinstance(item.get("inputSchema"), dict):
            raise BundleError("client bundle advertised tools are malformed")
        if not isinstance(item.get("outputSchema"), dict):
            raise BundleError("client bundle advertised tools are malformed")
        if "annotations" in item and not isinstance(item.get("annotations"), dict):
            raise BundleError("client bundle advertised tools are malformed")
        names.append(name)
        parsed.append(dict(item))
    if names != list(dict.fromkeys(names)):
        raise BundleError("client bundle advertised tools are duplicated")
    return tuple(parsed)


def _parse_string_map(value: object, expected: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != expected:
        raise BundleError("client bundle hash map is malformed")
    parsed: dict[str, str] = {}
    for key in expected:
        parsed[key] = _require_sha256(value.get(key), field=key)
    return parsed


def _parse_component_hashes(
    value: object, files: tuple[BundleFile, ...]
) -> ComponentHashes:
    if not isinstance(value, dict) or set(value) != _COMPONENT_KEYS:
        raise BundleError("client bundle hash map is malformed")
    templates = value.get("routine_templates")
    if not isinstance(templates, dict):
        raise BundleError("client bundle hash map is malformed")
    parsed_templates: dict[str, str] = {}
    for key, digest in templates.items():
        if not isinstance(key, str) or not is_named_routine_template(key):
            raise BundleError("client bundle routine template map is malformed")
        parsed_templates[key] = _require_sha256(digest, field=key)
    expected_paths = {
        item.path for item in files if is_named_routine_template(item.path)
    }
    if set(parsed_templates) != expected_paths:
        raise BundleError("client bundle routine template map is malformed")
    return ComponentHashes(
        tools=_require_sha256(value.get("tools"), field="tools"),
        skill=_require_sha256(value.get("skill"), field="skill"),
        routines_receiver=_require_sha256(
            value.get("routines_receiver"), field="routines_receiver"
        ),
        routine_templates=parsed_templates,
    )


def _parse_client_impact(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _IMPACT_KEYS:
        raise BundleError("client bundle client_impact is malformed")
    policy = value.get("catalog_policy")
    epoch = value.get("catalog_epoch")
    if not isinstance(policy, str) or not policy:
        raise BundleError("client bundle client_impact is malformed")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise BundleError("client bundle client_impact is malformed")
    for field in ("input_schema", "output_schema", "unknown_outcomes"):
        item = value.get(field)
        if not isinstance(item, str) or not item:
            raise BundleError("client bundle client_impact is malformed")
    return {
        "catalog_epoch": epoch,
        "catalog_policy": policy,
        "input_schema": value["input_schema"],
        "output_schema": value["output_schema"],
        "unknown_outcomes": value["unknown_outcomes"],
    }


def _parse_files(value: object) -> tuple[BundleFile, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_FILES:
        raise BundleError("client bundle files are malformed")
    files: list[BundleFile] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _FILE_KEYS:
            raise BundleError("client bundle files are malformed")
        path = item.get("path")
        content = item.get("content")
        digest = _require_sha256(item.get("sha256"), field="sha256")
        if not isinstance(path, str) or not _safe_relative_path(path):
            raise BundleError("client bundle file path is unsafe")
        if path in seen:
            raise BundleError("client bundle files are duplicated")
        if not isinstance(content, str):
            raise BundleError("client bundle file content is malformed")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise BundleError("client bundle file exceeds the size bound")
        if content_sha256(encoded) != digest:
            raise BundleError(f"client bundle file checksum does not match: {path}")
        seen.add(path)
        files.append(BundleFile(path=path, sha256=digest, content=content))
    _reject_file_path_collisions(seen)
    if RECEIVER_INSTRUCTION_PATH not in seen:
        raise BundleError("client bundle is missing the routines receiver instruction")
    if "SKILL.md" not in seen:
        raise BundleError("client bundle is missing SKILL.md")
    files.sort(key=lambda item: item.path)
    return tuple(files)


def _reject_file_path_collisions(paths: Iterable[str]) -> None:
    ordered = sorted(str(path) for path in paths)
    for index, path in enumerate(ordered):
        prefix = path + "/"
        for other in ordered[index + 1 :]:
            if other.startswith(prefix):
                raise BundleError(
                    f"client bundle path collides with a directory: {path}"
                )


def _safe_relative_path(path: str) -> bool:
    if not path or len(path) > _MAX_PATH_LENGTH:
        return False
    if path.startswith("/") or path.endswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    if any(part.startswith(RESERVED_PREFIX) for part in parts):
        return False
    return all(part not in {"", ".", ".."} and ":" not in part for part in parts)


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise BundleError(f"client bundle {field} is malformed")
    digest = value.removeprefix("sha256:")
    if any(character not in "0123456789abcdef" for character in digest):
        raise BundleError(f"client bundle {field} is malformed")
    if len(digest) not in {24, 64}:
        raise BundleError(f"client bundle {field} is malformed")
    return value
