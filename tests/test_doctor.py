from __future__ import annotations

from dataclasses import replace
from functools import partial

import anyio
import pytest
from mcp.types import Implementation

from things_orchestrator.config import McpBearer, McpUrl, normalize_mcp_url
from things_orchestrator.deployment import DeploymentIdentity
from things_orchestrator.doctor import (
    DoctorFailure,
    DoctorUnavailable,
    TargetReceipt,
    curl_tool_count_command,
    probe_target,
    run_doctor,
    validate_target,
)
from things_orchestrator.tools import (
    advertised_tools,
    tool_contract_hash,
    tool_discovery_hash,
    tool_schema_hash,
)
from things_orchestrator.v2 import MODELS


def _receipt() -> TargetReceipt:
    return TargetReceipt(
        url=normalize_mcp_url("http://127.0.0.1:8787/mcp"),
        public_health={"ok": True},
        detailed_health={
            "ok": True,
            "version": "0.8.0",
            "commit": "a" * 40,
            "tool_schema_hash": tool_schema_hash(),
            "tool_contract_hash": tool_contract_hash(),
            "tool_discovery_hash": tool_discovery_hash(),
            "client_bundle": {"format_version": 1, "path": "/client/bundle"},
        },
        server_info=Implementation(name="things", version="0.8.0"),
        tool_names=tuple(MODELS),
        tools=advertised_tools(),
    )


def _identity() -> DeploymentIdentity:
    return DeploymentIdentity(
        version="0.8.0",
        commit="a" * 40,
    )


def test_validate_target_accepts_exact_public_health_identity_and_tools() -> None:
    validate_target(_receipt(), _identity())


@pytest.mark.parametrize(
    ("receipt", "message"),
    [
        (replace(_receipt(), public_health={"ok": True, "version": "0.8.0"}), "public /health"),
        (replace(_receipt(), tool_names=tuple(MODELS)[:-1]), "eight tools"),
        (
            replace(
                _receipt(),
                detailed_health={
                    **_receipt().detailed_health,
                    "tool_schema_hash": "sha256:wrong",
                },
            ),
            "schema hash",
        ),
        (
            replace(
                _receipt(),
                detailed_health={
                    **_receipt().detailed_health,
                    "tool_contract_hash": "sha256:wrong",
                },
            ),
            "contract hash",
        ),
        (
            replace(
                _receipt(),
                detailed_health={
                    **_receipt().detailed_health,
                    "tool_discovery_hash": "sha256:wrong",
                },
            ),
            "discovery hash",
        ),
        (
            replace(
                _receipt(),
                detailed_health={
                    **{key: value for key, value in _receipt().detailed_health.items() if key != "client_bundle"},
                },
            ),
            "client bundle",
        ),
        (
            replace(
                _receipt(),
                detailed_health={
                    **_receipt().detailed_health,
                    "commit": "b" * 40,
                },
            ),
            "stale",
        ),
    ],
)
def test_validate_target_rejects_protocol_and_deployment_drift(
    receipt: TargetReceipt, message: str
) -> None:
    with pytest.raises(DoctorFailure, match=message):
        validate_target(receipt, _identity())


def test_validate_target_rejects_tools_list_fingerprint_drift() -> None:
    advertised = advertised_tools()
    drifted = (
        advertised[0].model_copy(update={"description": "drifted catalog"}),
        *advertised[1:],
    )
    receipt = replace(
        _receipt(),
        tools=drifted,
        tool_names=tuple(tool.name for tool in drifted),
    )
    with pytest.raises(DoctorFailure, match="fingerprint"):
        validate_target(receipt, _identity())


def test_validate_target_rejects_unknown_local_commit() -> None:
    identity = replace(_identity(), commit=None)
    with pytest.raises(DoctorFailure, match="installed commit is unknown"):
        validate_target(_receipt(), identity)


def test_probe_clients_do_not_trust_env_or_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"ok": True}

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.append(kwargs)
            self._headers = kwargs.get("headers")

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def get(self, url: str) -> FakeResponse:
            if self._headers:
                raise RuntimeError("stop after capturing authenticated bounds")
            return FakeResponse()

    monkeypatch.setattr("things_orchestrator.doctor.httpx2.AsyncClient", FakeClient)

    with pytest.raises(DoctorFailure):
        anyio.run(
            probe_target,
            normalize_mcp_url("http://127.0.0.1:8787"),
            McpBearer("token"),
        )

    assert seen
    public = seen[0]
    assert not public.get("headers")
    for kwargs in seen:
        assert kwargs.get("follow_redirects") is False
        assert kwargs.get("trust_env") is False


def test_wait_retries_folded_loopback_and_not_hosted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    async def probe(url: McpUrl, _bearer: McpBearer) -> TargetReceipt:
        attempts.append(url.origin)
        if attempts.count(url.origin) == 1:
            raise DoctorUnavailable(f"{url}: origin unreachable (public /health)")
        return _receipt()

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("things_orchestrator.doctor.anyio.sleep", no_sleep)
    monkeypatch.setattr("things_orchestrator.doctor.installed_identity", _identity)
    monkeypatch.setattr(
        "things_orchestrator.doctor.validate_target", lambda *_args: None
    )

    anyio.run(
        partial(
            run_doctor,
            [McpUrl("http://LocalHost:8787")],
            McpBearer("token"),
            wait=True,
            probe=probe,
        )
    )
    assert attempts == ["http://LocalHost:8787", "http://LocalHost:8787"]

    attempts.clear()
    with pytest.raises(DoctorUnavailable):
        anyio.run(
            partial(
                run_doctor,
                [normalize_mcp_url("https://tasks.example.com")],
                McpBearer("token"),
                wait=True,
                probe=probe,
            )
        )
    assert attempts == ["https://tasks.example.com"]


def test_curl_command_uses_environment_bearer_and_returns_tool_count() -> None:
    command = curl_tool_count_command(normalize_mcp_url("https://tasks.example.com/mcp"))
    assert "$THINGS_MCP_TOKEN" in command
    assert "things/list" not in command
    assert '"method":"tools/list"' in command
    assert "jq" in command
    assert "| length" in command
    assert "keep-me" not in command


def test_curl_command_shell_quotes_its_url_even_if_a_caller_bypasses_parsing() -> None:
    command = curl_tool_count_command(McpUrl("https://$(id)"))
    assert "POST 'https://$(id)/mcp'" in command


def test_transport_failure_reports_origin_unreachable_without_exception_secrets() -> None:
    url = normalize_mcp_url("https://tasks.example.com")
    error = ConnectionError("Bearer secret-bearer refused for https://tasks.example.com")

    failure = DoctorFailure.from_transport(url, error, stage="public /health")

    assert isinstance(failure, DoctorUnavailable)
    assert str(failure) == "https://tasks.example.com/mcp: origin unreachable (public /health)"
    assert "secret-bearer" not in str(failure)


def test_transport_failure_omits_raw_exception_text_for_other_errors() -> None:
    url = normalize_mcp_url("https://tasks.example.com")
    error = RuntimeError("Authorization: Bearer secret-bearer")

    failure = DoctorFailure.from_transport(
        url, error, stage="authenticated /health or MCP"
    )

    assert type(failure) is DoctorFailure
    assert str(failure) == (
        "https://tasks.example.com/mcp: authenticated /health or MCP failed"
    )
    assert "secret-bearer" not in str(failure)
