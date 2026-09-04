from __future__ import annotations

from typing import Any

import pytest

from things_orchestrator.cloud import (
    CloudClient,
    CloudError,
    HistoryIdentityChanged,
)


class _StaticResponseClient(CloudClient):
    def __init__(self, response: object) -> None:
        super().__init__("owner@example.com", "secret")
        self.history_id = "history-one"
        self._response = response

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ) -> Any:
        del method, path, query, body, retry
        return self._response


def _client(response: object) -> CloudClient:
    return _StaticResponseClient(response)


def test_grouped_history_preserves_empty_positions_and_freezes_payload() -> None:
    client = _client(
        {
            "current-item-index": 4,
            "items": [
                {},
                {"tag": {"t": 0, "e": "Tag4", "p": {"tt": "AI"}}},
            ],
        }
    )

    batch = client.history_groups(2)

    assert [group.index for group in batch.groups] == [2, 3]
    assert batch.groups[0].events == ()
    assert batch.caught_up is True
    with pytest.raises(TypeError):
        batch.groups[1].events[0].payload["tt"] = "other"  # type: ignore[index]


@pytest.mark.parametrize(
    "items, message",
    (
        (["bad"], "group"),
        ([{"task": []}], "event"),
        ([{"task": {"t": 0, "e": "Task8", "p": {}}}], "Unsupported"),
        ([{"tag": {"t": 0, "e": "Tag5", "p": {}}}], "Unsupported"),
        ([{"task": {"t": 0, "e": "Task7", "p": {"tg": "AI"}}}], "tags"),
        ([{"task": {"t": 0, "e": "Task7", "p": {"tp": True}}}], "kind"),
    ),
)
def test_grouped_history_rejects_whole_malformed_batch(
    items: object, message: str
) -> None:
    client = _client({"current-item-index": 1, "items": items})
    with pytest.raises(CloudError, match=message):
        client.history_groups(0)


def test_gap_shaped_empty_page_fails_closed() -> None:
    client = _client({"current-item-index": 9, "items": []})
    with pytest.raises(CloudError, match="unexplained gap"):
        client.history_groups(4)


def test_replacement_head_behind_durable_cursor_requests_identity_reset() -> None:
    client = _client({"current-item-index": 2, "items": []})
    with pytest.raises(HistoryIdentityChanged):
        client.history_groups(4)


def test_changed_history_identity_is_typed_and_never_returns_index_zero() -> None:
    class ChangedHistoryClient(CloudClient):
        def __init__(self) -> None:
            super().__init__("owner@example.com", "secret")
            self.history_id = "old-history"
            self.starts: list[int] = []

        def _request(
            self,
            method: str,
            path: str,
            *,
            query: dict[str, str] | None = None,
            body: bytes | None = None,
            retry: bool = True,
        ) -> Any:
            del method, body, retry
            if "/account/" in path:
                return {"history-key": "replacement-history"}
            self.starts.append(int((query or {})["start-index"]))
            raise CloudError("Things Cloud HTTP 404")

    client = ChangedHistoryClient()
    with pytest.raises(HistoryIdentityChanged):
        client.history_groups(4)
    assert client.starts == [4]
    assert client.history_id == "replacement-history"


def test_unchanged_history_404_retries_same_index_once() -> None:
    class UnchangedHistoryClient(CloudClient):
        def __init__(self) -> None:
            super().__init__("owner@example.com", "secret")
            self.history_id = "same-history"
            self.starts: list[int] = []

        def _request(
            self,
            method: str,
            path: str,
            *,
            query: dict[str, str] | None = None,
            body: bytes | None = None,
            retry: bool = True,
        ) -> Any:
            del method, body, retry
            if "/account/" in path:
                return {"history-key": "same-history"}
            self.starts.append(int((query or {})["start-index"]))
            if len(self.starts) == 1:
                raise CloudError("Things Cloud HTTP 404")
            return {"current-item-index": 7, "items": []}

    client = UnchangedHistoryClient()
    batch = client.history_groups(7)
    assert batch.caught_up is True
    assert client.starts == [7, 7]
