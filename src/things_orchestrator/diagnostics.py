"""Value-free diagnostics for Things Cloud and local deployment support."""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

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
)
from .journal import journal_path, read_operation_state_counts
from .library import Record
from .routines_config import (
    ROUTINE_TRIGGER,
    EnabledRoutineConfig,
    ReceiverKind,
    UnconfiguredRoutineConfig,
    account_digest,
    load_routines_config,
)
from .routines_store import read_routine_counts, routine_database_path
from .routines_webhook import RoutineHTTPOpener, proxyless_no_redirect_opener
from .service import diagnostic_service_status
from .tools import tool_contract_hash, tool_discovery_hash, tool_schema_hash

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
RoutineAccountBinding = Literal["not_applicable", "unknown", "bound", "mismatch"]
RoutineServiceState = Literal[
    "active", "loaded", "inactive", "not-installed", "unknown", "unsupported"
]
RoutineWorkerLiveness = Literal[
    "initializing", "running", "backing_off", "stopped", "unknown"
]

_TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_ROUTINE_HEALTH_URL = "http://127.0.0.1:8787/health"


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
    configuration_state: RoutineConfigState
    account_binding: RoutineAccountBinding
    service_state: RoutineServiceState
    worker_liveness: RoutineWorkerLiveness
    history_phase: str = "unknown"
    fixed_trigger: str = ROUTINE_TRIGGER
    trigger_tag_discovered: bool | None = None
    trigger_ready: bool | None = None
    counts: tuple[tuple[str, int], ...] | None = None
    receiver_kind: ReceiverKind | None = None
    poll_interval_seconds: int | None = None
    settlement_window_seconds: int | None = None
    last_successful_poll_at: int | None = None
    last_delivery_at: int | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "configuration_state": self.configuration_state,
            "account_binding": self.account_binding,
            "service_state": self.service_state,
            "worker_liveness": self.worker_liveness,
            "history_phase": self.history_phase,
            "fixed_trigger": self.fixed_trigger,
            "trigger_tag_discovered": self.trigger_tag_discovered,
            "trigger_ready": self.trigger_ready,
            "last_successful_poll_at": self.last_successful_poll_at,
            "last_delivery_at": self.last_delivery_at,
        }
        if self.receiver_kind is not None:
            result["receiver_kind"] = self.receiver_kind
        if self.poll_interval_seconds is not None:
            result["poll_interval_seconds"] = self.poll_interval_seconds
        if self.settlement_window_seconds is not None:
            result["settlement_window_seconds"] = self.settlement_window_seconds
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
    tool_discovery_hash: str
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
            "tool_discovery_hash": self.tool_discovery_hash,
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
        tool_discovery_hash=tool_discovery_hash(),
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


def collect_service_state() -> RoutineServiceState:
    return _routine_service_state(_service_status())


def probe_routine_runtime(
    bearer: str | None,
    *,
    timeout_seconds: float = 1.0,
    _opener: RoutineHTTPOpener | None = None,
) -> Mapping[str, object] | None:
    """Read the authenticated loopback runtime snapshot with fixed bounds."""

    if not bearer or not 0 < timeout_seconds <= 5:
        return None
    request = Request(
        _ROUTINE_HEALTH_URL,
        headers={"Authorization": f"Bearer {bearer}"},
    )
    opener = _opener or proxyless_no_redirect_opener()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read(65_537)
    except (HTTPError, URLError, TimeoutError, OSError):
        return None
    if len(body) > 65_536:
        return None
    try:
        payload: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    routines = payload.get("routines")
    return routines if isinstance(routines, dict) else None


def collect_routines_diagnostic(
    credentials: Credentials | None,
    *,
    config_path: Path | None = None,
    database_path: Path | None = None,
    service_state: str | None = None,
    runtime: Mapping[str, object] | None = None,
) -> RoutineDiagnostic:
    """Read only value-free routine state without creating local resources."""

    service = _routine_service_state(service_state)
    liveness, last_poll, runtime_delivery = _runtime_status(service, runtime)
    try:
        config = load_routines_config(path=config_path)
    except ConfigError:
        return RoutineDiagnostic("malformed", "unknown", service, liveness)
    if isinstance(config, UnconfiguredRoutineConfig):
        return RoutineDiagnostic("unconfigured", "not_applicable", service, liveness)
    state: Literal["disabled", "enabled"] = (
        "enabled" if isinstance(config, EnabledRoutineConfig) else "disabled"
    )
    profile = config.profile
    receiver_kind = profile.receiver.kind
    if credentials is None:
        return RoutineDiagnostic(
            state,
            "unknown",
            service,
            liveness,
            receiver_kind=receiver_kind,
            poll_interval_seconds=profile.poll_interval_seconds,
            settlement_window_seconds=profile.settle_seconds,
            last_successful_poll_at=last_poll,
            last_delivery_at=runtime_delivery,
        )
    digest = account_digest(credentials.email)
    if profile.account_digest != digest:
        return RoutineDiagnostic(
            state,
            "mismatch",
            service,
            liveness,
            receiver_kind=receiver_kind,
            poll_interval_seconds=profile.poll_interval_seconds,
            settlement_window_seconds=profile.settle_seconds,
            last_successful_poll_at=last_poll,
            last_delivery_at=runtime_delivery,
        )
    path = database_path or routine_database_path(digest)
    counts = read_routine_counts(path, digest)
    if counts is None:
        return RoutineDiagnostic(
            state,
            "bound",
            service,
            liveness,
            receiver_kind=receiver_kind,
            poll_interval_seconds=profile.poll_interval_seconds,
            settlement_window_seconds=profile.settle_seconds,
            last_successful_poll_at=last_poll,
            last_delivery_at=runtime_delivery,
        )
    if counts.phase not in {"uninitialized", "seeding", "live"}:
        return RoutineDiagnostic(
            state,
            "bound",
            service,
            liveness,
            receiver_kind=receiver_kind,
            poll_interval_seconds=profile.poll_interval_seconds,
            settlement_window_seconds=profile.settle_seconds,
            last_successful_poll_at=last_poll,
            last_delivery_at=runtime_delivery,
        )
    safe_counts = (
        ("candidates", counts.candidates),
        ("dead", counts.dead),
        ("delivered", counts.delivered),
        ("pending", counts.pending),
    )
    tag_discovered = counts.ai_tags > 0 if counts.phase == "live" else None
    ready = (
        state == "enabled"
        and counts.phase == "live"
        and tag_discovered
        and liveness in {"running", "backing_off"}
    )
    return RoutineDiagnostic(
        configuration_state=state,
        account_binding="bound",
        service_state=service,
        worker_liveness=liveness,
        history_phase=counts.phase,
        trigger_tag_discovered=tag_discovered,
        trigger_ready=ready,
        counts=safe_counts,
        receiver_kind=receiver_kind,
        poll_interval_seconds=config.profile.poll_interval_seconds,
        settlement_window_seconds=config.profile.settle_seconds,
        last_successful_poll_at=last_poll,
        last_delivery_at=(
            counts.last_delivery_at
            if counts.last_delivery_at is not None
            else runtime_delivery
        ),
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
    service = _service_status()
    return build_support_report(
        identity=installed_identity(),
        platform_name=platform.system().casefold() or sys.platform,
        python_version=platform.python_version(),
        cloud=cloud,
        routines=collect_routines_diagnostic(credentials, service_state=service),
        service=service,
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
        return diagnostic_service_status(
            platform=service_platform,
            uid=os.getuid(),
            home=Path.home(),
        ).value
    except Exception:
        return None


def _routine_service_state(value: str | None) -> RoutineServiceState:
    if value == "active":
        return "active"
    if value == "loaded":
        return "loaded"
    if value == "inactive":
        return "inactive"
    if value == "not-installed":
        return "not-installed"
    if value == "unknown":
        return "unknown"
    return "unsupported" if value is None else "unknown"


def _runtime_status(
    service: RoutineServiceState,
    runtime: Mapping[str, object] | None,
) -> tuple[RoutineWorkerLiveness, int | None, int | None]:
    if runtime is None:
        if service in {"loaded", "inactive", "not-installed"}:
            return "stopped", None, None
        return "unknown", None, None
    state = runtime.get("state")
    liveness: RoutineWorkerLiveness
    if state == "initializing":
        liveness = "initializing"
    elif state == "running":
        liveness = "running"
    elif state == "backing_off":
        liveness = "backing_off"
    elif state == "stopped":
        liveness = "stopped"
    elif state == "disabled":
        liveness = "stopped"
    else:
        return "unknown", None, None
    return (
        liveness,
        _safe_epoch(runtime.get("last_successful_poll_at")),
        _safe_epoch(runtime.get("last_delivery_at")),
    )


def _safe_epoch(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
