from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from things_orchestrator.cli import _routine_http_composition, build_parser, main
from things_orchestrator.cloud import HistoryBatch, HistoryEvent, HistoryGroup
from things_orchestrator.config import ConfigError, Credentials, McpBearer
from things_orchestrator.library import MemoryLibrary
from things_orchestrator.routines import RoutineWorker
from things_orchestrator.routines_config import (
    DisabledRoutineConfig,
    EnabledRoutineConfig,
    ReceiverSecret,
    RetryPolicy,
    RoutineProfile,
    UnconfiguredRoutineConfig,
    account_digest,
)
from things_orchestrator.routines_store import RoutineStore, StoredEvent
from things_orchestrator.routines_webhook import DeliveryResult
from things_orchestrator.server import RoutineHTTPComposition, ThingsMCPServer
from things_orchestrator.service import render_launchd_plist, render_systemd_unit
from things_orchestrator.workspace import ThingsWorkspace


def _profile(
    *, account: str = "a" * 64, retry: RetryPolicy | None = None
) -> RoutineProfile:
    return RoutineProfile(
        account_digest=account,
        host_profile="always_on",
        receiver_url="https://agent.example/webhooks/task",
        receiver_secret=ReceiverSecret("secret"),
        poll_interval_seconds=60,
        settle_seconds=120,
        retry=retry or RetryPolicy(),
    )


class _UnusedStore:
    def open(self) -> None:
        raise AssertionError("store open was not expected")

    def close(self) -> None:
        raise AssertionError("store close was not expected")

    def cursor(self) -> int:
        raise AssertionError("store cursor was not expected")

    def apply_batch(self, batch: HistoryBatch, *, observed_at: int) -> None:
        raise AssertionError((batch, observed_at))

    def reset_history(self) -> None:
        raise AssertionError("history reset was not expected")

    def due_events(
        self, *, now: int, limit: int = 25
    ) -> tuple[StoredEvent, ...]:
        raise AssertionError((now, limit))

    def record_attempt(
        self,
        event_id: str,
        *,
        attempted_at: int,
        state: str,
        next_attempt_at: int | None,
        http_status: int | None,
        result: str,
    ) -> None:
        raise AssertionError(
            (
                event_id,
                attempted_at,
                state,
                next_attempt_at,
                http_status,
                result,
            )
        )


class _UnusedWebhook:
    def deliver(
        self, event: StoredEvent, *, timestamp: int
    ) -> DeliveryResult:
        raise AssertionError((event, timestamp))


class _UnusedCloud:
    def history_groups(self, start_index: int) -> HistoryBatch:
        raise AssertionError(start_index)


def test_service_definitions_alone_include_hidden_provenance_marker() -> None:
    executable = Path("/opt/bin/things-orchestrator")
    assert "--service-managed" in render_systemd_unit(executable, user="owner")
    assert "--service-managed" in render_launchd_plist(executable)
    parsed = build_parser().parse_args(["serve-http"])
    assert parsed.service_managed is False
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "routines",
                "configure",
                "--profile",
                "always_on",
                "--url",
                "https://agent.example/hook",
                "--secret",
                "leak",
            ]
        )


@pytest.mark.parametrize("service_managed", (False, True))
def test_disabled_or_manual_composition_constructs_no_routine_resources(
    monkeypatch: pytest.MonkeyPatch, service_managed: bool
) -> None:
    credentials = Credentials("owner@example.com", "password", McpBearer("bearer"))
    monkeypatch.setattr(
        "things_orchestrator.cli.load_routines_config",
        lambda: DisabledRoutineConfig(_profile()),
    )
    monkeypatch.setattr(
        "things_orchestrator.cli.CloudClient",
        lambda *_args, **_kwargs: pytest.fail("Cloud client constructed"),
    )
    monkeypatch.setattr(
        "things_orchestrator.cli.RoutineStore",
        lambda *_args, **_kwargs: pytest.fail("store constructed"),
    )
    composition = _routine_http_composition(
        credentials, service_managed=service_managed
    )
    assert composition == RoutineHTTPComposition.disabled()


def test_account_mismatch_and_missing_bearer_fail_closed_before_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "things_orchestrator.cli.load_routines_config",
        lambda: EnabledRoutineConfig(_profile(account="b" * 64)),
    )
    mismatch = _routine_http_composition(
        Credentials("owner@example.com", "password", McpBearer("bearer")),
        service_managed=True,
    )
    missing_bearer = _routine_http_composition(
        Credentials("owner@example.com", "password", None),
        service_managed=True,
    )
    assert mismatch == RoutineHTTPComposition.disabled()
    assert missing_bearer == RoutineHTTPComposition.disabled()


def test_missing_config_and_personal_profile_fail_closed_before_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = Credentials(
        "owner@example.com", "password", McpBearer("bearer")
    )
    monkeypatch.setattr(
        "things_orchestrator.cli.load_routines_config",
        lambda: UnconfiguredRoutineConfig(),
    )
    assert _routine_http_composition(
        credentials, service_managed=True
    ) == RoutineHTTPComposition.disabled()

    personal = RoutineProfile(
        account_digest=_profile().account_digest,
        host_profile=cast(Any, "personal"),
        receiver_url=_profile().receiver_url,
        receiver_secret=_profile().receiver_secret,
        poll_interval_seconds=60,
        settle_seconds=120,
        retry=RetryPolicy(),
    )
    monkeypatch.setattr(
        "things_orchestrator.cli.load_routines_config",
        lambda: EnabledRoutineConfig(personal),
    )
    assert _routine_http_composition(
        credentials, service_managed=True
    ) == RoutineHTTPComposition.disabled()


def test_malformed_config_fails_closed_before_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "things_orchestrator.cli.load_routines_config",
        lambda: (_ for _ in ()).throw(ConfigError("private malformed value")),
    )

    composition = _routine_http_composition(
        Credentials("owner@example.com", "password", McpBearer("bearer")),
        service_managed=True,
    )

    assert composition == RoutineHTTPComposition.disabled()


def test_authenticated_health_reports_disabled_without_routine_resources() -> None:
    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))
    app = server.build_http_app(token="secret")

    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True}
        assert client.get(
            "/health", headers={"Authorization": "Bearer secret"}
        ).json()["routines"] == {"state": "disabled"}


def test_stdio_never_reads_routines_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = False

    class Server:
        def run(self) -> None:
            nonlocal ran
            ran = True

    monkeypatch.setattr(
        "things_orchestrator.cli.load_credentials",
        lambda: Credentials("owner@example.com", "password", None),
    )
    monkeypatch.setattr(
        "things_orchestrator.cli._server",
        lambda _parser, *, credentials, routines: Server(),
    )
    monkeypatch.setattr(
        "things_orchestrator.cli.load_routines_config",
        lambda: pytest.fail("stdio read routines config"),
    )

    main(["serve"])
    assert ran is True


def test_eligible_composition_returns_zero_resource_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from things_orchestrator.routines_config import account_digest

    profile = _profile(account=account_digest("owner@example.com"))
    monkeypatch.setattr(
        "things_orchestrator.cli.load_routines_config",
        lambda: EnabledRoutineConfig(profile),
    )
    monkeypatch.setattr(
        "things_orchestrator.cli.CloudClient",
        lambda *_args, **_kwargs: pytest.fail("factory was invoked"),
    )
    composition = _routine_http_composition(
        Credentials("owner@example.com", "password", McpBearer("bearer")),
        service_managed=True,
    )
    assert composition.state == "initializing"
    assert composition.factory is not None
    with pytest.raises(pytest.fail.Exception, match="factory was invoked"):
        composition.factory()


class _Gate:
    def __init__(self, ready: bool = False) -> None:
        self.ready = ready

    async def wait(self) -> None:
        while not self.ready:
            await anyio.sleep(0.01)


def test_lifecycle_factory_waits_for_explicit_readiness_and_shutdown_does_not_hang() -> (
    None
):
    created = 0

    class Lifecycle:
        async def run(self, stop: anyio.Event) -> None:
            await stop.wait()

        def snapshot(self) -> dict[str, object]:
            return {"state": "running"}

    def factory() -> Lifecycle:
        nonlocal created
        created += 1
        return Lifecycle()

    gate = _Gate()
    server = ThingsMCPServer(
        ThingsWorkspace(MemoryLibrary()),
        routines=RoutineHTTPComposition.enabled(factory),
    )
    app = server.build_http_app(token="secret", readiness=gate)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True}
        detail = client.get(
            "/health", headers={"Authorization": "Bearer secret"}
        ).json()
        assert detail["routines"] == {"state": "initializing"}
        assert created == 0
    assert created == 0


def test_run_http_starts_routines_only_after_uvicorn_socket_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_bound = False
    factory_observations: list[bool] = []

    class Lifecycle:
        async def run(self, stop: anyio.Event) -> None:
            await stop.wait()

        def snapshot(self) -> dict[str, object]:
            return {"state": "running"}

    def factory() -> Lifecycle:
        factory_observations.append(socket_bound)
        return Lifecycle()

    class FakeConfig:
        def __init__(
            self,
            app: Starlette,
            *,
            host: str,
            port: int,
            log_level: str,
        ) -> None:
            assert (host, port, log_level) == ("127.0.0.1", 9876, "warning")
            self.app = app

    class FakeUvicornServer:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config

        async def startup(self, sockets: list[Any] | None = None) -> None:
            nonlocal socket_bound
            assert sockets == []
            assert factory_observations == []
            socket_bound = True

        def run(self) -> None:
            async def exercise() -> None:
                app = self.config.app
                async with app.router.lifespan_context(app):
                    await anyio.lowlevel.checkpoint()
                    assert factory_observations == []
                    await self.startup(sockets=[])
                    for _ in range(10):
                        if factory_observations:
                            break
                        await anyio.lowlevel.checkpoint()
                    assert factory_observations == [True]

            anyio.run(exercise)

    fake_uvicorn = ModuleType("uvicorn")
    setattr(fake_uvicorn, "Config", FakeConfig)
    setattr(fake_uvicorn, "Server", FakeUvicornServer)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    server = ThingsMCPServer(
        ThingsWorkspace(MemoryLibrary()),
        routines=RoutineHTTPComposition.enabled(factory),
    )

    server.run_http(port=9876, token="secret")

    assert socket_bound is True
    assert factory_observations == [True]


def test_blocked_routines_cloud_call_does_not_delay_health_or_mcp_tools() -> None:
    entered = threading.Event()
    release = threading.Event()
    profile = _profile()
    enabled = EnabledRoutineConfig(profile)

    class Store(_UnusedStore):
        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        def cursor(self) -> int:
            return 0

        def apply_batch(self, batch: HistoryBatch, *, observed_at: int) -> None:
            del batch, observed_at

        def due_events(
            self, *, now: int, limit: int = 25
        ) -> tuple[StoredEvent, ...]:
            del now
            assert limit == 25
            return ()

        def reset_history(self) -> None:
            return None

    class Cloud:
        def history_groups(self, start_index: int) -> HistoryBatch:
            assert start_index == 0
            entered.set()
            assert release.wait(timeout=5)
            return HistoryBatch(b"h" * 32, 0, 0, (), True)

    def factory() -> RoutineWorker:
        return RoutineWorker(
            email="owner@example.com",
            profile=profile,
            cloud=Cloud(),
            store=Store(),
            webhook=_UnusedWebhook(),
            config_loader=lambda: enabled,
        )

    gate = _Gate(ready=True)
    server = ThingsMCPServer(
        ThingsWorkspace(MemoryLibrary()),
        routines=RoutineHTTPComposition.enabled(factory),
    )
    app = server.build_http_app(token="secret", readiness=gate)
    try:
        with TestClient(app) as client:
            assert entered.wait(timeout=2)
            assert client.get("/health").json() == {"ok": True}
            detail = client.get(
                "/health", headers={"Authorization": "Bearer secret"}
            ).json()
            assert detail["routines"] == {
                "state": "running",
                "cloud_failures": 0,
                "delivery_failures": 0,
            }
            calls: tuple[tuple[str, dict[str, object]], ...] = (
                ("things_view", {}),
                ("things_receipt", {"operation_id": "op_missing000"}),
                (
                    "things_capture",
                    {
                        "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
                        "items": [{"kind": "task", "title": "Safe"}],
                    },
                ),
            )
            for name, arguments in calls:
                result = asyncio.run(server.call_tool(name, arguments))
                assert result.structured_content is not None
            assert len(asyncio.run(server.list_tools())) == 8
            release.set()
    finally:
        release.set()


def test_retry_policy_persists_backoff_and_dead_letters_at_attempt_bound() -> None:
    recorded: list[dict[str, object]] = []

    class Store(_UnusedStore):
        def record_attempt(
            self,
            event_id: str,
            *,
            attempted_at: int,
            state: str,
            next_attempt_at: int | None,
            http_status: int | None,
            result: str,
        ) -> None:
            recorded.append(
                {
                    "event_id": event_id,
                    "attempted_at": attempted_at,
                    "state": state,
                    "next_attempt_at": next_attempt_at,
                    "http_status": http_status,
                    "result": result,
                }
            )

    class Cloud:
        def history_groups(self, start_index: int) -> HistoryBatch:
            raise AssertionError(start_index)

    worker = RoutineWorker(
        email="owner@example.com",
        profile=_profile(),
        cloud=Cloud(),
        store=Store(),
        webhook=_UnusedWebhook(),
        epoch=lambda: 1_000,
        jitter=lambda ceiling: ceiling / 2,
    )

    async def exercise() -> None:
        await worker._record_delivery(
            StoredEvent("evt_retry", "routine", "task", 900, b"{}", 0),
            DeliveryResult("retry", "network_failure"),
        )
        await worker._record_delivery(
            StoredEvent("evt_dead", "routine", "task", 900, b"{}", 9),
            DeliveryResult("retry", "retryable_http", 500),
        )

    anyio.run(exercise)

    assert recorded[0]["state"] == "pending"
    assert recorded[0]["next_attempt_at"] == 1_002
    assert recorded[1]["state"] == "dead"
    assert recorded[1]["next_attempt_at"] is None


def test_retry_policy_dead_letters_at_maximum_event_age() -> None:
    recorded: list[tuple[str, int | None]] = []

    class Store(_UnusedStore):
        def record_attempt(
            self,
            event_id: str,
            *,
            attempted_at: int,
            state: str,
            next_attempt_at: int | None,
            http_status: int | None,
            result: str,
        ) -> None:
            assert attempted_at == 1_000
            assert http_status is None
            assert result == "network_failure"
            recorded.append((state, next_attempt_at))

    worker = RoutineWorker(
        email="owner@example.com",
        profile=_profile(retry=RetryPolicy(max_age_seconds=60)),
        cloud=_UnusedCloud(),
        store=Store(),
        webhook=_UnusedWebhook(),
        epoch=lambda: 1_000,
    )

    async def exercise() -> None:
        await worker._record_delivery(
            StoredEvent("evt_aged", "routine", "task", 940, b"{}", 0),
            DeliveryResult("retry", "network_failure"),
        )

    anyio.run(exercise)

    assert recorded == [("dead", None)]


def test_accepted_then_crashed_delivery_retries_same_event_as_duplicate(
    tmp_path: Path,
) -> None:
    now = 122
    profile = _profile(account=account_digest("owner@example.com"))
    enabled = EnabledRoutineConfig(profile)
    path = tmp_path / "routines.sqlite3"
    store = RoutineStore(
        profile,
        path=path,
        namespace_factory=lambda: b"n" * 32,
    )
    store.open()
    store.apply_batch(HistoryBatch(b"h" * 32, 0, 0, (), True), observed_at=0)
    store.apply_batch(
        HistoryBatch(
            b"h" * 32,
            0,
            1,
            (
                HistoryGroup(
                    0,
                    (
                        HistoryEvent("ai", 0, "Tag4", {"tt": "AI"}),
                        HistoryEvent(
                            "task",
                            0,
                            "Task7",
                            {
                                "tp": 0,
                                "ss": 0,
                                "tr": False,
                                "tg": ("ai",),
                            },
                        ),
                    ),
                ),
            ),
            True,
        ),
        observed_at=1,
    )
    store.apply_batch(HistoryBatch(b"h" * 32, 1, 1, (), True), observed_at=121)
    original = store.due_events(now=now)[0]
    deliveries: list[tuple[str, bytes, int]] = []

    class Webhook:
        def deliver(
            self, event: StoredEvent, *, timestamp: int
        ) -> DeliveryResult:
            deliveries.append((event.event_id, event.body, timestamp))
            return (
                DeliveryResult("delivered", "accepted", 202)
                if len(deliveries) == 1
                else DeliveryResult("delivered", "duplicate", 200)
            )

    class CrashBeforeCommitStore(_UnusedStore):
        def due_events(
            self, *, now: int, limit: int = 25
        ) -> tuple[StoredEvent, ...]:
            return store.due_events(now=now, limit=limit)

        def record_attempt(
            self,
            event_id: str,
            *,
            attempted_at: int,
            state: str,
            next_attempt_at: int | None,
            http_status: int | None,
            result: str,
        ) -> None:
            del (
                event_id,
                attempted_at,
                state,
                next_attempt_at,
                http_status,
                result,
            )
            raise RuntimeError("simulated crash before delivery commit")

    first = RoutineWorker(
        email="owner@example.com",
        profile=profile,
        cloud=_UnusedCloud(),
        store=CrashBeforeCommitStore(),
        webhook=Webhook(),
        epoch=lambda: now,
        config_loader=lambda: enabled,
    )

    async def first_attempt() -> None:
        with pytest.raises(RuntimeError, match="before delivery commit"):
            await first._drain_due(
                stop=anyio.Event(), poll_due_at=float("inf")
            )

    anyio.run(first_attempt)
    store.close()

    restarted = RoutineStore(profile, path=path)
    restarted.open()
    try:
        pending = restarted.due_events(now=now)
        assert len(pending) == 1
        assert (pending[0].event_id, pending[0].body) == (
            original.event_id,
            original.body,
        )

        now = 123
        second = RoutineWorker(
            email="owner@example.com",
            profile=profile,
            cloud=_UnusedCloud(),
            store=restarted,
            webhook=Webhook(),
            epoch=lambda: now,
            config_loader=lambda: enabled,
        )

        async def duplicate_attempt() -> None:
            attempted, failed = await second._drain_due(
                stop=anyio.Event(), poll_due_at=float("inf")
            )
            assert (attempted, failed) == (1, 0)

        anyio.run(duplicate_attempt)

        counts = restarted.counts()
        assert (counts.pending, counts.delivered, counts.dead) == (0, 1, 0)
        assert restarted.due_events(now=999) == ()
        assert deliveries == [
            (original.event_id, original.body, 122),
            (original.event_id, original.body, 123),
        ]
    finally:
        restarted.close()


def test_timeout_after_send_retries_same_event_identity_and_body(
    tmp_path: Path,
) -> None:
    now = 122
    profile = _profile(account=account_digest("owner@example.com"))
    enabled = EnabledRoutineConfig(profile)
    store = RoutineStore(
        profile,
        path=tmp_path / "routines.sqlite3",
        namespace_factory=lambda: b"n" * 32,
    )
    store.open()
    try:
        store.apply_batch(HistoryBatch(b"h" * 32, 0, 0, (), True), observed_at=0)
        store.apply_batch(
            HistoryBatch(
                b"h" * 32,
                0,
                1,
                (
                    HistoryGroup(
                        0,
                        (
                            HistoryEvent("ai", 0, "Tag4", {"tt": "AI"}),
                            HistoryEvent(
                                "task",
                                0,
                                "Task7",
                                {
                                    "tp": 0,
                                    "ss": 0,
                                    "tr": False,
                                    "tg": ("ai",),
                                },
                            ),
                        ),
                    ),
                ),
                True,
            ),
            observed_at=1,
        )
        store.apply_batch(
            HistoryBatch(b"h" * 32, 1, 1, (), True), observed_at=121
        )
        sent: list[tuple[str, bytes]] = []

        class TimeoutThenDuplicateWebhook:
            def deliver(
                self, event: StoredEvent, *, timestamp: int
            ) -> DeliveryResult:
                assert timestamp in {122, 123}
                sent.append((event.event_id, event.body))
                if len(sent) == 1:
                    return DeliveryResult("retry", "network_failure")
                return DeliveryResult("delivered", "duplicate", 200)

        worker = RoutineWorker(
            email="owner@example.com",
            profile=profile,
            cloud=_UnusedCloud(),
            store=store,
            webhook=TimeoutThenDuplicateWebhook(),
            epoch=lambda: now,
            jitter=lambda _ceiling: 0,
            config_loader=lambda: enabled,
        )

        async def first_attempt_times_out_after_send() -> None:
            assert await worker._drain_due(
                stop=anyio.Event(), poll_due_at=float("inf")
            ) == (1, 1)

        anyio.run(first_attempt_times_out_after_send)
        pending = store.due_events(now=123)
        assert len(pending) == 1
        assert pending[0].attempt_count == 1

        now = 123

        async def duplicate_acknowledges_retry() -> None:
            assert await worker._drain_due(
                stop=anyio.Event(), poll_due_at=float("inf")
            ) == (1, 0)

        anyio.run(duplicate_acknowledges_retry)

        assert sent == [sent[0], sent[0]]
        counts = store.counts()
        assert (counts.pending, counts.delivered, counts.dead) == (0, 1, 0)
    finally:
        store.close()


def test_idle_caught_up_worker_polls_at_most_once_per_interval() -> None:
    clock = 0.0
    poll_times: list[float] = []
    profile = _profile(account=account_digest("owner@example.com"))
    enabled = EnabledRoutineConfig(profile)

    class Store(_UnusedStore):
        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        def cursor(self) -> int:
            return 0

        def apply_batch(self, batch: HistoryBatch, *, observed_at: int) -> None:
            assert batch.caught_up is True
            assert observed_at == int(clock)

        def due_events(
            self, *, now: int, limit: int = 25
        ) -> tuple[StoredEvent, ...]:
            assert now == int(clock)
            assert limit == 25
            return ()

    class Cloud:
        def history_groups(self, start_index: int) -> HistoryBatch:
            assert start_index == 0
            poll_times.append(clock)
            return HistoryBatch(b"h" * 32, 0, 0, (), True)

    async def advance(delay: float, stop: anyio.Event) -> None:
        nonlocal clock
        assert delay > 0
        clock += delay
        if clock >= 180:
            stop.set()
        await anyio.lowlevel.checkpoint()

    worker = RoutineWorker(
        email="owner@example.com",
        profile=profile,
        cloud=Cloud(),
        store=Store(),
        webhook=_UnusedWebhook(),
        epoch=lambda: int(clock),
        monotonic=lambda: clock,
        config_loader=lambda: enabled,
        wait_for_stop=advance,
    )

    async def exercise() -> None:
        await worker.run(anyio.Event())

    anyio.run(exercise)

    assert poll_times == [0.0, 60.0, 120.0]


def test_runtime_status_combines_poll_and_delivery_failure_state() -> None:
    clock = 0.0
    due_calls = 0
    poll_times: list[float] = []
    snapshots: dict[str, dict[str, object]] = {}
    attempts: list[tuple[str, str]] = []
    profile = _profile(account=account_digest("owner@example.com"))
    enabled = EnabledRoutineConfig(profile)
    event = StoredEvent("evt_retry", "routine", "task", 0, b"{}", 0)
    worker: RoutineWorker

    class Store(_UnusedStore):
        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        def cursor(self) -> int:
            return 0

        def apply_batch(self, batch: HistoryBatch, *, observed_at: int) -> None:
            assert batch.caught_up is True
            assert observed_at == int(clock)

        def due_events(
            self, *, now: int, limit: int = 25
        ) -> tuple[StoredEvent, ...]:
            nonlocal due_calls
            assert now == int(clock)
            assert limit == 25
            due_calls += 1
            if due_calls == 1:
                return (event,)
            snapshots["after_clean_poll"] = worker.snapshot()
            return ()

        def record_attempt(
            self,
            event_id: str,
            *,
            attempted_at: int,
            state: str,
            next_attempt_at: int | None,
            http_status: int | None,
            result: str,
        ) -> None:
            assert attempted_at == 0
            assert next_attempt_at is not None
            assert http_status is None
            attempts.append((event_id, state))
            assert result == "network_failure"

    class Cloud:
        def history_groups(self, start_index: int) -> HistoryBatch:
            assert start_index == 0
            poll_times.append(clock)
            return HistoryBatch(b"h" * 32, 0, 0, (), True)

    class Webhook:
        def deliver(
            self, delivered: StoredEvent, *, timestamp: int
        ) -> DeliveryResult:
            assert delivered == event
            assert timestamp == 0
            return DeliveryResult("retry", "network_failure")

    wait_calls = 0

    async def advance(delay: float, stop: anyio.Event) -> None:
        nonlocal clock, wait_calls
        wait_calls += 1
        if wait_calls == 1:
            assert delay == 1
            snapshots["after_retry"] = worker.snapshot()
            clock = 60
        else:
            assert delay == 5
            snapshots["after_clean_drain"] = worker.snapshot()
            stop.set()
        await anyio.lowlevel.checkpoint()

    worker = RoutineWorker(
        email="owner@example.com",
        profile=profile,
        cloud=Cloud(),
        store=Store(),
        webhook=Webhook(),
        epoch=lambda: int(clock),
        monotonic=lambda: clock,
        config_loader=lambda: enabled,
        wait_for_stop=advance,
    )

    async def exercise() -> None:
        await worker.run(anyio.Event())

    anyio.run(exercise)

    backing_off = {
        "state": "backing_off",
        "cloud_failures": 0,
        "delivery_failures": 1,
    }
    assert snapshots == {
        "after_retry": backing_off,
        "after_clean_poll": backing_off,
        "after_clean_drain": {
            "state": "running",
            "cloud_failures": 0,
            "delivery_failures": 0,
        },
    }
    assert attempts == [("evt_retry", "pending")]
    assert poll_times == [0.0, 60]


def test_store_open_failure_stops_worker_without_escaping_into_http_lifecycle() -> None:
    class Store(_UnusedStore):
        def open(self) -> None:
            raise RuntimeError("lock busy")

    class Cloud:
        def history_groups(self, start_index: int) -> HistoryBatch:
            raise AssertionError(start_index)

    worker = RoutineWorker(
        email="owner@example.com",
        profile=_profile(),
        cloud=Cloud(),
        store=Store(),
        webhook=_UnusedWebhook(),
    )

    async def exercise() -> None:
        await worker.run(anyio.Event())

    anyio.run(exercise)
    assert worker.snapshot() == {
        "state": "stopped",
        "cloud_failures": 0,
        "delivery_failures": 0,
    }
