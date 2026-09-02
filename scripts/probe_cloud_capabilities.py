"""Run an explicitly requested, read-only v2 diagnostic against Things Cloud.

Mutation proof belongs to deterministic Cloud fixtures. This script does not
automate the owner factor, create disposable records, or permanently delete
anything.
"""

from __future__ import annotations

import argparse
import json
import subprocess

from things_orchestrator.cloud import CloudClient, CloudLibrary
from things_orchestrator.config import load_credentials
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


def bare_uuid(public_id: str) -> str:
    return public_id.partition(":")[2]


def _unique_ids(values: list[str], *, source: str, view: str) -> set[str]:
    unique = set(values)
    if len(unique) != len(values):
        raise RuntimeError(f"duplicate {source} {view} IDs")
    return unique


def _view_ids(interface: ThingsV2, view: str) -> set[str]:
    result = interface.dispatch("things_view", {"view": view, "limit": 40})
    ids: list[str] = []
    while True:
        if result.state != "ok":
            raise RuntimeError(f"{view} read returned {result.state}")
        ids.extend(bare_uuid(item.id) for item in result.items)
        if result.cursor is None:
            return _unique_ids(ids, source="public", view=view.title())
        result = interface.dispatch(
            "things_view", {"cursor": result.cursor, "limit": 40}
        )


def _native_ids(view: str) -> set[str]:
    script = f'tell application "Things3" to get id of every to do of list "{view}"'
    completed = subprocess.run(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return _unique_ids(
        [
            value.strip()
            for value in completed.stdout.strip().split(",")
            if value.strip()
        ],
        source="native",
        view=view,
    )


def run(*, native_parity: bool = False) -> dict[str, object]:
    credentials = load_credentials()
    library = CloudLibrary(CloudClient(credentials.email, credentials.password))
    workspace = ThingsWorkspace(
        library,
        journal=MemoryJournal(),
        account_id=credentials.email,
    )
    interface = ThingsV2(workspace)
    today = interface.dispatch("things_view", {"view": "today", "limit": 1})
    tags = interface.dispatch("things_view", {"view": "tags", "limit": 1})
    output: dict[str, object] = {
        "version": 2,
        "mode": "read_only",
        "today_state": today.state,
        "tags_state": tags.state,
        "mutation_probe": "local Cloud fixtures only",
    }
    if native_parity:
        views: dict[str, object] = {}
        passed = True
        for view in ("Today", "Inbox"):
            native = _native_ids(view)
            public = _view_ids(interface, view.casefold())
            matches = native == public
            passed = passed and matches
            views[view.casefold()] = {
                "native_count": len(native),
                "public_count": len(public),
                "exact_id_match": matches,
            }
        output["native_parity"] = {
            "passed": passed,
            "views": views,
            "cloud_record_count": len(library.records),
            "loaded_index": library.client.loaded_index,
            "server_index": library.client.server_index,
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only-live-probe", action="store_true")
    parser.add_argument("--native-parity", action="store_true")
    args = parser.parse_args()
    if not args.read_only_live_probe:
        parser.error("live Cloud reads need --read-only-live-probe")
    output = run(native_parity=args.native_parity)
    print(json.dumps(output, indent=2, sort_keys=True))
    parity = output.get("native_parity")
    if isinstance(parity, dict) and not parity.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
