"""Idempotent launchd and systemd lifecycle for the loopback HTTP server."""

from __future__ import annotations

import getpass
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from .config import ConfigError

_LABEL = "com.matsvarnskuhler.things-orchestrator-http"
_UNIT = "things-orchestrator-http.service"
_SYSTEMD_PATH = Path("/etc/systemd/system") / _UNIT


class ServiceStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    NOT_INSTALLED = "not-installed"


@dataclass(frozen=True)
class ServiceEffect:
    kind: Literal["write", "remove", "command"]
    description: str
    path: Path | None = None
    content: str | None = None
    mode: int | None = None
    elevated: bool = False
    argv: tuple[str, ...] = ()


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


def render_systemd_unit(executable: Path, *, user: str) -> str:
    return (
        "[Unit]\n"
        "Description=Things Orchestrator MCP HTTP\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"ExecStart={_systemd_quote(executable)} serve-http --port 8787\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_launchd_plist(executable: Path) -> str:
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
    return plistlib.dumps(payload, sort_keys=True).decode()


def service_action(
    action: Literal["install", "uninstall", "status"],
    *,
    dry_run: bool,
) -> ServiceOperationResult:
    platform = _platform()
    executable = resolve_console_script()
    user = getpass.getuser()
    uid = os.getuid()
    home = Path.home()
    status = service_status(platform=platform, uid=uid, home=home)
    if action == "status":
        return ServiceOperationResult(action, status, (), applied=False)
    plan = _plan_service(
        action=action,
        platform=platform,
        executable=executable,
        user=user,
        uid=uid,
        home=home,
        status=status,
    )
    if not dry_run:
        for effect in plan.effects:
            _apply(effect)
        status = service_status(platform=platform, uid=uid, home=home)
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
    if not path.is_file():
        return ServiceStatus.NOT_INSTALLED
    command: tuple[str, ...]
    if platform == "darwin":
        command = ("launchctl", "print", f"gui/{uid}/{_LABEL}")
    else:
        command = ("systemctl", "is-active", "--quiet", _UNIT)
    try:
        if platform == "darwin":
            launchctl_result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            systemctl_result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError:
        return ServiceStatus.INACTIVE
    if platform == "darwin":
        return (
            ServiceStatus.ACTIVE
            if launchctl_result.returncode == 0
            and any(
                line.strip() == "state = running"
                for line in launchctl_result.stdout.splitlines()
            )
            else ServiceStatus.INACTIVE
        )
    return (
        ServiceStatus.ACTIVE
        if systemctl_result.returncode == 0
        else ServiceStatus.INACTIVE
    )


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
        raise ConfigError("The resolved things-orchestrator console script is not executable")
    return path


def _plan_service(
    *,
    action: Literal["install", "uninstall"],
    platform: Literal["darwin", "linux"],
    executable: Path,
    user: str,
    uid: int,
    home: Path,
    status: ServiceStatus,
) -> ServicePlan:
    path = _service_path(platform, home)
    if action == "uninstall" and status is ServiceStatus.NOT_INSTALLED:
        return ServicePlan(platform, action, (), ServiceStatus.NOT_INSTALLED)
    effects: tuple[ServiceEffect, ...]
    if platform == "darwin":
        domain = f"gui/{uid}"
        if action == "install":
            unload = (
                ServiceEffect(
                    "command",
                    "reload launchd agent",
                    argv=("launchctl", "bootout", f"{domain}/{_LABEL}"),
                ),
            ) if status is ServiceStatus.ACTIVE else ()
            effects = (
                ServiceEffect(
                    "write",
                    f"install {path}",
                    path=path,
                    content=render_launchd_plist(executable),
                    mode=0o600,
                ),
            ) + unload + (
                ServiceEffect(
                    "command",
                    "start launchd agent",
                    argv=("launchctl", "bootstrap", domain, str(path)),
                ),
            )
            return ServicePlan(platform, action, effects, ServiceStatus.ACTIVE)
        stop = (
            ServiceEffect(
                "command",
                "stop launchd agent",
                argv=("launchctl", "bootout", f"{domain}/{_LABEL}"),
            ),
        ) if status is ServiceStatus.ACTIVE else ()
        effects = stop + (
            ServiceEffect("remove", f"remove {path}", path=path),
        )
        return ServicePlan(platform, action, effects, ServiceStatus.NOT_INSTALLED)
    if action == "install":
        effects = (
            ServiceEffect(
                "write",
                f"install {_SYSTEMD_PATH}",
                path=_SYSTEMD_PATH,
                content=render_systemd_unit(executable, user=user),
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


def _apply(effect: ServiceEffect) -> None:
    if effect.kind == "command":
        subprocess.run(effect.argv, check=True)
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
