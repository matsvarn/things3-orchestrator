from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import things_orchestrator.diagnostics as diagnostics
from things_orchestrator.cloud import CloudError
from things_orchestrator.config import Credentials, normalize_mcp_url
from things_orchestrator.deployment import DeploymentIdentity
from things_orchestrator.diagnostics import (
    CloudCheck,
    RoutineDiagnostic,
    SupportReport,
    build_support_report,
    classify_endpoint,
    run_cloud_check,
)
from things_orchestrator.library import ApplyResult, MemoryLibrary, Record, Write
from things_orchestrator.recurrence import RecurrenceState
from things_orchestrator.routines_config import (
    ReceiverKind,
    ReceiverSecret,
    configure_routines,
    set_routines_enabled,
)
from things_orchestrator.routines_store import RoutineStore, StoreCounts


class _ReadOnlyLibrary(MemoryLibrary):
    def __init__(self, records: list[Record]) -> None:
        super().__init__(records)
        self.refreshed = False
        self.tags = {"private-tag-id": "private tag"}

    def refresh(self, *, force: bool = False) -> None:
        assert force is True
        self.refreshed = True

    def apply(self, writes: list[Write]) -> ApplyResult:
        del writes
        raise AssertionError("cloud-check must not write")


def test_cloud_check_force_refreshes_and_returns_only_structural_counts() -> None:
    library = _ReadOnlyLibrary(
        [
            Record(
                "private-task-id",
                "task",
                "private title",
                notes="private notes",
                checklists=[],
            ),
            Record("private-project-id", "project", "private project", status="done"),
            Record("private-area-id", "area", "private area"),
            Record(
                "private-heading-id",
                "project",
                "private heading",
                heading=True,
            ),
            Record("private-trash-id", "task", "private trash", trashed=True),
        ]
    )

    check = run_cloud_check(library)

    assert library.refreshed is True
    assert check.status == "ok"
    assert check.as_dict() == {
        "status": "ok",
        "counts": {
            "areas": 1,
            "checklist_items": 0,
            "done": 1,
            "dropped": 0,
            "headings": 1,
            "open": 2,
            "projects": 1,
            "records": 5,
            "repeating_templates": 0,
            "tags": 1,
            "tasks": 2,
            "trashed": 1,
        },
    }
    serialized = json.dumps(check.as_dict(), sort_keys=True)
    for private in (
        "private-task-id",
        "private title",
        "private notes",
        "private tag",
    ):
        assert private not in serialized


def test_cloud_check_open_count_excludes_hidden_and_trashed_rows() -> None:
    visible = Record("visible", "task", "Visible")
    trashed = Record("trashed", "task", "Trashed", trashed=True)
    heading = Record("heading", "project", "Heading", heading=True)
    template = Record(
        "template",
        "task",
        "Template",
        recurrence=RecurrenceState(role="template"),
    )

    check = run_cloud_check(_ReadOnlyLibrary([visible, trashed, heading, template]))

    counts = check.as_dict()["counts"]
    assert isinstance(counts, dict)
    assert counts["open"] == 1


def test_collected_cloud_check_uses_and_removes_a_fresh_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_cache: list[Path] = []

    class FakeClient:
        def __init__(self, email: str, password: str) -> None:
            assert (email, password) == ("private@example.com", "private-password")

    class FakeLibrary:
        records: dict[str, Record] = {}
        tags: dict[str, str] = {}

        def __init__(self, client: FakeClient, *, cache: Path) -> None:
            observed_cache.append(cache)
            assert not cache.exists()

        def refresh(self, *, force: bool = False) -> None:
            assert force is True

    monkeypatch.setattr(diagnostics, "CloudClient", FakeClient)
    monkeypatch.setattr(diagnostics, "CloudLibrary", FakeLibrary)
    monkeypatch.setattr(
        diagnostics,
        "_credentials",
        lambda: Credentials("private@example.com", "private-password", None),
    )

    assert diagnostics.collect_cloud_check().status == "ok"
    assert len(observed_cache) == 1
    assert not observed_cache[0].parent.exists()


def test_cloud_check_maps_cloud_errors_to_fixed_statuses() -> None:
    class FailingLibrary(MemoryLibrary):
        def refresh(self, *, force: bool = False) -> None:
            raise CloudError("private upstream response containing a URL and title")

    check = run_cloud_check(FailingLibrary())

    assert check == CloudCheck("unavailable")
    assert check.as_dict() == {"status": "unavailable"}


def test_endpoint_classification_never_returns_the_endpoint() -> None:
    assert classify_endpoint(normalize_mcp_url("http://127.0.0.1:8787")) == "loopback"
    assert (
        classify_endpoint(normalize_mcp_url("https://owner.example.ts.net"))
        == "tailnet"
    )
    assert classify_endpoint(normalize_mcp_url("https://mcp.example.com")) == "public"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://[fd7a:115c:a1e0::53]/mcp", "tailnet"),
        ("https://[fd00::53]/mcp", "public"),
        ("https://[2001:db8::53]/mcp", "public"),
        ("http://[::1]:8787/mcp", "loopback"),
    ],
)
def test_endpoint_classification_handles_tailscale_ipv6_exactly(
    url: str, expected: str
) -> None:
    assert classify_endpoint(normalize_mcp_url(url)) == expected


def test_diagnostics_count_operations_across_email_capitalization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    journal = tmp_path / "journal.sqlite3"
    with sqlite3.connect(journal) as connection:
        connection.execute(
            "CREATE TABLE owner_operations_v2 (account_id TEXT, state TEXT)"
        )
        connection.executemany(
            "INSERT INTO owner_operations_v2 VALUES (?, ?)",
            [
                ("Owner@Example.com", "pending"),
                ("owner@example.COM", "applied"),
                ("someone-else@example.com", "rejected"),
            ],
        )
    monkeypatch.setattr(diagnostics, "journal_path", lambda _email: journal)

    counts = diagnostics._operation_counts(
        Credentials("OWNER@example.com", "private-password", None)
    )

    assert counts == (("v2.applied", 1), ("v2.pending", 1))


@pytest.mark.parametrize(
    "contents",
    [
        '{"email":"private@example.com","password":',
        '{"email":"private@example.com"}',
        '{"email":"private@example.com","password":"private","mcp_token":""}',
        None,
    ],
)
def test_unreadable_credentials_have_a_fixed_value_free_diagnostic_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str | None
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    credentials = tmp_path / "things-orchestrator/credentials.json"

    assert diagnostics.collect_cloud_check() == CloudCheck("not_configured")

    credentials.parent.mkdir()
    if contents is None:
        credentials.mkdir()
    else:
        credentials.write_text(contents)
    monkeypatch.setattr(
        diagnostics,
        "installed_identity",
        lambda: DeploymentIdentity(
            version="0.9.1",
            commit="a" * 40,
            requested_revision=None,
            source="pep610",
        ),
    )
    monkeypatch.setattr(diagnostics, "_service_status", lambda: None)
    monkeypatch.setattr(diagnostics, "_endpoint_class", lambda _path: None)

    cloud = diagnostics.collect_cloud_check()
    report = diagnostics.collect_support_report()

    assert cloud == CloudCheck("credentials_unreadable")
    assert report.cloud_check == CloudCheck("credentials_unreadable")
    serialized = report.to_json()
    assert "private@example.com" not in serialized
    assert "password" not in serialized
    assert str(credentials) not in serialized


def test_support_report_serialization_is_value_free_and_deterministic(
    tmp_path: Path,
) -> None:
    report = build_support_report(
        identity=DeploymentIdentity(
            version="0.9.1",
            commit="a" * 40,
            requested_revision="private tag",
            source="pep610",
        ),
        platform_name="darwin",
        python_version="3.12.11",
        service="active",
        endpoint_class="public",
        cloud=CloudCheck("ok", (("records", 3), ("tasks", 2))),
        routines=RoutineDiagnostic(
            "enabled", "bound", "active", "running",
            history_phase="live",
            trigger_tag_discovered=True,
            trigger_ready=True,
            counts=(("candidates", 1), ("dead", 0), ("delivered", 2), ("pending", 1)),
            receiver_kind="grok",
            poll_interval_seconds=60,
            settlement_window_seconds=120,
            last_successful_poll_at=10,
            last_delivery_at=9,
        ),
        operation_states=(("v2.applied", 2), ("v2.pending", 1)),
    )

    first = report.to_json()
    second = report.to_json()

    assert first == second
    assert json.loads(first) == {
        "cloud_check": {"counts": {"records": 3, "tasks": 2}, "status": "ok"},
        "commit": "a" * 40,
        "endpoint_class": "public",
        "operation_states": {"v2.applied": 2, "v2.pending": 1},
        "platform": "darwin",
        "python": "3.12.11",
        "routines": {
            "account_binding": "bound",
            "configuration_state": "enabled",
            "counts": {
                "candidates": 1,
                "dead": 0,
                "delivered": 2,
                "pending": 1,
            },
            "fixed_trigger": "new normal open untrashed task with the exact directly assigned AI tag",
            "history_phase": "live",
            "last_delivery_at": 9,
            "last_successful_poll_at": 10,
            "poll_interval_seconds": 60,
            "receiver_kind": "grok",
            "service_state": "active",
            "settlement_window_seconds": 120,
            "trigger_ready": True,
            "trigger_tag_discovered": True,
            "worker_liveness": "running",
        },
        "service_status": "active",
        "tool_contract_hash": report.tool_contract_hash,
        "tool_schema_hash": report.tool_schema_hash,
        "version": "0.9.1",
    }
    assert "private tag" not in first
    assert str(tmp_path) not in first
    assert "http" not in first
    assert isinstance(report, SupportReport)


def test_runtime_probe_ignores_environment_proxy_and_keeps_bearer_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_requests: list[str | None] = []
    proxy_requests: list[str | None] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"routines":{"state":"running"}}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            proxy_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b'{"ok":true,"routines":{"state":"backing_off"}}'
            )

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    target = HTTPServer(("127.0.0.1", 0), TargetHandler)
    proxy = HTTPServer(("127.0.0.1", 0), ProxyHandler)
    target_thread = threading.Thread(target=target.serve_forever)
    proxy_thread = threading.Thread(target=proxy.serve_forever)
    target_thread.start()
    proxy_thread.start()
    monkeypatch.setattr(
        diagnostics,
        "_ROUTINE_HEALTH_URL",
        f"http://127.0.0.1:{target.server_port}/health",
    )
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    try:
        result = diagnostics.probe_routine_runtime("private-bearer")
    finally:
        target.shutdown()
        proxy.shutdown()
        target_thread.join()
        proxy_thread.join()
        target.server_close()
        proxy.server_close()

    assert result == {"state": "running"}
    assert target_requests == ["Bearer private-bearer"]
    assert proxy_requests == []


def test_runtime_probe_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            if self.path == "/health":
                self.send_response(302)
                self.send_header("Location", "/forged")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"routines":{"state":"running"}}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(
        diagnostics,
        "_ROUTINE_HEALTH_URL",
        f"http://127.0.0.1:{server.server_port}/health",
    )
    try:
        result = diagnostics.probe_routine_runtime("private-bearer")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result is None
    assert paths == ["/health"]


def test_runtime_probe_without_bearer_attempts_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_opener() -> None:
        raise AssertionError("probe must not construct a transport without a bearer")

    monkeypatch.setattr(diagnostics, "proxyless_no_redirect_opener", fail_opener)

    assert diagnostics.probe_routine_runtime(None) is None


def test_routines_diagnostic_reads_only_safe_aggregates_without_creating_database(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "routines.json"
    database_path = tmp_path / "routines.sqlite3"
    credentials = Credentials("private@example.com", "private-password", None)

    unconfigured = diagnostics.collect_routines_diagnostic(
        credentials,
        config_path=config_path,
        database_path=database_path,
    )

    assert unconfigured.configuration_state == "unconfigured"
    assert unconfigured.account_binding == "not_applicable"
    assert unconfigured.history_phase == "unknown"
    assert unconfigured.worker_liveness == "unknown"
    assert not database_path.exists()

    configured = configure_routines(
        email=credentials.email,
        receiver_url="https://private.example/webhooks/private-route",
        receiver_secret=ReceiverSecret("private-receiver-secret"),
        poll_interval_seconds=60,
        path=config_path,
    )
    enabled = set_routines_enabled(
        True,
        email=credentials.email,
        path=config_path,
    )
    store = RoutineStore(enabled.profile, path=database_path)
    store.open()
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE meta SET phase = 'live', history_fingerprint = ?, cursor = 99",
            (b"private-history-fingerprint!!".ljust(32, b"!"),),
        )
        connection.execute(
            "INSERT INTO candidates VALUES (?, 1, 'task', 'open', 0, 1, 1, 2)",
            ("private-task-id",),
        )
        connection.executemany(
            "INSERT INTO events (event_id, routine_id, task_uuid, creation_group, observed_at, body, state, next_attempt_at, terminal_at) VALUES (?, 'routine', ?, 1, 1, ?, ?, ?, ?)",
            (
                ("private-event-pending", "private-task-p", b"{}", "pending", 1, None),
                ("private-event-delivered", "private-task-d", None, "delivered", None, 2),
                ("private-event-dead", "private-task-x", b"{}", "dead", None, 2),
            ),
        )

    diagnostic = diagnostics.collect_routines_diagnostic(
        credentials,
        config_path=config_path,
        database_path=database_path,
    )

    assert configured.state == "disabled"
    rendered = diagnostic.as_dict()
    assert rendered["configuration_state"] == "enabled"
    assert rendered["account_binding"] == "bound"
    assert rendered["receiver_kind"] == "hermes"
    assert rendered["history_phase"] == "live"
    assert rendered["trigger_tag_discovered"] is False
    assert rendered["trigger_ready"] is False
    assert rendered["last_delivery_at"] == 2
    assert rendered["counts"] == {
        "candidates": 1,
        "dead": 1,
        "delivered": 1,
        "pending": 1,
    }
    serialized = json.dumps(diagnostic.as_dict(), sort_keys=True)
    for private in (
        "private@example.com",
        "private-password",
        "private.example",
        "private-route",
        "private-receiver-secret",
        "private-task-id",
        "private-event-pending",
        "history_fingerprint",
        "cursor",
    ):
        assert private not in serialized


@pytest.mark.parametrize("malformed", (False, True))
def test_routines_diagnostic_mismatch_or_malformed_config_never_opens_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed: bool,
) -> None:
    config_path = tmp_path / "routines.json"
    database_path = tmp_path / "old-account.sqlite3"
    if malformed:
        config_path.write_text('{"version":1,"state":"enabled","profile":')
    else:
        configure_routines(
            email="old-account@example.com",
            receiver_url="https://private.example/webhooks/private-route",
            receiver_secret=ReceiverSecret("private-secret"),
            poll_interval_seconds=60,
            path=config_path,
        )
    monkeypatch.setattr(
        diagnostics,
        "read_routine_counts",
        lambda *_args: pytest.fail("old routines database was opened"),
    )

    diagnostic = diagnostics.collect_routines_diagnostic(
        Credentials("current@example.com", "password", None),
        config_path=config_path,
        database_path=database_path,
    )

    assert diagnostic.account_binding == ("unknown" if malformed else "mismatch")
    assert diagnostic.configuration_state == (
        "malformed" if malformed else "disabled"
    )
    assert diagnostic.receiver_kind == (None if malformed else "hermes")
    assert not database_path.exists()


@pytest.mark.parametrize(
    ("receiver_kind", "receiver_url"),
    (
        ("hermes", "https://private.example/webhooks/private-route"),
        ("grok", "https://api2.cursor.sh/automations/webhook/private-route"),
    ),
)
def test_configured_diagnostic_exposes_only_receiver_kind_without_database(
    receiver_kind: ReceiverKind,
    receiver_url: str,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "routines.json"
    database_path = tmp_path / "missing.sqlite3"
    configure_routines(
        email="owner@example.com",
        receiver_kind=receiver_kind,
        receiver_url=receiver_url,
        receiver_secret=ReceiverSecret("private-credential"),
        poll_interval_seconds=60,
        path=config_path,
    )

    diagnostic = diagnostics.collect_routines_diagnostic(
        Credentials("owner@example.com", "private-password", None),
        config_path=config_path,
        database_path=database_path,
    )

    assert diagnostic.configuration_state == "disabled"
    assert diagnostic.account_binding == "bound"
    assert diagnostic.receiver_kind == receiver_kind
    assert diagnostic.history_phase == "unknown"
    assert diagnostic.worker_liveness == "unknown"
    serialized = json.dumps(diagnostic.as_dict())
    for private in ("private-route", "private-credential", "private-password"):
        assert private not in serialized
    assert not database_path.exists()


def test_configured_diagnostic_keeps_receiver_kind_for_unknown_store_phase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "routines.json"
    configure_routines(
        email="owner@example.com",
        receiver_url="https://private.example/webhooks/private-route",
        receiver_secret=ReceiverSecret("private-secret"),
        poll_interval_seconds=60,
        path=config_path,
    )
    monkeypatch.setattr(
        diagnostics,
        "read_routine_counts",
        lambda *_args: StoreCounts("future", 0, 0, 0, 0, 0, 0),
    )

    diagnostic = diagnostics.collect_routines_diagnostic(
        Credentials("owner@example.com", "private-password", None),
        config_path=config_path,
        database_path=tmp_path / "unused.sqlite3",
    )

    assert diagnostic.configuration_state == "disabled"
    assert diagnostic.account_binding == "bound"
    assert diagnostic.receiver_kind == "hermes"
    assert diagnostic.history_phase == "unknown"


@pytest.mark.parametrize(
    ("runtime_state", "expected_liveness"),
    (
        ("initializing", "initializing"),
        ("running", "running"),
        ("backing_off", "backing_off"),
        ("stopped", "stopped"),
        ("future", "unknown"),
    ),
)
def test_routines_diagnostic_uses_runtime_only_as_live_liveness_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime_state: str,
    expected_liveness: str,
) -> None:
    config_path = tmp_path / "routines.json"
    configured = configure_routines(
        email="owner@example.com",
        receiver_url="https://agent.example/webhooks/route",
        receiver_secret=ReceiverSecret("secret"),
        poll_interval_seconds=60,
        path=config_path,
    )
    set_routines_enabled(True, email="owner@example.com", path=config_path)
    monkeypatch.setattr(
        diagnostics,
        "read_routine_counts",
        lambda *_args: StoreCounts("live", 4, 1, 0, 0, 2, 0, 70),
    )

    result = diagnostics.collect_routines_diagnostic(
        Credentials("owner@example.com", "password", None),
        config_path=config_path,
        database_path=tmp_path / "unused.sqlite3",
        service_state="active",
        runtime={
            "state": runtime_state,
            "last_successful_poll_at": 80,
            "last_delivery_at": 75,
        },
    )

    assert configured.state == "disabled"
    assert result.worker_liveness == expected_liveness
    assert result.history_phase == "live"
    assert result.trigger_tag_discovered is True
    assert result.trigger_ready is (expected_liveness in {"running", "backing_off"})
    assert result.last_successful_poll_at == (
        80 if expected_liveness != "unknown" else None
    )
    assert result.last_delivery_at == 70


def test_live_database_does_not_imply_a_running_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "routines.json"
    configure_routines(
        email="owner@example.com",
        receiver_url="https://agent.example/webhooks/route",
        receiver_secret=ReceiverSecret("secret"),
        poll_interval_seconds=60,
        path=config_path,
    )
    set_routines_enabled(True, email="owner@example.com", path=config_path)
    monkeypatch.setattr(
        diagnostics,
        "read_routine_counts",
        lambda *_args: StoreCounts("live", 4, 1, 0, 0, 1, 0, 70),
    )

    result = diagnostics.collect_routines_diagnostic(
        Credentials("owner@example.com", "password", None),
        config_path=config_path,
        database_path=tmp_path / "unused.sqlite3",
        service_state="inactive",
        runtime={"state": "running", "last_successful_poll_at": 80},
    )

    assert result.history_phase == "live"
    assert result.service_state == "inactive"
    assert result.worker_liveness == "stopped"
    assert result.trigger_tag_discovered is True
    assert result.trigger_ready is False


@pytest.mark.parametrize(("ai_tags", "ready"), ((0, False), (1, True)))
def test_trigger_readiness_requires_an_exact_current_ai_tag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ai_tags: int,
    ready: bool,
) -> None:
    config_path = tmp_path / "routines.json"
    configure_routines(
        email="owner@example.com",
        receiver_url="https://agent.example/webhooks/route",
        receiver_secret=ReceiverSecret("secret"),
        poll_interval_seconds=60,
        path=config_path,
    )
    set_routines_enabled(True, email="owner@example.com", path=config_path)
    monkeypatch.setattr(
        diagnostics,
        "read_routine_counts",
        lambda *_args: StoreCounts("live", 4, ai_tags, 0, 0, 0, 0),
    )

    result = diagnostics.collect_routines_diagnostic(
        Credentials("owner@example.com", "password", None),
        config_path=config_path,
        database_path=tmp_path / "unused.sqlite3",
        service_state="active",
        runtime={"state": "running", "last_successful_poll_at": 80},
    )

    assert result.trigger_tag_discovered is bool(ai_tags)
    assert result.trigger_ready is ready


@pytest.mark.parametrize("phase", ("uninitialized", "seeding"))
def test_trigger_discovery_is_unknown_until_the_tag_baseline_is_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    config_path = tmp_path / "routines.json"
    configure_routines(
        email="owner@example.com",
        receiver_url="https://agent.example/webhooks/route",
        receiver_secret=ReceiverSecret("secret"),
        poll_interval_seconds=60,
        path=config_path,
    )
    set_routines_enabled(True, email="owner@example.com", path=config_path)
    monkeypatch.setattr(
        diagnostics,
        "read_routine_counts",
        lambda *_args: StoreCounts(phase, 4, 0, 0, 0, 0, 0),
    )

    result = diagnostics.collect_routines_diagnostic(
        Credentials("owner@example.com", "password", None),
        config_path=config_path,
        database_path=tmp_path / "unused.sqlite3",
        service_state="active",
        runtime={"state": "initializing"},
    )

    assert result.trigger_tag_discovered is None
    assert result.trigger_ready is False
