"""Exclusive SQLite projection and delivery ledger for routines."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from secrets import token_bytes
from typing import cast

from .cloud import HistoryBatch, HistoryEvent
from .routines_config import RoutineProfile, routines_state_dir

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    account_digest TEXT NOT NULL,
    account_event_namespace BLOB NOT NULL CHECK (length(account_event_namespace) = 32),
    history_fingerprint BLOB CHECK (history_fingerprint IS NULL OR length(history_fingerprint) = 32),
    phase TEXT NOT NULL CHECK (phase IN ('uninitialized', 'seeding', 'live')),
    baseline_head INTEGER CHECK (baseline_head IS NULL OR baseline_head >= 0),
    cursor INTEGER NOT NULL CHECK (cursor >= 0)
);
CREATE TABLE IF NOT EXISTS ai_tags (tag_uuid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS candidates (
    task_uuid TEXT PRIMARY KEY,
    creation_group INTEGER NOT NULL CHECK (creation_group >= 0),
    task_kind TEXT NOT NULL CHECK (task_kind IN ('unknown', 'task', 'project', 'heading')),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('unknown', 'open', 'done', 'dropped')),
    trashed INTEGER NOT NULL CHECK (trashed IN (0, 1)),
    first_observed_at INTEGER NOT NULL,
    last_observed_at INTEGER NOT NULL,
    settle_after INTEGER NOT NULL,
    CHECK (first_observed_at <= last_observed_at),
    CHECK (last_observed_at <= settle_after)
);
CREATE TABLE IF NOT EXISTS candidate_tags (
    task_uuid TEXT NOT NULL REFERENCES candidates(task_uuid) ON DELETE CASCADE,
    tag_uuid TEXT NOT NULL,
    PRIMARY KEY (task_uuid, tag_uuid)
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    routine_id TEXT NOT NULL,
    task_uuid TEXT NOT NULL,
    creation_group INTEGER NOT NULL CHECK (creation_group >= 0),
    observed_at INTEGER NOT NULL,
    body BLOB,
    state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'dead')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at INTEGER,
    last_attempt_at INTEGER,
    last_http_status INTEGER,
    last_result TEXT,
    terminal_at INTEGER,
    UNIQUE (routine_id, task_uuid),
    CHECK (
        (state = 'pending' AND body IS NOT NULL AND next_attempt_at IS NOT NULL AND terminal_at IS NULL)
        OR (state = 'delivered' AND body IS NULL AND next_attempt_at IS NULL AND terminal_at IS NOT NULL)
        OR (state = 'dead' AND body IS NOT NULL AND next_attempt_at IS NULL AND terminal_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS events_due ON events(next_attempt_at, event_id) WHERE state = 'pending';
"""


class RoutineStoreError(RuntimeError):
    """The routines ledger cannot safely continue."""


class RoutineStoreAlreadyOwned(RoutineStoreError):
    """Another process owns this account's routines ledger."""


class RoutineHistoryIdentityChanged(RoutineStoreError):
    """The validated batch belongs to a replacement history."""


@dataclass(frozen=True, slots=True)
class StoredEvent:
    event_id: str
    routine_id: str
    task_uuid: str
    observed_at: int
    body: bytes
    attempt_count: int


@dataclass(frozen=True, slots=True)
class StoreCounts:
    phase: str
    cursor: int
    ai_tags: int
    candidates: int
    pending: int
    delivered: int
    dead: int


def routine_database_path(account_digest: str) -> Path:
    return routines_state_dir() / f"{account_digest}.sqlite3"


class RoutineStore:
    """Owns a path and opens and closes one SQLite connection per operation."""

    def __init__(
        self,
        profile: RoutineProfile,
        *,
        path: Path | None = None,
        namespace_factory: Callable[[], bytes] = lambda: token_bytes(32),
    ) -> None:
        self.profile = profile
        self.path = path or routine_database_path(profile.account_digest)
        self.lock_path = self.path.with_suffix(".lock")
        self._namespace_factory = namespace_factory
        self._lock_fd: int | None = None

    def open(self) -> None:
        if self._lock_fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise RoutineStoreAlreadyOwned(
                "Routines are already running for this account"
            ) from error
        self._lock_fd = descriptor
        try:
            with closing(self._connect()) as connection, connection:
                version_row = connection.execute("PRAGMA user_version").fetchone()
                version = int(version_row[0]) if version_row is not None else 0
                if version not in {0, 1}:
                    raise RoutineStoreError(
                        "Routines database has an unsupported schema version"
                    )
                connection.executescript(_SCHEMA)
                row = connection.execute(
                    "SELECT account_digest, account_event_namespace FROM meta WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    namespace = self._namespace_factory()
                    if len(namespace) != 32:
                        raise RoutineStoreError(
                            "Account event namespace must be 32 bytes"
                        )
                    connection.execute(
                        "INSERT INTO meta VALUES (1, ?, ?, NULL, 'uninitialized', NULL, 0)",
                        (self.profile.account_digest, namespace),
                    )
                elif row[0] != self.profile.account_digest or len(bytes(row[1])) != 32:
                    raise RoutineStoreError(
                        "Routines database belongs to a different account"
                    )
                connection.execute("PRAGMA user_version = 1")
            os.chmod(self.path, 0o600)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        descriptor, self._lock_fd = self._lock_fd, None
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def reset_history(self) -> None:
        self._require_open()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_account(connection)
            connection.execute("DELETE FROM candidate_tags")
            connection.execute("DELETE FROM candidates")
            connection.execute("DELETE FROM ai_tags")
            connection.execute(
                "UPDATE meta SET history_fingerprint = NULL, phase = 'uninitialized', baseline_head = NULL, cursor = 0 WHERE singleton = 1"
            )
            connection.commit()

    def cursor(self) -> int:
        self._require_open()
        with closing(self._connect()) as connection, connection:
            self._verify_account(connection)
            row = connection.execute(
                "SELECT cursor FROM meta WHERE singleton = 1"
            ).fetchone()
            assert row is not None
            return int(row[0])

    def apply_batch(self, batch: HistoryBatch, *, observed_at: int) -> None:
        self._require_open()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            meta = self._meta(connection)
            _, namespace, fingerprint, phase, baseline_head, cursor = meta
            if batch.requested_start != cursor:
                raise RoutineStoreError(
                    "History batch does not start at the durable cursor"
                )
            if phase == "uninitialized":
                if cursor != 0 or batch.requested_start != 0:
                    raise RoutineStoreError("History seeding must begin at index zero")
                fingerprint = batch.history_fingerprint
                phase = "seeding"
                baseline_head = batch.current_head
            elif fingerprint != batch.history_fingerprint:
                raise RoutineHistoryIdentityChanged(
                    "Things Cloud history identity changed"
                )
            assert isinstance(baseline_head, int)
            if phase == "seeding" and batch.current_head < baseline_head:
                raise RoutineHistoryIdentityChanged(
                    "Things Cloud history head regressed during seeding"
                )
            for group in batch.groups:
                live = phase == "live" or group.index >= baseline_head
                if live and phase != "live":
                    phase = "live"
                for event in group.events:
                    if _is_tag(event.entity):
                        self._reduce_tag(connection, event)
                    elif live and _is_task(event.entity):
                        self._reduce_task(connection, event, group.index, observed_at)
            cursor += len(batch.groups)
            if phase == "seeding" and cursor >= baseline_head:
                phase = "live"
            if phase == "live" and batch.caught_up:
                self._settle(connection, namespace, observed_at)
            connection.execute(
                "UPDATE meta SET history_fingerprint = ?, phase = ?, baseline_head = ?, cursor = ? WHERE singleton = 1",
                (fingerprint, phase, baseline_head, cursor),
            )
            connection.commit()

    def due_events(self, *, now: int, limit: int = 25) -> tuple[StoredEvent, ...]:
        self._require_open()
        bounded = min(max(limit, 0), 25)
        with closing(self._connect()) as connection, connection:
            self._verify_account(connection)
            rows = connection.execute(
                "SELECT event_id, routine_id, task_uuid, observed_at, body, attempt_count FROM events WHERE state = 'pending' AND next_attempt_at <= ? ORDER BY next_attempt_at, event_id LIMIT ?",
                (now, bounded),
            ).fetchall()
        return tuple(
            StoredEvent(
                str(row[0]),
                str(row[1]),
                str(row[2]),
                int(row[3]),
                bytes(row[4]),
                int(row[5]),
            )
            for row in rows
        )

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
        self._require_open()
        if state not in {"pending", "delivered", "dead"}:
            raise RoutineStoreError("Invalid delivery state")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_account(connection)
            terminal = attempted_at if state != "pending" else None
            body_sql = "NULL" if state == "delivered" else "body"
            changed = connection.execute(
                f"UPDATE events SET state = ?, body = {body_sql}, attempt_count = attempt_count + 1, next_attempt_at = ?, last_attempt_at = ?, last_http_status = ?, last_result = ?, terminal_at = ? WHERE event_id = ? AND state = 'pending'",
                (
                    state,
                    next_attempt_at,
                    attempted_at,
                    http_status,
                    result,
                    terminal,
                    event_id,
                ),
            ).rowcount
            if changed != 1:
                raise RoutineStoreError("Pending event disappeared during delivery")
            connection.commit()

    def counts(self) -> StoreCounts:
        self._require_open()
        with closing(self._connect()) as connection, connection:
            meta = self._meta(connection)
            tag_count = _count(connection, "ai_tags")
            candidate_count = _count(connection, "candidates")
            states = dict(
                connection.execute("SELECT state, COUNT(*) FROM events GROUP BY state")
            )
        return StoreCounts(
            phase=str(meta[3]),
            cursor=int(meta[5]),
            ai_tags=tag_count,
            candidates=candidate_count,
            pending=int(states.get("pending", 0)),
            delivered=int(states.get("delivered", 0)),
            dead=int(states.get("dead", 0)),
        )

    def _reduce_tag(self, connection: sqlite3.Connection, event: HistoryEvent) -> None:
        if event.action == 2:
            connection.execute("DELETE FROM ai_tags WHERE tag_uuid = ?", (event.uuid,))
            return
        if "tt" not in event.payload and event.action == 1:
            return
        if event.payload.get("tt") == "AI":
            connection.execute(
                "INSERT OR IGNORE INTO ai_tags VALUES (?)", (event.uuid,)
            )
        else:
            connection.execute("DELETE FROM ai_tags WHERE tag_uuid = ?", (event.uuid,))

    def _reduce_task(
        self,
        connection: sqlite3.Connection,
        event: HistoryEvent,
        group_index: int,
        observed_at: int,
    ) -> None:
        if event.action == 2:
            connection.execute(
                "DELETE FROM candidates WHERE task_uuid = ?", (event.uuid,)
            )
            return
        exists = (
            connection.execute(
                "SELECT 1 FROM candidates WHERE task_uuid = ?", (event.uuid,)
            ).fetchone()
            is not None
        )
        if event.action == 0:
            connection.execute(
                "DELETE FROM candidates WHERE task_uuid = ?", (event.uuid,)
            )
            connection.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.uuid,
                    group_index,
                    _task_kind(event.payload.get("tp")),
                    _lifecycle(event.payload.get("ss")),
                    bool(event.payload.get("tr", False)),
                    observed_at,
                    observed_at,
                    observed_at + self.profile.settle_seconds,
                ),
            )
            exists = True
        elif not exists:
            return
        else:
            assignments = ["last_observed_at = ?", "settle_after = ?"]
            values: list[object] = [
                observed_at,
                observed_at + self.profile.settle_seconds,
            ]
            if "tp" in event.payload:
                assignments.append("task_kind = ?")
                values.append(_task_kind(event.payload["tp"]))
            if "ss" in event.payload:
                assignments.append("lifecycle = ?")
                values.append(_lifecycle(event.payload["ss"]))
            if "tr" in event.payload:
                assignments.append("trashed = ?")
                values.append(bool(event.payload["tr"]))
            values.append(event.uuid)
            connection.execute(
                f"UPDATE candidates SET {', '.join(assignments)} WHERE task_uuid = ?",
                values,
            )
        if exists and "tg" in event.payload:
            connection.execute(
                "DELETE FROM candidate_tags WHERE task_uuid = ?", (event.uuid,)
            )
            connection.executemany(
                "INSERT INTO candidate_tags VALUES (?, ?)",
                (
                    (event.uuid, tag)
                    for tag in cast(tuple[str, ...], event.payload["tg"])
                ),
            )

    def _settle(
        self, connection: sqlite3.Connection, namespace: bytes, now: int
    ) -> None:
        rows = connection.execute(
            "SELECT task_uuid, creation_group, first_observed_at, task_kind, lifecycle, trashed FROM candidates WHERE settle_after <= ? ORDER BY task_uuid",
            (now,),
        ).fetchall()
        for task_uuid, creation_group, observed_at, kind, lifecycle, trashed in rows:
            matches = (
                connection.execute(
                    "SELECT 1 FROM candidate_tags ct JOIN ai_tags ai ON ai.tag_uuid = ct.tag_uuid WHERE ct.task_uuid = ? LIMIT 1",
                    (task_uuid,),
                ).fetchone()
                is not None
            )
            if kind == "task" and lifecycle == "open" and not trashed and matches:
                event_id = routine_event_id(
                    namespace, self.profile.routine_id, str(task_uuid)
                )
                body = canonical_event_body(
                    event_id=event_id,
                    routine_id=self.profile.routine_id,
                    task_uuid=str(task_uuid),
                    observed_at=int(observed_at),
                )
                existing = connection.execute(
                    "SELECT event_id FROM events WHERE routine_id = ? AND task_uuid = ?",
                    (self.profile.routine_id, task_uuid),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO events (event_id, routine_id, task_uuid, creation_group, observed_at, body, state, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (
                            event_id,
                            self.profile.routine_id,
                            task_uuid,
                            creation_group,
                            observed_at,
                            body,
                            now,
                        ),
                    )
                elif existing[0] != event_id:
                    raise RoutineStoreError("Conflicting logical event identity")
            connection.execute(
                "DELETE FROM candidates WHERE task_uuid = ?", (task_uuid,)
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _meta(
        self, connection: sqlite3.Connection
    ) -> tuple[str, bytes, bytes | None, str, int | None, int]:
        self._verify_account(connection)
        row = connection.execute(
            "SELECT account_digest, account_event_namespace, history_fingerprint, phase, baseline_head, cursor FROM meta WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RoutineStoreError("Routines database metadata is missing")
        return (
            str(row[0]),
            bytes(row[1]),
            None if row[2] is None else bytes(row[2]),
            str(row[3]),
            None if row[4] is None else int(row[4]),
            int(row[5]),
        )

    def _verify_account(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT account_digest FROM meta WHERE singleton = 1"
        ).fetchone()
        if row is None or row[0] != self.profile.account_digest:
            raise RoutineStoreError("Routines database belongs to a different account")

    def _require_open(self) -> None:
        if self._lock_fd is None:
            raise RoutineStoreError("Routines store is not open")


def routine_event_id(namespace: bytes, routine_id: str, task_uuid: str) -> str:
    digest = hashlib.sha256(
        b"things-orchestrator/routine-event/v1\0"
        + namespace
        + b"\0"
        + routine_id.encode("utf-8")
        + b"\0"
        + f"task:{task_uuid}".encode("utf-8")
    ).digest()
    return "evt_" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def canonical_event_body(
    *, event_id: str, routine_id: str, task_uuid: str, observed_at: int
) -> bytes:
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": "task.created",
            "observed_at": observed_at,
            "routine_id": routine_id,
            "schema_version": 1,
            "task_id": f"task:{task_uuid}",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def read_routine_counts(path: Path, account_digest: str) -> StoreCounts | None:
    if not path.is_file():
        return None
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection, connection:
            row = connection.execute(
                "SELECT phase, cursor, account_digest FROM meta WHERE singleton = 1"
            ).fetchone()
            if row is None or row[2] != account_digest:
                return None
            states = dict(
                connection.execute("SELECT state, COUNT(*) FROM events GROUP BY state")
            )
            return StoreCounts(
                phase=str(row[0]),
                cursor=int(row[1]),
                ai_tags=_count(connection, "ai_tags"),
                candidates=_count(connection, "candidates"),
                pending=int(states.get("pending", 0)),
                delivered=int(states.get("delivered", 0)),
                dead=int(states.get("dead", 0)),
            )
    except sqlite3.Error:
        return None


def _task_kind(value: object) -> str:
    return (
        "task"
        if value == 0
        else "project"
        if value == 1
        else "heading"
        if value == 2
        else "unknown"
    )


def _lifecycle(value: object) -> str:
    return (
        "open"
        if value == 0
        else "done"
        if value == 3
        else "dropped"
        if value == 2
        else "unknown"
    )


def _is_task(entity: str) -> bool:
    return entity in {"Task", "Task3", "Task4", "Task6", "Task7"}


def _is_tag(entity: str) -> bool:
    return entity in {"Tag", "Tag3", "Tag4"}


def _count(connection: sqlite3.Connection, table: str) -> int:
    allowed = {"ai_tags", "candidates"}
    if table not in allowed:
        raise RoutineStoreError("Unsupported count table")
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row is not None else 0
