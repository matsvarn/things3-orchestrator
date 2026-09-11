"""Short-lived read contexts for model-friendly, revision-safe changes."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Protocol

from .interface import Purpose, View, validate_read_selector

_CONTEXT_ID = re.compile(r"^ctx_[A-Za-z0-9_-]{8,120}$")
_SHORT_REF = re.compile(r"^[a-z][a-z0-9]{0,11}$")


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
        selectors = sum(
            value is not None for value in (self.view, self.item_id, self.find)
        )
        if selectors > 1:
            raise ValueError("selector accepts only one of view, item_id, or find")
        if self.from_date is not None and self.to_date is not None:
            try:
                date.fromisoformat(self.from_date)
                date.fromisoformat(self.to_date)
            except ValueError as error:
                raise ValueError("selector dates must be ISO dates") from error
        validate_read_selector(
            purpose=self.purpose,
            view=self.view,
            item_id=self.item_id,
            find=self.find,
            within=self.within,
            from_date=self.from_date,
            to_date=self.to_date,
            has_includes=bool(self.includes),
        )

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
    """In-process seam for short-lived read evidence."""

    def create(
        self,
        *,
        account_id: str,
        selector: ReadSelector,
        refs: Iterable[ContextRef] = (),
        completeness: Iterable[CompletenessFact] = (),
        ttl: timedelta = timedelta(minutes=30),
    ) -> ReadContext: ...


class MemoryContextStore:
    """In-process context adapter for tests and local sessions."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        token_factory: Callable[[], str],
    ) -> None:
        self._clock = clock
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
        context = _new_context(
            context_id=self._available_id(),
            account_id=account_id,
            selector=selector,
            refs=refs,
            completeness=completeness,
            expires_at=_aware_now(self._clock) + _valid_ttl(ttl),
        )
        self._contexts[context.id] = context
        return context

    def get(self, context_id: str, *, account_id: str) -> ReadContext:
        context = self._contexts.get(context_id)
        try:
            return self._require(context, account_id=account_id)
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
            extended = _extend(
                self._require(self._contexts.get(context_id), account_id=account_id),
                refs=refs,
                completeness=completeness,
            )
        except ContextExpired:
            self._contexts.pop(context_id, None)
            raise
        self._contexts[context_id] = extended
        return extended

    def _require(
        self, context: ReadContext | None, *, account_id: str
    ) -> ReadContext:
        if context is None or not _same_account(context, account_id):
            raise ContextNotFound("context is unknown")
        if context.expires_at <= _aware_now(self._clock):
            raise ContextExpired(selector=context.selector)
        return context

    def _available_id(self) -> str:
        for _ in range(8):
            candidate = _context_id(self._token_factory())
            if candidate not in self._contexts:
                return candidate
        raise ContextConflict("could not allocate a unique context ID")


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
