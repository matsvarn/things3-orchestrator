"""Durable local journal for idempotent Things intents and approvals."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol, cast, overload

IntentState = Literal[
    "prepared",
    "needs_approval",
    "pending",
    "applied",
    "unchanged",
    "stale",
]
JsonDict = dict[str, object]


@dataclass(frozen=True, slots=True)
class IntentRecord:
    """One owner intent. Plan data must not contain credentials or Cloud payloads."""

    intent_id: str
    fingerprint: str
    state: IntentState
    plan: JsonDict = field(default_factory=dict)
    plan_id: str | None = None
    expires_at: str | None = None
    result: JsonDict | None = None


class Journal(Protocol):
    """Persistence seam used by the workspace."""

    def get(self, intent_id: str) -> IntentRecord | None: ...

    def get_by_plan_id(self, plan_id: str) -> IntentRecord | None: ...

    def save(self, record: IntentRecord) -> None: ...

    def reserve(self, record: IntentRecord) -> IntentRecord: ...

    def transition(self, record: IntentRecord, *, expected: IntentState) -> bool: ...


class MemoryJournal:
    """In-process adapter for deterministic tests."""

    def __init__(self) -> None:
        self._records: dict[str, IntentRecord] = {}
        self._lock = RLock()

    def get(self, intent_id: str) -> IntentRecord | None:
        with self._lock:
            return _copy(self._records.get(intent_id))

    def get_by_plan_id(self, plan_id: str) -> IntentRecord | None:
        with self._lock:
            return next(
                (
                    _copy(record)
                    for record in self._records.values()
                    if record.plan_id == plan_id
                ),
                None,
            )

    def save(self, record: IntentRecord) -> None:
        with self._lock:
            copied = _copy(record)
            if copied.plan_id is not None and any(
                other.intent_id != copied.intent_id and other.plan_id == copied.plan_id
                for other in self._records.values()
            ):
                raise ValueError(f"plan ID already exists: {copied.plan_id}")
            self._records[copied.intent_id] = copied

    def reserve(self, record: IntentRecord) -> IntentRecord:
        with self._lock:
            existing = self._records.get(record.intent_id)
            if existing is not None:
                return _copy(existing)
            self.save(record)
            return _copy(record)

    def transition(self, record: IntentRecord, *, expected: IntentState) -> bool:
        with self._lock:
            current = self._records.get(record.intent_id)
            if (
                current is None
                or current.state != expected
                or current.fingerprint != record.fingerprint
            ):
                return False
            self.save(record)
            return True


class SQLiteJournal:
    """SQLite adapter for process-safe, transactional persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else journal_path()
        _ensure_private_dir(self.path.parent)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'prepared', 'needs_approval', 'pending',
                        'applied', 'unchanged', 'stale'
                    )),
                    plan_json TEXT NOT NULL,
                    plan_id TEXT UNIQUE,
                    expires_at TEXT,
                    result_json TEXT
                )
                """
            )
        self.path.chmod(0o600)

    def get(self, intent_id: str) -> IntentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return _from_row(row)

    def get_by_plan_id(self, plan_id: str) -> IntentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM intents WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return _from_row(row)

    def save(self, record: IntentRecord) -> None:
        plan_json = _json(record.plan)
        result_json = _json(record.result) if record.result is not None else None
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO intents (
                        intent_id, fingerprint, state, plan_json,
                        plan_id, expires_at, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(intent_id) DO UPDATE SET
                        fingerprint = excluded.fingerprint,
                        state = excluded.state,
                        plan_json = excluded.plan_json,
                        plan_id = excluded.plan_id,
                        expires_at = excluded.expires_at,
                        result_json = excluded.result_json
                    """,
                    (
                        record.intent_id,
                        record.fingerprint,
                        record.state,
                        plan_json,
                        record.plan_id,
                        record.expires_at,
                        result_json,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if record.plan_id is not None:
                raise ValueError(f"plan ID already exists: {record.plan_id}") from error
            raise

    def reserve(self, record: IntentRecord) -> IntentRecord:
        plan_json = _json(record.plan)
        result_json = _json(record.result) if record.result is not None else None
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO intents (
                        intent_id, fingerprint, state, plan_json,
                        plan_id, expires_at, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.intent_id,
                        record.fingerprint,
                        record.state,
                        plan_json,
                        record.plan_id,
                        record.expires_at,
                        result_json,
                    ),
                )
        except sqlite3.IntegrityError:
            existing = self.get(record.intent_id)
            if existing is None:
                raise
            return existing
        return record

    def transition(self, record: IntentRecord, *, expected: IntentState) -> bool:
        plan_json = _json(record.plan)
        result_json = _json(record.result) if record.result is not None else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE intents SET
                    fingerprint = ?, state = ?, plan_json = ?, plan_id = ?,
                    expires_at = ?, result_json = ?
                WHERE intent_id = ? AND state = ? AND fingerprint = ?
                """,
                (
                    record.fingerprint,
                    record.state,
                    plan_json,
                    record.plan_id,
                    record.expires_at,
                    result_json,
                    record.intent_id,
                    expected,
                    record.fingerprint,
                ),
            )
            return cursor.rowcount == 1

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


def journal_path(account: str | None = None) -> Path:
    """Return the journal path beside the existing XDG state cache."""

    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    name = "journal.sqlite3"
    if account:
        digest = sha256(account.strip().casefold().encode()).hexdigest()[:16]
        name = f"journal-{digest}.sqlite3"
    return base / "things-orchestrator" / name


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _json(value: JsonDict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@overload
def _copy(record: IntentRecord) -> IntentRecord: ...


@overload
def _copy(record: None) -> None: ...


def _copy(record: IntentRecord | None) -> IntentRecord | None:
    if record is None:
        return None
    return IntentRecord(
        intent_id=record.intent_id,
        fingerprint=record.fingerprint,
        state=record.state,
        plan=json.loads(_json(record.plan)),
        plan_id=record.plan_id,
        expires_at=record.expires_at,
        result=json.loads(_json(record.result)) if record.result is not None else None,
    )


def _from_row(row: sqlite3.Row | None) -> IntentRecord | None:
    if row is None:
        return None
    return IntentRecord(
        intent_id=str(row["intent_id"]),
        fingerprint=str(row["fingerprint"]),
        state=cast(IntentState, row["state"]),
        plan=cast(JsonDict, json.loads(row["plan_json"])),
        plan_id=cast(str | None, row["plan_id"]),
        expires_at=cast(str | None, row["expires_at"]),
        result=(
            cast(JsonDict, json.loads(row["result_json"]))
            if row["result_json"] is not None
            else None
        ),
    )
