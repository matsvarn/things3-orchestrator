"""Value-free diagnostics for Things Cloud and local deployment support."""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

from .cloud import CloudClient, CloudError, CloudLibrary
from .config import (
    ConfigError,
    Credentials,
    McpUrl,
    credentials_path,
    load_credentials,
    load_mcp_url,
    normalize_mcp_url,
)
from .deployment import (
    DeploymentIdentity,
    installed_identity,
    tool_contract_hash,
    tool_schema_hash,
)
from .journal import journal_path, read_operation_state_counts
from .library import Record
from .routines_config import (
    EnabledRoutineConfig,
    ReceiverKind,
    UnconfiguredRoutineConfig,
    account_digest,
    load_routines_config,
)
from .routines_store import read_routine_counts, routine_database_path
from .service import service_status

CloudStatus = Literal[
    "ok",
    "credentials_unreadable",
    "credentials_rejected",
    "timeout",
    "unreachable",
    "unavailable",
    "not_configured",
]
EndpointClass = Literal["loopback", "tailnet", "public"]
RoutineConfigState = Literal["unconfigured", "malformed", "disabled", "enabled"]

_TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class DiagnosticLibrary(Protocol):
    records: dict[str, Record]
    tags: dict[str, str]

    def refresh(self, *, force: bool = False) -> None: ...


@dataclass(frozen=True, slots=True)
class CloudCheck:
    status: CloudStatus
    counts: tuple[tuple[str, int], ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status}
        if self.counts:
            result["counts"] = dict(self.counts)
        return result


@dataclass(frozen=True, slots=True)
class RoutineDiagnostic:
    state: RoutineConfigState
    account_bound: bool
    phase: str | None = None
    counts: tuple[tuple[str, int], ...] | None = None
    receiver_kind: ReceiverKind | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "state": self.state,
            "account_bound": self.account_bound,
        }
        if self.receiver_kind is not None:
            result["receiver_kind"] = self.receiver_kind
        if self.phase is not None:
            result["phase"] = self.phase
        if self.counts is not None:
            result["counts"] = dict(self.counts)
        return result


@dataclass(frozen=True, slots=True)
class SupportReport:
    version: str
    commit: str | None
    platform: str
    python: str
    tool_schema_hash: str
    tool_contract_hash: str
    cloud_check: CloudCheck
    routines: RoutineDiagnostic
    service_status: str | None = None
    endpoint_class: EndpointClass | None = None
    operation_states: tuple[tuple[str, int], ...] | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "version": self.version,
            "commit": self.commit,
            "platform": self.platform,
            "python": self.python,
            "tool_schema_hash": self.tool_schema_hash,
            "tool_contract_hash": self.tool_contract_hash,
            "cloud_check": self.cloud_check.as_dict(),
            "routines": self.routines.as_dict(),
        }
        if self.service_status is not None:
            result["service_status"] = self.service_status
        if self.endpoint_class is not None:
            result["endpoint_class"] = self.endpoint_class
        if self.operation_states is not None:
            result["operation_states"] = dict(self.operation_states)
        return result

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"


def run_cloud_check(library: DiagnosticLibrary) -> CloudCheck:
    """Refresh all current Cloud state and retain only aggregate counts."""

    try:
        library.refresh(force=True)
        records = tuple(library.records.values())
        counts = {
            "areas": sum(item.kind == "area" and not item.heading for item in records),
            "checklist_items": sum(len(item.checklists) for item in records),
            "done": sum(item.status == "done" for item in records),
            "dropped": sum(item.status == "dropped" for item in records),
            "headings": sum(item.heading for item in records),
            "open": sum(item.is_open() for item in records),
            "projects": sum(
                item.kind == "project" and not item.heading for item in records
            ),
            "records": len(records),
            "repeating_templates": sum(
                item.recurrence.role == "template" for item in records
            ),
            "tags": len(library.tags),
            "tasks": sum(
                item.kind == "task" and not item.heading for item in records
            ),
            "trashed": sum(item.trashed for item in records),
        }
    except CloudError as error:
        return CloudCheck(_cloud_failure_status(error))
    except Exception:
        return CloudCheck("unavailable")
    return CloudCheck("ok", tuple(sorted(counts.items())))


def classify_endpoint(url: McpUrl) -> EndpointClass:
    hostname = urlsplit(str(url)).hostname or ""
    normalized = hostname.casefold().rstrip(".")
    if normalized == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return "tailnet" if normalized.endswith(".ts.net") else "public"
    if address.is_loopback:
        return "loopback"
    if isinstance(address, ipaddress.IPv4Address) and address in _TAILSCALE_IPV4:
        return "tailnet"
    if isinstance(address, ipaddress.IPv6Address) and address in _TAILSCALE_IPV6:
        return "tailnet"
    return "public"


def build_support_report(
    *,
    identity: DeploymentIdentity,
    platform_name: str,
    python_version: str,
    cloud: CloudCheck,
    routines: RoutineDiagnostic,
    service: str | None,
    endpoint_class: EndpointClass | None,
    operation_states: tuple[tuple[str, int], ...] | None,
) -> SupportReport:
    return SupportReport(
        version=identity.version,
        commit=identity.commit,
        platform=platform_name,
        python=python_version,
        tool_schema_hash=tool_schema_hash(),
        tool_contract_hash=tool_contract_hash(),
        cloud_check=cloud,
        routines=routines,
        service_status=service,
        endpoint_class=endpoint_class,
        operation_states=(
            tuple(sorted(operation_states)) if operation_states is not None else None
        ),
    )


def collect_cloud_check() -> CloudCheck:
    try:
        credentials = _credentials()
    except ConfigError:
        return CloudCheck("credentials_unreadable")
    if credentials is None:
        return CloudCheck("not_configured")
    return _fresh_cloud_check(credentials)


def collect_routines_diagnostic(
    credentials: Credentials | None,
    *,
    config_path: Path | None = None,
    database_path: Path | None = None,
) -> RoutineDiagnostic:
    """Read only value-free routine state without creating local resources."""

    try:
        config = load_routines_config(path=config_path)
    except ConfigError:
        return RoutineDiagnostic(state="malformed", account_bound=False)
    if isinstance(config, UnconfiguredRoutineConfig):
        return RoutineDiagnostic(state="unconfigured", account_bound=False)
    state: Literal["disabled", "enabled"] = (
        "enabled" if isinstance(config, EnabledRoutineConfig) else "disabled"
    )
    receiver_kind = config.profile.receiver.kind
    if credentials is None:
        return RoutineDiagnostic(
            state=state,
            account_bound=False,
            receiver_kind=receiver_kind,
        )
    digest = account_digest(credentials.email)
    if config.profile.account_digest != digest:
        return RoutineDiagnostic(
            state=state,
            account_bound=False,
            receiver_kind=receiver_kind,
        )
    path = database_path or routine_database_path(digest)
    counts = read_routine_counts(path, digest)
    if counts is None:
        return RoutineDiagnostic(
            state=state,
            account_bound=True,
            receiver_kind=receiver_kind,
        )
    if counts.phase not in {"uninitialized", "seeding", "live"}:
        return RoutineDiagnostic(
            state=state,
            account_bound=True,
            receiver_kind=receiver_kind,
        )
    safe_counts = (
        ("candidates", counts.candidates),
        ("dead", counts.dead),
        ("delivered", counts.delivered),
        ("pending", counts.pending),
    )
    return RoutineDiagnostic(
        state=state,
        account_bound=True,
        phase=counts.phase,
        counts=safe_counts,
        receiver_kind=receiver_kind,
    )


def collect_support_report() -> SupportReport:
    credentials_file = credentials_path()
    try:
        credentials = _credentials(path=credentials_file)
    except ConfigError:
        credentials = None
        cloud = CloudCheck("credentials_unreadable")
    else:
        cloud = (
            CloudCheck("not_configured")
            if credentials is None
            else _fresh_cloud_check(credentials)
        )
    endpoint = _endpoint_class(credentials_file)
    operations = _operation_counts(credentials)
    return build_support_report(
        identity=installed_identity(),
        platform_name=platform.system().casefold() or sys.platform,
        python_version=platform.python_version(),
        cloud=cloud,
        routines=collect_routines_diagnostic(credentials),
        service=_service_status(),
        endpoint_class=endpoint,
        operation_states=operations,
    )


def _cloud_failure_status(error: CloudError) -> CloudStatus:
    message = str(error)
    if message == "Things Cloud credentials were rejected":
        return "credentials_rejected"
    if message == "Things Cloud timed out":
        return "timeout"
    if message == "Things Cloud is unreachable":
        return "unreachable"
    return "unavailable"


def _fresh_cloud_check(credentials: Credentials) -> CloudCheck:
    with tempfile.TemporaryDirectory(prefix="things-orchestrator-cloud-check-") as root:
        library = CloudLibrary(
            CloudClient(credentials.email, credentials.password),
            cache=Path(root) / "state.json",
        )
        return run_cloud_check(library)


def _credentials(*, path: Path | None = None) -> Credentials | None:
    target = path or credentials_path()
    try:
        target.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError("Saved credentials are unreadable") from error
    return load_credentials(path=target)


def _endpoint_class(credentials_file: Path) -> EndpointClass | None:
    try:
        endpoint = load_mcp_url(
            preferences_file=credentials_file.with_name("preferences.json"),
            credentials_file=credentials_file,
        ) or normalize_mcp_url("http://127.0.0.1:8787")
    except ConfigError:
        return None
    return classify_endpoint(endpoint)


def _operation_counts(
    credentials: Credentials | None,
) -> tuple[tuple[str, int], ...] | None:
    if credentials is None:
        return None
    path = journal_path(credentials.email)
    if not path.is_file():
        return None
    try:
        return read_operation_state_counts(path, credentials.email)
    except Exception:
        return None


def _service_status() -> str | None:
    service_platform: Literal["darwin", "linux"]
    if sys.platform == "darwin":
        service_platform = "darwin"
    elif sys.platform.startswith("linux"):
        service_platform = "linux"
    else:
        return None
    try:
        return service_status(
            platform=service_platform,
            uid=os.getuid(),
            home=Path.home(),
        ).value
    except Exception:
        return None
