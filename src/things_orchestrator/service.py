"""Idempotent launchd and systemd lifecycle for the loopback HTTP server."""

from __future__ import annotations

import os
import plistlib
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from .config import ConfigError

_LABEL = "com.matsvarnskuhler.things-orchestrator-http"
_UNIT = "things-orchestrator-http.service"
_SYSTEMD_PATH = Path("/etc/systemd/system") / _UNIT
_SETTLE_TIMEOUT = 5.0


class ServiceStatus(str, Enum):
    ACTIVE = "active"
    LOADED = "loaded"
    INACTIVE = "inactive"
    NOT_INSTALLED = "not-installed"
    UNKNOWN = "unknown"


class ServiceApplyError(RuntimeError):
    """A convergent service operation did not reach its next safe state."""


@dataclass(frozen=True)
class ServiceEffect:
    kind: Literal["write", "remove", "command"]
    description: str
    path: Path | None = None
    content: str | None = None
    mode: int | None = None
    elevated: bool = False
    argv: tuple[str, ...] = ()
    settles_to: ServiceStatus | None = None
    settles_also: tuple[ServiceStatus, ...] = ()


@dataclass(frozen=True)
class ServicePlan:
    platform: Literal["darwin", "linux"]
    action: Literal["install", "uninstall"]
    effects: tuple[ServiceEffect, ...]
    result_status: ServiceStatus


@dataclass(frozen=True)
class ServiceOperationResult:
    action: Literal["install", "uninstall", "status"]
    status: ServiceStatus
    effects: tuple[ServiceEffect, ...]
    applied: bool


def render_systemd_unit(
    executable: Path,
    *,
    user: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    environment_lines = "".join(
        f"Environment={_systemd_quote_environment(name, value)}\n"
        for name, value in _xdg_environment(environment).items()
    )
    return (
        "[Unit]\n"
        "Description=Things Orchestrator MCP HTTP\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"{environment_lines}"
        f"ExecStart={_systemd_quote(executable)} serve-http --port 8787\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_launchd_plist(
    executable: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    payload: dict[str, object] = {
        "Label": _LABEL,
        "ProgramArguments": [
            str(executable),
            "serve-http",
            "--port",
            "8787",
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "ThrottleInterval": 2,
    }
    xdg_environment = _xdg_environment(environment)
    if xdg_environment:
        payload["EnvironmentVariables"] = xdg_environment
    return plistlib.dumps(payload, sort_keys=True).decode()


def service_action(
    action: Literal["install", "uninstall", "status"],
    *,
    dry_run: bool,
) -> ServiceOperationResult:
    platform = _platform()
    executable = resolve_console_script()
    uid = os.getuid()
    home = Path.home()
    status = service_status(platform=platform, uid=uid, home=home)
    if action == "status":
        return ServiceOperationResult(action, status, (), applied=False)
    user: str | None = None
    if platform == "linux" and action == "install":
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError as error:
            raise ConfigError(f"No local account exists for UID {uid}") from error
    plan = _plan_service(
        action=action,
        platform=platform,
        executable=executable,
        user=user,
        uid=uid,
        home=home,
        status=status,
        environment=os.environ,
    )
    if not dry_run:
        for effect in plan.effects:
            _apply(effect, platform=platform, uid=uid, home=home)
        status = _wait_for_plan_result(
            plan,
            platform=platform,
            uid=uid,
            home=home,
            timeout=_SETTLE_TIMEOUT,
        )
    else:
        status = plan.result_status
    return ServiceOperationResult(
        action=action,
        status=status,
        effects=plan.effects,
        applied=not dry_run and bool(plan.effects),
    )


def service_status(
    *,
    platform: Literal["darwin", "linux"],
    uid: int,
    home: Path,
) -> ServiceStatus:
    path = _service_path(platform, home)
    if platform == "darwin":
        try:
            launchctl_result = subprocess.run(
                ("launchctl", "print", f"gui/{uid}/{_LABEL}"),
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return ServiceStatus.UNKNOWN
        if launchctl_result.returncode != 0:
            if launchctl_result.returncode != 113:
                return ServiceStatus.UNKNOWN
            return (
                ServiceStatus.INACTIVE
                if path.is_file()
                else ServiceStatus.NOT_INSTALLED
            )
        return (
            ServiceStatus.ACTIVE
            if any(
                line.strip() == "state = running"
                for line in launchctl_result.stdout.splitlines()
            )
            else ServiceStatus.LOADED
        )
    try:
        active_result = subprocess.run(
            ("systemctl", "is-active", "--quiet", _UNIT),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return ServiceStatus.UNKNOWN
    if active_result.returncode == 0:
        return ServiceStatus.ACTIVE
    if path.is_file():
        return (
            ServiceStatus.INACTIVE
            if active_result.returncode == 3
            else ServiceStatus.UNKNOWN
        )
    if active_result.returncode not in {3, 4}:
        return ServiceStatus.UNKNOWN
    try:
        load_result = subprocess.run(
            ("systemctl", "show", "--property=LoadState", "--value", _UNIT),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ServiceStatus.UNKNOWN
    if load_result.returncode != 0:
        return ServiceStatus.UNKNOWN
    load_state = load_result.stdout.strip()
    if load_state == "loaded":
        return ServiceStatus.LOADED
    if load_state == "not-found":
        return ServiceStatus.NOT_INSTALLED
    return ServiceStatus.UNKNOWN


def _wait_for_plan_result(
    plan: ServicePlan,
    *,
    platform: Literal["darwin", "linux"],
    uid: int,
    home: Path,
    timeout: float,
) -> ServiceStatus:
    deadline = time.monotonic() + timeout
    while True:
        observed = service_status(platform=platform, uid=uid, home=home)
        if observed is plan.result_status:
            return observed
        if time.monotonic() >= deadline:
            raise ServiceApplyError(
                f"Service {plan.action} did not reach {plan.result_status.value} "
                f"(observed {observed.value}). The operation may be partially applied; "
                "rerun the same command safely."
            )
        time.sleep(0.05)


def resolve_console_script() -> Path:
    candidate = shutil.which("things-orchestrator")
    if candidate is None:
        raw = Path(sys.argv[0])
        if raw.is_file():
            candidate = str(raw)
    if candidate is None:
        raise ConfigError(
            "Cannot resolve things-orchestrator. Reinstall it with uv tool install."
        )
    path = Path(candidate).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ConfigError(
            "The resolved things-orchestrator console script is not executable"
        )
    return path


def _plan_service(
    *,
    action: Literal["install", "uninstall"],
    platform: Literal["darwin", "linux"],
    executable: Path,
    user: str | None,
    uid: int,
    home: Path,
    status: ServiceStatus,
    environment: Mapping[str, str] | None = None,
) -> ServicePlan:
    path = _service_path(platform, home)
    if (
        action == "uninstall"
        and platform == "darwin"
        and status is ServiceStatus.NOT_INSTALLED
    ):
        return ServicePlan(platform, action, (), ServiceStatus.NOT_INSTALLED)
    effects: tuple[ServiceEffect, ...]
    if platform == "darwin":
        domain = f"gui/{uid}"
        if action == "install":
            unload = (
                (
                    ServiceEffect(
                        "command",
                        "reload launchd agent",
                        argv=("launchctl", "bootout", f"{domain}/{_LABEL}"),
                        settles_to=ServiceStatus.INACTIVE,
                    ),
                )
                if status in {
                    ServiceStatus.ACTIVE,
                    ServiceStatus.LOADED,
                    ServiceStatus.UNKNOWN,
                }
                else ()
            )
            effects = (
                (
                    ServiceEffect(
                        "write",
                        f"install {path}",
                        path=path,
                        content=render_launchd_plist(
                            executable,
                            environment=environment,
                        ),
                        mode=0o600,
                    ),
                )
                + unload
                + (
                    ServiceEffect(
                        "command",
                        "start launchd agent",
                        argv=("launchctl", "bootstrap", domain, str(path)),
                    ),
                )
            )
            return ServicePlan(platform, action, effects, ServiceStatus.ACTIVE)
        stop = (
            (
                ServiceEffect(
                    "command",
                    "stop launchd agent",
                    argv=("launchctl", "bootout", f"{domain}/{_LABEL}"),
                    settles_to=ServiceStatus.INACTIVE,
                    settles_also=(ServiceStatus.NOT_INSTALLED,),
                ),
            )
            if status in {
                ServiceStatus.ACTIVE,
                ServiceStatus.LOADED,
                ServiceStatus.UNKNOWN,
            }
            else ()
        )
        effects = stop + (ServiceEffect("remove", f"remove {path}", path=path),)
        return ServicePlan(platform, action, effects, ServiceStatus.NOT_INSTALLED)
    if action == "install":
        if user is None:
            raise AssertionError("Linux service install requires a local account")
        effects = (
            ServiceEffect(
                "write",
                f"install {_SYSTEMD_PATH}",
                path=_SYSTEMD_PATH,
                content=render_systemd_unit(
                    executable,
                    user=user,
                    environment=environment,
                ),
                mode=0o644,
                elevated=True,
            ),
            ServiceEffect(
                "command",
                "reload systemd units",
                argv=("sudo", "systemctl", "daemon-reload"),
            ),
            ServiceEffect(
                "command",
                "enable systemd service",
                argv=("sudo", "systemctl", "enable", _UNIT),
            ),
            ServiceEffect(
                "command",
                "restart systemd service",
                argv=("sudo", "systemctl", "restart", _UNIT),
            ),
        )
        return ServicePlan(platform, action, effects, ServiceStatus.ACTIVE)
    if status is ServiceStatus.NOT_INSTALLED:
        return ServicePlan(
            platform,
            action,
            (
                ServiceEffect(
                    "command",
                    "reload systemd units",
                    argv=("sudo", "systemctl", "daemon-reload"),
                ),
            ),
            ServiceStatus.NOT_INSTALLED,
        )
    effects = (
        ServiceEffect(
            "command",
            "disable and stop systemd service",
            argv=("sudo", "systemctl", "disable", "--now", _UNIT),
        ),
        ServiceEffect(
            "remove",
            f"remove {_SYSTEMD_PATH}",
            path=_SYSTEMD_PATH,
            elevated=True,
        ),
        ServiceEffect(
            "command",
            "reload systemd units",
            argv=("sudo", "systemctl", "daemon-reload"),
        ),
    )
    return ServicePlan(platform, action, effects, ServiceStatus.NOT_INSTALLED)


def _apply(
    effect: ServiceEffect,
    *,
    platform: Literal["darwin", "linux"],
    uid: int,
    home: Path,
    settle_timeout: float = 5.0,
) -> None:
    try:
        _apply_unchecked(
            effect,
            platform=platform,
            uid=uid,
            home=home,
            settle_timeout=settle_timeout,
        )
    except ServiceApplyError:
        raise
    except subprocess.CalledProcessError as error:
        command = _command_text(error.cmd)
        raise ServiceApplyError(
            f"Service effect failed: {effect.description} "
            f"({command}; exit {error.returncode}). The operation may be partially "
            "applied; rerun the same command safely."
        ) from error
    except OSError as error:
        raise ServiceApplyError(
            f"Service effect failed: {effect.description} ({error}). "
            "The operation may be partially applied; rerun the same command safely."
        ) from error


def _apply_unchecked(
    effect: ServiceEffect,
    *,
    platform: Literal["darwin", "linux"],
    uid: int,
    home: Path,
    settle_timeout: float,
) -> None:
    if effect.kind == "command":
        if effect.settles_to is None:
            subprocess.run(effect.argv, check=True)
            return
        command_result = subprocess.run(
            effect.argv,
            check=False,
            capture_output=True,
            text=True,
        )
        _wait_for_service_status(
            effect,
            platform=platform,
            uid=uid,
            home=home,
            timeout=settle_timeout,
            command_result=command_result,
        )
        return
    if effect.path is None:
        raise AssertionError("file effect has no path")
    if effect.kind == "remove":
        if effect.elevated:
            subprocess.run(("sudo", "rm", "-f", str(effect.path)), check=True)
        else:
            effect.path.unlink(missing_ok=True)
        return
    if effect.content is None or effect.mode is None:
        raise AssertionError("write effect is incomplete")
    if effect.elevated:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as staged:
            staged.write(effect.content)
            staged.flush()
            subprocess.run(
                (
                    "sudo",
                    "install",
                    "-m",
                    f"{effect.mode:o}",
                    staged.name,
                    str(effect.path),
                ),
                check=True,
            )
        return
    effect.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=effect.path.parent, delete=False
    ) as staged:
        staged.write(effect.content)
        staged_path = Path(staged.name)
    try:
        staged_path.chmod(effect.mode)
        staged_path.replace(effect.path)
    finally:
        staged_path.unlink(missing_ok=True)


def _wait_for_service_status(
    effect: ServiceEffect,
    *,
    platform: Literal["darwin", "linux"],
    uid: int,
    home: Path,
    timeout: float,
    command_result: subprocess.CompletedProcess[str],
) -> None:
    expected = effect.settles_to
    if expected is None:
        raise AssertionError("settling effect has no expected status")
    accepted = (expected, *effect.settles_also)
    deadline = time.monotonic() + timeout
    while service_status(platform=platform, uid=uid, home=home) not in accepted:
        if time.monotonic() >= deadline:
            command_failure = ""
            if command_result.returncode != 0:
                stderr = command_result.stderr.strip()
                detail = f"exit {command_result.returncode}"
                if stderr:
                    detail = f"{detail}: {stderr}"
                command_failure = f" ({_command_text(effect.argv)}; {detail})"
            expected_label = " or ".join(status.value for status in accepted)
            raise ServiceApplyError(
                f"Service effect failed: {effect.description} did not reach "
                f"{expected_label}{command_failure}. The operation may be partially "
                "applied; rerun the same command safely."
            )
        time.sleep(0.05)


def _service_path(platform: Literal["darwin", "linux"], home: Path) -> Path:
    if platform == "darwin":
        return home / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
    return _SYSTEMD_PATH


def _platform() -> Literal["darwin", "linux"]:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ConfigError("service lifecycle supports macOS launchd and Linux systemd")


def _systemd_quote(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_quote_environment(name: str, value: str) -> str:
    escaped = (
        f"{name}={value}".replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _xdg_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    if environment is None:
        return {}
    return {
        name: value
        for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")
        if (value := environment.get(name))
    }


def _command_text(command: object) -> str:
    if isinstance(command, (tuple, list)):
        return " ".join(str(part) for part in command)
    return str(command)
