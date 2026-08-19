"""Short-lived read contexts for model-friendly, revision-safe changes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast

Purpose = Literal["review", "change", "organize", "recurrence"]
View = Literal[
    "today",
    "inbox",
    "week",
    "system",
    "project",
    "area",
    "audit",
    "diagnostics",
    "logbook",
    "trash",
    "tags",
]

_CONTEXT_ID = re.compile(r"^ctx_[A-Za-z0-9_-]{8,120}$")
_SHORT_REF = re.compile(r"^[a-z][a-z0-9]{0,11}$")
_ACCOUNT_BINDING = re.compile(r"^sha256:[0-9a-f]{64}$")


class ContextError(Exception):
    """Base error for the context seam."""


class ContextNotFound(ContextError):
    """The opaque context is unknown for this account."""


class ContextExpired(ContextError):
    """The opaque context existed, but its evidence is no longer current."""

    def __init__(
        self,
        message: str = "context expired",
        *,
        selector: ReadSelector | None = None,
    ) -> None:
        # Keep only the original, credential-free selector. The account binding,
        # refs, and mutable evidence stay private and cannot be used again.
        super().__init__(message)
        self.selector = selector


class UnknownReference(ContextError):
    """A short reference is not part of the supplied context."""


class ContextConflict(ContextError):
    """An extension conflicts with facts already bound to the context."""


class ContextCorrupt(ContextError):
    """Stored context data failed its typed integrity checks."""


@dataclass(frozen=True, slots=True)
class ReadIncludeSelector:
    """One credential-free bounded lookup that extends a change read."""

    item_id: str | None = None
    find: str | None = None
    within: str | None = None

    def __post_init__(self) -> None:
        if (self.item_id is None) == (self.find is None):
            raise ValueError("include needs exactly one item_id or find")
        if self.within is not None and self.find is None:
            raise ValueError("include within needs find")

    def recovery_arguments(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.item_id is not None:
            values["id"] = self.item_id
        if self.find is not None:
            values["find"] = self.find
        if self.within is not None:
            values["within"] = self.within
        return values


@dataclass(frozen=True, slots=True)
class ReadSelector:
    """Safe data needed to repeat a read after stale context.

    This type cannot contain Cloud credentials or transport configuration.
    """

    purpose: Purpose = "review"
    view: View | None = None
    item_id: str | None = None
    find: str | None = None
    within: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    limit: int = 20
    includes: tuple[ReadIncludeSelector, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 500:
            raise ValueError("selector limit must be between 1 and 500")
        if len(self.includes) > 40:
            raise ValueError("selector includes cannot exceed 40")
        if len({entry for entry in self.includes}) != len(self.includes):
            raise ValueError("selector includes must be unique")
        if self.purpose not in {"review", "change", "organize"} and self.includes:
            raise ValueError(
                "selector includes are only available for review, change, or organize"
            )
        selectors = sum(
            value is not None for value in (self.view, self.item_id, self.find)
        )
        if selectors > 1:
            raise ValueError("selector accepts only one of view, item_id, or find")
        if self.purpose == "change" and self.item_id is None and self.find is None:
            raise ValueError("change context needs an exact item or unique find")
        if self.purpose == "recurrence" and self.item_id is None:
            raise ValueError("recurrence context needs an exact Task")
        if self.purpose == "recurrence" and any(
            value is not None
            for value in (
                self.view,
                self.find,
                self.within,
                self.from_date,
                self.to_date,
            )
        ):
            raise ValueError("recurrence context accepts only an exact Task")
        if self.purpose == "organize" and not (
            self.item_id is not None
            or self.find is not None
            or (self.view == "project" and self.within is not None)
        ):
            raise ValueError(
                "organize context needs an exact Project id, Project find, or Project read"
            )
        if self.purpose == "organize" and self.item_id is not None:
            if not self.item_id.startswith("project:"):
                raise ValueError("organize context needs an exact Project")
        if self.purpose == "organize" and self.view == "project":
            if self.within is None or not self.within.startswith("project:"):
                raise ValueError("organize Project view needs a Project scope")
        if self.view == "project" and self.within is None:
            raise ValueError("Project selector needs within")
        if self.view == "area" and self.within is None:
            raise ValueError("Area selector needs within")
        if (
            self.within is not None
            and self.find is None
            and self.view not in {"project", "area"}
        ):
            raise ValueError("within needs find or a Project or Area selector")
        has_range = self.from_date is not None or self.to_date is not None
        if has_range and self.view != "logbook":
            raise ValueError("date range needs a Logbook selector")
        if self.view == "logbook" and (self.from_date is None) != (self.to_date is None):
            raise ValueError("Logbook selector needs both from_date and to_date, or neither")
        if self.from_date is not None and self.to_date is not None:
            try:
                start = date.fromisoformat(self.from_date)
                end = date.fromisoformat(self.to_date)
            except ValueError as error:
                raise ValueError("selector dates must be ISO dates") from error
            if start > end:
                raise ValueError("selector from_date cannot be after to_date")

    def recovery_arguments(self) -> dict[str, object]:
        """Return one credential-free read payload for guided recovery."""

        arguments: dict[str, object] = {"purpose": self.purpose}
        fields = {
            "view": self.view,
            "id": self.item_id,
            "find": self.find,
            "within": self.within,
            "from": self.from_date,
            "to": self.to_date,
        }
        arguments.update(
            {key: value for key, value in fields.items() if value is not None}
        )
        if self.limit != 20:
            arguments["limit"] = self.limit
        if self.includes:
            arguments["include"] = [
                entry.recovery_arguments() for entry in self.includes
            ]
        return arguments


@dataclass(frozen=True, slots=True)
class ContextRef:
    """One short model reference bound to an exact item revision."""

    ref: str
    exact_id: str
    revision: str

    def __post_init__(self) -> None:
        if _SHORT_REF.fullmatch(self.ref) is None:
            raise ValueError("context ref must be a short lowercase reference")
        if not self.exact_id or len(self.exact_id) > 512:
            raise ValueError("exact_id must contain 1 to 512 characters")
        if not self.revision or len(self.revision) > 512:
            raise ValueError("revision must contain 1 to 512 characters")


@dataclass(frozen=True, slots=True)
class CompletenessFact:
    """Pagination evidence for one independently complete read scope."""

    scope: str
    seen: int
    total: int | None = None
    next_cursor: str | None = None
    complete: bool = False

    def __post_init__(self) -> None:
        if not self.scope or len(self.scope) > 512:
            raise ValueError("completeness scope must contain 1 to 512 characters")
        if self.seen < 0:
            raise ValueError("seen cannot be negative")
        if self.total is not None and self.total < self.seen:
            raise ValueError("total cannot be smaller than seen")
        if self.complete and self.next_cursor is not None:
            raise ValueError("a complete scope cannot have a next cursor")
        if self.complete and self.total is not None and self.total != self.seen:
            raise ValueError("a complete known total must equal seen")


@dataclass(frozen=True, slots=True)
class ReadContext:
    """Opaque evidence that binds model refs to one account and read snapshot."""

    id: str
    account_binding: str
    selector: ReadSelector
    refs: tuple[ContextRef, ...]
    completeness: tuple[CompletenessFact, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        if _CONTEXT_ID.fullmatch(self.id) is None:
            raise ValueError("invalid context ID")
        if self.expires_at.utcoffset() is None:
            raise ValueError("context expiry must include a UTC offset")
        if len({entry.ref for entry in self.refs}) != len(self.refs):
            raise ValueError("context refs must be unique")
        if len({entry.exact_id for entry in self.refs}) != len(self.refs):
            raise ValueError("exact IDs must be unique in one context")
        if len({entry.scope for entry in self.completeness}) != len(self.completeness):
            raise ValueError("completeness scopes must be unique")

    @property
    def complete(self) -> bool:
        """True only when each declared scope has complete evidence."""

        return bool(self.completeness) and all(
            fact.complete for fact in self.completeness
        )

    def is_complete(self, scope: str) -> bool:
        return any(fact.scope == scope and fact.complete for fact in self.completeness)

    def resolve(self, ref: str) -> ContextRef:
        try:
            return next(entry for entry in self.refs if entry.ref == ref)
        except StopIteration as error:
            raise UnknownReference(f"unknown context reference: {ref}") from error


class ContextStore(Protocol):
    """Persistence seam for short-lived read evidence."""

    def create(
        self,
        *,
        account_id: str,
        selector: ReadSelector,
        refs: Iterable[ContextRef] = (),
        completeness: Iterable[CompletenessFact] = (),
        ttl: timedelta = timedelta(minutes=30),
    ) -> ReadContext: ...

    def get(self, context_id: str, *, account_id: str) -> ReadContext: ...

    def resolve(self, context_id: str, ref: str, *, account_id: str) -> ContextRef: ...

    def extend(
        self,
        context_id: str,
        *,
        account_id: str,
        refs: Iterable[ContextRef] = (),
        completeness: Iterable[CompletenessFact] = (),
    ) -> ReadContext: ...


@dataclass(frozen=True, slots=True)
class _ContextPolicy:
    """Shared account, expiry, and merge rules for context stores."""

    clock: Callable[[], datetime]

    def expires_at(self, ttl: timedelta) -> datetime:
        return _aware_now(self.clock) + _valid_ttl(ttl)

    def build(
        self,
        *,
        context_id: str,
        account_id: str,
        selector: ReadSelector,
        refs: Iterable[ContextRef],
        completeness: Iterable[CompletenessFact],
        expires_at: datetime,
    ) -> ReadContext:
        return _new_context(
            context_id=context_id,
            account_id=account_id,
            selector=selector,
            refs=refs,
            completeness=completeness,
            expires_at=expires_at,
        )

    def require(
        self, context: ReadContext | None, *, account_id: str
    ) -> ReadContext:
        if context is None or not _same_account(context, account_id):
            raise ContextNotFound("context is unknown")
        if context.expires_at <= _aware_now(self.clock):
            raise ContextExpired(selector=context.selector)
        return context

    def extend(
        self,
        context: ReadContext | None,
        *,
        account_id: str,
        refs: Iterable[ContextRef],
        completeness: Iterable[CompletenessFact],
    ) -> ReadContext:
        return _extend(
            self.require(context, account_id=account_id),
            refs=refs,
            completeness=completeness,
        )


class MemoryContextStore:
    """In-process context adapter for tests and local sessions."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        token_factory: Callable[[], str],
    ) -> None:
        self._policy = _ContextPolicy(clock=clock)
        self._token_factory = token_factory
        self._contexts: dict[str, ReadContext] = {}

    def create(
        self,
        *,
        account_id: str,
        selector: ReadSelector,
        refs: Iterable[ContextRef] = (),
        completeness: Iterable[CompletenessFact] = (),
        ttl: timedelta = timedelta(minutes=30),
    ) -> ReadContext:
        context = self._policy.build(
            context_id=self._available_id(),
            account_id=account_id,
            selector=selector,
            refs=refs,
            completeness=completeness,
            expires_at=self._policy.expires_at(ttl),
        )
        self._contexts[context.id] = context
        return context

    def get(self, context_id: str, *, account_id: str) -> ReadContext:
        context = self._contexts.get(context_id)
        try:
            return self._policy.require(context, account_id=account_id)
        except ContextExpired:
            self._contexts.pop(context_id, None)
            raise

    def resolve(self, context_id: str, ref: str, *, account_id: str) -> ContextRef:
        return self.get(context_id, account_id=account_id).resolve(ref)

    def extend(
        self,
        context_id: str,
        *,
        account_id: str,
        refs: Iterable[ContextRef] = (),
        completeness: Iterable[CompletenessFact] = (),
    ) -> ReadContext:
        try:
            extended = self._policy.extend(
                self._contexts.get(context_id),
                account_id=account_id,
                refs=refs,
                completeness=completeness,
            )
        except ContextExpired:
            self._contexts.pop(context_id, None)
            raise
        self._contexts[context_id] = extended
        return extended

    def _available_id(self) -> str:
        for _ in range(8):
            candidate = _context_id(self._token_factory())
            if candidate not in self._contexts:
                return candidate
        raise ContextConflict("could not allocate a unique context ID")


class SQLiteContextStore:
    """Process-safe SQLite adapter for contexts that survive server restarts."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime],
        token_factory: Callable[[], str],
    ) -> None:
        self.path = path
        self._policy = _ContextPolicy(clock=clock)
        self._token_factory = token_factory
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS read_contexts (
                    context_id TEXT PRIMARY KEY,
                    account_binding TEXT NOT NULL,
                    selector_json TEXT NOT NULL,
                    refs_json TEXT NOT NULL,
                    completeness_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
        path.chmod(0o600)

    def create(
        self,
        *,
        account_id: str,
        selector: ReadSelector,
        refs: Iterable[ContextRef] = (),
        completeness: Iterable[CompletenessFact] = (),
        ttl: timedelta = timedelta(minutes=30),
    ) -> ReadContext:
        expiry = self._policy.expires_at(ttl)
        saved_refs = tuple(refs)
        saved_completeness = tuple(completeness)
        for _ in range(8):
            context = self._policy.build(
                context_id=_context_id(self._token_factory()),
                account_id=account_id,
                selector=selector,
                refs=saved_refs,
                completeness=saved_completeness,
                expires_at=expiry,
            )
            try:
                with self._connect() as connection:
                    self._insert(connection, context)
                return context
            except sqlite3.IntegrityError:
                continue
        raise ContextConflict("could not allocate a unique context ID")

    def get(self, context_id: str, *, account_id: str) -> ReadContext:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM read_contexts WHERE context_id = ?", (context_id,)
            ).fetchone()
            try:
                context = _context_from_row(row) if row is not None else None
            except ContextCorrupt:
                raise
            try:
                return self._policy.require(context, account_id=account_id)
            except ContextExpired:
                connection.execute(
                    "DELETE FROM read_contexts WHERE context_id = ?", (context_id,)
                )
                connection.commit()
                raise

    def resolve(self, context_id: str, ref: str, *, account_id: str) -> ContextRef:
        return self.get(context_id, account_id=account_id).resolve(ref)

    def extend(
        self,
        context_id: str,
        *,
        account_id: str,
        refs: Iterable[ContextRef] = (),
        completeness: Iterable[CompletenessFact] = (),
    ) -> ReadContext:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM read_contexts WHERE context_id = ?", (context_id,)
            ).fetchone()
            try:
                current = _context_from_row(row) if row is not None else None
            except ContextCorrupt:
                raise
            try:
                extended = self._policy.extend(
                    current,
                    account_id=account_id,
                    refs=refs,
                    completeness=completeness,
                )
            except ContextExpired:
                connection.execute(
                    "DELETE FROM read_contexts WHERE context_id = ?", (context_id,)
                )
                connection.commit()
                raise
            self._update(connection, extended)
            return extended

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _insert(connection: sqlite3.Connection, context: ReadContext) -> None:
        values = _context_values(context)
        connection.execute(
            "INSERT INTO read_contexts VALUES (?, ?, ?, ?, ?, ?)", values
        )

    @staticmethod
    def _update(connection: sqlite3.Connection, context: ReadContext) -> None:
        values = _context_values(context)
        connection.execute(
            """
            UPDATE read_contexts SET
                account_binding = ?, selector_json = ?, refs_json = ?,
                completeness_json = ?, expires_at = ?
            WHERE context_id = ?
            """,
            (*values[1:], values[0]),
        )


def _new_context(
    *,
    context_id: str,
    account_id: str,
    selector: ReadSelector,
    refs: Iterable[ContextRef],
    completeness: Iterable[CompletenessFact],
    expires_at: datetime,
) -> ReadContext:
    return ReadContext(
        id=context_id,
        account_binding=_bind_account(account_id),
        selector=selector,
        refs=tuple(refs),
        completeness=tuple(completeness),
        expires_at=expires_at,
    )


def _extend(
    context: ReadContext,
    *,
    refs: Iterable[ContextRef],
    completeness: Iterable[CompletenessFact],
) -> ReadContext:
    return replace(
        context,
        refs=_merge_refs(context.refs, tuple(refs)),
        completeness=_merge_completeness(context.completeness, tuple(completeness)),
    )


def _merge_refs(
    current: tuple[ContextRef, ...], additions: tuple[ContextRef, ...]
) -> tuple[ContextRef, ...]:
    by_ref = {entry.ref: entry for entry in current}
    by_id = {entry.exact_id: entry for entry in current}
    for entry in additions:
        existing_ref = by_ref.get(entry.ref)
        existing_id = by_id.get(entry.exact_id)
        if existing_ref is not None and existing_ref != entry:
            raise ContextConflict(f"reference changed: {entry.ref}")
        if existing_id is not None and existing_id != entry:
            raise ContextConflict(f"exact item already has ref: {entry.exact_id}")
        if existing_ref is None:
            by_ref[entry.ref] = entry
            by_id[entry.exact_id] = entry
    return tuple(by_ref.values())


def _merge_completeness(
    current: tuple[CompletenessFact, ...],
    additions: tuple[CompletenessFact, ...],
) -> tuple[CompletenessFact, ...]:
    by_scope = {fact.scope: fact for fact in current}
    for fact in additions:
        old = by_scope.get(fact.scope)
        if old is not None:
            if fact.seen < old.seen:
                raise ContextConflict(f"pagination moved backwards: {fact.scope}")
            if old.complete and not fact.complete:
                raise ContextConflict(f"complete scope became incomplete: {fact.scope}")
            if (
                old.total is not None
                and fact.total is not None
                and old.total != fact.total
            ):
                raise ContextConflict(f"scope total changed: {fact.scope}")
        by_scope[fact.scope] = fact
    return tuple(by_scope.values())


def _context_id(token: str) -> str:
    value = token if token.startswith("ctx_") else f"ctx_{token}"
    if _CONTEXT_ID.fullmatch(value) is None:
        raise ValueError("token factory returned an invalid context token")
    return value


def _bind_account(account_id: str) -> str:
    normalized = account_id.strip().casefold()
    if not normalized or len(normalized) > 1000:
        raise ValueError("account_id must contain 1 to 1000 characters")
    return "sha256:" + hashlib.sha256(normalized.encode()).hexdigest()


def _same_account(context: ReadContext, account_id: str) -> bool:
    return hmac.compare_digest(context.account_binding, _bind_account(account_id))


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.utcoffset() is None:
        raise ValueError("context clock must return a UTC-offset date-time")
    return now


def _valid_ttl(ttl: timedelta) -> timedelta:
    if ttl <= timedelta(0) or ttl > timedelta(hours=24):
        raise ValueError("context ttl must be more than zero and at most 24 hours")
    return ttl


def _context_values(context: ReadContext) -> tuple[str, str, str, str, str, str]:
    return (
        context.id,
        context.account_binding,
        json.dumps(asdict(context.selector), separators=(",", ":"), sort_keys=True),
        json.dumps(
            [asdict(entry) for entry in context.refs],
            separators=(",", ":"),
            sort_keys=True,
        ),
        json.dumps(
            [asdict(fact) for fact in context.completeness],
            separators=(",", ":"),
            sort_keys=True,
        ),
        context.expires_at.isoformat(),
    )


def _context_from_row(row: sqlite3.Row) -> ReadContext:
    try:
        selector_data = _json_mapping(row["selector_json"])
        ref_data = _json_list(row["refs_json"])
        fact_data = _json_list(row["completeness_json"])
        context_id = _text(row["context_id"])
        account_binding = _text(row["account_binding"])
        if _ACCOUNT_BINDING.fullmatch(account_binding) is None:
            raise ValueError("stored account binding is not canonical")
        expires_at = _text(row["expires_at"])
        return ReadContext(
            id=context_id,
            account_binding=account_binding,
            selector=_selector_from_data(selector_data),
            refs=tuple(_ref_from_data(entry) for entry in ref_data),
            completeness=tuple(_fact_from_data(entry) for entry in fact_data),
            expires_at=datetime.fromisoformat(expires_at),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ContextCorrupt("stored context failed integrity checks") from error


def _selector_from_data(data: dict[str, object]) -> ReadSelector:
    required = {
        "purpose",
        "view",
        "item_id",
        "find",
        "within",
        "from_date",
        "to_date",
        "limit",
    }
    if set(data) - required - {"includes"}:
        raise ValueError("stored object has an invalid shape")
    if not required <= set(data):
        raise ValueError("stored object has an invalid shape")
    purpose = data["purpose"]
    view = data["view"]
    if purpose not in {"review", "change", "organize", "recurrence"}:
        raise ValueError("invalid selector purpose")
    if view is not None and view not in {
        "today",
        "inbox",
        "week",
        "system",
        "project",
        "area",
        "audit",
        "diagnostics",
        "logbook",
        "trash",
        "tags",
    }:
        raise ValueError("invalid selector view")
    includes_data = data.get("includes", [])
    if not isinstance(includes_data, list):
        raise ValueError("includes must be a list")
    includes = tuple(_include_from_data(entry) for entry in includes_data)
    return ReadSelector(
        purpose=cast(Purpose, _required_text_or_none(purpose, "purpose")),
        view=cast(View | None, _required_text_or_none(view, "view")),
        item_id=_required_text_or_none(data["item_id"], "item_id"),
        find=_required_text_or_none(data["find"], "find"),
        within=_required_text_or_none(data["within"], "within"),
        from_date=_required_text_or_none(data["from_date"], "from_date"),
        to_date=_required_text_or_none(data["to_date"], "to_date"),
        limit=_required_int(data["limit"], "limit"),
        includes=includes,
    )


def _include_from_data(data: object) -> ReadIncludeSelector:
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        raise ValueError("stored include must be an object")
    _require_keys(data, {"item_id", "find", "within"})
    return ReadIncludeSelector(
        item_id=_required_text_or_none(data["item_id"], "include item_id"),
        find=_required_text_or_none(data["find"], "include find"),
        within=_required_text_or_none(data["within"], "include within"),
    )


def _ref_from_data(data: dict[str, object]) -> ContextRef:
    _require_keys(data, {"ref", "exact_id", "revision"})
    return ContextRef(
        ref=_required_text(data["ref"], "ref"),
        exact_id=_required_text(data["exact_id"], "exact_id"),
        revision=_required_text(data["revision"], "revision"),
    )


def _fact_from_data(data: dict[str, object]) -> CompletenessFact:
    _require_keys(data, {"scope", "seen", "total", "next_cursor", "complete"})
    total = data["total"]
    if total is not None:
        total = _required_int(total, "total")
    next_cursor = data["next_cursor"]
    if next_cursor is not None:
        next_cursor = _required_text(next_cursor, "next_cursor")
    complete = data["complete"]
    if type(complete) is not bool:
        raise ValueError("complete must be a boolean")
    return CompletenessFact(
        scope=_required_text(data["scope"], "scope"),
        seen=_required_int(data["seen"], "seen"),
        total=total,
        next_cursor=next_cursor,
        complete=complete,
    )


def _json_mapping(value: object) -> dict[str, object]:
    decoded = json.loads(_text(value))
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) for key in decoded
    ):
        raise ValueError("stored JSON must be an object")
    return decoded


def _json_list(value: object) -> list[dict[str, object]]:
    decoded = json.loads(_text(value))
    if not isinstance(decoded, list) or not all(
        isinstance(entry, dict)
        and all(isinstance(key, str) for key in entry)
        for entry in decoded
    ):
        raise ValueError("stored JSON must be a list of objects")
    return cast(list[dict[str, object]], decoded)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("stored value must be text")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value


def _required_text_or_none(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _required_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def _require_keys(data: dict[str, object], expected: set[str]) -> None:
    if set(data) != expected:
        raise ValueError("stored object has an invalid shape")
