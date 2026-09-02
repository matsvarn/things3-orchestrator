from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from things_orchestrator.service import (
    ServiceStatus,
    _plan_service,
    render_launchd_plist,
    render_systemd_unit,
    service_status,
)

EXECUTABLE = Path("/opt/uv tools/bin/things-orchestrator")


def test_systemd_unit_binds_exact_executable_user_and_restart_policy() -> None:
    unit = render_systemd_unit(EXECUTABLE, user="mats")
    assert "After=network-online.target" in unit
    assert "Wants=network-online.target" in unit
    assert "User=mats" in unit
    assert 'ExecStart="/opt/uv tools/bin/things-orchestrator" serve-http --port 8787' in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "THINGS_ORCHESTRATOR_COMMIT" not in unit
    assert "THINGS_MCP_TOKEN" not in unit
    assert "WorkingDirectory" not in unit


def test_launchd_plist_binds_exact_executable_and_failure_restart() -> None:
    plist = render_launchd_plist(EXECUTABLE)
    assert "/opt/uv tools/bin/things-orchestrator" in plist
    assert "serve-http" in plist
    assert "8787" in plist
    assert "RunAtLoad" in plist
    assert "SuccessfulExit" in plist
    assert "THINGS_ORCHESTRATOR_COMMIT" not in plist
    assert "THINGS_MCP_TOKEN" not in plist


def test_launchd_install_converges_according_to_supervisor_state() -> None:
    unloaded = _plan_service(
        action="install",
        platform="darwin",
        executable=EXECUTABLE,
        user="mats",
        uid=501,
        home=Path("/Users/mats"),
        status=ServiceStatus.INACTIVE,
    )
    loaded = _plan_service(
        action="install",
        platform="darwin",
        executable=EXECUTABLE,
        user="mats",
        uid=501,
        home=Path("/Users/mats"),
        status=ServiceStatus.ACTIVE,
    )
    loaded_but_stopped = _plan_service(
        action="install",
        platform="darwin",
        executable=EXECUTABLE,
        user="mats",
        uid=501,
        home=Path("/Users/mats"),
        status=ServiceStatus.LOADED,
    )
    assert [effect.kind for effect in unloaded.effects] == ["write", "command"]
    assert "bootstrap" in unloaded.effects[-1].argv
    assert [effect.kind for effect in loaded.effects] == [
        "write",
        "command",
        "command",
    ]
    assert "bootout" in loaded.effects[1].argv
    assert "bootstrap" in loaded.effects[2].argv
    assert loaded_but_stopped.effects == loaded.effects


def test_uninstall_is_idempotent_when_service_is_not_installed() -> None:
    plan = _plan_service(
        action="uninstall",
        platform="linux",
        executable=EXECUTABLE,
        user="mats",
        uid=1000,
        home=Path("/home/mats"),
        status=ServiceStatus.NOT_INSTALLED,
    )
    assert plan.effects == ()
    assert plan.result_status is ServiceStatus.NOT_INSTALLED


def test_launchd_uninstall_does_not_bootout_an_inactive_agent() -> None:
    plan = _plan_service(
        action="uninstall",
        platform="darwin",
        executable=EXECUTABLE,
        user="mats",
        uid=501,
        home=Path("/Users/mats"),
        status=ServiceStatus.INACTIVE,
    )
    assert [effect.kind for effect in plan.effects] == ["remove"]


def test_launchd_uninstall_boots_out_a_loaded_stopped_agent() -> None:
    plan = _plan_service(
        action="uninstall",
        platform="darwin",
        executable=EXECUTABLE,
        user="mats",
        uid=501,
        home=Path("/Users/mats"),
        status=ServiceStatus.LOADED,
    )
    assert [effect.kind for effect in plan.effects] == ["command", "remove"]
    assert "bootout" in plan.effects[0].argv


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (0, "state = running\n", ServiceStatus.ACTIVE),
        (0, "state = exited\n", ServiceStatus.LOADED),
        (0, "", ServiceStatus.LOADED),
        (113, "", ServiceStatus.INACTIVE),
    ],
)
def test_launchd_status_requires_a_running_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    output: str,
    expected: ServiceStatus,
) -> None:
    agent = (
        tmp_path
        / "Library/LaunchAgents/com.matsvarnskuhler.things-orchestrator-http.plist"
    )
    agent.parent.mkdir(parents=True)
    agent.write_text("plist")
    monkeypatch.setattr(
        "things_orchestrator.service.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=output,
        ),
    )

    assert service_status(platform="darwin", uid=501, home=tmp_path) is expected


def test_linux_install_writes_reloads_and_starts_the_supervisor() -> None:
    plan = _plan_service(
        action="install",
        platform="linux",
        executable=EXECUTABLE,
        user="mats",
        uid=1000,
        home=Path("/home/mats"),
        status=ServiceStatus.INACTIVE,
    )
    assert [effect.kind for effect in plan.effects] == [
        "write",
        "command",
        "command",
        "command",
    ]
    assert plan.effects[0].path == Path(
        "/etc/systemd/system/things-orchestrator-http.service"
    )
    assert plan.effects[0].elevated is True
    assert plan.effects[1].argv == ("sudo", "systemctl", "daemon-reload")
    assert plan.effects[2].argv == (
        "sudo",
        "systemctl",
        "enable",
        "things-orchestrator-http.service",
    )
    assert plan.effects[3].argv == (
        "sudo",
        "systemctl",
        "restart",
        "things-orchestrator-http.service",
    )
