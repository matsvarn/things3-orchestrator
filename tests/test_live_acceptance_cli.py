import pytest

from scripts.run_live_acceptance import acceptance_urls, summary_exit_code


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
    assert summary_exit_code({"state": "awaiting_owner", "passed": False}) == 2
    assert summary_exit_code({"state": "partial", "passed": False}) == 1
