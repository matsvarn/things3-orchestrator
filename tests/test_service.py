from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from things_orchestrator.service import (
    ServiceApplyError,
    ServiceEffect,
    ServiceOperationResult,
    ServicePlan,
    ServiceStatus,
    _apply,
    _plan_service,
    render_launchd_plist,
    render_systemd_unit,
    service_action,
    service_status,
)

EXECUTABLE = Path("/opt/uv tools/bin/things-orchestrator")


def test_systemd_unit_binds_exact_executable_user_and_restart_policy() -> None:
    unit = render_systemd_unit(EXECUTABLE, user="mats")
    assert "After=network-online.target" in unit
    assert "Wants=network-online.target" in unit
    assert "User=mats" in unit
    assert (
        'ExecStart="/opt/uv tools/bin/things-orchestrator" serve-http --port 8787'
        in unit
    )
    assert "Restart=on-failure" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "THINGS_ORCHESTRATOR_COMMIT" not in unit
    assert "THINGS_MCP_TOKEN" not in unit
    assert "WorkingDirectory" not in unit


def test_systemd_unit_preserves_only_nonempty_xdg_roots() -> None:
    unit = render_systemd_unit(
        EXECUTABLE,
        user="mats",
        environment={
            "XDG_CONFIG_HOME": "/srv/things config",
            "XDG_STATE_HOME": "/srv/things-state",
        },
    )

    assert 'Environment="XDG_CONFIG_HOME=/srv/things config"' in unit
    assert 'Environment="XDG_STATE_HOME=/srv/things-state"' in unit
    assert "Environment=" not in render_systemd_unit(EXECUTABLE, user="mats")


def test_launchd_plist_binds_exact_executable_and_failure_restart() -> None:
    plist = render_launchd_plist(EXECUTABLE)
    assert "/opt/uv tools/bin/things-orchestrator" in plist
    assert "serve-http" in plist
    assert "8787" in plist
    assert "RunAtLoad" in plist
    assert "SuccessfulExit" in plist
    assert "THINGS_ORCHESTRATOR_COMMIT" not in plist
    assert "THINGS_MCP_TOKEN" not in plist


def test_launchd_plist_preserves_only_nonempty_xdg_roots() -> None:
    payload = plistlib.loads(
        render_launchd_plist(
            EXECUTABLE,
            environment={
                "XDG_CONFIG_HOME": "/srv/things-config",
                "XDG_STATE_HOME": "/srv/things-state",
            },
        ).encode()
    )

    assert payload["EnvironmentVariables"] == {
        "XDG_CONFIG_HOME": "/srv/things-config",
        "XDG_STATE_HOME": "/srv/things-state",
    }
    default_payload = plistlib.loads(render_launchd_plist(EXECUTABLE).encode())
    assert "EnvironmentVariables" not in default_payload


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
    assert loaded.effects[1].settles_to is ServiceStatus.INACTIVE
    assert "bootstrap" in loaded.effects[2].argv
    assert loaded_but_stopped.effects == loaded.effects


def test_service_plan_passes_xdg_roots_into_the_supervisor_definition() -> None:
    plan = _plan_service(
        action="install",
        platform="darwin",
        executable=EXECUTABLE,
        user="mats",
        uid=501,
        home=Path("/Users/mats"),
        status=ServiceStatus.INACTIVE,
        environment={"XDG_CONFIG_HOME": "/srv/things-config"},
    )

    content = plan.effects[0].content
    assert content is not None
    payload = plistlib.loads(content.encode())
    assert payload["EnvironmentVariables"] == {"XDG_CONFIG_HOME": "/srv/things-config"}


def test_linux_uninstall_reloads_stale_state_when_definition_is_already_missing() -> None:
    plan = _plan_service(
        action="uninstall",
        platform="linux",
        executable=EXECUTABLE,
        user="mats",
        uid=1000,
        home=Path("/home/mats"),
        status=ServiceStatus.NOT_INSTALLED,
    )
    assert len(plan.effects) == 1
    assert plan.effects[0].argv == ("sudo", "systemctl", "daemon-reload")
    assert plan.result_status is ServiceStatus.NOT_INSTALLED


@pytest.mark.parametrize("platform", ("darwin", "linux"))
def test_orphaned_loaded_service_is_stopped_before_definition_cleanup(
    platform: Literal["darwin", "linux"],
    tmp_path: Path,
) -> None:
    plan = _plan_service(
        action="uninstall",
        platform=platform,
        executable=EXECUTABLE,
        user="mats",
        uid=501,
        home=tmp_path,
        status=ServiceStatus.LOADED,
    )

    if platform == "darwin":
        assert "bootout" in plan.effects[0].argv
        assert plan.effects[1].kind == "remove"
    else:
        assert plan.effects[0].argv == (
            "sudo",
            "systemctl",
            "disable",
            "--now",
            "things-orchestrator-http.service",
        )
        assert plan.effects[1].kind == "remove"
        assert plan.effects[2].argv == ("sudo", "systemctl", "daemon-reload")


def test_orphaned_launchd_uninstall_accepts_not_installed_after_bootout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan_service(
        action="uninstall",
        platform="darwin",
        executable=EXECUTABLE,
        user="mats",
        uid=501,
        home=tmp_path,
        status=ServiceStatus.LOADED,
    )

    def run(command: tuple[str, ...], **_kwargs: object) -> object:
        if "bootout" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert "print" in command
        return SimpleNamespace(returncode=113, stdout="", stderr="not found")

    monkeypatch.setattr("things_orchestrator.service.subprocess.run", run)

    _apply(
        plan.effects[0],
        platform="darwin",
        uid=501,
        home=tmp_path,
        settle_timeout=0.0,
    )


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
    assert plan.effects[0].settles_to is ServiceStatus.INACTIVE


def test_launchd_bootout_accepts_nonzero_when_agent_is_already_unloaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = (
        tmp_path
        / "Library/LaunchAgents/com.matsvarnskuhler.things-orchestrator-http.plist"
    )
    agent.parent.mkdir(parents=True)
    agent.write_text("plist")
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        calls.append(argv)
        if "bootout" in argv:
            return SimpleNamespace(returncode=113, stdout="")
        return SimpleNamespace(returncode=113, stdout="")

    monkeypatch.setattr("things_orchestrator.service.subprocess.run", run)
    effect = ServiceEffect(
        "command",
        "reload launchd agent",
        argv=(
            "launchctl",
            "bootout",
            "gui/501/com.matsvarnskuhler.things-orchestrator-http",
        ),
        settles_to=ServiceStatus.INACTIVE,
    )

    _apply(effect, platform="darwin", uid=501, home=tmp_path)

    assert calls[0][1] == "bootout"
    assert calls[1][1] == "print"


def test_launchd_bootout_waits_for_an_asynchronous_unload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = (
        tmp_path
        / "Library/LaunchAgents/com.matsvarnskuhler.things-orchestrator-http.plist"
    )
    agent.parent.mkdir(parents=True)
    agent.write_text("plist")
    states = iter(
        (
            SimpleNamespace(returncode=0, stdout="state = running\n"),
            SimpleNamespace(returncode=113, stdout=""),
        )
    )

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        if "bootout" in argv:
            return SimpleNamespace(returncode=0, stdout="")
        return next(states)

    monkeypatch.setattr("things_orchestrator.service.subprocess.run", run)
    monkeypatch.setattr("things_orchestrator.service.time.sleep", lambda _delay: None)
    effect = ServiceEffect(
        "command",
        "reload launchd agent",
        argv=(
            "launchctl",
            "bootout",
            "gui/501/com.matsvarnskuhler.things-orchestrator-http",
        ),
        settles_to=ServiceStatus.INACTIVE,
    )

    _apply(effect, platform="darwin", uid=501, home=tmp_path)


def test_launchd_bootout_timeout_is_a_concise_service_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
            returncode=0,
            stdout="state = running\n",
        ),
    )
    effect = ServiceEffect(
        "command",
        "reload launchd agent",
        argv=(
            "launchctl",
            "bootout",
            "gui/501/com.matsvarnskuhler.things-orchestrator-http",
        ),
        settles_to=ServiceStatus.INACTIVE,
    )

    with pytest.raises(ServiceApplyError, match="did not reach inactive"):
        _apply(
            effect,
            platform="darwin",
            uid=501,
            home=tmp_path,
            settle_timeout=0.0,
        )


def test_launchd_bootout_timeout_preserves_nonzero_command_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = (
        tmp_path
        / "Library/LaunchAgents/com.matsvarnskuhler.things-orchestrator-http.plist"
    )
    agent.parent.mkdir(parents=True)
    agent.write_text("plist")

    def run(argv: tuple[str, ...], **_kwargs: object) -> object:
        if "bootout" in argv:
            return SimpleNamespace(
                returncode=5,
                stdout="",
                stderr="operation still in progress",
            )
        return SimpleNamespace(returncode=5, stdout="", stderr="probe failed")

    monkeypatch.setattr("things_orchestrator.service.subprocess.run", run)
    effect = ServiceEffect(
        "command",
        "reload launchd agent",
        argv=(
            "launchctl",
            "bootout",
            "gui/501/com.matsvarnskuhler.things-orchestrator-http",
        ),
        settles_to=ServiceStatus.INACTIVE,
    )

    with pytest.raises(ServiceApplyError) as caught:
        _apply(
            effect,
            platform="darwin",
            uid=501,
            home=tmp_path,
            settle_timeout=0.0,
        )

    message = str(caught.value)
    assert "exit 5" in message
    assert "operation still in progress" in message


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (0, "state = running\n", ServiceStatus.ACTIVE),
        (0, "state = exited\n", ServiceStatus.LOADED),
        (0, "", ServiceStatus.LOADED),
        (113, "", ServiceStatus.INACTIVE),
        (5, "", ServiceStatus.UNKNOWN),
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


def test_launchd_status_does_not_treat_a_probe_error_as_unloaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = (
        tmp_path
        / "Library/LaunchAgents/com.matsvarnskuhler.things-orchestrator-http.plist"
    )
    agent.parent.mkdir(parents=True)
    agent.write_text("plist")

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OSError("launchctl unavailable")

    monkeypatch.setattr("things_orchestrator.service.subprocess.run", unavailable)

    assert (
        service_status(platform="darwin", uid=501, home=tmp_path)
        is ServiceStatus.UNKNOWN
    )


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (0, "state = running\n", ServiceStatus.ACTIVE),
        (0, "state = exited\n", ServiceStatus.LOADED),
        (113, "", ServiceStatus.NOT_INSTALLED),
        (5, "", ServiceStatus.UNKNOWN),
    ],
)
def test_launchd_status_queries_an_orphaned_job_without_a_plist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    output: str,
    expected: ServiceStatus,
) -> None:
    monkeypatch.setattr(
        "things_orchestrator.service.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=output,
        ),
    )

    assert service_status(platform="darwin", uid=501, home=tmp_path) is expected


@pytest.mark.parametrize(
    ("active_code", "load_state", "expected"),
    [
        (0, "loaded", ServiceStatus.ACTIVE),
        (3, "loaded", ServiceStatus.LOADED),
        (3, "not-found", ServiceStatus.NOT_INSTALLED),
    ],
)
def test_systemd_status_queries_an_orphaned_unit_without_a_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active_code: int,
    load_state: str,
    expected: ServiceStatus,
) -> None:
    def run(command: tuple[str, ...], **_kwargs: object) -> object:
        if "is-active" in command:
            return SimpleNamespace(returncode=active_code, stdout="")
        assert "show" in command
        return SimpleNamespace(returncode=0, stdout=f"{load_state}\n")

    monkeypatch.setattr("things_orchestrator.service.subprocess.run", run)

    assert service_status(platform="linux", uid=1000, home=tmp_path) is expected


@pytest.mark.parametrize(
    ("platform", "failed_status"),
    (
        ("darwin", ServiceStatus.LOADED),
        ("linux", ServiceStatus.INACTIVE),
    ),
)
def test_service_install_fails_when_the_declared_active_state_is_not_reached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: Literal["darwin", "linux"],
    failed_status: ServiceStatus,
) -> None:
    observed = iter((ServiceStatus.INACTIVE, failed_status))
    plan = ServicePlan(
        platform,
        "install",
        (ServiceEffect("command", "start supervisor", argv=("start",)),),
        ServiceStatus.ACTIVE,
    )
    monkeypatch.setattr("things_orchestrator.service._platform", lambda: platform)
    monkeypatch.setattr(
        "things_orchestrator.service.resolve_console_script", lambda: EXECUTABLE
    )
    monkeypatch.setattr(
        "things_orchestrator.service.service_status",
        lambda **_kwargs: next(observed, failed_status),
    )
    monkeypatch.setattr("things_orchestrator.service._plan_service", lambda **_kwargs: plan)
    monkeypatch.setattr("things_orchestrator.service._apply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("things_orchestrator.service._SETTLE_TIMEOUT", 0.0)

    with pytest.raises(ServiceApplyError, match=f"observed {failed_status.value}"):
        service_action("install", dry_run=False)


def test_service_install_waits_for_the_declared_active_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = iter(
        (ServiceStatus.INACTIVE, ServiceStatus.LOADED, ServiceStatus.ACTIVE)
    )
    plan = ServicePlan(
        "darwin",
        "install",
        (ServiceEffect("command", "start supervisor", argv=("start",)),),
        ServiceStatus.ACTIVE,
    )
    monkeypatch.setattr("things_orchestrator.service._platform", lambda: "darwin")
    monkeypatch.setattr(
        "things_orchestrator.service.resolve_console_script", lambda: EXECUTABLE
    )
    monkeypatch.setattr(
        "things_orchestrator.service.service_status",
        lambda **_kwargs: next(observed),
    )
    monkeypatch.setattr("things_orchestrator.service._plan_service", lambda **_kwargs: plan)
    monkeypatch.setattr("things_orchestrator.service._apply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("things_orchestrator.service.time.sleep", lambda _delay: None)

    result = service_action("install", dry_run=False)

    assert isinstance(result, ServiceOperationResult)
    assert result.status is ServiceStatus.ACTIVE


def test_service_uninstall_fails_when_the_supervisor_remains_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = iter((ServiceStatus.ACTIVE, ServiceStatus.LOADED))
    plan = ServicePlan(
        "darwin",
        "uninstall",
        (ServiceEffect("command", "stop supervisor", argv=("stop",)),),
        ServiceStatus.NOT_INSTALLED,
    )
    monkeypatch.setattr("things_orchestrator.service._platform", lambda: "darwin")
    monkeypatch.setattr(
        "things_orchestrator.service.resolve_console_script", lambda: EXECUTABLE
    )
    monkeypatch.setattr(
        "things_orchestrator.service.service_status",
        lambda **_kwargs: next(observed, ServiceStatus.LOADED),
    )
    monkeypatch.setattr("things_orchestrator.service._plan_service", lambda **_kwargs: plan)
    monkeypatch.setattr("things_orchestrator.service._apply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("things_orchestrator.service._SETTLE_TIMEOUT", 0.0)

    with pytest.raises(ServiceApplyError, match="observed loaded"):
        service_action("uninstall", dry_run=False)


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
