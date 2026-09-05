from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from mcp.types import Tool

from things_orchestrator.client_bundle import (
    RECEIVER_INSTRUCTION_PATH,
    BundleError,
    PackageIdentity,
    bundle_file,
    encode_client_bundle_from,
    parse_client_bundle,
)
from things_orchestrator.client_sync import (
    MARKER_NAME,
    MAX_BUNDLE_BYTES,
    PENDING_NAME,
    ClientSyncError,
    fetch_client_bundle,
    resolve_client_token,
    run_client_sync,
)
from things_orchestrator.config import ConfigError, McpBearer, normalize_mcp_url
from things_orchestrator.tools import (
    advertised_tools,
    content_sha256,
    tool_discovery_hash,
)

URL = normalize_mcp_url("https://mcp.example.com")
BEARER = McpBearer("unit-test-token")
CLOSED_V0105 = Path(__file__).parents[1] / "tests/fixtures/v0.10.5-closed-public-result.schema.json"
WEEKLY = "references/routine-weekly-review.md"
ENRICHMENT = "references/routine-enrichment.md"


def _rechecksum(payload: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "bundle_checksum"}
    blob = json.dumps(
        unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    payload["bundle_checksum"] = content_sha256(blob)
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _sync(
    directory: Path,
    raw: bytes,
    *,
    observed_tools: Path | None = None,
    apply_interrupt: Callable[[str], None] | None = None,
) -> object:
    return run_client_sync(
        url=URL,
        directory=directory,
        bearer=BEARER,
        observed_tools=observed_tools,
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: advertised_tools(),
        apply_interrupt=apply_interrupt,
    )


def _write_observed(path: Path, tools: tuple[Tool, ...]) -> Path:
    path.write_text(
        json.dumps(
            {
                "tools": [
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for tool in tools
                ]
            }
        )
    )
    return path


def _bundle(
    *,
    version: str = "0.10.5",
    commit: str | None = "a" * 40,
    files: dict[str, str] | None = None,
    tools: tuple[Tool, ...] | None = None,
) -> bytes:
    contents = {
        "SKILL.md": "# skill\n",
        RECEIVER_INSTRUCTION_PATH: "receiver instruction\n",
    }
    if files:
        contents.update(files)
    packed = tuple(
        bundle_file(path, content) for path, content in sorted(contents.items())
    )
    return encode_client_bundle_from(
        package=PackageIdentity("things-orchestrator", version, commit),
        tools=tools or advertised_tools(),
        files=packed,
    )


def test_client_sync_does_not_require_cloud_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    raw = _bundle()
    tools = advertised_tools()
    directory = tmp_path / "skill"
    report = run_client_sync(
        url=URL,
        directory=directory,
        bearer=BEARER,
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: tools,
    )
    assert (tmp_path / "config" / "things-orchestrator" / "credentials.json").exists() is False
    assert report.managed_files["status"] == "synced"
    assert (directory / "SKILL.md").read_text() == "# skill\n"
    assert report.client_cache["status"] == "unknown"
    assert "Export this client's tools/list JSON" in report.client_cache["note"]
    dumped = json.dumps(report.as_dict())
    assert "unit-test-token" not in dumped
    assert BEARER.reveal() not in dumped


def test_client_sync_is_idempotent_and_follows_host_rollback(tmp_path: Path) -> None:
    tools = advertised_tools()
    directory = tmp_path / "skill"
    newer = _bundle(version="0.10.5", files={"SKILL.md": "# newer\n"})
    older = _bundle(version="0.10.4", files={"SKILL.md": "# older\n"})

    def run(raw: bytes) -> object:
        return run_client_sync(
            url=URL,
            directory=directory,
            bearer=BEARER,
            fetch_bundle=lambda _url, _bearer: raw,
            discover=lambda _url, _bearer: tools,
        )

    first = run(newer)
    second = run(newer)
    assert first.managed_files["status"] == "synced"
    assert second.managed_files["status"] == "unchanged"
    assert (directory / "SKILL.md").read_text() == "# newer\n"
    rolled = run(older)
    assert rolled.managed_files["status"] == "synced"
    assert rolled.managed_files["host_version"] == "0.10.4"
    assert rolled.managed_files["previous_version"] == "0.10.5"
    assert (directory / "SKILL.md").read_text() == "# older\n"
    marker = json.loads((directory / MARKER_NAME).read_text())
    assert marker["package_version"] == "0.10.4"


def test_client_sync_preserves_customized_files(tmp_path: Path) -> None:
    tools = advertised_tools()
    directory = tmp_path / "skill"
    raw = _bundle()
    run_client_sync(
        url=URL,
        directory=directory,
        bearer=BEARER,
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: tools,
    )
    (directory / "SKILL.md").write_text("# edited by owner\n")
    with pytest.raises(ClientSyncError, match="customized file"):
        run_client_sync(
            url=URL,
            directory=directory,
            bearer=BEARER,
            fetch_bundle=lambda _url, _bearer: raw,
            discover=lambda _url, _bearer: tools,
        )
    assert (directory / "SKILL.md").read_text() == "# edited by owner\n"


def test_client_sync_refuses_unknown_files_symlinks_and_corrupt_hashes(
    tmp_path: Path,
) -> None:
    tools = advertised_tools()
    directory = tmp_path / "skill"
    directory.mkdir()
    (directory / "notes.txt").write_text("personal\n")
    raw = _bundle()
    with pytest.raises(ClientSyncError, match="unmanaged files"):
        run_client_sync(
            url=URL,
            directory=directory,
            bearer=BEARER,
            fetch_bundle=lambda _url, _bearer: raw,
            discover=lambda _url, _bearer: tools,
        )

    empty = tmp_path / "empty"
    run_client_sync(
        url=URL,
        directory=empty,
        bearer=BEARER,
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: tools,
    )
    (empty / "SKILL.md").unlink()
    (empty / "SKILL.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("escape\n")
    with pytest.raises(ClientSyncError, match="non-file path"):
        run_client_sync(
            url=URL,
            directory=empty,
            bearer=BEARER,
            fetch_bundle=lambda _url, _bearer: raw,
            discover=lambda _url, _bearer: tools,
        )

    payload = json.loads(raw)
    payload["files"][0]["sha256"] = "sha256:" + ("0" * 64)
    with pytest.raises(BundleError, match="checksum"):
        parse_client_bundle(json.dumps(payload).encode())

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / MARKER_NAME).write_text(
        json.dumps(
            {
                "package_name": "other-product",
                "package_version": "1.0.0",
                "files": {},
            }
        )
    )
    with pytest.raises(ClientSyncError, match="foreign"):
        run_client_sync(
            url=URL,
            directory=foreign,
            bearer=BEARER,
            fetch_bundle=lambda _url, _bearer: raw,
            discover=lambda _url, _bearer: tools,
        )


def test_client_sync_fails_closed_on_malformed_bundle(tmp_path: Path) -> None:
    directory = tmp_path / "skill"
    with pytest.raises(ClientSyncError, match="not JSON"):
        run_client_sync(
            url=URL,
            directory=directory,
            bearer=BEARER,
            fetch_bundle=lambda _url, _bearer: b"{not-json",
            discover=lambda _url, _bearer: advertised_tools(),
        )
    with pytest.raises(ClientSyncError, match="unknown or incomplete format"):
        run_client_sync(
            url=URL,
            directory=directory,
            bearer=BEARER,
            fetch_bundle=lambda _url, _bearer: b'{"format_version":99}',
            discover=lambda _url, _bearer: advertised_tools(),
        )
    assert directory.exists() is False or not any(directory.iterdir())


def test_observed_tools_stale_and_unknown_cache(tmp_path: Path) -> None:
    tools = advertised_tools()
    raw = _bundle()
    directory = tmp_path / "skill"
    observed = tmp_path / "observed.json"
    stale = [tool.model_copy(update={"description": "stale catalog"}) for tool in tools]
    observed.write_text(json.dumps({"tools": [tool.model_dump(mode="json", by_alias=True) for tool in stale]}))
    stale_report = run_client_sync(
        url=URL,
        directory=directory,
        bearer=BEARER,
        observed_tools=observed,
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: tools,
    )
    assert stale_report.client_cache["status"] == "stale"
    assert "not proof of the current connection" in stale_report.client_cache["note"]
    assert stale_report.client_cache["catalog_delta"] == "recommended_refresh"
    assert stale_report.required_actions == ()
    assert stale_report.recommended_actions
    matching = tmp_path / "matching.json"
    matching.write_text(
        json.dumps(
            {
                "tools": [
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for tool in tools
                ]
            }
        )
    )
    match_report = run_client_sync(
        url=URL,
        directory=directory,
        bearer=BEARER,
        observed_tools=matching,
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: tools,
    )
    assert match_report.client_cache["status"] == "matches_snapshot"
    assert "not proof of the current connection" in match_report.client_cache["note"]
    unknown = run_client_sync(
        url=URL,
        directory=directory,
        bearer=BEARER,
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: tools,
    )
    assert unknown.client_cache["status"] == "unknown"


def test_fresh_connection_read_failure_is_not_activation_success(tmp_path: Path) -> None:
    tools = advertised_tools()
    raw = _bundle()
    directory = tmp_path / "skill"

    def fail_read(_url: object, _bearer: object, _item_id: str) -> dict[str, object]:
        raise RuntimeError("unreachable")

    failed = run_client_sync(
        url=URL,
        directory=directory,
        bearer=BEARER,
        read_id="task:sample",
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: tools,
        read_item=fail_read,
    )
    assert failed.read["status"] == "failed"
    assert failed.read["source"] == "fresh_connection"
    assert "not treat this as activation success" in json.dumps(failed.as_dict())
    assert failed.required_actions

    not_ok = run_client_sync(
        url=URL,
        directory=directory,
        bearer=BEARER,
        read_id="task:sample",
        fetch_bundle=lambda _url, _bearer: raw,
        discover=lambda _url, _bearer: tools,
        read_item=lambda _url, _bearer, _item_id: {
            "state": "rejected",
            "code": "missing_target",
            "instruction": "missing",
            "next_action": "correct_request",
        },
    )
    assert not_ok.read["status"] == "not_ok"
    assert not_ok.read["source"] == "fresh_connection"
    assert "activation success" in not_ok.read["note"]


def test_token_comes_from_env_or_prompt_never_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THINGS_MCP_TOKEN", "from-env")
    assert resolve_client_token("THINGS_MCP_TOKEN").reveal() == "from-env"
    monkeypatch.delenv("THINGS_MCP_TOKEN")
    prompted = resolve_client_token(
        "THINGS_MCP_TOKEN",
        environ={},
        prompt=lambda _label: "from-prompt",
        tty=True,
    )
    assert prompted.reveal() == "from-prompt"
    with pytest.raises(ConfigError, match="private terminal"):
        resolve_client_token("THINGS_MCP_TOKEN", environ={}, tty=False)


def test_discovery_mismatch_does_not_write_files(tmp_path: Path) -> None:
    directory = tmp_path / "skill"
    raw = _bundle()
    other = advertised_tools()[0].model_copy(update={"description": "other"})
    mismatched = (other, *advertised_tools()[1:])
    assert tool_discovery_hash(mismatched) != tool_discovery_hash()
    with pytest.raises(ClientSyncError, match="does not match"):
        run_client_sync(
            url=URL,
            directory=directory,
            bearer=BEARER,
            fetch_bundle=lambda _url, _bearer: raw,
            discover=lambda _url, _bearer: mismatched,
        )
    assert not directory.exists() or not any(directory.iterdir())


def test_rollback_removes_unchanged_managed_files(tmp_path: Path) -> None:
    tools = advertised_tools()
    directory = tmp_path / "skill"
    matching = _write_observed(tmp_path / "matching.json", tools)
    full = _bundle(
        files={WEEKLY: "# weekly\n", ENRICHMENT: "# enrichment\n"}
    )
    reduced = _bundle(files={ENRICHMENT: "# enrichment\n"})
    _sync(directory, full, observed_tools=matching)
    assert (directory / WEEKLY).read_text() == "# weekly\n"
    rolled = _sync(directory, reduced, observed_tools=matching)
    assert rolled.managed_files["status"] == "synced"
    assert (directory / WEEKLY).exists() is False
    assert (directory / ENRICHMENT).read_text() == "# enrichment\n"
    assert WEEKLY in rolled.managed_files["changed"]["routine_templates"]


def test_rollback_refuses_edited_removed_file_before_mutation(tmp_path: Path) -> None:
    directory = tmp_path / "skill"
    full = _bundle(files={WEEKLY: "# weekly\n"})
    reduced = _bundle()
    _sync(directory, full)
    (directory / WEEKLY).write_text("# edited weekly\n")
    skill_before = (directory / "SKILL.md").read_text()
    with pytest.raises(ClientSyncError, match="refusing to remove edited file"):
        _sync(directory, reduced)
    assert (directory / WEEKLY).read_text() == "# edited weekly\n"
    assert (directory / "SKILL.md").read_text() == skill_before
    assert (directory / PENDING_NAME).exists() is False


def test_first_install_resume_after_injected_crash(tmp_path: Path) -> None:
    directory = tmp_path / "skill"
    raw = _bundle()

    def boom(stage: str) -> None:
        if stage == "before_marker":
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        _sync(directory, raw, apply_interrupt=boom)
    assert (directory / PENDING_NAME).is_file()
    assert (directory / MARKER_NAME).exists() is False
    assert (directory / "SKILL.md").is_file()
    resumed = _sync(directory, raw)
    assert resumed.managed_files["status"] in {"synced", "unchanged"}
    assert (directory / MARKER_NAME).is_file()
    assert (directory / PENDING_NAME).exists() is False
    assert (directory / "SKILL.md").read_text() == "# skill\n"


def test_named_template_change_recommends_even_when_catalog_matches(
    tmp_path: Path,
) -> None:
    tools = advertised_tools()
    directory = tmp_path / "skill"
    matching = _write_observed(tmp_path / "matching.json", tools)
    first = _bundle(files={WEEKLY: "# weekly v1\n", ENRICHMENT: "# enrichment\n"})
    second = _bundle(files={WEEKLY: "# weekly v2\n", ENRICHMENT: "# enrichment\n"})
    _sync(directory, first, observed_tools=matching)
    report = _sync(directory, second, observed_tools=matching)
    assert report.client_cache["status"] == "matches_snapshot"
    assert report.required_actions == ()
    assert report.managed_files["changed"]["skill"] is False
    assert report.managed_files["changed"]["routines_receiver"] is False
    assert report.managed_files["changed"]["routine_templates"] == [WEEKLY]
    assert any(WEEKLY in action for action in report.recommended_actions)
    assert not any("Things skill" in action for action in report.recommended_actions)


def test_version_only_host_update_does_not_ask_to_reapply_prompts(
    tmp_path: Path,
) -> None:
    tools = advertised_tools()
    directory = tmp_path / "skill"
    matching = _write_observed(tmp_path / "matching.json", tools)
    files = {WEEKLY: "# weekly\n"}
    first = _bundle(version="0.10.5", files=files)
    second = _bundle(version="0.10.6", files=files)
    _sync(directory, first, observed_tools=matching)
    report = _sync(directory, second, observed_tools=matching)
    assert report.managed_files["status"] == "unchanged"
    assert report.managed_files["host_version"] == "0.10.6"
    assert report.managed_files["changed"]["skill"] is False
    assert report.managed_files["changed"]["routine_templates"] == []
    dumped = json.dumps(report.as_dict())
    assert "Reapply" not in dumped
    assert "saved routine" not in dumped


def test_closed_output_snapshot_requires_refresh(tmp_path: Path) -> None:
    directory = tmp_path / "skill"
    closed_schema = json.loads(CLOSED_V0105.read_text())
    closed = tuple(
        tool.model_copy(update={"output_schema": closed_schema})
        for tool in advertised_tools()
    )
    observed = _write_observed(tmp_path / "closed.json", closed)
    report = _sync(directory, _bundle(), observed_tools=observed)
    assert report.client_cache["status"] == "stale"
    assert report.client_cache["catalog_delta"] == "required_refresh"
    assert report.required_actions
    assert "tools/list" in report.required_actions[0]


def test_http_bundle_fetch_rejects_overflow_while_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OverflowResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def iter_bytes(self, chunk_size: int = 65536) -> object:
            yield b"x" * 64
            yield b"y" * (MAX_BUNDLE_BYTES)

    class OverflowStream:
        def __enter__(self) -> OverflowResponse:
            return OverflowResponse()

        def __exit__(self, *args: object) -> bool:
            return False

    class OverflowClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            assert kwargs.get("follow_redirects") is False

        def __enter__(self) -> OverflowClient:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def stream(self, method: str, url: str) -> OverflowStream:
            assert method == "GET"
            assert url.endswith("/client/bundle")
            return OverflowStream()

    monkeypatch.setattr("things_orchestrator.client_sync.httpx2.Client", OverflowClient)
    with pytest.raises(ClientSyncError, match="size bound"):
        fetch_client_bundle(URL, BEARER)


def test_reserved_paths_malformed_structure_and_non_hex_commit_fail_closed(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "skill"
    raw = _bundle()
    payload = json.loads(raw.decode())
    payload["files"].append(
        {
            "path": ".things-orchestrator-staging/extra.md",
            "sha256": content_sha256(b"nope"),
            "content": "nope",
        }
    )
    reserved = _rechecksum(payload)
    with pytest.raises(ClientSyncError, match="unsafe"):
        _sync(directory, reserved)
    assert directory.exists() is False or not any(directory.iterdir())

    colliding = json.loads(_bundle(files={"references/form.md": "# form\n"}).decode())
    colliding["files"].append(
        {
            "path": "references",
            "sha256": content_sha256(b"dir"),
            "content": "dir",
        }
    )
    with pytest.raises(ClientSyncError, match="collides"):
        _sync(directory, _rechecksum(colliding))

    nested = json.loads(raw.decode())
    nested["advertised_tools"][0]["unexpected_nested"] = {"token": "x"}
    with pytest.raises(ClientSyncError, match="advertised tools are malformed"):
        _sync(directory, _rechecksum(nested))

    commit = json.loads(raw.decode())
    commit["package"]["commit"] = "g" * 40
    with pytest.raises(ClientSyncError, match="commit is malformed"):
        _sync(directory, _rechecksum(commit))


def test_managed_directory_symlink_is_rejected_before_resolve(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(ClientSyncError, match="symlink"):
        _sync(alias, _bundle())
    assert (real / "SKILL.md").exists() is False

    parent = tmp_path / "parent"
    parent.mkdir()
    linked = tmp_path / "linked-parent"
    linked.symlink_to(parent)
    report = _sync(linked / "skill", _bundle())
    assert Path(report.managed_files["directory"]) == (parent / "skill").resolve()
    assert (parent / "skill" / "SKILL.md").read_text() == "# skill\n"


@pytest.mark.parametrize("stage", ["after_pending", "before_marker"])
def test_interrupted_update_accepts_previous_and_pending_file_bytes(tmp_path: Path, stage: str) -> None:
    directory = tmp_path / "skill"
    _sync(directory, _bundle(files={"SKILL.md": "old skill"}))
    updated = _bundle(files={"SKILL.md": "new skill"})

    def interrupt(current: str) -> None:
        if current == stage:
            raise RuntimeError("interrupted update")

    with pytest.raises(RuntimeError, match="interrupted update"):
        _sync(directory, updated, apply_interrupt=interrupt)
    _sync(directory, updated)
    assert (directory / "SKILL.md").read_text() == "new skill"
    assert not (directory / PENDING_NAME).exists()


@pytest.mark.parametrize("change", ["input_type", "output_type", "output_enum", "removed_output"])
def test_unclassified_contract_changes_require_review(tmp_path: Path, change: str) -> None:
    observed = advertised_tools()
    live = tuple(tool.model_copy(deep=True) for tool in observed)
    if change == "input_type":
        live[0].input_schema["properties"]["view"]["type"] = "integer"
    elif change == "output_type":
        live[0].output_schema["properties"]["instruction"]["type"] = "integer"
    elif change == "output_enum":
        live[0].output_schema["properties"]["state"]["enum"].append("unknown_outcome")
    else:
        del live[0].output_schema["properties"]["items"]
    report = run_client_sync(
        url=URL, directory=tmp_path / "skill", bearer=BEARER,
        observed_tools=_write_observed(tmp_path / "observed.json", observed),
        fetch_bundle=lambda _url, _bearer: _bundle(tools=live),
        discover=lambda _url, _bearer: live,
    )
    assert report.client_cache["catalog_delta"] == "required_review"
    assert report.required_actions


def test_nested_additive_output_is_only_a_recommended_refresh(tmp_path: Path) -> None:
    observed = advertised_tools()
    live = tuple(tool.model_copy(deep=True) for tool in observed)
    item = live[0].output_schema["properties"]["items"]["items"]
    item["properties"]["new_metadata"] = {"type": "string"}
    item["required"].append("new_metadata")
    report = run_client_sync(
        url=URL, directory=tmp_path / "skill", bearer=BEARER,
        observed_tools=_write_observed(tmp_path / "observed.json", observed),
        fetch_bundle=lambda _url, _bearer: _bundle(tools=live),
        discover=lambda _url, _bearer: live,
    )
    assert report.client_cache["catalog_delta"] == "recommended_refresh"
    assert not report.required_actions


def test_managed_files_preserve_lf_bytes_on_text_translating_platforms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.write_text

    def windows_write(path: Path, data: str, *args: object, **kwargs: object) -> int:
        return original(path, data.replace("\n", "\r\n"), *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", windows_write)
    report = _sync(tmp_path / "skill", _bundle())
    assert report.managed_files["status"] == "synced"
    assert (tmp_path / "skill" / "SKILL.md").read_bytes() == b"# skill\n"


def test_same_directory_sync_has_one_writer(tmp_path: Path) -> None:
    directory = tmp_path / "skill"

    def nested(stage: str) -> None:
        if stage == "after_pending":
            with pytest.raises(ClientSyncError, match="Another client-sync"):
                _sync(directory, _bundle(files={"SKILL.md": "second writer"}))

    _sync(directory, _bundle(), apply_interrupt=nested)
    assert (directory / "SKILL.md").read_text() == "# skill\n"
    _sync(directory, _bundle(files={"SKILL.md": "next writer"}))
    assert (directory / "SKILL.md").read_text() == "next writer"


def test_base_skill_file_rename_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "skill"
    _sync(directory, _bundle(files={"references/old.md": "unchanged text"}))
    report = _sync(directory, _bundle(files={"references/new.md": "unchanged text"}))
    assert report.managed_files["changed"]["skill"] is True
    assert not (directory / "references/old.md").exists()
    assert (directory / "references/new.md").read_text() == "unchanged text"
    assert any("skill" in action.lower() for action in report.recommended_actions)


@pytest.mark.parametrize("fingerprint", ["tool_schema_hash", "tool_contract_hash"])
def test_inconsistent_declared_fingerprint_is_rejected(tmp_path: Path, fingerprint: str) -> None:
    payload = json.loads(_bundle())
    payload["fingerprints"][fingerprint] = "sha256:" + "0" * 24
    with pytest.raises(ClientSyncError, match="fingerprints"):
        _sync(tmp_path / "skill", _rechecksum(payload))
    assert not (tmp_path / "skill").exists()


@pytest.mark.parametrize("category", ["authentication", "reachability", "protocol"])
def test_read_errors_keep_safe_categories(tmp_path: Path, category: str) -> None:
    request = httpx2.Request("POST", URL.mcp)
    response = httpx2.Response(401, request=request)
    errors = {
        "authentication": httpx2.HTTPStatusError("private error", request=request, response=response),
        "reachability": httpx2.ConnectError("private error"),
        "protocol": ValueError("private error"),
    }

    def read(*_args: object) -> dict[str, object]:
        raise errors[category]

    report = run_client_sync(
        url=URL, directory=tmp_path / "skill", bearer=BEARER, read_id="task:test",
        fetch_bundle=lambda *_args: _bundle(), discover=lambda *_args: advertised_tools(),
        read_item=read,
    )
    assert report.read["category"] == category
    assert "private error" not in json.dumps(report.as_dict())


def test_client_only_startup_does_not_import_unix_host_modules(tmp_path: Path) -> None:
    packet = tmp_path / "bundle.json"
    packet.write_bytes(_bundle())
    script = r"""
import importlib.abc
import json
import sys
from pathlib import Path
class NoHostModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {"things_orchestrator.journal", "things_orchestrator.service", "things_orchestrator.routines_store", "things_orchestrator.v2", "things_orchestrator.deployment"}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoHostModules())
from things_orchestrator.entrypoint import main
try:
    main(["client-sync", "--help"])
except SystemExit as error:
    assert error.code == 0
from mcp.types import Tool
from things_orchestrator.client_bundle import parse_client_bundle
from things_orchestrator.tools import tool_discovery_hash
bundle = parse_client_bundle(Path(sys.argv[1]).read_bytes())
assert tool_discovery_hash(tuple(Tool.model_validate(t) for t in bundle.advertised_tools)) == bundle.fingerprints["tool_discovery_hash"]
assert "things_orchestrator.cli" not in sys.modules
assert "things_orchestrator.journal" not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script, str(packet)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
def test_sync_writes_inside_delegated_directory(tmp_path: Path) -> None:
    parent = tmp_path / "delegated"
    directory = parent / "skill"
    directory.mkdir(parents=True)
    parent.chmod(0o555)
    try:
        report = _sync(directory, _bundle())
        assert report.managed_files["status"] == "synced"
        assert (directory / "SKILL.md").read_text() == "# skill\n"
    finally:
        parent.chmod(0o755)


def test_client_command_reports_filesystem_failure_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import errno

    from things_orchestrator import client_sync

    raw = _bundle()
    monkeypatch.setenv("THINGS_MCP_TOKEN", BEARER.reveal())
    monkeypatch.setattr(client_sync, "fetch_client_bundle", lambda *_args: raw)
    monkeypatch.setattr(client_sync, "discover_tools", lambda *_args: advertised_tools())
    original = Path.replace

    def full_disk(path: Path, target: Path) -> Path:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "replace", full_disk)
    directory = tmp_path / "skill"
    arguments = ["--url", str(URL), "--directory", str(directory)]
    with pytest.raises(SystemExit) as result:
        client_sync.main(arguments)
    assert result.value.code == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["managed_files"]["status"] == "not_verified"
    assert report["required_actions"]
    assert "Traceback" not in output.err
    assert (directory / PENDING_NAME).is_file()
    monkeypatch.setattr(Path, "replace", original)
    client_sync.main(arguments)
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["managed_files"]["status"] == "synced"
    assert not (directory / PENDING_NAME).exists()
