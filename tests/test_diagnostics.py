from __future__ import annotations

import json
from pathlib import Path

import pytest

import things_orchestrator.diagnostics as diagnostics
from things_orchestrator.cloud import CloudError
from things_orchestrator.config import Credentials, normalize_mcp_url
from things_orchestrator.deployment import DeploymentIdentity
from things_orchestrator.diagnostics import (
    CloudCheck,
    SupportReport,
    build_support_report,
    classify_endpoint,
    run_cloud_check,
)
from things_orchestrator.library import MemoryLibrary, Record
from things_orchestrator.recurrence import RecurrenceState


class _ReadOnlyLibrary(MemoryLibrary):
    def __init__(self, records: list[Record]) -> None:
        super().__init__(records)
        self.refreshed = False
        self.tags = {"private-tag-id": "private tag"}

    def refresh(self, *, force: bool = False) -> None:
        assert force is True
        self.refreshed = True

    def apply(self, writes: object) -> object:
        raise AssertionError("cloud-check must not write")


def test_cloud_check_force_refreshes_and_returns_only_structural_counts() -> None:
    library = _ReadOnlyLibrary(
        [
            Record(
                "private-task-id",
                "task",
                "private title",
                notes="private notes",
                checklists=[],
            ),
            Record("private-project-id", "project", "private project", status="done"),
            Record("private-area-id", "area", "private area"),
            Record(
                "private-heading-id",
                "project",
                "private heading",
                heading=True,
            ),
            Record("private-trash-id", "task", "private trash", trashed=True),
        ]
    )

    check = run_cloud_check(library)

    assert library.refreshed is True
    assert check.status == "ok"
    assert check.as_dict() == {
        "status": "ok",
        "counts": {
            "areas": 1,
            "checklist_items": 0,
            "done": 1,
            "dropped": 0,
            "headings": 1,
            "open": 2,
            "projects": 1,
            "records": 5,
            "repeating_templates": 0,
            "tags": 1,
            "tasks": 2,
            "trashed": 1,
        },
    }
    serialized = json.dumps(check.as_dict(), sort_keys=True)
    for private in (
        "private-task-id",
        "private title",
        "private notes",
        "private tag",
    ):
        assert private not in serialized


def test_cloud_check_open_count_excludes_hidden_and_trashed_rows() -> None:
    visible = Record("visible", "task", "Visible")
    trashed = Record("trashed", "task", "Trashed", trashed=True)
    heading = Record("heading", "project", "Heading", heading=True)
    template = Record(
        "template",
        "task",
        "Template",
        recurrence=RecurrenceState(role="template"),
    )

    check = run_cloud_check(_ReadOnlyLibrary([visible, trashed, heading, template]))

    counts = check.as_dict()["counts"]
    assert isinstance(counts, dict)
    assert counts["open"] == 1


def test_collected_cloud_check_uses_and_removes_a_fresh_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_cache: list[Path] = []

    class FakeClient:
        def __init__(self, email: str, password: str) -> None:
            assert (email, password) == ("private@example.com", "private-password")

    class FakeLibrary:
        records: dict[str, Record] = {}
        tags: dict[str, str] = {}

        def __init__(self, client: FakeClient, *, cache: Path) -> None:
            observed_cache.append(cache)
            assert not cache.exists()

        def refresh(self, *, force: bool = False) -> None:
            assert force is True

    monkeypatch.setattr(diagnostics, "CloudClient", FakeClient)
    monkeypatch.setattr(diagnostics, "CloudLibrary", FakeLibrary)
    monkeypatch.setattr(
        diagnostics,
        "_credentials",
        lambda: Credentials("private@example.com", "private-password", None),
    )

    assert diagnostics.collect_cloud_check().status == "ok"
    assert len(observed_cache) == 1
    assert not observed_cache[0].parent.exists()


def test_cloud_check_maps_cloud_errors_to_fixed_statuses() -> None:
    class FailingLibrary(MemoryLibrary):
        def refresh(self, *, force: bool = False) -> None:
            raise CloudError("private upstream response containing a URL and title")

    check = run_cloud_check(FailingLibrary())

    assert check == CloudCheck("unavailable")
    assert check.as_dict() == {"status": "unavailable"}


def test_endpoint_classification_never_returns_the_endpoint() -> None:
    assert classify_endpoint(normalize_mcp_url("http://127.0.0.1:8787")) == "loopback"
    assert (
        classify_endpoint(normalize_mcp_url("https://owner.example.ts.net"))
        == "tailnet"
    )
    assert classify_endpoint(normalize_mcp_url("https://mcp.example.com")) == "public"


def test_support_report_serialization_is_value_free_and_deterministic(
    tmp_path: Path,
) -> None:
    report = build_support_report(
        identity=DeploymentIdentity(
            version="0.9.1",
            commit="a" * 40,
            requested_revision="private tag",
            source="pep610",
        ),
        platform_name="darwin",
        python_version="3.12.11",
        service="active",
        endpoint_class="public",
        cloud=CloudCheck("ok", (("records", 3), ("tasks", 2))),
        operation_states=(("v2.applied", 2), ("v2.pending", 1)),
    )

    first = report.to_json()
    second = report.to_json()

    assert first == second
    assert json.loads(first) == {
        "cloud_check": {"counts": {"records": 3, "tasks": 2}, "status": "ok"},
        "commit": "a" * 40,
        "endpoint_class": "public",
        "operation_states": {"v2.applied": 2, "v2.pending": 1},
        "platform": "darwin",
        "python": "3.12.11",
        "service_status": "active",
        "tool_contract_hash": report.tool_contract_hash,
        "tool_schema_hash": report.tool_schema_hash,
        "version": "0.9.1",
    }
    assert "private tag" not in first
    assert str(tmp_path) not in first
    assert "http" not in first
    assert isinstance(report, SupportReport)
