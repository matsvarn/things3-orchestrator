from __future__ import annotations

from functools import partial
from pathlib import Path

import anyio
import pytest

from scripts.run_live_acceptance import (
    acceptance_failure_message,
    acceptance_urls,
    run,
    summary_exit_code,
)
from things_orchestrator.live_acceptance import AcceptanceFailure


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "http://127.0.0.2/mcp",
        "https://user:secret@example.com/mcp",
        "https://example.com/not-mcp",
        "https://example.com/mcp?token=secret",
    ],
)
def test_acceptance_url_rejects_unsafe_or_ambiguous_targets(url: str) -> None:
    with pytest.raises(ValueError):
        acceptance_urls(url)


@pytest.mark.parametrize(
    "url,mcp,health",
    [
        (
            "http://127.0.0.1:8787/mcp",
            "http://127.0.0.1:8787/mcp/",
            "http://127.0.0.1:8787/health",
        ),
        (
            "http://localhost:8787/mcp",
            "http://localhost:8787/mcp/",
            "http://localhost:8787/health",
        ),
        (
            "http://[::1]:8787/mcp",
            "http://[::1]:8787/mcp/",
            "http://[::1]:8787/health",
        ),
        (
            "https://example.com/mcp",
            "https://example.com/mcp/",
            "https://example.com/health",
        ),
    ],
)
def test_acceptance_url_avoids_the_mount_redirect_before_sending_bearer(
    url: str, mcp: str, health: str
) -> None:
    assert acceptance_urls(url) == (mcp, health)


def test_only_cleanup_complete_is_a_success_exit() -> None:
    assert summary_exit_code({"state": "cleaned", "passed": True}) == 0
    assert summary_exit_code({"state": "pending", "passed": False}) == 1
    assert summary_exit_code({"state": "partial", "passed": False}) == 1


def test_expected_acceptance_failure_unwraps_from_transport_task_groups() -> None:
    error = ExceptionGroup(
        "transport",
        [
            ExceptionGroup(
                "session",
                [AcceptanceFailure("state belongs to a different target or commit")],
            )
        ],
    )

    assert (
        acceptance_failure_message(error)
        == "state belongs to a different target or commit"
    )


def test_expected_failure_messages_preserve_nested_causal_order() -> None:
    error = ExceptionGroup(
        "transport",
        [
            AcceptanceFailure("first"),
            ExceptionGroup("session", [AcceptanceFailure("second")]),
        ],
    )

    assert acceptance_failure_message(error) == "first; second"


def test_mixed_task_group_is_not_downgraded_to_an_expected_failure() -> None:
    error = ExceptionGroup(
        "transport",
        [AcceptanceFailure("expected"), RuntimeError("unexpected")],
    )

    assert acceptance_failure_message(error) is None


def test_authenticated_clients_do_not_trust_env_or_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"commit": "a" * 40, "version": "0.11.0"}

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.append(kwargs)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def get(self, url: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("scripts.run_live_acceptance.httpx2.AsyncClient", FakeClient)
    monkeypatch.setattr(
        "scripts.run_live_acceptance.package_version", lambda: "0.11.0"
    )

    with pytest.raises(Exception):
        anyio.run(
            partial(
                run,
                url="http://127.0.0.1:8787/mcp/",
                health_url="http://127.0.0.1:8787/health",
                state_path=Path("unused"),
                token="token",
                expect_commit="a" * 40,
            )
        )

    assert seen
    for kwargs in seen:
        assert kwargs.get("follow_redirects") is False
        assert kwargs.get("trust_env") is False
        assert kwargs.get("headers") == {"Authorization": "Bearer token"}
