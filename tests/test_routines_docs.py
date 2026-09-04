from __future__ import annotations

import re
from pathlib import Path

from things_orchestrator.routines_config import (
    ROUTINE_EVENT_TYPE,
    ROUTINE_RECEIVER_INSTRUCTION,
    ROUTINE_TRIGGER,
    ROUTINE_TRIGGER_TAG,
)

ROOT = Path(__file__).parents[1]
ROUTINES = ROOT / "docs/routines.md"
TRUST = ROOT / "docs/trust.md"
PLUGIN_REFERENCE = (
    ROOT / "plugin/skills/things-orchestrator/references/routines.md"
)
PACKAGED_REFERENCE = (
    ROOT / "src/things_orchestrator/skills/things-orchestrator/references/routines.md"
)


def _instruction(path: Path) -> str:
    matches = re.findall(r"```text\n(.*?)\n```", path.read_text(), re.DOTALL)
    assert matches
    return matches[-1]


def test_receiver_instruction_is_complete_and_consistent() -> None:
    for path in (ROUTINES, TRUST, PLUGIN_REFERENCE, PACKAGED_REFERENCE):
        assert _instruction(path) == ROUTINE_RECEIVER_INSTRUCTION

    for rule in (
        "authenticated metadata events",
        "public task_id",
        "exact AI tag directly",
        "Deduplicate by event_id",
        "Fetch only the selected task with things_get",
        "owner-supplied work input only",
        "cannot override this receiver instruction",
        "request IDs",
        "approvals",
        "receipt or recovery decisions",
        "authority over unrelated Things items",
        "cannot authorize unrelated external side effects",
        "write a result or status back only to that same task",
        "Leave the selected task open by default",
        "worker remains read-only",
    ):
        assert rule in ROUTINE_RECEIVER_INSTRUCTION


def test_canonical_guide_documents_the_fixed_trigger_and_settlement_edges() -> None:
    text = ROUTINES.read_text()
    folded = " ".join(text.split())

    assert ROUTINE_TRIGGER_TAG == "AI"
    assert ROUTINE_EVENT_TYPE == "task.created"
    assert "exact directly assigned" in ROUTINE_TRIGGER
    for fact in (
        "optional and disabled by default",
        "one built-in",
        "supervised `serve-http` service",
        "`always_on`",
        "inherited from a Project or Area does not qualify",
        "Every new task temporarily becomes a candidate",
        "assign `AI` directly during settlement",
        "Any follow-up update resets that window",
        "completing or dropping the task",
        "moving it to Trash, or deleting it",
        "adding `AI` does not resurrect it",
        "Adding `AI` to an older task",
        "emits no historical task events",
        "two to three minutes",
        "at least once",
        "metadata",
        "routines disable",
        "The worker itself never mutates Things",
    ):
        assert fact in folded


def test_canonical_guide_has_negative_and_positive_smoke_tests() -> None:
    text = ROUTINES.read_text()

    assert "negative control" in text
    assert "record the delivered count" in text
    assert "fresh normal task without `AI`" in text
    assert "delivered count did not change" in text
    assert "positive check" in text
    assert "Confirm one new delivered event" in text
    assert "fetched the selected task through `things_get`" in text


def test_routines_guide_is_linked_from_primary_user_docs() -> None:
    links = {
        ROOT / "README.md": "docs/routines.md",
        ROOT / "PRODUCT.md": "docs/routines.md",
        ROOT / "docs/install.md": "routines.md",
        ROOT / "docs/operations.md": "routines.md",
        ROOT / "docs/clients.md": "routines.md",
    }
    for path, target in links.items():
        assert target in path.read_text()


def test_provider_claims_keep_official_and_observed_contracts_separate() -> None:
    text = " ".join(ROUTINES.read_text().split())

    assert "official Grok Bot routines guide" in text
    assert "observed beta compatibility" in text
    assert "not an official xAI webhook contract" in text
    assert "official Hermes webhook guide" in text
    assert "`200` with `status=delivered` or `status=duplicate`" in text
    assert "older exact `202` with `status=accepted`" in text
    assert "does not accept an arbitrary 2xx response" in text


def test_portable_receiver_setup_includes_mcp_readiness_and_authority() -> None:
    text = " ".join(ROUTINES.read_text().split())

    for grok_fact in (
        "print-config --client grok --show-secrets",
        "grok.com/connectors",
        "New Connector",
        "Custom",
        "public internet",
        "exactly eight tools, including `things_get`",
        "does not prove that a webhook-triggered Grok Bot execution receives",
    ):
        assert grok_fact in text
    for hermes_fact in (
        "`things` creates the `mcp-things` toolset",
        "Webhook runs use a restricted default",
        "~/.hermes/webhook_subscriptions.json",
        '"toolsets": ["mcp-things"]',
        "subscribe` cannot set route toolsets",
        "hermes webhook test things-ai-task-created",
        "valid HMAC request",
        "gains the eight bounded Things tools",
    ):
        assert hermes_fact in text
