from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from things_orchestrator.cloud import HistoryBatch, HistoryEvent, HistoryGroup
from things_orchestrator.routines_config import (
    HermesReceiver,
    ReceiverSecret,
    RetryPolicy,
    RoutineProfile,
)
from things_orchestrator.routines_store import (
    RoutineHistoryIdentityChanged,
    RoutineStore,
    RoutineStoreAlreadyOwned,
    RoutineStoreError,
    canonical_event_body,
    read_routine_counts,
    routine_event_id,
)

FINGERPRINT = b"h" * 32
NAMESPACE = b"n" * 32


def _profile() -> RoutineProfile:
    return RoutineProfile(
        account_digest="a" * 64,
        host_profile="always_on",
        receiver=HermesReceiver(
            "https://agent.example/webhooks/things-ai", ReceiverSecret("secret")
        ),
        poll_interval_seconds=60,
        settle_seconds=120,
        retry=RetryPolicy(),
    )


def _event(
    uuid: str, action: int, entity: str, payload: dict[str, object]
) -> HistoryEvent:
    return HistoryEvent(uuid, action, entity, payload)


def _batch(
    start: int,
    current: int,
    groups: list[list[HistoryEvent]],
    *,
    caught_up: bool,
) -> HistoryBatch:
    return HistoryBatch(
        FINGERPRINT,
        start,
        current,
        tuple(
            HistoryGroup(start + offset, tuple(events))
            for offset, events in enumerate(groups)
        ),
        caught_up,
    )


def _store(tmp_path: Path) -> RoutineStore:
    store = RoutineStore(
        _profile(),
        path=tmp_path / "routines.sqlite3",
        namespace_factory=lambda: NAMESPACE,
    )
    store.open()
    return store


def test_tag_seed_ignores_historical_tasks_and_live_task_settles_once(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(
            _batch(
                0,
                1,
                [
                    [
                        _event("ai-1", 0, "Tag4", {"tt": "AI"}),
                        _event(
                            "historical",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai-1",)},
                        ),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=10,
        )
        assert store.counts().phase == "live"
        assert store.counts().candidates == 0

        store.apply_batch(
            _batch(
                1,
                2,
                [
                    [
                        _event(
                            "fresh",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai-1",)},
                        )
                    ]
                ],
                caught_up=True,
            ),
            observed_at=100,
        )
        store.apply_batch(
            _batch(
                2,
                3,
                [[_event("fresh", 1, "Task7", {"tt": "late sparse title"})]],
                caught_up=True,
            ),
            observed_at=110,
        )
        assert b"late sparse title" not in store.path.read_bytes()
        store.apply_batch(_batch(3, 3, [], caught_up=True), observed_at=229)
        assert store.counts().pending == 0
        assert store.counts().candidates == 1

        store.apply_batch(_batch(3, 3, [], caught_up=True), observed_at=230)
        due = store.due_events(now=230)
        assert len(due) == 1
        body = json.loads(due[0].body)
        assert body == {
            "event_id": due[0].event_id,
            "event_type": "task.created",
            "observed_at": 100,
            "routine_id": _profile().routine_id,
            "schema_version": 1,
            "task_id": "task:fresh",
        }
        assert "historical" not in due[0].body.decode()

        store.apply_batch(
            _batch(
                3,
                4,
                [
                    [
                        _event(
                            "fresh",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai-1",)},
                        )
                    ]
                ],
                caught_up=True,
            ),
            observed_at=300,
        )
        store.apply_batch(_batch(4, 4, [], caught_up=True), observed_at=420)
        assert store.counts().pending == 1
        assert store.counts().candidates == 0
    finally:
        store.close()


def test_large_tag_seed_keeps_only_exact_ai_tags_and_no_historical_tasks(
    tmp_path: Path,
) -> None:
    group_count = 240
    tasks_per_group = 25
    groups: list[list[HistoryEvent]] = []
    for group_index in range(group_count):
        events: list[HistoryEvent] = []
        if group_index == 0:
            events.extend(
                (
                    _event("ai-one", 0, "Tag4", {"tt": "AI"}),
                    _event("ai-two", 0, "Tag4", {"tt": "Later"}),
                    _event("renamed-away", 0, "Tag4", {"tt": "AI"}),
                )
            )
        elif group_index == 80:
            events.append(_event("ai-two", 1, "Tag4", {"tt": "AI"}))
        elif group_index == 160:
            events.append(
                _event("renamed-away", 1, "Tag4", {"tt": "Not AI"})
            )
        events.extend(
            _event(
                f"historical-task-{group_index}-{task_index}",
                0,
                "Task7",
                {
                    "tp": 0,
                    "ss": 0,
                    "tr": False,
                    "tg": ("ai-one",),
                },
            )
            for task_index in range(tasks_per_group)
        )
        groups.append(events)

    store = _store(tmp_path)
    try:
        page_size = 20
        for start in range(0, group_count, page_size):
            page = groups[start : start + page_size]
            store.apply_batch(
                _batch(
                    start,
                    group_count,
                    page,
                    caught_up=start + len(page) == group_count,
                ),
                observed_at=start,
            )
    finally:
        store.close()

    persisted = read_routine_counts(store.path, _profile().account_digest)
    assert persisted is not None
    assert (
        persisted.phase,
        persisted.cursor,
        persisted.ai_tags,
        persisted.candidates,
        persisted.pending,
        persisted.delivered,
        persisted.dead,
    ) == ("live", group_count, 2, 0, 0, 0, 0)
    with sqlite3.connect(store.path) as connection:
        ai_tags = connection.execute(
            "SELECT tag_uuid FROM ai_tags ORDER BY tag_uuid"
        ).fetchall()
        candidate_tags = connection.execute(
            "SELECT COUNT(*) FROM candidate_tags"
        ).fetchone()
        event_count_row = connection.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()
    assert ai_tags == [("ai-one",), ("ai-two",)]
    assert candidate_tags == (0,)
    assert event_count_row == (0,)


def test_task_created_during_multi_page_seed_is_not_lost_or_settled_early(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(
            _batch(
                0,
                3,
                [
                    [
                        _event("ai-one", 0, "Tag4", {"tt": "AI"}),
                        _event("ai-two", 0, "Tag4", {"tt": "AI"}),
                    ]
                ],
                caught_up=False,
            ),
            observed_at=1,
        )
        store.apply_batch(
            _batch(
                1,
                4,
                [
                    [
                        _event(
                            "historical",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai-one",)},
                        )
                    ],
                    [],
                ],
                caught_up=False,
            ),
            observed_at=2,
        )
        assert store.counts().phase == "live"
        assert store.counts().ai_tags == 2
        assert store.counts().candidates == 0

        store.apply_batch(
            _batch(
                3,
                5,
                [
                    [
                        _event(
                            "during-seed",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai-two",)},
                        )
                    ]
                ],
                caught_up=False,
            ),
            observed_at=3,
        )
        store.apply_batch(_batch(4, 5, [[]], caught_up=False), observed_at=500)
        assert store.counts().pending == 0
        assert store.counts().candidates == 1
        store.apply_batch(_batch(5, 5, [], caught_up=True), observed_at=500)
        assert store.counts().pending == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload",
    (
        {"tp": 1, "ss": 0, "tr": False, "tg": ("ai",)},
        {"tp": 2, "ss": 0, "tr": False, "tg": ("ai",)},
        {"tp": 0, "ss": 3, "tr": False, "tg": ("ai",)},
        {"tp": 0, "ss": 2, "tr": False, "tg": ("ai",)},
        {"tp": 0, "ss": 0, "tr": True, "tg": ("ai",)},
        {"ss": 0, "tr": False, "tg": ("ai",)},
        {"tp": 0, "ss": 0, "tr": False},
    ),
)
def test_non_open_normal_task_candidates_never_emit(
    payload: dict[str, object], tmp_path: Path
) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        store.apply_batch(
            _batch(
                0,
                1,
                [
                    [
                        _event("ai", 0, "Tag4", {"tt": "AI"}),
                        _event("candidate", 0, "Task7", payload),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=1,
        )
        store.apply_batch(_batch(1, 1, [], caught_up=True), observed_at=121)
        assert store.counts().candidates == 0
        assert store.counts().pending == 0
    finally:
        store.close()


def test_explicit_empty_tags_and_tag_rename_remove_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        store.apply_batch(
            _batch(
                0,
                1,
                [
                    [
                        _event("ai", 0, "Tag4", {"tt": "AI"}),
                        _event(
                            "task",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai",)},
                        ),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=1,
        )
        store.apply_batch(
            _batch(
                1,
                2,
                [
                    [
                        _event("task", 1, "Task7", {"tg": ()}),
                        _event("ai", 1, "Tag4", {"tt": "Away"}),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=2,
        )
        store.apply_batch(_batch(2, 2, [], caught_up=True), observed_at=122)
        assert store.counts().pending == 0
    finally:
        store.close()


def test_only_direct_tags_emit_and_deleted_tag_loses_eligibility(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        store.apply_batch(
            _batch(
                0,
                1,
                [
                    [
                        _event("ai-context", 0, "Tag4", {"tt": "AI"}),
                        _event("ai-direct", 0, "Tag4", {"tt": "AI"}),
                        _event("ai-deleted", 0, "Tag4", {"tt": "AI"}),
                        _event(
                            "area-context",
                            0,
                            "Area3",
                            {"tg": ("ai-context",)},
                        ),
                        _event(
                            "project-context",
                            0,
                            "Task7",
                            {
                                "tp": 1,
                                "ss": 0,
                                "tr": False,
                                "tg": ("ai-context",),
                            },
                        ),
                        _event(
                            "inherited-from-area",
                            0,
                            "Task7",
                            {
                                "tp": 0,
                                "ss": 0,
                                "tr": False,
                                "ar": "area-context",
                            },
                        ),
                        _event(
                            "inherited-from-project",
                            0,
                            "Task7",
                            {
                                "tp": 0,
                                "ss": 0,
                                "tr": False,
                                "pr": "project-context",
                            },
                        ),
                        _event(
                            "direct-sibling",
                            0,
                            "Task7",
                            {
                                "tp": 0,
                                "ss": 0,
                                "tr": False,
                                "tg": ("ai-direct",),
                            },
                        ),
                        _event(
                            "tag-deleted-before-settlement",
                            0,
                            "Task7",
                            {
                                "tp": 0,
                                "ss": 0,
                                "tr": False,
                                "tg": ("ai-deleted",),
                            },
                        ),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=1,
        )
        store.apply_batch(
            _batch(
                1,
                2,
                [[_event("ai-deleted", 2, "Tag4", {})]],
                caught_up=True,
            ),
            observed_at=2,
        )
        store.apply_batch(_batch(2, 2, [], caught_up=True), observed_at=121)

        due = store.due_events(now=121)
        assert [event.task_uuid for event in due] == ["direct-sibling"]
        counts = store.counts()
        assert (counts.ai_tags, counts.candidates, counts.pending) == (2, 0, 1)
    finally:
        store.close()


def test_reopened_restored_task_can_emit_but_permanent_delete_cannot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        store.apply_batch(
            _batch(
                0,
                1,
                [[
                    _event("ai", 0, "Tag4", {"tt": "AI"}),
                    _event("restored", 0, "Task7", {"tp": 1, "ss": 3, "tr": True, "tg": ("ai",)}),
                    _event("deleted", 0, "Task7", {"tp": 0, "ss": 0, "tr": False, "tg": ("ai",)}),
                ]],
                caught_up=True,
            ),
            observed_at=1,
        )
        store.apply_batch(
            _batch(
                1,
                2,
                [[
                    _event("restored", 1, "Task7", {"tp": 0, "ss": 0, "tr": False}),
                    _event("deleted", 2, "Task7", {}),
                ]],
                caught_up=True,
            ),
            observed_at=2,
        )
        store.apply_batch(_batch(2, 2, [], caught_up=True), observed_at=122)
        due = store.due_events(now=122)
        assert [event.task_uuid for event in due] == ["restored"]
    finally:
        store.close()


def test_crash_before_event_insert_rolls_back_cursor_and_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        store.apply_batch(
            _batch(
                0,
                1,
                [
                    [
                        _event("ai", 0, "Tag4", {"tt": "AI"}),
                        _event(
                            "task",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai",)},
                        ),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=1,
        )
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        settling = _batch(
            1,
            2,
            [[]],
            caught_up=True,
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            store.apply_batch(settling, observed_at=121)
        assert store.cursor() == 1
        assert store.counts().pending == 0

        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP TRIGGER fail_event")
        store.apply_batch(settling, observed_at=121)
        assert store.cursor() == 2
        assert store.counts().pending == 1
    finally:
        store.close()


def test_crash_after_event_insert_before_commit_rolls_back_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        store.apply_batch(
            _batch(
                0,
                1,
                [
                    [
                        _event("ai", 0, "Tag4", {"tt": "AI"}),
                        _event(
                            "task",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai",)},
                        ),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=1,
        )
        settling = _batch(1, 2, [[]], caught_up=True)
        original_settle = RoutineStore._settle

        def crash_after_insert(
            target: RoutineStore,
            connection: sqlite3.Connection,
            namespace: bytes,
            now: int,
        ) -> None:
            original_settle(target, connection, namespace, now)
            assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (
                1,
            )
            raise RuntimeError("simulated crash after insert before commit")

        with monkeypatch.context() as crash:
            crash.setattr(RoutineStore, "_settle", crash_after_insert)
            with pytest.raises(RuntimeError, match="after insert before commit"):
                store.apply_batch(settling, observed_at=121)

        assert store.cursor() == 1
        assert store.counts().pending == 0
        store.apply_batch(settling, observed_at=121)
        assert store.cursor() == 2
        assert store.counts().pending == 1
    finally:
        store.close()


def test_crash_after_event_commit_reopens_with_cursor_and_event(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
    store.apply_batch(
        _batch(
            0,
            1,
            [
                [
                    _event("ai", 0, "Tag4", {"tt": "AI"}),
                    _event(
                        "task",
                        0,
                        "Task7",
                        {"tp": 0, "ss": 0, "tr": False, "tg": ("ai",)},
                    ),
                ]
            ],
            caught_up=True,
        ),
        observed_at=1,
    )

    with pytest.raises(RuntimeError, match="after commit"):
        store.apply_batch(_batch(1, 2, [[]], caught_up=True), observed_at=121)
        raise RuntimeError("simulated crash after commit")
    store.close()

    reopened = RoutineStore(_profile(), path=store.path)
    reopened.open()
    try:
        due = reopened.due_events(now=121)
        assert reopened.cursor() == 2
        assert len(due) == 1
        expected_id = routine_event_id(NAMESPACE, _profile().routine_id, "task")
        assert due[0].event_id == expected_id
        assert due[0].body == canonical_event_body(
            event_id=expected_id,
            routine_id=_profile().routine_id,
            task_uuid="task",
            observed_at=1,
        )
    finally:
        reopened.close()


def test_history_reset_preserves_event_ledger_and_clears_projection(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        store.apply_batch(
            _batch(
                0,
                1,
                [
                    [
                        _event("ai", 0, "Tag4", {"tt": "AI"}),
                        *(
                            _event(
                                task_uuid,
                                0,
                                "Task7",
                                {
                                    "tp": 0,
                                    "ss": 0,
                                    "tr": False,
                                    "tg": ("ai",),
                                },
                            )
                            for task_uuid in (
                                "task-pending",
                                "task-delivered",
                                "task-dead",
                            )
                        ),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=1,
        )
        store.apply_batch(_batch(1, 1, [], caught_up=True), observed_at=121)
        due = {event.task_uuid: event for event in store.due_events(now=121)}
        store.record_attempt(
            due["task-delivered"].event_id,
            attempted_at=122,
            state="delivered",
            next_attempt_at=None,
            http_status=202,
            result="accepted",
        )
        store.record_attempt(
            due["task-dead"].event_id,
            attempted_at=122,
            state="dead",
            next_attempt_at=None,
            http_status=400,
            result="client_error",
        )
        store.reset_history()
        counts = store.counts()
        assert (counts.phase, counts.cursor, counts.ai_tags, counts.candidates) == (
            "uninitialized",
            0,
            0,
            0,
        )
        assert (counts.pending, counts.delivered, counts.dead) == (1, 1, 1)
    finally:
        store.close()


def test_new_fingerprint_at_old_cursor_is_a_typed_reset_signal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        replacement = HistoryBatch(b"r" * 32, 0, 0, (), True)
        with pytest.raises(RoutineHistoryIdentityChanged):
            store.apply_batch(replacement, observed_at=1)
        assert (store.counts().phase, store.cursor()) == ("live", 0)
    finally:
        store.close()


def test_regressed_head_during_seed_is_a_typed_reset_signal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(
            _batch(0, 5, [[_event("ai", 0, "Tag4", {"tt": "AI"})]], caught_up=False),
            observed_at=0,
        )
        assert (store.counts().phase, store.cursor()) == ("seeding", 1)

        with pytest.raises(RoutineHistoryIdentityChanged):
            store.apply_batch(_batch(1, 3, [[]], caught_up=False), observed_at=1)

        counts = store.counts()
        assert (counts.phase, counts.cursor, counts.ai_tags) == ("seeding", 1, 1)
    finally:
        store.close()


def test_only_one_process_owner_and_diagnostics_never_create(tmp_path: Path) -> None:
    profile = _profile()
    path = tmp_path / "routines.sqlite3"
    assert read_routine_counts(path, profile.account_digest) is None
    assert not path.exists()
    first = RoutineStore(profile, path=path, namespace_factory=lambda: NAMESPACE)
    second = RoutineStore(profile, path=path, namespace_factory=lambda: NAMESPACE)
    first.open()
    try:
        with pytest.raises(RoutineStoreAlreadyOwned):
            second.open()
    finally:
        first.close()


def test_each_store_operation_creates_uses_and_closes_its_connection_on_one_thread(
    tmp_path: Path,
) -> None:
    observations: list[tuple[int, list[int], sqlite3.Connection]] = []

    class InstrumentedStore(RoutineStore):
        def _connect(self) -> sqlite3.Connection:
            created_on = threading.get_ident()
            connection = super()._connect()
            used_on: list[int] = []
            connection.set_trace_callback(
                lambda _statement: used_on.append(threading.get_ident())
            )
            observations.append((created_on, used_on, connection))
            return connection

    store = InstrumentedStore(
        _profile(),
        path=tmp_path / "routines.sqlite3",
        namespace_factory=lambda: NAMESPACE,
    )
    store.open()
    observations.clear()
    failures: list[BaseException] = []

    def run_one_operation() -> None:
        try:
            store.counts()
            created_on, used_on, connection = observations[-1]
            assert created_on == threading.get_ident()
            assert used_on
            assert set(used_on) == {created_on}
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        except BaseException as error:
            failures.append(error)

    operation = threading.Thread(target=run_one_operation)
    operation.start()
    operation.join()
    try:
        assert failures == []
        assert len(observations) == 1
    finally:
        store.close()


def test_second_process_cannot_open_the_same_account_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "routines.sqlite3"
    ready = tmp_path / "ready"
    stop = tmp_path / "stop"
    script = """
import sys, time
from pathlib import Path
from things_orchestrator.routines_config import HermesReceiver, ReceiverSecret, RetryPolicy, RoutineProfile
from things_orchestrator.routines_store import RoutineStore
profile = RoutineProfile('a' * 64, 'always_on', HermesReceiver('https://agent.example/webhooks/task', ReceiverSecret('secret')), 60, 120, RetryPolicy())
store = RoutineStore(profile, path=Path(sys.argv[1]))
store.open()
Path(sys.argv[2]).write_text('ready')
while not Path(sys.argv[3]).exists():
    time.sleep(0.01)
store.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(ready), str(stop)]
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.is_file()
        contender = RoutineStore(_profile(), path=path)
        with pytest.raises(RoutineStoreAlreadyOwned):
            contender.open()
    finally:
        stop.write_text("stop")
        process.wait(timeout=5)


def test_namespace_is_generated_once_and_database_is_private(tmp_path: Path) -> None:
    path = tmp_path / "routines.sqlite3"
    calls = 0

    def namespace() -> bytes:
        nonlocal calls
        calls += 1
        return NAMESPACE

    first = RoutineStore(_profile(), path=path, namespace_factory=namespace)
    first.open()
    first.close()
    second = RoutineStore(_profile(), path=path, namespace_factory=namespace)
    second.open()
    second.close()

    assert calls == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_unknown_database_schema_version_fails_without_downgrade(
    tmp_path: Path,
) -> None:
    path = tmp_path / "routines.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 2")
    store = RoutineStore(_profile(), path=path)

    with pytest.raises(RoutineStoreError, match="unsupported schema version"):
        store.open()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_event_identity_and_body_have_stable_privacy_vectors() -> None:
    event_id = routine_event_id(NAMESPACE, _profile().routine_id, "task-uuid")
    body = canonical_event_body(
        event_id=event_id,
        routine_id=_profile().routine_id,
        task_uuid="task-uuid",
        observed_at=123,
    )
    assert event_id == "evt_x4Chx_6ydGGpa500qXyepanOdY-z5Ozpgbx7T1xsTd8"
    assert b"history" not in body
    assert b"secret" not in body
    assert b"title" not in body


def test_delivery_state_keeps_tombstone_or_metadata_body_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        store.apply_batch(
            _batch(
                0,
                1,
                [
                    [
                        _event("ai", 0, "Tag4", {"tt": "AI"}),
                        _event(
                            "task",
                            0,
                            "Task7",
                            {"tp": 0, "ss": 0, "tr": False, "tg": ("ai",)},
                        ),
                    ]
                ],
                caught_up=True,
            ),
            observed_at=1,
        )
        store.apply_batch(_batch(1, 1, [], caught_up=True), observed_at=121)
        event = store.due_events(now=121)[0]
        store.record_attempt(
            event.event_id,
            attempted_at=122,
            state="delivered",
            next_attempt_at=None,
            http_status=202,
            result="accepted",
        )
        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                "SELECT state, body, attempt_count, last_result FROM events"
            ).fetchone()
        assert row == ("delivered", None, 1, "accepted")
        assert store.due_events(now=999) == ()
        raw = store.path.read_bytes()
        for forbidden in (
            b"receiver-secret",
            b"agent.example",
            b"owner@example.com",
            b"cloud-secret",
            b"task title",
        ):
            assert forbidden not in raw
    finally:
        store.close()


def test_due_delivery_drain_is_hard_limited_to_25(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.apply_batch(_batch(0, 0, [], caught_up=True), observed_at=0)
        creates = [_event("ai", 0, "Tag4", {"tt": "AI"})]
        creates.extend(
            _event(
                f"task-{index}",
                0,
                "Task7",
                {"tp": 0, "ss": 0, "tr": False, "tg": ("ai",)},
            )
            for index in range(30)
        )
        store.apply_batch(_batch(0, 1, [creates], caught_up=True), observed_at=1)
        store.apply_batch(_batch(1, 1, [], caught_up=True), observed_at=121)
        assert store.counts().pending == 30
        assert len(store.due_events(now=121, limit=1000)) == 25
    finally:
        store.close()
