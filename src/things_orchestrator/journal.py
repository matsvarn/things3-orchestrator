"""Durable local journal for idempotent Things intents and approvals."""

from __future__ import annotations

import base64
import hmac
import json
import os
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes
from threading import RLock
from typing import Literal, Protocol, cast, overload

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

IntentState = Literal[
    "prepared",
    "needs_approval",
    "pending",
    "applied",
    "unchanged",
    "stale",
]
JsonDict = dict[str, object]
V2State = Literal[
    "awaiting_owner",
    "pending",
    "applied",
    "unchanged",
    "not_applied",
    "partial",
    "partial_resolved",
    "stale",
    "declined",
    "rejected",
]


@dataclass(frozen=True, slots=True)
class V2Operation:
    account_id: str
    api_version: str
    request_id: str
    request_hash: str
    operation_id: str
    tool: str
    state: V2State
    manifest: JsonDict
    manifest_hash: str
    safety_policy_digest: str
    expires_at: str | None = None
    response: JsonDict | None = None
    authorization: str | None = None
    resolution: Literal["accepted_as_is", "superseded"] | None = None
    receipt_hash: str | None = None


@dataclass(frozen=True, slots=True)
class V2ReceiptPage:
    rows: list[JsonDict]
    cursor: str | None
    receipt_hash: str


@dataclass(frozen=True, slots=True)
class OwnerAuthorization:
    """Signed host authorization; its private signing key is never server-loaded."""

    binding_json: str
    signature: str

    @property
    def record(self) -> str:
        digest = sha256(self.binding_json.encode()).hexdigest()
        return f"ed25519:v1:{digest}:{self.signature}"


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

    def get_v2_request(self, account_id: str, api_version: str, request_id: str) -> V2Operation | None: ...
    def get_v2_operation(self, operation_id: str) -> V2Operation | None: ...
    def blocking_v2_operations(self, account_id: str) -> list[str]: ...
    def create_v2(self, operation: V2Operation, *, claim_fence: bool, receipt_rows: list[JsonDict] | None = None) -> tuple[Literal["created", "existing", "conflict", "blocked"], V2Operation | None, list[str]]: ...
    def transition_v2(self, operation_id: str, *, expected: V2State, state: V2State, response: JsonDict | None = None, authorization: object = None, resolution: Literal["accepted_as_is", "superseded"] | None = None) -> bool: ...
    def authorize_v2(self, operation_id: str, authorization: object) -> tuple[bool, list[str]]: ...
    def append_v2_receipts(self, operation_id: str, rows: list[JsonDict]) -> str: ...
    def settle_v2(self, operation_id: str, *, expected: V2State, state: V2State, response: JsonDict, rows: list[JsonDict], authorization: object = None, action: str | None = None) -> bool: ...
    def v2_receipt_page(self, account_id: str, operation_id: str, *, limit: int, cursor: str | None = None) -> V2ReceiptPage: ...
    def prune_v2(self, *, now: str, retention_days: int = 7) -> int: ...
    def cutover_v1(self) -> JsonDict: ...
    def annotate_v1_pending(self, intent_id: str, *, result: JsonDict) -> bool: ...
    def resolve_v1_pending(self, intent_id: str, *, expected_fingerprint: str, expected_plan_digest: str, state: Literal["applied", "stale"], result: JsonDict) -> bool: ...
    def verify_v2_authorization(self, operation: V2Operation, action: str, authorization: object) -> str | None: ...


class MemoryJournal:
    """In-process adapter for deterministic tests."""

    def __init__(self, *, owner_public_key: bytes | None = None) -> None:
        self._records: dict[str, IntentRecord] = {}
        self._v2_operations: dict[str, V2Operation] = {}
        self._v2_receipts: dict[str, list[JsonDict]] = {}
        self._v2_times: dict[str, tuple[str, str | None]] = {}
        self._v2_tombstones: dict[str, V2Operation] = {}
        self._cursor_key = token_bytes(32)
        self._owner_public_key = owner_public_key
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

    def get_v2_request(
        self, account_id: str, api_version: str, request_id: str
    ) -> V2Operation | None:
        with self._lock:
            active = next(
                (
                    _copy_v2(row)
                    for row in self._v2_operations.values()
                    if (row.account_id, row.api_version, row.request_id)
                    == (account_id, api_version, request_id)
                ),
                None,
            )
            if active is not None:
                return active
            return _copy_v2(
                self._v2_tombstones.get(
                    _v2_request_key(account_id, api_version, request_id)
                )
            )

    def get_v2_operation(self, operation_id: str) -> V2Operation | None:
        with self._lock:
            return _copy_v2(self._v2_operations.get(operation_id))

    def blocking_v2_operations(self, account_id: str) -> list[str]:
        with self._lock:
            current = [
                row.operation_id
                for row in self._v2_operations.values()
                if row.account_id == account_id and row.state in {"pending", "partial"}
            ]
            legacy = [
                row.intent_id
                for row in self._records.values()
                if row.state == "pending"
            ]
            return sorted({*current, *legacy})

    def create_v2(
        self, operation: V2Operation, *, claim_fence: bool, receipt_rows: list[JsonDict] | None = None
    ) -> tuple[Literal["created", "existing", "conflict", "blocked"], V2Operation | None, list[str]]:
        with self._lock:
            if not v2_manifest_is_valid(operation):
                raise ValueError("v2 manifest hash does not match its persisted content")
            if operation.state not in {"pending", "awaiting_owner", "unchanged"}:
                raise ValueError("v2 operation must start pending, awaiting_owner, or unchanged")
            if operation.state != "unchanged" and receipt_rows:
                raise ValueError("nonterminal creation cannot preseed receipt rows")
            normalized = _validate_v2_operation_receipts(operation, receipt_rows or []) if operation.state == "unchanged" else []
            existing = self.get_v2_request(
                operation.account_id, operation.api_version, operation.request_id
            )
            if existing is not None:
                outcome: Literal["existing", "conflict"] = "existing" if existing.request_hash == operation.request_hash else "conflict"
                return outcome, existing, []
            blockers = self.blocking_v2_operations(operation.account_id)
            if blockers:
                return "blocked", None, blockers
            if claim_fence and operation.state != "pending":
                raise ValueError("routine operation creation must enter pending")
            copied = _copy_v2(operation)
            assert copied is not None
            if normalized:
                copied = replace(copied, receipt_hash=_v2_receipt_hash(normalized))
            self._v2_operations[operation.operation_id] = copied
            if normalized:
                self._v2_receipts[operation.operation_id] = normalized
            created_at = _utc_now()
            self._v2_times[operation.operation_id] = (
                created_at,
                None if operation.state in {"awaiting_owner", "pending", "partial"} else created_at,
            )
            return "created", _copy_v2(copied), []

    def settle_v2(
        self,
        operation_id: str,
        *,
        expected: V2State,
        state: V2State,
        response: JsonDict,
        rows: list[JsonDict],
        authorization: object = None,
        action: str | None = None,
    ) -> bool:
        if expected != "pending" or state not in {"applied", "not_applied", "partial"}:
            return False
        with self._lock:
            current = self._v2_operations.get(operation_id)
            if current is None or current.state != expected or not _legal_v2_transition(expected, state):
                return False
            normalized = _validate_v2_operation_receipts(current, rows)
            authorization_record = current.authorization
            if action is not None:
                authorization_record = self.verify_v2_authorization(current, action, authorization)
                if authorization_record is None:
                    return False
            digest = _v2_receipt_hash(normalized)
            self._v2_receipts[operation_id] = normalized
            self._v2_operations[operation_id] = replace(
                current, state=state, response=_copy_json(response), receipt_hash=digest,
                authorization=authorization_record,
            )
            created, _ = self._v2_times.get(operation_id, (_utc_now(), None))
            self._v2_times[operation_id] = (
                created,
                None if state in {"awaiting_owner", "pending", "partial"} else _utc_now(),
            )
            return True

    def transition_v2(
        self,
        operation_id: str,
        *,
        expected: V2State,
        state: V2State,
        response: JsonDict | None = None,
        authorization: object = None,
        resolution: Literal["accepted_as_is", "superseded"] | None = None,
    ) -> bool:
        with self._lock:
            if expected == "pending" or (expected == "awaiting_owner" and state == "pending"):
                return False
            current = self._v2_operations.get(operation_id)
            if current is None or current.state != expected or not _legal_v2_transition(expected, state):
                return False
            authorization_record = current.authorization
            if state in {"pending", "declined", "partial_resolved"}:
                action = (
                    "approve"
                    if state == "pending"
                    else "decline"
                    if state == "declined"
                    else cast(str, resolution)
                )
                authorization_record = self.verify_v2_authorization(
                    current, action, authorization
                )
                if authorization_record is None:
                    return False
            self._v2_operations[operation_id] = replace(
                current,
                state=state,
                response=_copy_json(response),
                authorization=authorization_record,
                resolution=resolution,
            )
            if state not in {"awaiting_owner", "pending", "partial"}:
                created, _settled = self._v2_times.get(operation_id, (_utc_now(), None))
                self._v2_times[operation_id] = (created, _utc_now())
            return True

    def authorize_v2(self, operation_id: str, authorization: object) -> tuple[bool, list[str]]:
        with self._lock:
            current = self._v2_operations.get(operation_id)
            if (
                current is None
                or current.state != "awaiting_owner"
                or self.verify_v2_authorization(current, "approve", authorization) is None
            ):
                return False, []
            blockers = self.blocking_v2_operations(current.account_id)
            if blockers:
                return False, blockers
            self._v2_operations[operation_id] = replace(
                current,
                state="pending",
                authorization=cast(OwnerAuthorization, authorization).record,
            )
            return True, []

    def append_v2_receipts(self, operation_id: str, rows: list[JsonDict]) -> str:
        with self._lock:
            normalized = _validate_v2_receipts(rows)
            existing = self._v2_receipts.get(operation_id)
            if existing is not None and existing != normalized:
                raise ValueError("receipt rows are append-only and immutable")
            self._v2_receipts[operation_id] = normalized
            digest = _v2_receipt_hash(normalized)
            operation = self._v2_operations.get(operation_id)
            if operation is not None:
                self._v2_operations[operation_id] = replace(operation, receipt_hash=digest)
            return digest

    def v2_receipt_page(
        self,
        account_id: str,
        operation_id: str,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> V2ReceiptPage:
        with self._lock:
            operation = self._v2_operations.get(operation_id)
            if operation is None or operation.account_id != account_id:
                raise KeyError(operation_id)
            return _v2_page(
                account_id,
                operation_id,
                self._v2_receipts.get(operation_id, []),
                limit=limit,
                cursor=cursor,
                cursor_key=self._cursor_key,
            )

    def install_v2_test_fence(self, *, account_id: str, operation_id: str) -> None:
        operation = V2Operation(
            account_id=account_id,
            api_version="2",
            request_id="00000000-0000-4000-8000-000000000000",
            request_hash="sha256:test",
            operation_id=operation_id,
            tool="test",
            state="pending",
            manifest={},
            manifest_hash="sha256:test",
            safety_policy_digest="sha256:test",
        )
        self._v2_operations[operation_id] = operation

    def prune_v2(self, *, now: str, retention_days: int = 7) -> int:
        threshold = datetime.fromisoformat(now) - timedelta(days=retention_days)
        count = 0
        with self._lock:
            for operation_id, operation in list(self._v2_operations.items()):
                if operation.state == "awaiting_owner" and operation.expires_at and datetime.fromisoformat(operation.expires_at) <= datetime.fromisoformat(now):
                    self.transition_v2(operation_id, expected="awaiting_owner", state="stale", response={"state": "stale", "instruction": "The approval window expired.", "operation_id": operation_id})
                    operation = self._v2_operations[operation_id]
                _created, settled = self._v2_times.get(operation_id, (_utc_now(), None))
                if operation.state in {"awaiting_owner", "pending", "partial"} or settled is None or datetime.fromisoformat(settled) >= threshold:
                    continue
                key = _v2_request_key(operation.account_id, operation.api_version, operation.request_id)
                self._v2_tombstones[key] = replace(operation, request_id=key, manifest={}, response={"state": operation.state, "instruction": "This operation is retained as a content-minimized tombstone.", "operation_id": operation.operation_id})
                self._v2_operations.pop(operation_id)
                self._v2_receipts.pop(operation_id, None)
                self._v2_times.pop(operation_id, None)
                count += 1
        return count

    def cutover_v1(self) -> JsonDict:
        with self._lock:
            quarantined: list[str] = []
            unresolved: list[str] = []
            partial_like: list[str] = []
            terminal: list[str] = []
            for intent_id, record in list(self._records.items()):
                if record.state in {"prepared", "needs_approval"}:
                    quarantined.append(intent_id)
                    self._records[intent_id] = replace(
                        record,
                        state="stale",
                        plan={},
                        result={
                            "status": "quarantined",
                            "instruction": "This v1 operation cannot be approved or replayed after the v2 cutover.",
                        },
                    )
                elif record.state == "pending":
                    unresolved.append(intent_id)
                    if record.result is not None and record.result.get("status") == "partial":
                        partial_like.append(intent_id)
                else:
                    terminal.append(intent_id)
                    self._records[intent_id] = replace(
                        record,
                        plan={},
                        result=_legacy_terminal_result(record.state, record.result),
                    )
            return _legacy_report(quarantined, unresolved, terminal, partial_like)

    def annotate_v1_pending(self, intent_id: str, *, result: JsonDict) -> bool:
        with self._lock:
            current = self._records.get(intent_id)
            if current is None or current.state != "pending":
                return False
            self._records[intent_id] = replace(current, result=_copy_json(result))
            return True

    def resolve_v1_pending(self, intent_id: str, *, expected_fingerprint: str, expected_plan_digest: str, state: Literal["applied", "stale"], result: JsonDict) -> bool:
        with self._lock:
            current = self._records.get(intent_id)
            if (
                current is None or current.state != "pending"
                or current.fingerprint != expected_fingerprint
                or _legacy_plan_digest(current.plan) != expected_plan_digest
            ):
                return False
            self._records[intent_id] = replace(current, state=state, plan={}, result=_copy_json(result))
            return True

    def verify_v2_authorization(
        self, operation: V2Operation, action: str, authorization: object
    ) -> str | None:
        return _verify_owner_authorization(
            operation, action, authorization, self._owner_public_key
        )


class SQLiteJournal:
    """SQLite adapter for process-safe, transactional persistence."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        owner_public_key: bytes | None = None,
    ) -> None:
        self.path = path if path is not None else journal_path()
        if owner_public_key is None:
            try:
                owner_public_key = owner_public_key_path().read_bytes()
            except OSError:
                owner_public_key = None
        self._owner_public_key = owner_public_key
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner_operations_v2 (
                    account_id TEXT NOT NULL,
                    api_version TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    operation_id TEXT PRIMARY KEY,
                    tool TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'awaiting_owner','pending','applied','unchanged',
                        'not_applied','partial','partial_resolved','stale',
                        'declined','rejected'
                    )),
                    manifest_json TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    safety_policy_digest TEXT NOT NULL,
                    expires_at TEXT,
                    response_json TEXT,
                    authorization TEXT,
                    resolution TEXT CHECK (resolution IS NULL OR resolution IN (
                        'accepted_as_is','superseded'
                    )),
                    receipt_hash TEXT,
                    UNIQUE(account_id, api_version, request_id)
                );
                CREATE INDEX IF NOT EXISTS owner_operation_fence_v2
                    ON owner_operations_v2(account_id, state);
                CREATE TABLE IF NOT EXISTS owner_receipts_v2 (
                    operation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    row_json TEXT NOT NULL,
                    PRIMARY KEY(operation_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS owner_operation_times_v2 (
                    operation_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    settled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS owner_tombstones_v2 (
                    request_key_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    api_version TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    final_state TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    safety_policy_digest TEXT NOT NULL,
                    receipt_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS owner_journal_secrets_v2 (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    cursor_key BLOB NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO owner_journal_secrets_v2 VALUES (1,?)",
                (token_bytes(32),),
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

    def get_v2_request(
        self, account_id: str, api_version: str, request_id: str
    ) -> V2Operation | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM owner_operations_v2
                   WHERE account_id=? AND api_version=? AND request_id=?""",
                (account_id, api_version, request_id),
            ).fetchone()
            if row is None:
                tombstone = connection.execute(
                    "SELECT * FROM owner_tombstones_v2 WHERE request_key_hash=?",
                    (_v2_request_key(account_id, api_version, request_id),),
                ).fetchone()
                if tombstone is not None:
                    return _v2_from_tombstone(tombstone, request_id)
        return _v2_from_row(row)

    def get_v2_operation(self, operation_id: str) -> V2Operation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM owner_operations_v2 WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return _v2_from_row(row)

    def blocking_v2_operations(self, account_id: str) -> list[str]:
        with self._connect() as connection:
            current = connection.execute(
                """SELECT operation_id FROM owner_operations_v2
                   WHERE account_id=? AND state IN ('pending','partial')""",
                (account_id,),
            ).fetchall()
            legacy = connection.execute(
                "SELECT intent_id FROM intents WHERE state='pending'"
            ).fetchall()
        return sorted(
            {
                *[str(row["operation_id"]) for row in current],
                *[str(row["intent_id"]) for row in legacy],
            }
        )

    def create_v2(
        self, operation: V2Operation, *, claim_fence: bool, receipt_rows: list[JsonDict] | None = None
    ) -> tuple[Literal["created", "existing", "conflict", "blocked"], V2Operation | None, list[str]]:
        if not v2_manifest_is_valid(operation):
            raise ValueError("v2 manifest hash does not match its persisted content")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if operation.state not in {"pending", "awaiting_owner", "unchanged"}:
                connection.rollback()
                raise ValueError("v2 operation must start pending, awaiting_owner, or unchanged")
            if operation.state != "unchanged" and receipt_rows:
                connection.rollback()
                raise ValueError("nonterminal creation cannot preseed receipt rows")
            normalized = _validate_v2_operation_receipts(operation, receipt_rows or []) if operation.state == "unchanged" else []
            existing = connection.execute(
                """SELECT * FROM owner_operations_v2
                   WHERE account_id=? AND api_version=? AND request_id=?""",
                (operation.account_id, operation.api_version, operation.request_id),
            ).fetchone()
            if existing is not None:
                found = _v2_from_row(existing)
                assert found is not None
                connection.commit()
                outcome: Literal["existing", "conflict"] = "existing" if found.request_hash == operation.request_hash else "conflict"
                return outcome, found, []
            tombstone = connection.execute(
                "SELECT * FROM owner_tombstones_v2 WHERE request_key_hash=?",
                (
                    _v2_request_key(
                        operation.account_id,
                        operation.api_version,
                        operation.request_id,
                    ),
                ),
            ).fetchone()
            if tombstone is not None:
                found = _v2_from_tombstone(tombstone, operation.request_id)
                connection.commit()
                outcome = (
                    "existing"
                    if found.request_hash == operation.request_hash
                    else "conflict"
                )
                return outcome, found, []
            blockers = _sqlite_blockers(connection, operation.account_id)
            if blockers:
                connection.rollback()
                return "blocked", None, blockers
            if claim_fence and operation.state != "pending":
                connection.rollback()
                raise ValueError("routine operation creation must enter pending")
            connection.execute(
                """INSERT INTO owner_operations_v2 VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""",
                _v2_sql_values(operation),
            )
            stored_operation = operation
            if normalized:
                digest = _insert_v2_receipts(connection, operation.operation_id, normalized)
                connection.execute(
                    "UPDATE owner_operations_v2 SET receipt_hash=? WHERE operation_id=?",
                    (digest, operation.operation_id),
                )
                stored_operation = replace(operation, receipt_hash=digest)
            created_at = _utc_now()
            settled_at = (
                None
                if operation.state in {"awaiting_owner", "pending", "partial"}
                else created_at
            )
            connection.execute(
                "INSERT INTO owner_operation_times_v2 VALUES (?,?,?)",
                (operation.operation_id, created_at, settled_at),
            )
            connection.commit()
            return "created", stored_operation, []
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def transition_v2(
        self,
        operation_id: str,
        *,
        expected: V2State,
        state: V2State,
        response: JsonDict | None = None,
        authorization: object = None,
        resolution: Literal["accepted_as_is", "superseded"] | None = None,
    ) -> bool:
        if expected == "pending" or (expected == "awaiting_owner" and state == "pending"):
            return False
        if not _legal_v2_transition(expected, state):
            return False
        with self._connect() as connection:
            authorization_record: str | None = None
            if state in {"pending", "declined", "partial_resolved"}:
                row = connection.execute(
                    "SELECT * FROM owner_operations_v2 WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                operation = _v2_from_row(row)
                if operation is None:
                    return False
                action = (
                    "approve"
                    if state == "pending"
                    else "decline"
                    if state == "declined"
                    else cast(str, resolution)
                )
                authorization_record = self.verify_v2_authorization(
                    operation, action, authorization
                )
                if authorization_record is None:
                    return False
            cursor = connection.execute(
                """UPDATE owner_operations_v2 SET state=?, response_json=?,
                   authorization=COALESCE(?, authorization), resolution=?
                   WHERE operation_id=? AND state=?""",
                (
                    state,
                    _json(response) if response is not None else None,
                    authorization_record,
                    resolution,
                    operation_id,
                    expected,
                ),
            )
            changed = cursor.rowcount == 1
            if changed and state not in {"awaiting_owner", "pending", "partial"}:
                connection.execute(
                    "UPDATE owner_operation_times_v2 SET settled_at=? WHERE operation_id=?",
                    (_utc_now(), operation_id),
                )
            return changed

    def authorize_v2(self, operation_id: str, authorization: object) -> tuple[bool, list[str]]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM owner_operations_v2 WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            operation = _v2_from_row(row)
            if (
                operation is None
                or operation.state != "awaiting_owner"
                or self.verify_v2_authorization(operation, "approve", authorization) is None
            ):
                connection.rollback()
                return False, []
            blockers = _sqlite_blockers(connection, operation.account_id)
            if blockers:
                connection.rollback()
                return False, blockers
            changed = connection.execute(
                """UPDATE owner_operations_v2
                   SET state='pending', authorization=?
                   WHERE operation_id=? AND state='awaiting_owner'""",
                (cast(OwnerAuthorization, authorization).record, operation_id),
            ).rowcount
            connection.commit()
            return changed == 1, []
        finally:
            connection.close()

    def append_v2_receipts(self, operation_id: str, rows: list[JsonDict]) -> str:
        normalized = _validate_v2_receipts(rows)
        digest = _v2_receipt_hash(normalized)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT row_json FROM owner_receipts_v2 WHERE operation_id=? ORDER BY sequence",
                (operation_id,),
            ).fetchall()
            current = [cast(JsonDict, json.loads(row["row_json"])) for row in existing]
            if current and current != normalized:
                raise ValueError("receipt rows are append-only and immutable")
            for row in normalized:
                connection.execute(
                    "INSERT OR IGNORE INTO owner_receipts_v2 VALUES (?,?,?)",
                    (operation_id, int(cast(int, row["sequence"])), _json(row)),
                )
            connection.execute(
                "UPDATE owner_operations_v2 SET receipt_hash=? WHERE operation_id=?",
                (digest, operation_id),
            )
        return digest

    def settle_v2(
        self,
        operation_id: str,
        *,
        expected: V2State,
        state: V2State,
        response: JsonDict,
        rows: list[JsonDict],
        authorization: object = None,
        action: str | None = None,
    ) -> bool:
        if (
            expected != "pending"
            or state not in {"applied", "not_applied", "partial"}
            or not _legal_v2_transition(expected, state)
        ):
            return False
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM owner_operations_v2 WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if current is None or current["state"] != expected:
                connection.rollback()
                return False
            current_operation = _v2_from_row(current)
            assert current_operation is not None
            normalized = _validate_v2_operation_receipts(current_operation, rows)
            authorization_record: str | None = None
            if action is not None:
                authorization_record = self.verify_v2_authorization(current_operation, action, authorization)
                if authorization_record is None:
                    connection.rollback()
                    return False
            digest = _insert_v2_receipts(connection, operation_id, normalized)
            changed = connection.execute(
                """UPDATE owner_operations_v2
                   SET state=?, response_json=?, receipt_hash=?, authorization=COALESCE(?, authorization)
                   WHERE operation_id=? AND state=?""",
                (state, _json(response), digest, authorization_record, operation_id, expected),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return False
            if state not in {"awaiting_owner", "pending", "partial"}:
                connection.execute(
                    "UPDATE owner_operation_times_v2 SET settled_at=? WHERE operation_id=?",
                    (_utc_now(), operation_id),
                )
            connection.commit()
            return True
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def v2_receipt_page(
        self,
        account_id: str,
        operation_id: str,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> V2ReceiptPage:
        operation = self.get_v2_operation(operation_id)
        if operation is None or operation.account_id != account_id:
            raise KeyError(operation_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT row_json FROM owner_receipts_v2 WHERE operation_id=? ORDER BY sequence",
                (operation_id,),
            ).fetchall()
        return _v2_page(
            account_id,
            operation_id,
            [cast(JsonDict, json.loads(row["row_json"])) for row in rows],
            limit=limit,
            cursor=cursor,
            cursor_key=self._cursor_key_v2(),
        )

    def prune_v2(self, *, now: str, retention_days: int = 7) -> int:
        now_value = datetime.fromisoformat(now).astimezone(timezone.utc)
        threshold = (now_value - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            expired = connection.execute(
                """SELECT operation_id, expires_at FROM owner_operations_v2
                   WHERE state='awaiting_owner' AND expires_at IS NOT NULL""",
            ).fetchall()
            for row in expired:
                expires_at = datetime.fromisoformat(str(row["expires_at"]))
                if expires_at.astimezone(timezone.utc) > now_value:
                    continue
                operation_id = str(row["operation_id"])
                connection.execute(
                    """UPDATE owner_operations_v2 SET state='stale', response_json=?
                       WHERE operation_id=? AND state='awaiting_owner'""",
                    (_json({"state": "stale", "instruction": "The approval window expired.", "operation_id": operation_id}), operation_id),
                )
                connection.execute(
                    "UPDATE owner_operation_times_v2 SET settled_at=? WHERE operation_id=?",
                    (now_value.isoformat(), operation_id),
                )
            rows = connection.execute(
                """SELECT o.*, t.settled_at FROM owner_operations_v2 o
                   JOIN owner_operation_times_v2 t USING(operation_id)
                   WHERE o.state NOT IN ('awaiting_owner','pending','partial')
                     AND t.settled_at IS NOT NULL AND t.settled_at<?""",
                (threshold,),
            ).fetchall()
            for row in rows:
                request_key = _v2_request_key(str(row["account_id"]), str(row["api_version"]), str(row["request_id"]))
                connection.execute(
                    """INSERT OR IGNORE INTO owner_tombstones_v2 VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (request_key, str(row["account_id"]), str(row["api_version"]), str(row["request_hash"]), str(row["operation_id"]), str(row["tool"]), str(row["state"]), str(row["manifest_hash"]), str(row["safety_policy_digest"]), cast(str | None, row["receipt_hash"])),
                )
                operation_id = str(row["operation_id"])
                connection.execute("DELETE FROM owner_receipts_v2 WHERE operation_id=?", (operation_id,))
                connection.execute("DELETE FROM owner_operation_times_v2 WHERE operation_id=?", (operation_id,))
                connection.execute("DELETE FROM owner_operations_v2 WHERE operation_id=?", (operation_id,))
            return len(rows)

    def cutover_v1(self) -> JsonDict:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT intent_id, state, result_json FROM intents ORDER BY intent_id"
            ).fetchall()
            quarantined = [
                str(row["intent_id"])
                for row in rows
                if row["state"] in {"prepared", "needs_approval"}
            ]
            unresolved = [
                str(row["intent_id"]) for row in rows if row["state"] == "pending"
            ]
            partial_like = [
                str(row["intent_id"])
                for row in rows
                if row["state"] == "pending"
                and row["result_json"] is not None
                and cast(JsonDict, json.loads(str(row["result_json"]))).get("status")
                == "partial"
            ]
            terminal = [
                str(row["intent_id"])
                for row in rows
                if row["state"] not in {"prepared", "needs_approval", "pending"}
            ]
            reason = _json(
                {
                    "status": "quarantined",
                    "instruction": "This v1 operation cannot be approved or replayed after the v2 cutover.",
                }
            )
            connection.execute(
                """UPDATE intents SET state='stale', plan_json='{}', result_json=?
                   WHERE state IN ('prepared','needs_approval')""",
                (reason,),
            )
            for row in rows:
                if row["state"] in {"prepared", "needs_approval", "pending"}:
                    continue
                previous = cast(JsonDict | None, json.loads(str(row["result_json"]))) if row["result_json"] is not None else None
                connection.execute(
                    "UPDATE intents SET plan_json='{}', result_json=? WHERE intent_id=?",
                    (_json(_legacy_terminal_result(str(row["state"]), previous)), str(row["intent_id"])),
                )
            connection.commit()
            return _legacy_report(quarantined, unresolved, terminal, partial_like)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def annotate_v1_pending(self, intent_id: str, *, result: JsonDict) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "UPDATE intents SET result_json=? WHERE intent_id=? AND state='pending'",
                (_json(result), intent_id),
            ).rowcount == 1

    def resolve_v1_pending(self, intent_id: str, *, expected_fingerprint: str, expected_plan_digest: str, state: Literal["applied", "stale"], result: JsonDict) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, plan_json FROM intents WHERE intent_id=? AND state='pending'",
                (intent_id,),
            ).fetchone()
            if (
                row is None or str(row["fingerprint"]) != expected_fingerprint
                or _legacy_plan_digest(cast(JsonDict, json.loads(str(row["plan_json"])))) != expected_plan_digest
            ):
                connection.rollback()
                return False
            changed = connection.execute(
                "UPDATE intents SET state=?, plan_json='{}', result_json=? WHERE intent_id=? AND state='pending' AND fingerprint=?",
                (state, _json(result), intent_id, expected_fingerprint),
            ).rowcount == 1
            connection.commit()
            return changed
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def verify_v2_authorization(
        self, operation: V2Operation, action: str, authorization: object
    ) -> str | None:
        return _verify_owner_authorization(
            operation, action, authorization, self._owner_public_key
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _cursor_key_v2(self) -> bytes:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cursor_key FROM owner_journal_secrets_v2 WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("v2 cursor key is unavailable")
        return bytes(row["cursor_key"])


def journal_path(account: str | None = None) -> Path:
    """Return the journal path beside the existing XDG state cache."""

    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    name = "journal.sqlite3"
    if account:
        digest = sha256(account.strip().casefold().encode()).hexdigest()[:16]
        name = f"journal-{digest}.sqlite3"
    return base / "things-orchestrator" / name


def owner_public_key_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root) if root else Path.home() / ".config"
    return base / "things-orchestrator" / "owner-public-key.ed25519"


def owner_authorization_binding_json(operation: V2Operation, *, action: str) -> str:
    return _json(
        {
            "version": 1,
            "account": operation.account_id,
            "action": action,
            "operation": operation.operation_id,
            "manifest_hash": operation.manifest_hash,
            "safety_policy_digest": operation.safety_policy_digest,
            "expiry": operation.expires_at,
        }
    )


def _verify_owner_authorization(
    operation: V2Operation,
    action: str,
    authorization: object,
    public_key: bytes | None,
) -> str | None:
    if (
        not v2_manifest_is_valid(operation)
        or not isinstance(authorization, OwnerAuthorization)
        or public_key is None
    ):
        return None
    expected = owner_authorization_binding_json(operation, action=action)
    if not hmac.compare_digest(expected, authorization.binding_json):
        return None
    try:
        signature = base64.b64decode(authorization.signature, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, authorization.binding_json.encode()
        )
    except (ValueError, InvalidSignature):
        return None
    return authorization.record


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def v2_manifest_hash(manifest: JsonDict) -> str:
    return "sha256:v1:" + sha256(_json(manifest).encode()).hexdigest()


def v2_manifest_is_valid(operation: V2Operation) -> bool:
    if operation.api_version != "2":
        return True
    manifest = operation.manifest
    envelope = {
        "account_id": operation.account_id,
        "api_version": operation.api_version,
        "request_hash": operation.request_hash,
        "tool": operation.tool,
        "safety_policy_digest": operation.safety_policy_digest,
        "expires_at": operation.expires_at,
    }
    return (
        manifest.get("version") == "v1"
        and manifest.get("schema_version") == "v2.0"
        and all(manifest.get(key) == value for key, value in envelope.items())
        and hmac.compare_digest(v2_manifest_hash(manifest), operation.manifest_hash)
    )


def _legacy_plan_digest(plan: JsonDict) -> str:
    return "sha256:v1:" + sha256(_json(plan).encode()).hexdigest()


def _insert_v2_receipts(
    connection: sqlite3.Connection, operation_id: str, rows: list[JsonDict]
) -> str:
    existing = connection.execute(
        "SELECT row_json FROM owner_receipts_v2 WHERE operation_id=? ORDER BY sequence",
        (operation_id,),
    ).fetchall()
    current = [cast(JsonDict, json.loads(row["row_json"])) for row in existing]
    if current and current != rows:
        raise ValueError("receipt rows are append-only and immutable")
    for row in rows:
        connection.execute(
            "INSERT OR IGNORE INTO owner_receipts_v2 VALUES (?,?,?)",
            (operation_id, int(cast(int, row["sequence"])), _json(row)),
        )
    return _v2_receipt_hash(rows)


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


def _copy_json(value: JsonDict | None) -> JsonDict | None:
    return cast(JsonDict, json.loads(_json(value))) if value is not None else None


@overload
def _copy_v2(operation: V2Operation) -> V2Operation: ...


@overload
def _copy_v2(operation: None) -> None: ...


def _copy_v2(operation: V2Operation | None) -> V2Operation | None:
    if operation is None:
        return None
    return replace(
        operation,
        manifest=cast(JsonDict, json.loads(_json(operation.manifest))),
        response=_copy_json(operation.response),
    )


def _v2_values(operation: V2Operation) -> dict[str, object]:
    return {
        "account_id": operation.account_id,
        "api_version": operation.api_version,
        "request_id": operation.request_id,
        "request_hash": operation.request_hash,
        "operation_id": operation.operation_id,
        "tool": operation.tool,
        "state": operation.state,
        "manifest": operation.manifest,
        "manifest_hash": operation.manifest_hash,
        "safety_policy_digest": operation.safety_policy_digest,
        "expires_at": operation.expires_at,
        "response": operation.response,
        "authorization": operation.authorization,
        "resolution": operation.resolution,
        "receipt_hash": operation.receipt_hash,
    }


def _v2_sql_values(operation: V2Operation) -> tuple[object, ...]:
    return (
        operation.account_id,
        operation.api_version,
        operation.request_id,
        operation.request_hash,
        operation.operation_id,
        operation.tool,
        operation.state,
        _json(operation.manifest),
        operation.manifest_hash,
        operation.safety_policy_digest,
        operation.expires_at,
        _json(operation.response) if operation.response is not None else None,
        operation.authorization,
        operation.resolution,
        operation.receipt_hash,
    )


def _v2_from_row(row: sqlite3.Row | None) -> V2Operation | None:
    if row is None:
        return None
    return V2Operation(
        account_id=str(row["account_id"]),
        api_version=str(row["api_version"]),
        request_id=str(row["request_id"]),
        request_hash=str(row["request_hash"]),
        operation_id=str(row["operation_id"]),
        tool=str(row["tool"]),
        state=cast(V2State, row["state"]),
        manifest=cast(JsonDict, json.loads(row["manifest_json"])),
        manifest_hash=str(row["manifest_hash"]),
        safety_policy_digest=str(row["safety_policy_digest"]),
        expires_at=cast(str | None, row["expires_at"]),
        response=(
            cast(JsonDict, json.loads(row["response_json"]))
            if row["response_json"] is not None
            else None
        ),
        authorization=cast(str | None, row["authorization"]),
        resolution=cast(Literal["accepted_as_is", "superseded"] | None, row["resolution"]),
        receipt_hash=cast(str | None, row["receipt_hash"]),
    )


def _v2_from_tombstone(row: sqlite3.Row, request_id: str) -> V2Operation:
    state = cast(V2State, row["final_state"])
    operation_id = str(row["operation_id"])
    return V2Operation(
        account_id=str(row["account_id"]),
        api_version=str(row["api_version"]),
        request_id=request_id,
        request_hash=str(row["request_hash"]),
        operation_id=operation_id,
        tool=str(row["tool"]),
        state=state,
        manifest={},
        manifest_hash=str(row["manifest_hash"]),
        safety_policy_digest=str(row["safety_policy_digest"]),
        response={
            "state": state,
            "instruction": "This operation is retained as a content-minimized tombstone.",
            "operation_id": operation_id,
        },
        receipt_hash=cast(str | None, row["receipt_hash"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _v2_request_key(account_id: str, api_version: str, request_id: str) -> str:
    payload = {
        "version": 1,
        "account": account_id,
        "api_version": api_version,
        "request_id": request_id,
    }
    return "sha256:v1:" + sha256(_json(payload).encode()).hexdigest()


def _legacy_report(
    quarantined: list[str],
    unresolved: list[str],
    terminal: list[str],
    partial_like: list[str],
) -> JsonDict:
    return {
        "version": 1,
        "quarantined": sorted(quarantined),
        "unresolved": sorted(unresolved),
        "partial_like": sorted(partial_like),
        "terminal": sorted(terminal),
        "writes_blocked": bool(unresolved),
        "instruction": (
            "Resolve every unresolved v1 operation by read-back; never approve or replay it."
            if unresolved
            else "No unresolved v1 operation fences v2 writes."
        ),
    }


def _sqlite_blockers(connection: sqlite3.Connection, account_id: str) -> list[str]:
    current = connection.execute(
        """SELECT operation_id FROM owner_operations_v2
           WHERE account_id=? AND state IN ('pending','partial')""",
        (account_id,),
    ).fetchall()
    legacy = connection.execute(
        "SELECT intent_id FROM intents WHERE state='pending'"
    ).fetchall()
    return sorted(
        {
            *[str(row["operation_id"]) for row in current],
            *[str(row["intent_id"]) for row in legacy],
        }
    )


def _legal_v2_transition(before: V2State, after: V2State) -> bool:
    return after in {
        "awaiting_owner": {"pending", "stale", "declined"},
        "pending": {"applied", "not_applied", "partial"},
        "partial": {"partial_resolved"},
    }.get(before, set())


def _validate_v2_receipts(rows: list[JsonDict]) -> list[JsonDict]:
    copied = cast(list[JsonDict], json.loads(_json(rows)))
    if [row.get("sequence") for row in copied] != list(range(1, len(copied) + 1)):
        raise ValueError("receipt sequences must be contiguous")
    return copied


def _validate_v2_operation_receipts(
    operation: V2Operation, rows: list[JsonDict]
) -> list[JsonDict]:
    if not v2_manifest_is_valid(operation):
        raise ValueError("v2 manifest hash does not match its persisted content")
    normalized = _validate_v2_receipts(rows)
    writes = operation.manifest.get("writes")
    if not isinstance(writes, list) or not writes or len(normalized) != len(writes):
        raise ValueError("exactly one receipt row per manifest write is required")
    return normalized


def _legacy_terminal_result(state: str, result: JsonDict | None) -> JsonDict:
    if result is None:
        return {"status": "legacy_tombstone", "state": state}
    status = result.get("status")
    if status == "owner_resolved_no_replay":
        classification = result.get("classification")
        resolution = result.get("resolution")
        authorization = result.get("authorization")
        if (
            classification in {"applied", "partial", "unknown", "malformed"}
            and resolution in {"accepted_as_is", "superseded"}
            and isinstance(authorization, str)
            and authorization.startswith("ed25519:v1:")
        ):
            return {
                "status": status,
                "classification": classification,
                "resolution": resolution,
                "authorization": authorization,
            }
    if status == "reconciled_no_replay" and result.get("classification") == "applied":
        return {"status": status, "classification": "applied"}
    if status == "quarantined":
        return {
            "status": "quarantined",
            "instruction": "This v1 operation cannot be approved or replayed after the v2 cutover.",
        }
    return {"status": "legacy_tombstone", "state": state}


def _v2_receipt_hash(rows: list[JsonDict]) -> str:
    return "sha256:v1:" + sha256(_json(rows).encode()).hexdigest()


def _v2_cursor(
    account_id: str,
    operation_id: str,
    next_sequence: int,
    receipt_hash: str,
    cursor_key: bytes,
) -> str:
    payload = {
        "version": 1,
        "account": account_id,
        "operation": operation_id,
        "next": next_sequence,
        "receipt_hash": receipt_hash,
    }
    body = _json(payload).encode()
    signature = hmac.new(cursor_key, body, sha256).hexdigest()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + signature


def _v2_page(
    account_id: str,
    operation_id: str,
    rows: list[JsonDict],
    *,
    limit: int,
    cursor: str | None,
    cursor_key: bytes,
) -> V2ReceiptPage:
    digest = _v2_receipt_hash(rows)
    start = 1
    if cursor is not None:
        try:
            encoded, checksum = cursor.split(".", 1)
            body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            expected_signature = hmac.new(cursor_key, body, sha256).hexdigest()
            if not hmac.compare_digest(expected_signature, checksum):
                raise ValueError
            payload = json.loads(body)
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("receipt cursor is invalid") from error
        expected = (1, account_id, operation_id, digest)
        observed = (
            payload.get("version"), payload.get("account"),
            payload.get("operation"), payload.get("receipt_hash"),
        )
        if observed != expected or type(payload.get("next")) is not int:
            raise ValueError("receipt cursor is invalid")
        start = int(payload["next"])
    page = [row for row in rows if int(cast(int, row["sequence"])) >= start][:limit]
    following = None
    if page and int(cast(int, page[-1]["sequence"])) < len(rows):
        following = _v2_cursor(
            account_id,
            operation_id,
            int(cast(int, page[-1]["sequence"])) + 1,
            digest,
            cursor_key,
        )
    return V2ReceiptPage(rows=page, cursor=following, receipt_hash=digest)
