"""Run an explicitly requested, read-only v2 diagnostic against Things Cloud.

Mutation proof belongs to deterministic Cloud fixtures. This script does not
automate the owner factor, create disposable records, or permanently delete
anything.
"""

from __future__ import annotations

import argparse
import json

from things_orchestrator.cloud import CloudClient, CloudLibrary, load_credentials
from things_orchestrator.journal import MemoryJournal
from things_orchestrator.v2 import ThingsV2
from things_orchestrator.workspace import ThingsWorkspace

V2_CAPABILITY_KEYS = (
    "exact default eight",
    "bounded reads",
    "immutable private manifests",
    "signed host authorization",
    "receipt HMAC cursors",
    "content-minimized tombstones",
)


def run() -> dict[str, object]:
    email, password, _token = load_credentials()
    workspace = ThingsWorkspace(
        CloudLibrary(CloudClient(email, password)),
        journal=MemoryJournal(),
        account_id=email,
    )
    interface = ThingsV2(workspace)
    today = interface.dispatch("things_view", {"view": "today", "limit": 1})
    tags = interface.dispatch("things_view", {"view": "tags", "limit": 1})
    return {
        "version": 2,
        "mode": "read_only",
        "today_state": today.state,
        "tags_state": tags.state,
        "mutation_probe": "local Cloud fixtures only",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only-live-probe", action="store_true")
    args = parser.parse_args()
    if not args.read_only_live_probe:
        parser.error("live Cloud reads need --read-only-live-probe")
    print(json.dumps(run(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
