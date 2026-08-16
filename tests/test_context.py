from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from things_orchestrator.context import (
    CompletenessFact,
    ContextConflict,
    ContextCorrupt,
    ContextExpired,
    ContextNotFound,
    ContextRef,
    MemoryContextStore,
    ReadIncludeSelector,
    ReadSelector,
    SQLiteContextStore,
    UnknownReference,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Tokens:
    def __init__(self, *values: str) -> None:
        self.values = iter(values)

    def __call__(self) -> str:
        return next(self.values)


def ref(name: str, item: str, revision: str = "revision-1") -> ContextRef:
    return ContextRef(ref=name, exact_id=item, revision=revision)


def selector() -> ReadSelector:
    return ReadSelector(
        purpose="organize", view="project", within="project:launch", limit=200
    )


def test_memory_context_resolves_short_ref_and_recovery_selector() -> None:
    clock = Clock()
    store = MemoryContextStore(clock=clock, token_factory=Tokens("abcdefgh"))

    context = store.create(
        account_id="account-one",
        selector=selector(),
        refs=[ref("t1", "task:first")],
        completeness=[
            CompletenessFact(scope="project:launch", seen=1, total=1, complete=True)
        ],
    )

    assert context.id == "ctx_abcdefgh"
    assert context.complete is True
    assert context.account_binding != "account-one"
    assert store.resolve(context.id, "t1", account_id="account-one") == ref(
        "t1", "task:first"
    )
    assert context.selector.recovery_arguments() == {
        "purpose": "organize",
        "view": "project",
        "within": "project:launch",
        "limit": 200,
    }


def test_context_extends_across_pages_and_becomes_complete() -> None:
    store = MemoryContextStore(clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(
        account_id="account-one",
        selector=selector(),
        refs=[ref("t1", "task:first")],
        completeness=[
            CompletenessFact(
                scope="project:launch",
                seen=1,
                total=2,
                next_cursor="cursor-next",
            )
        ],
    )

    extended = store.extend(
        context.id,
        account_id="account-one",
        refs=[ref("t2", "task:second")],
        completeness=[
            CompletenessFact(scope="project:launch", seen=2, total=2, complete=True)
        ],
    )

    assert context.complete is False
    assert extended.complete is True
    assert extended.is_complete("project:launch") is True
    assert extended.resolve("t2").exact_id == "task:second"


def test_context_extension_rejects_ref_tampering_and_pagination_regression() -> None:
    store = MemoryContextStore(clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(
        account_id="account-one",
        selector=selector(),
        refs=[ref("t1", "task:first")],
        completeness=[CompletenessFact(scope="project:launch", seen=2)],
    )

    with pytest.raises(ContextConflict, match="reference changed"):
        store.extend(
            context.id,
            account_id="account-one",
            refs=[ref("t1", "task:attacker")],
        )
    with pytest.raises(ContextConflict, match="moved backwards"):
        store.extend(
            context.id,
            account_id="account-one",
            completeness=[CompletenessFact(scope="project:launch", seen=1)],
        )


def test_expired_context_is_not_resolved() -> None:
    clock = Clock()
    store = MemoryContextStore(clock=clock, token_factory=Tokens("abcdefgh"))
    context = store.create(
        account_id="account-one",
        selector=selector(),
        refs=[ref("t1", "task:first")],
        ttl=timedelta(minutes=5),
    )
    clock.now += timedelta(minutes=5)

    with pytest.raises(ContextExpired) as error:
        store.resolve(context.id, "t1", account_id="account-one")
    assert error.value.selector == context.selector
    with pytest.raises(ContextNotFound):
        store.get(context.id, account_id="account-one")


def test_contexts_are_isolated_by_account_without_an_identity_oracle() -> None:
    store = MemoryContextStore(clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(
        account_id="account-one",
        selector=selector(),
        refs=[ref("t1", "task:first")],
    )

    with pytest.raises(ContextNotFound):
        store.get(context.id, account_id="account-two")
    with pytest.raises(ContextNotFound):
        store.get("ctx_tampered", account_id="account-one")


def test_cloud_email_account_binding_is_normalized() -> None:
    store = MemoryContextStore(clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(
        account_id=" Owner@Example.COM ",
        selector=selector(),
    )

    assert store.get(context.id, account_id="owner@example.com") == context


def test_unknown_short_ref_does_not_fall_back_to_an_exact_id() -> None:
    store = MemoryContextStore(clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(
        account_id="account-one",
        selector=selector(),
        refs=[ref("t1", "task:first")],
    )

    with pytest.raises(UnknownReference):
        store.resolve(context.id, "task:first", account_id="account-one")


def test_sqlite_context_survives_adapter_restart(tmp_path: Path) -> None:
    path = tmp_path / "contexts.sqlite3"
    clock = Clock()
    first = SQLiteContextStore(path, clock=clock, token_factory=Tokens("abcdefgh"))
    context = first.create(
        account_id="account-one",
        selector=selector(),
        refs=[ref("t1", "task:first")],
        completeness=[CompletenessFact(scope="project:launch", seen=1)],
    )

    second = SQLiteContextStore(path, clock=clock, token_factory=Tokens("ijklmnop"))
    restored = second.get(context.id, account_id="account-one")
    extended = second.extend(
        context.id,
        account_id="account-one",
        refs=[ref("t2", "task:second")],
        completeness=[
            CompletenessFact(scope="project:launch", seen=2, total=2, complete=True)
        ],
    )

    assert restored == context
    assert extended.complete is True
    assert second.resolve(context.id, "t2", account_id="account-one").revision == (
        "revision-1"
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"account-one" not in path.read_bytes()


def test_sqlite_context_enforces_expiry_and_account_isolation(tmp_path: Path) -> None:
    clock = Clock()
    store = SQLiteContextStore(
        tmp_path / "contexts.sqlite3",
        clock=clock,
        token_factory=Tokens("abcdefgh"),
    )
    context = store.create(
        account_id="account-one",
        selector=selector(),
        ttl=timedelta(seconds=1),
    )

    with pytest.raises(ContextNotFound):
        store.get(context.id, account_id="account-two")
    clock.now += timedelta(seconds=1)
    with pytest.raises(ContextExpired) as error:
        store.get(context.id, account_id="account-one")
    assert error.value.selector == context.selector
    # The expired evidence is a tombstone, not a usable mutable context.
    with pytest.raises(ContextNotFound):
        store.get(context.id, account_id="account-one")


def test_expiry_payload_keeps_bounded_include_recovery_without_account_data() -> None:
    clock = Clock()
    include = ReadIncludeSelector(find="Anchor", within="project:launch")
    selector_with_include = ReadSelector(
        purpose="change", item_id="task:target", includes=(include,)
    )
    store = MemoryContextStore(clock=clock, token_factory=Tokens("abcdefgh"))
    context = store.create(
        account_id="account-one", selector=selector_with_include, ttl=timedelta(seconds=1)
    )
    clock.now += timedelta(seconds=1)

    with pytest.raises(ContextExpired) as error:
        store.get(context.id, account_id="account-one")

    assert error.value.selector is not None
    assert error.value.selector.recovery_arguments() == {
        "purpose": "change",
        "id": "task:target",
        "include": [{"find": "Anchor", "within": "project:launch"}],
    }
    assert "account-one" not in repr(error.value)


def test_sqlite_extension_uses_the_same_merge_rules_as_memory(
    tmp_path: Path,
) -> None:
    store = SQLiteContextStore(
        tmp_path / "contexts.sqlite3",
        clock=Clock(),
        token_factory=Tokens("abcdefgh"),
    )
    context = store.create(
        account_id="account-one",
        selector=selector(),
        refs=[ref("t1", "task:first")],
        completeness=[CompletenessFact(scope="project:launch", seen=2)],
    )

    with pytest.raises(ContextConflict, match="reference changed"):
        store.extend(
            context.id,
            account_id="account-one",
            refs=[ref("t1", "task:attacker")],
        )
    with pytest.raises(ContextConflict, match="moved backwards"):
        store.extend(
            context.id,
            account_id="account-one",
            completeness=[CompletenessFact(scope="project:launch", seen=1)],
        )


def test_context_id_collisions_fail_closed_after_bounded_retries(
    tmp_path: Path,
) -> None:
    memory = MemoryContextStore(clock=Clock(), token_factory=lambda: "same-token")
    memory.create(account_id="one", selector=selector())
    with pytest.raises(ContextConflict, match="unique context ID"):
        memory.create(account_id="one", selector=selector())

    sqlite = SQLiteContextStore(
        tmp_path / "contexts.sqlite3",
        clock=Clock(),
        token_factory=lambda: "same-token",
    )
    sqlite.create(account_id="one", selector=selector())
    with pytest.raises(ContextConflict, match="unique context ID"):
        sqlite.create(account_id="one", selector=selector())


def test_sqlite_rejects_tampered_rows_without_exposing_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contexts.sqlite3"
    store = SQLiteContextStore(path, clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(account_id="one", selector=selector())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE read_contexts SET refs_json = ? WHERE context_id = ?",
            ('{"credential":"secret"}', context.id),
        )

    with pytest.raises(ContextCorrupt) as caught:
        store.get(context.id, account_id="one")

    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "account_binding",
    [
        "account-one",
        "sha256:ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde",
        "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n",
    ],
)
def test_sqlite_rejects_noncanonical_account_binding(
    tmp_path: Path, account_binding: str
) -> None:
    path = tmp_path / "contexts.sqlite3"
    store = SQLiteContextStore(path, clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(account_id="one", selector=selector())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE read_contexts SET account_binding = ? WHERE context_id = ?",
            (account_binding, context.id),
        )

    with pytest.raises(ContextCorrupt, match="stored context failed integrity checks"):
        store.get(context.id, account_id="one")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purpose", "evil"),
        ("limit", "20"),
        ("within", None),
    ],
)
def test_sqlite_rejects_tampered_selector_values(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "contexts.sqlite3"
    store = SQLiteContextStore(path, clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(account_id="one", selector=selector())
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT selector_json FROM read_contexts WHERE context_id = ?",
            (context.id,),
        ).fetchone()
        assert row is not None
        selector_data = json.loads(row[0])
        selector_data[field] = value
        connection.execute(
            "UPDATE read_contexts SET selector_json = ? WHERE context_id = ?",
            (json.dumps(selector_data), context.id),
        )

    with pytest.raises(ContextCorrupt, match="stored context failed integrity checks"):
        store.get(context.id, account_id="one")


def test_sqlite_extensions_serialize_without_lost_refs(tmp_path: Path) -> None:
    path = tmp_path / "private" / "contexts.sqlite3"
    path.parent.mkdir(mode=0o755)
    store = SQLiteContextStore(path, clock=Clock(), token_factory=Tokens("abcdefgh"))
    context = store.create(
        account_id="one",
        selector=selector(),
        completeness=[
            CompletenessFact(
                scope="project:launch",
                seen=0,
                total=2,
                next_cursor="cursor-first",
            )
        ],
    )

    def extend(entry: ContextRef) -> None:
        store.extend(
            context.id,
            account_id="one",
            refs=[entry],
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(
            workers.map(
                extend,
                [ref("t1", "task:first"), ref("t2", "task:second")],
            )
        )

    store.extend(
        context.id,
        account_id="one",
        completeness=[
            CompletenessFact(
                scope="project:launch", seen=2, total=2, complete=True
            )
        ],
    )
    stored = store.get(context.id, account_id="one")
    assert {entry.ref for entry in stored.refs} == {"t1", "t2"}
    assert stored.complete is True
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_types_reject_unsafe_or_inconsistent_context_facts() -> None:
    with pytest.raises(ValueError, match="short lowercase"):
        ref("task:first", "task:first")
    with pytest.raises(ValueError, match="next cursor"):
        CompletenessFact(
            scope="project:launch", seen=1, complete=True, next_cursor="cursor"
        )
    with pytest.raises(ValueError, match="exact item"):
        ReadSelector(purpose="change", view="today")
    assert ReadSelector(purpose="organize", find="Launch").find == "Launch"
    with pytest.raises(ValueError, match="ISO dates"):
        ReadSelector(
            view="logbook", from_date="not-a-date", to_date="2026-08-16"
        )


def test_change_include_selector_round_trips_in_both_stores(tmp_path: Path) -> None:
    include = ReadIncludeSelector(find="Anchor", within="project:launch")
    contextual = ReadSelector(
        purpose="change", item_id="task:target", includes=(include,)
    )
    memory = MemoryContextStore(clock=Clock(), token_factory=Tokens("memory123"))
    memory_context = memory.create(account_id="one", selector=contextual)
    assert memory.get(memory_context.id, account_id="one").selector == contextual
    assert contextual.recovery_arguments()["include"] == [
        {"find": "Anchor", "within": "project:launch"}
    ]

    sqlite = SQLiteContextStore(
        tmp_path / "contexts.sqlite3",
        clock=Clock(),
        token_factory=Tokens("sqlite123"),
    )
    sqlite_context = sqlite.create(account_id="one", selector=contextual)
    assert sqlite.get(sqlite_context.id, account_id="one").selector == contextual
