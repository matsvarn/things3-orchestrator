"""Things Cloud HTTP client. Unofficial sync protocol used by Things.app."""

from __future__ import annotations

import json
import os
import time
import zlib
from base64 import b64encode
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .library import (
    MAX_RECURRENCE_INSTANCE_COUNT,
    ApplyResult,
    ChecklistLine,
    Kind,
    MemoryLibrary,
    Record,
    Status,
    Write,
    _ChecklistMutation,
    _compile_mutation,
    _CreateMutation,
    _EditMutation,
    _LifecycleMutation,
    _Mutation,
    _MutationHandler,
    _RecurrenceMutation,
    _TagMutation,
    day_ts,
    from_ts,
    offset_from_remind,
    remind_from_offset,
)
from .recurrence import RecurrenceState

ENDPOINT = "https://cloud.culturedcode.com"
USER_AGENT = "ThingsMac/32209501"
CLIENT_INFO = {
    "dm": "MacBookPro18,3",
    "lr": "US",
    "nf": True,
    "nk": True,
    "nn": "ThingsMac",
    "nv": "32209501",
    "on": "macOS",
    "ov": "15.7.3",
    "pl": "en-US",
    "ul": "en-Latn-US",
}

_TASK_KINDS = {"Task7", "Task6", "Task4", "Task3", "Task"}
_AREA_KINDS = {"Area3", "Area2", "Area"}
_TAG_KINDS = {"Tag4", "Tag3", "Tag"}
_CHECKLIST_KINDS = {"ChecklistItem3", "ChecklistItem2", "ChecklistItem"}
_CACHE_VERSION = 10


class CloudError(RuntimeError):
    pass


def _note(text: str) -> dict[str, Any]:
    return {"_t": "tx", "ch": zlib.crc32(text.encode()) & 0xFFFFFFFF, "v": text, "t": 1}


def _empty_note() -> dict[str, Any]:
    return {"_t": "tx", "ch": 0, "v": "", "t": 1}


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _note_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        note_type = value.get("t")
        if note_type == 1:
            raw = value.get("v")
            return raw if isinstance(raw, str) else ""
        if note_type == 2:
            lines: list[str] = []
            for paragraph in value.get("ps") or []:
                if isinstance(paragraph, dict):
                    run = paragraph.get("r")
                    if isinstance(run, str) and run:
                        lines.append(
                            run.replace("\u2028", "\n").replace("\u2029", "\n")
                        )
            return "\n".join(lines)
    return ""


def _note_metadata(
    value: object,
) -> tuple[
    Literal["none", "legacy", "structured"],
    Literal["plain", "markdown", "rich"],
]:
    if value is None:
        return "none", "markdown"
    if isinstance(value, str):
        return "legacy", "plain"
    if isinstance(value, dict) and value.get("t") == 2:
        return "structured", "rich"
    return "structured", "markdown"


def _native_date(value: object) -> date | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return from_ts(value)
    except (OverflowError, OSError, ValueError):
        return None


def _native_datetime(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return (
            datetime.fromtimestamp(value, timezone.utc) if value > 0 else None
        )
    except (OverflowError, OSError, ValueError):
        return None


def _native_reminder(value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < 86_400
        or value % 60 != 0
    ):
        return None
    return remind_from_offset(value)


def _native_count(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_RECURRENCE_INSTANCE_COUNT
    ):
        return None
    return value


@dataclass
class Envelope:
    uuid: str
    action: int
    kind: str
    payload: dict[str, Any]

    def as_wire(self) -> dict[str, Any]:
        return {"t": self.action, "e": self.kind, "p": self.payload}


@dataclass(frozen=True)
class HistoryPage:
    events: list[dict[str, Any]]
    current: int
    groups: int
    end_size: int = 0
    latest_size: int = 0


class CloudClient:
    def __init__(self, email: str, password: str, *, endpoint: str = ENDPOINT) -> None:
        self.email = email
        self._password = password
        self.endpoint = endpoint.rstrip("/")
        self.history_id = ""
        self.server_index = 0
        self.loaded_index = 0
        self._last_write = 0.0

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        info = b64encode(
            json.dumps(CLIENT_INFO, separators=(",", ":")).encode()
        ).decode()
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Charset": "UTF-8",
            "Authorization": f"Password {quote(self._password, safe='')}",
            "Things-Client-Info": info,
            "Schema": "301",
            "App-Id": "com.culturedcode.ThingsMac",
            "App-Instance-Id": (
                "000000000000000000000000000000000000000000000000000000000000000"
                "-com.culturedcode.ThingsMac-"
                "000000000000000000000000000000000000000000000000000000000000000"
            ),
        }
        if write:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            headers["Content-Encoding"] = "UTF-8"
            headers["Push-Priority"] = "5"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ) -> Any:
        if body is not None:
            wait = 1.0 - (time.monotonic() - self._last_write)
            if wait > 0:
                time.sleep(wait)
            self._last_write = time.monotonic()
        url = f"{self.endpoint}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        request = Request(
            url,
            data=body,
            method=method,
            headers=self._headers(write=body is not None),
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
        except HTTPError as error:
            if error.code == 401:
                raise CloudError("Things Cloud credentials were rejected") from error
            if error.code in {408, 504}:
                raise CloudError("Things Cloud timed out") from error
            if error.code == 429 and retry:
                time.sleep(1)
                return self._request(method, path, query=query, body=body, retry=False)
            raise CloudError(f"Things Cloud HTTP {error.code}") from error
        except URLError as error:
            reason = error.reason
            timed_out = (
                isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()
            )
            if timed_out:
                raise CloudError("Things Cloud timed out") from error
            raise CloudError("Things Cloud is unreachable") from error
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as error:
            raise CloudError("Things Cloud response was unreadable") from error

    def verify(self) -> str:
        data = self._request("GET", f"/version/1/account/{quote(self.email, safe='')}")
        if not isinstance(data, dict) or not data.get("history-key"):
            raise CloudError("Things Cloud account has no history key")
        key = str(data["history-key"])
        if self.history_id and self.history_id != key:
            self.loaded_index = 0
            self.server_index = 0
        self.history_id = key
        return self.history_id

    def items(self, start_index: int, *, retried: bool = False) -> HistoryPage:
        if not self.history_id:
            self.verify()
        try:
            data = self._request(
                "GET",
                f"/version/1/history/{self.history_id}/items",
                query={"start-index": str(start_index)},
            )
        except CloudError as error:
            if retried or "HTTP 404" not in str(error):
                raise
            previous = self.history_id
            self.verify()
            retry_at = self.loaded_index if self.history_id != previous else start_index
            return self.items(retry_at, retried=True)
        if not isinstance(data, dict):
            raise CloudError("Things Cloud history was unreadable")
        current = int(data.get("current-item-index") or 0)
        raw_items = data.get("items") or []
        events: list[dict[str, Any]] = []
        groups = 0
        if isinstance(raw_items, list):
            groups = len(raw_items)
            for group in raw_items:
                if not isinstance(group, dict):
                    continue
                for uuid, item in group.items():
                    if isinstance(item, dict):
                        events.append({"uuid": uuid, **item})
        return HistoryPage(
            events=events,
            current=current,
            groups=groups,
            end_size=int(data.get("end-total-content-size") or 0),
            latest_size=int(data.get("latest-total-content-size") or 0),
        )

    def commit(self, envelopes: list[Envelope]) -> None:
        if not envelopes:
            return
        uuids = [item.uuid for item in envelopes]
        if len(uuids) != len(set(uuids)):
            raise CloudError("Cloud commits need unique envelope UUIDs")
        if not self.history_id:
            self.verify()
        ancestor = self.server_index
        body = json.dumps(
            {item.uuid: item.as_wire() for item in envelopes},
            separators=(",", ":"),
        ).encode()
        try:
            data = self._post_commit(body, ancestor)
        except CloudError as error:
            text = str(error)
            if "HTTP 404" in text:
                previous = self.history_id
                self.verify()
                if previous and self.history_id != previous:
                    raise
                try:
                    retry_ancestor = self.server_index
                    data = self._post_commit(body, retry_ancestor)
                except CloudError as inner:
                    if "timed out" not in str(inner):
                        raise
                    data = self._commit_after_timeout(envelopes, retry_ancestor)
            elif "timed out" in text:
                data = self._commit_after_timeout(envelopes, ancestor)
            else:
                raise
        if isinstance(data, dict) and "server-head-index" in data:
            self._adopt_head(int(data["server-head-index"]))

    def _adopt_head(self, current: int) -> None:
        if current > self.server_index:
            self.server_index = current

    def _post_commit(self, body: bytes, ancestor: int) -> Any:
        return self._request(
            "POST",
            f"/version/1/history/{self.history_id}/commit",
            query={"ancestor-index": str(ancestor), "_cnt": "1"},
            body=body,
        )

    def _commit_visible(self, wanted: list[Envelope], ancestor: int) -> int | None:
        found: dict[str, dict[str, Any]] = {}
        start = ancestor
        for _ in range(32):
            page = self.items(start)
            for event in page.events:
                uuid = str(event.get("uuid") or "")
                expected = next((item for item in wanted if item.uuid == uuid), None)
                if expected is not None and _event_matches_envelope(event, expected):
                    found[uuid] = event
            self._adopt_head(page.current)
            if len(found) == len(wanted):
                return page.current
            if page.groups == 0:
                return None
            start += page.groups
            if page.latest_size and page.end_size >= page.latest_size:
                return None
        return None

    def _commit_after_timeout(
        self,
        envelopes: list[Envelope],
        ancestor: int,
    ) -> Any:
        try:
            head = self._commit_visible(envelopes, ancestor)
        except CloudError as error:
            raise CloudError(
                "Things Cloud outcome is unknown; reconciliation failed"
            ) from error
        if head is not None:
            return {"server-head-index": head}
        raise CloudError(
            "Things Cloud outcome is unknown; reconcile the workspace before retrying"
        )


def _event_matches_envelope(event: dict[str, Any], envelope: Envelope) -> bool:
    raw = event.get("p")
    payload = raw if isinstance(raw, dict) else {}
    return (
        event.get("t") == envelope.action
        and event.get("e") == envelope.kind
        and all(
            key in payload and payload[key] == value
            for key, value in envelope.payload.items()
        )
    )


def fold_events(events: list[dict[str, Any]], *, library: MemoryLibrary) -> None:
    for event in events:
        kind = str(event.get("e") or "")
        if _unsupported_versioned_entity(kind):
            raise CloudError(f"unsupported Things Cloud entity: {kind}")

    checklists: list[dict[str, Any]] = []
    for event in events:
        kind = str(event.get("e") or "")
        if kind in _CHECKLIST_KINDS:
            checklists.append(event)
            continue
        uuid = str(event.get("uuid") or "")
        action = event.get("t")
        raw = event.get("p")
        payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if action == 2:
            deleted = library.records.get(uuid)
            if deleted is not None:
                for item in library.records.values():
                    if item.parent_uuid == uuid:
                        item.parent_uuid = None
                    if item.area_uuid == uuid:
                        item.area_uuid = None
                    if item.heading_uuid == uuid:
                        item.heading_uuid = None
            library.records.pop(uuid, None)
            library.tags.pop(uuid, None)
            library.tag_parents.pop(uuid, None)
            if kind in _TAG_KINDS:
                for item in library.records.values():
                    item.tag_uuids = [tag for tag in item.tag_uuids if tag != uuid]
                for tag, parents in library.tag_parents.items():
                    library.tag_parents[tag] = [
                        parent for parent in parents if parent != uuid
                    ]
            continue
        if kind in _AREA_KINDS:
            existing = library.records.get(uuid)
            title = str(payload.get("tt") or (existing.title if existing else ""))
            index = (
                int(payload["ix"])
                if payload.get("ix") is not None
                else (existing.sort_index if existing else 0)
            )
            item = existing or Record(uuid=uuid, kind="area", title=title)
            item.title = title
            item.entity = kind
            item.sort_index = index
            if "tg" in payload and isinstance(payload["tg"], list):
                item.tag_uuids = [str(tag) for tag in payload["tg"]]
            library.records[uuid] = item
            continue
        if kind in _TAG_KINDS:
            title = str(payload.get("tt") or library.tags.get(uuid) or "")
            library.tags[uuid] = title
            if "pn" in payload:
                raw_parents = payload.get("pn")
                if isinstance(raw_parents, list):
                    library.tag_parents[uuid] = [str(parent) for parent in raw_parents]
                elif raw_parents:
                    library.tag_parents[uuid] = [str(raw_parents)]
                else:
                    library.tag_parents[uuid] = []
            elif action == 0:
                library.tag_parents[uuid] = []
            continue
        if kind not in _TASK_KINDS:
            continue
        existing = library.records.get(uuid)
        if action == 0:
            item = Record(
                uuid=uuid,
                kind="task",
                title="",
                recurrence_instance_count_known=False,
                recurrence_paused_known=False,
                recurrence_generated_on_known=False,
            )
            if existing is not None:
                item.checklists = existing.checklists
        else:
            item = existing or Record(
                uuid=uuid,
                kind="task",
                title="",
                recurrence_instance_count_known=False,
                recurrence_paused_known=False,
                recurrence_generated_on_known=False,
            )
        item.entity = kind
        if "tt" in payload and payload["tt"] is not None:
            item.title = str(payload["tt"])
        if "nt" in payload:
            item.notes = _note_text(payload["nt"])
            item.notes_source, item.notes_format = _note_metadata(payload["nt"])
        if "tp" in payload and payload["tp"] is not None:
            type_code = int(payload["tp"])
            if type_code == 2:
                item.heading = True
                item.kind = "task"
            else:
                item.heading = False
                item.kind = "project" if type_code == 1 else "task"
        if "ss" in payload and payload["ss"] is not None:
            status = int(payload["ss"])
            item.status = (
                "done" if status == 3 else "dropped" if status == 2 else "open"
            )
        if "sp" in payload:
            item.completed_at = _native_datetime(payload.get("sp"))
        if "tr" in payload and payload["tr"] is not None:
            item.trashed = bool(payload["tr"])
        if "sr" in payload:
            item.start = _native_date(payload.get("sr"))
            if item.start is not None:
                item.someday = False
        if "dd" in payload:
            item.deadline = _native_date(payload.get("dd"))
        if "ato" in payload:
            item.remind = _native_reminder(payload.get("ato"))
        if "sb" in payload and payload["sb"] is not None:
            item.tonight = int(payload["sb"]) == 1
        if "ix" in payload and payload["ix"] is not None:
            item.sort_index = int(payload["ix"])
        if "ti" in payload and payload["ti"] is not None:
            item.today_index = int(payload["ti"])
        if "pr" in payload and isinstance(payload["pr"], list) and payload["pr"]:
            item.parent_uuid = str(payload["pr"][0])
            item.area_uuid = None
        elif "pr" in payload:
            item.parent_uuid = None
        if "ar" in payload and isinstance(payload["ar"], list) and payload["ar"]:
            item.area_uuid = str(payload["ar"][0])
            item.parent_uuid = None
            item.heading_uuid = None
        elif "ar" in payload:
            item.area_uuid = None
        if "st" in payload and payload["st"] is not None:
            state = int(payload["st"])
            item.inbox = _inbox_from_list_state(item, state)
            item.someday = state == 2 and item.start is None
        if "agr" in payload and isinstance(payload["agr"], list) and payload["agr"]:
            item.heading_uuid = str(payload["agr"][0])
        elif "agr" in payload:
            item.heading_uuid = None
        if "tg" in payload and isinstance(payload["tg"], list):
            item.tag_uuids = [str(tag) for tag in payload["tg"]]
        if "rr" in payload:
            item.recurrence = item.recurrence.fold_rule(payload.get("rr"))
        if "rt" in payload:
            item.recurrence = item.recurrence.fold_links(payload.get("rt"))
        if "rp" in payload:
            item.repeater = deepcopy(payload.get("rp"))
        if "icp" in payload:
            paused = payload.get("icp")
            if isinstance(paused, bool):
                item.recurrence = item.recurrence.fold_paused(paused)
                item.recurrence_paused_known = True
        if "icsd" in payload:
            item.recurrence_created_through = _native_date(payload.get("icsd"))
        if "icc" in payload and payload["icc"] is not None:
            count = _native_count(payload["icc"])
            if count is not None:
                item.recurrence_instance_count = count
                item.recurrence_instance_count_known = True
        if "acrd" in payload:
            item.recurrence_completed_on = _native_date(payload.get("acrd"))
        if "tir" in payload and item.recurrence.role == "template":
            item.recurrence_next_on = _native_date(payload.get("tir"))
        if "lt" in payload and isinstance(payload["lt"], bool):
            item.leavable = payload["lt"]
            if not item.leavable:
                item.recurrence_generated_on = None
                item.recurrence_generated_on_known = True
            elif action == 0 and item.recurrence.role == "instance":
                item.recurrence_generated_on = item.start
                item.recurrence_generated_on_known = item.start is not None
        library.records[uuid] = item
    for event in checklists:
        raw = event.get("p")
        _fold_checklist(
            str(event.get("uuid") or ""),
            event.get("t"),
            raw if isinstance(raw, dict) else {},
            library,
        )
    library.resolve_instance_types()


def _unsupported_versioned_entity(kind: str) -> bool:
    families = (
        ("ChecklistItem", _CHECKLIST_KINDS),
        ("Task", _TASK_KINDS),
        ("Area", _AREA_KINDS),
        ("Tag", _TAG_KINDS),
    )
    for prefix, supported in families:
        generation = kind.removeprefix(prefix)
        if generation != kind and generation.isdecimal():
            return kind not in supported
    return False


def _fold_checklist(
    uuid: str, action: object, payload: dict[str, Any], library: MemoryLibrary
) -> None:
    if action == 2:
        for parent in library.records.values():
            parent.checklists = [
                line for line in parent.checklists if line.uuid != uuid
            ]
        return
    existing: ChecklistLine | None = None
    host: Record | None = None
    for parent in library.records.values():
        for line in parent.checklists:
            if line.uuid == uuid:
                existing = line
                host = parent
                break
        if existing is not None:
            break
    raw_parents = payload.get("ts")
    if raw_parents is None:
        parents = [host.uuid] if host is not None else []
    elif isinstance(raw_parents, str):
        parents = [raw_parents]
    else:
        parents = [str(item) for item in raw_parents]
    title = existing.title if existing is not None else ""
    if "tt" in payload and payload["tt"] is not None:
        title = str(payload["tt"])
    status: Status = existing.status if existing is not None else "open"
    if payload.get("ss") is not None:
        status_code = int(payload["ss"])
        status = (
            "done" if status_code == 3 else "dropped" if status_code == 2 else "open"
        )
    index = existing.sort_index if existing is not None else 0
    if payload.get("ix") is not None:
        index = int(payload["ix"])
    line = ChecklistLine(uuid=uuid, title=title, status=status, sort_index=index)
    for parent in library.records.values():
        parent.checklists = [item for item in parent.checklists if item.uuid != uuid]
    for parent_id in parents:
        dest = library.records.get(str(parent_id))
        if dest is None:
            continue
        dest.checklists = [item for item in dest.checklists if item.uuid != uuid]
        dest.checklists.append(line)
        dest.checklists.sort(key=lambda item: (item.sort_index, item.uuid))


class CloudLibrary(MemoryLibrary):
    def __init__(self, client: CloudClient, *, cache: Path | None = None) -> None:
        super().__init__()
        self.client = client
        self._cache = cache if cache is not None else state_cache_path()
        self._synced_at = 0.0
        self._seen_history = ""

    def refresh(self, *, force: bool = False) -> None:
        self._pull(force=force)

    def _pull(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and self._synced_at and now - self._synced_at < 2:
            return
        if not self.client.history_id:
            self.client.verify()
            if not self._restore_cache(self.client.history_id):
                self.records.clear()
                self.tags.clear()
                self.tag_parents.clear()
                self.client.loaded_index = 0
            self._seen_history = self.client.history_id
        changed = False
        while True:
            page = self.client.items(self.client.loaded_index)
            if self._seen_history and self.client.history_id != self._seen_history:
                self.records.clear()
                self.tags.clear()
                self.tag_parents.clear()
                changed = True
            self._seen_history = self.client.history_id
            if page.groups == 0:
                if page.current > self.client.server_index:
                    self.client.server_index = page.current
                    changed = True
                break
            fold_events(page.events, library=self)
            self.client.loaded_index += page.groups
            if page.current > self.client.server_index:
                self.client.server_index = page.current
            changed = True
            self._save_cache()
            if page.latest_size and page.end_size >= page.latest_size:
                break
        self._synced_at = time.monotonic()
        if changed or not self._cache.is_file():
            self._save_cache()

    def apply(self, writes: list[Write]) -> ApplyResult:
        self._pull(force=False)
        try:
            envelopes, _ = self._plan(writes)
            self.client.commit(envelopes)
        except CloudError as error:
            if "HTTP 409" not in str(error):
                raise
            self._pull(force=True)
            raise CloudError("Things Cloud conflict; read fresh facts") from error
        try:
            self._pull(force=True)
        except CloudError as error:
            raise CloudError(
                "Things Cloud outcome is unknown; commit read-back failed"
            ) from error
        if not all(self._pulled_matches(item) for item in envelopes):
            raise CloudError(
                "Things Cloud read-back did not match the requested changes"
            )
        verified = self._verified_titles(writes)
        return ApplyResult(
            verified=verified,
            created=self._created_from_pull(writes),
            read_back_verified=True,
        )

    def matches(self, writes: list[Write]) -> bool:
        repeat_next = [write for write in writes if write.action == "repeat_next"]
        if repeat_next and not super().matches(repeat_next):
            return False
        envelope_writes = [write for write in writes if write.action != "repeat_next"]
        if not envelope_writes:
            return True
        try:
            envelopes, _ = self._plan(envelope_writes)
        except CloudError:
            return False
        dynamic_indexes = {
            write.uuid
            for write in envelope_writes
            if write.action in {"create", "create_heading"}
            and write.kind in {"task", "project"}
            and write.sort_index is None
        }
        for envelope in envelopes:
            if envelope.uuid not in dynamic_indexes:
                if not self._pulled_matches(envelope):
                    return False
                continue
            item = self.records.get(envelope.uuid)
            if item is None or item.sort_index <= 0:
                return False
            payload = dict(envelope.payload)
            payload.pop("ix", None)
            if not self._pulled_matches(replace(envelope, payload=payload)):
                return False
        return True

    def _plan(self, writes: list[Write]) -> tuple[list[Envelope], dict[str, str]]:
        return _CloudPlanHandler(self).plan(writes)

    def _envelope(self, write: Write) -> Envelope:
        return _compile_mutation(write).dispatch(_CloudEnvelopeHandler(self))

    def _pulled_matches(self, envelope: Envelope) -> bool:
        if envelope.kind in _CHECKLIST_KINDS:
            parent, line = self._find_checklist(envelope.uuid)
            if envelope.action == 2:
                return line is None
            if line is None or parent is None:
                return False
            payload = envelope.payload
            return (
                ("tt" not in payload or line.title == payload["tt"])
                and (
                    "ss" not in payload
                    or line.status == _status_from_code(payload["ss"])
                )
                and ("ix" not in payload or line.sort_index == payload["ix"])
                and ("ts" not in payload or [parent.uuid] == payload["ts"])
            )
        if envelope.kind in _TAG_KINDS:
            if envelope.action == 2:
                return envelope.uuid not in self.tags
            return (
                "tt" not in envelope.payload
                or self.tags.get(envelope.uuid) == envelope.payload["tt"]
            ) and (
                "pn" not in envelope.payload
                or self.tag_parents.get(envelope.uuid, []) == envelope.payload["pn"]
            )
        item = self.records.get(envelope.uuid)
        if envelope.action == 2:
            return item is None
        return item is not None and _record_matches_payload(item, envelope.payload)

    def _verified_titles(self, writes: list[Write]) -> list[str]:
        verified: list[str] = []
        for write in writes:
            if write.action == "ensure_tag":
                continue
            if write.action == "checklist":
                _, line = self._find_checklist(write.uuid)
                if write.checklist_remove:
                    verified.append(write.title or write.uuid)
                elif line is not None:
                    verified.append(line.title)
                continue
            item = self.records.get(write.uuid)
            if item is not None:
                verified.append(item.title)
            elif write.title:
                verified.append(write.title)
        return list(dict.fromkeys(verified))

    def _created_from_pull(self, writes: list[Write]) -> dict[str, str]:
        created: dict[str, str] = {}
        for write in writes:
            if write.action == "ensure_tag":
                title = write.title or ""
                uuid = self.tag_uuid(title)
                if uuid is not None:
                    created[title or uuid] = uuid
            elif write.action in {"create", "create_heading"}:
                item = self.records.get(write.uuid)
                if item is not None:
                    created[item.title] = item.id
        return created

    def _restore_cache(self, history_id: str) -> bool:
        if not self._cache.is_file():
            return False
        try:
            payload = json.loads(self._cache.read_text())
            if not isinstance(payload, dict):
                return False
        except (OSError, json.JSONDecodeError):
            return False
        if (
            payload.get("version") != _CACHE_VERSION
            or payload.get("history_id") != history_id
        ):
            return False
        try:
            raw_records = payload.get("records") or []
            raw_tags = payload.get("tags") or {}
            raw_parents = payload.get("tag_parents") or {}
            if (
                not isinstance(raw_records, list)
                or not isinstance(raw_tags, dict)
                or not isinstance(raw_parents, dict)
            ):
                return False
            records = {
                str(item["uuid"]): _record_from_json(item)
                for item in raw_records
                if isinstance(item, dict)
            }
            if len(records) != len(raw_records):
                return False
            tags = {str(key): str(value) for key, value in raw_tags.items()}
            tag_parents = {
                str(key): [str(parent) for parent in value]
                for key, value in raw_parents.items()
                if isinstance(value, list)
            }
            if len(tag_parents) != len(raw_parents):
                return False
            loaded_index = int(payload.get("loaded_index") or 0)
            server_index = int(payload.get("server_index") or 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        self.records = records
        self.tags = tags
        self.tag_parents = tag_parents
        self.client.loaded_index = loaded_index
        self.client.server_index = server_index
        self._seen_history = history_id
        return True

    def _save_cache(self) -> None:
        payload = {
            "version": _CACHE_VERSION,
            "history_id": self.client.history_id,
            "loaded_index": self.client.loaded_index,
            "server_index": self.client.server_index,
            "records": [_record_to_json(item) for item in self.records.values()],
            "tags": self.tags,
            "tag_parents": self.tag_parents,
        }
        _atomic_write(self._cache, json.dumps(payload, separators=(",", ":")))


def _coalesce_envelopes(envelopes: list[Envelope]) -> list[Envelope]:
    coalesced: dict[str, Envelope] = {}
    for envelope in envelopes:
        previous = coalesced.get(envelope.uuid)
        if previous is None:
            coalesced[envelope.uuid] = envelope
            continue
        if previous.kind != envelope.kind:
            raise CloudError(f"Conflicting entity kinds for {envelope.uuid}")
        if previous.action == 2 or envelope.action == 2:
            raise CloudError(f"Conflicting delete for {envelope.uuid}")
        if previous.action == 1 and envelope.action == 0:
            raise CloudError(f"Create must be the first change for {envelope.uuid}")
        if previous.action == 0 and envelope.action == 0:
            raise CloudError(f"Duplicate create for {envelope.uuid}")
        coalesced[envelope.uuid] = Envelope(
            uuid=envelope.uuid,
            action=previous.action,
            kind=previous.kind,
            payload={**previous.payload, **envelope.payload},
        )
    return list(coalesced.values())


def _status_code(status: Status) -> int:
    return 3 if status == "done" else 2 if status == "dropped" else 0


def _status_from_code(value: object) -> Status:
    code = int(value) if isinstance(value, (int, str)) else 0
    return "done" if code == 3 else "dropped" if code == 2 else "open"


def _record_matches_payload(item: Record, payload: dict[str, Any]) -> bool:
    checks = [
        "tt" not in payload or item.title == str(payload["tt"] or ""),
        "nt" not in payload or item.notes == _note_text(payload["nt"]),
        "ss" not in payload or item.status == _status_from_code(payload["ss"]),
        "sp" not in payload
        or (item.completed_at is not None) == (payload["sp"] is not None),
        "sr" not in payload or item.start == from_ts(payload["sr"]),
        "dd" not in payload or item.deadline == from_ts(payload["dd"]),
        "ato" not in payload or item.remind == remind_from_offset(payload["ato"]),
        "sb" not in payload or item.tonight == (int(payload["sb"] or 0) == 1),
        "pr" not in payload
        or item.parent_uuid == (str(payload["pr"][0]) if payload["pr"] else None),
        not payload.get("pr") or not item.inbox,
        "ar" not in payload
        or item.area_uuid == (str(payload["ar"][0]) if payload["ar"] else None),
        not payload.get("ar") or not item.inbox,
        "agr" not in payload
        or item.heading_uuid == (str(payload["agr"][0]) if payload["agr"] else None),
        "tg" not in payload or item.tag_uuids == [str(tag) for tag in payload["tg"]],
        "ix" not in payload or item.sort_index == int(payload["ix"] or 0),
        "ti" not in payload or item.today_index == int(payload["ti"] or 0),
        "tr" not in payload or item.trashed == bool(payload["tr"]),
        "rr" not in payload or item.recurrence.rule == payload["rr"],
        "rt" not in payload
        or list(item.recurrence.links) == [str(link) for link in payload["rt"]],
        "rp" not in payload or item.repeater == payload["rp"],
        "icp" not in payload or item.recurrence.paused == bool(payload["icp"]),
        "icsd" not in payload
        or item.recurrence_created_through == from_ts(payload.get("icsd")),
        "icc" not in payload
        or item.recurrence_instance_count == int(payload.get("icc") or 0),
        "acrd" not in payload
        or item.recurrence_completed_on == from_ts(payload.get("acrd")),
        "tir" not in payload
        or payload.get("acrd") is None
        or item.recurrence_next_on == from_ts(payload.get("tir")),
        "lt" not in payload or item.leavable == bool(payload.get("lt")),
    ]
    if "tp" in payload:
        expected_kind: Kind = "project" if payload["tp"] == 1 else "task"
        checks.append(item.kind == expected_kind)
        checks.append(item.heading == (payload["tp"] == 2))
    if "st" in payload:
        state = int(payload["st"])
        checks.append(item.inbox == _inbox_from_list_state(item, state))
        checks.append(item.someday == (state == 2 and item.start is None))
    return all(checks)


class _CloudPlanHandler(_MutationHandler[None]):
    def __init__(self, library: CloudLibrary) -> None:
        self.library = library
        self.tag_map: dict[str, str] = {}
        self.envelopes: list[Envelope] = []
        self.created: dict[str, str] = {}
        self.created_ix: dict[tuple[str, str | None, str | None], int] = {}
        self.created_kinds: dict[str, Kind] = {}
        self.created_headings: dict[str, str | None] = {}
        self.project_heading_moves: dict[str, str] = {}
        self.deleted_tags: set[str] = set()

    def plan(self, writes: list[Write]) -> tuple[list[Envelope], dict[str, str]]:
        self.created_kinds = {
            item.uuid: item.kind
            for item in writes
            if item.action in {"create", "create_heading"}
        }
        self.created_headings = {
            item.uuid: item.into_uuid
            for item in writes
            if item.action == "create_heading"
        }
        self.deleted_tags = {
            item.uuid for item in writes if item.action == "delete_tag"
        }
        # A Project merge can move a heading and its assigned Tasks in one
        # batch. Project the heading destination before validating any Task
        # envelope, so planning does not depend on input order.
        self.project_heading_moves = {}
        for write in writes:
            if write.into_kind != "project" or not write.into_uuid:
                continue
            current = self.library.records.get(write.uuid)
            if current is not None and current.heading:
                self.project_heading_moves[current.uuid] = write.into_uuid
        for write in writes:
            mutation = _compile_mutation(write)
            mutation = self._prepare(mutation)
            mutation.dispatch(self)
        envelopes = _coalesce_envelopes(self.envelopes)
        uuids = [item.uuid for item in envelopes]
        if len(uuids) != len(set(uuids)):
            raise CloudError("planned envelope UUIDs must be unique")
        return envelopes, self.created

    def _prepare(self, mutation: _Mutation) -> _Mutation:
        write = mutation.write
        current = self.library.records.get(write.uuid)
        planned_kind = self.created_kinds.get(write.uuid)
        if (
            planned_kind is not None
            and write.action not in {"create", "create_heading"}
            and write.kind != planned_kind
        ):
            write = replace(write, kind=planned_kind)
            mutation = replace(mutation, write=write)
        actual_kind = (
            current.kind if current is not None else planned_kind or write.kind
        )
        if actual_kind in {"task", "project"} and (
            write.sort_index is not None and write.sort_index <= 0
        ):
            # Things traps while it applies incremental Cloud history when a
            # Task6 record has a non-positive ix. Keep first position positive.
            write = replace(write, sort_index=1)
            mutation = replace(mutation, write=write)
        if actual_kind == "project" and write.inbox:
            raise CloudError("Projects cannot enter Inbox")
        if write.heading_uuid is not None:
            heading = self.library.records.get(write.heading_uuid)
            project_uuid = (
                write.into_uuid
                or (current.parent_uuid if current else None)
                or self.created_headings.get(write.heading_uuid)
                or (heading.parent_uuid if heading is not None else None)
            )
            existing = (
                heading is not None
                and heading.heading
                and bool(project_uuid)
                and heading.parent_uuid == project_uuid
            )
            projected = (
                heading is not None
                and heading.heading
                and bool(project_uuid)
                and self.project_heading_moves.get(heading.uuid) == project_uuid
            )
            planned = (
                bool(project_uuid)
                and self.created_headings.get(write.heading_uuid) == project_uuid
            )
            if not existing and not projected and not planned:
                raise CloudError("The heading must belong to the destination Project")
        return mutation

    def _emit(self, write: Write) -> None:
        self.envelopes.append(self.library._envelope(write))  # noqa: SLF001

    def _normalize_item_tags(self, write: Write) -> Write:
        if not write.tag_uuids:
            return write
        return replace(
            write,
            tag_uuids=[self.tag_map.get(tag, tag) for tag in write.tag_uuids],
        )

    def create(self, mutation: _CreateMutation) -> None:
        write = mutation.write
        write = self._normalize_item_tags(write)
        if write.sort_index is None:
            index = self.library.next_index(write)
            key = (write.kind, write.into_uuid, write.into_kind)
            if key in self.created_ix:
                index = self.created_ix[key] + 1024
            if write.kind in {"task", "project"}:
                index = max(1024, index)
            self.created_ix[key] = index
            write = replace(write, sort_index=index)
        self._emit(write)
        if write.title:
            self.created[write.title] = f"{write.kind}:{write.uuid}"

    def edit(self, mutation: _EditMutation) -> None:
        self._emit(self._normalize_item_tags(mutation.write))

    def lifecycle(self, mutation: _LifecycleMutation) -> None:
        self._emit(mutation.write)

    def tag(self, mutation: _TagMutation) -> None:
        write = mutation.write
        if write.action == "ensure_tag":
            title = write.title or ""
            existing = self.library.tag_uuid(title)
            if existing in self.deleted_tags:
                existing = None
            parents = [
                self.tag_map.get(parent, parent)
                for parent in (write.tag_parent_uuids or [])
            ]
            if existing is None:
                self.envelopes.append(
                    Envelope(
                        uuid=write.uuid,
                        action=0,
                        kind="Tag4",
                        payload={
                            "tt": title,
                            "ix": 0,
                            "sh": None,
                            "pn": parents,
                            "xx": {"sn": {}, "_t": "oo"},
                        },
                    )
                )
                self.tag_map[write.uuid] = write.uuid
                self.created[title or write.uuid] = write.uuid
            else:
                self.tag_map[write.uuid] = existing
                if write.tag_parent_uuids is not None:
                    self.envelopes.append(
                        Envelope(
                            uuid=existing,
                            action=1,
                            kind="Tag4",
                            payload={"pn": parents, "md": _now()},
                        )
                    )
                self.created[title or existing] = existing
            return
        write = replace(
            write,
            uuid=self.tag_map.get(write.uuid, write.uuid),
            tag_parent_uuids=(
                [self.tag_map.get(parent, parent) for parent in write.tag_parent_uuids]
                if write.tag_parent_uuids is not None
                else None
            ),
        )
        self._emit(write)

    def checklist(self, mutation: _ChecklistMutation) -> None:
        write = mutation.write
        parent, _ = self.library._find_checklist(write.uuid)  # noqa: SLF001
        destination_uuid = write.checklist_parent_uuid or (
            parent.uuid if parent else None
        )
        destination = self.library.records.get(destination_uuid or "")
        destination_kind = (
            destination.kind
            if destination
            else self.created_kinds.get(destination_uuid or "")
        )
        if not write.checklist_remove and destination_kind != "task":
            raise CloudError("A checklist row needs a task parent")
        if not write.checklist_remove and write.checklist_index is None:
            siblings = destination.checklists if destination is not None else []
            write = replace(
                write,
                checklist_index=max((item.sort_index for item in siblings), default=-1)
                + 1,
            )
        self._emit(write)

    def recurrence(self, mutation: _RecurrenceMutation) -> None:
        write = mutation.write
        current = self.library.records.get(write.uuid)
        if write.action == "repeat":
            if current is None or (
                write.recurrence_rule is None and not write.clear_recurrence_rule
                and write.recurrence_paused is None
            ):
                raise CloudError("Repeat changes need an exact repeating Task template")
            try:
                current.recurrence.validate_interval_template(kind=current.kind)
            except ValueError as error:
                raise CloudError(str(error)) from error
        if write.action == "repeat_progress" and (
            current is None
            or current.recurrence.role != "template"
            or current.recurrence.repeat_type != "after_completion"
            or write.recurrence_completed_on is None
            or write.recurrence_next_on is None
        ):
            raise CloudError(
                "Repeat progress needs an after-completion template and two dates"
            )
        if write.action == "repeat_next" and (
            current is None
            or current.recurrence.role != "template"
            or write.recurrence_instance_count is None
            or write.recurrence_instance_count
            != current.recurrence_instance_count + 1
        ):
            raise CloudError(
                "Create Next Copy needs the next count for an exact repeat template"
            )
        self._emit(write)


class _CloudEnvelopeHandler(_MutationHandler[Envelope]):
    def __init__(self, library: CloudLibrary) -> None:
        self.library = library

    def _entity(self, write: Write) -> str:
        existing = self.library.records.get(write.uuid)
        if write.kind == "area":
            return (existing.entity if existing and existing.entity else "") or "Area3"
        return "Task7"

    def create(self, mutation: _CreateMutation) -> Envelope:
        write = mutation.write
        payload = _create_payload(write)
        if mutation.heading:
            payload["tp"] = 2
        return Envelope(
            uuid=write.uuid,
            action=0,
            kind="Area3" if write.kind == "area" else "Task7",
            payload=payload,
        )

    def checklist(self, mutation: _ChecklistMutation) -> Envelope:
        write = mutation.write
        parent, existing = self.library._find_checklist(write.uuid)  # noqa: SLF001
        if write.checklist_remove:
            return Envelope(
                uuid=write.uuid, action=2, kind="ChecklistItem3", payload={}
            )
        status = write.checklist_status or (existing.status if existing else "open")
        index = (
            write.checklist_index
            if write.checklist_index is not None
            else write.sort_index
            if write.sort_index is not None
            else existing.sort_index
            if existing
            else 0
        )
        parent_uuid = write.checklist_parent_uuid or (parent.uuid if parent else None)
        now = _now()
        if existing is None:
            return Envelope(
                uuid=write.uuid,
                action=0,
                kind="ChecklistItem3",
                payload={
                    "tt": write.title or "",
                    "ss": _status_code(status),
                    "sp": now if status != "open" else None,
                    "ts": [parent_uuid] if parent_uuid else [],
                    "ix": index,
                    "cd": now,
                    "md": None,
                    "lt": False,
                    "xx": {"sn": {}, "_t": "oo"},
                },
            )
        payload: dict[str, Any] = {"md": now}
        if write.title is not None:
            payload["tt"] = write.title
        if write.checklist_status is not None:
            payload.update(
                {"ss": _status_code(status), "sp": now if status != "open" else None}
            )
        if write.checklist_parent_uuid is not None:
            payload["ts"] = [write.checklist_parent_uuid]
        if write.checklist_index is not None or write.sort_index is not None:
            payload["ix"] = index
        return Envelope(
            uuid=write.uuid, action=1, kind="ChecklistItem3", payload=payload
        )

    def lifecycle(self, mutation: _LifecycleMutation) -> Envelope:
        write = mutation.write
        entity = self._entity(write)
        if write.action in {"delete_area", "permanent_delete"}:
            return Envelope(uuid=write.uuid, action=2, kind=entity, payload={})
        if write.action in {"trash", "restore"}:
            return Envelope(
                uuid=write.uuid,
                action=1,
                kind=entity,
                payload={"tr": write.action == "trash", "md": _now()},
            )
        payload = {
            "md": _now(),
            "ss": 3 if write.action == "complete" else 2,
            "sp": _now(),
        }
        return Envelope(uuid=write.uuid, action=1, kind=entity, payload=payload)

    def tag(self, mutation: _TagMutation) -> Envelope:
        write = mutation.write
        if write.action == "rename_tag":
            if not write.title or not write.title.strip():
                raise CloudError("Tag rename needs a title")
            return Envelope(
                uuid=write.uuid,
                action=1,
                kind="Tag4",
                payload={"tt": write.title.strip(), "md": _now()},
            )
        if write.action == "reparent_tag":
            return Envelope(
                uuid=write.uuid,
                action=1,
                kind="Tag4",
                payload={"pn": list(write.tag_parent_uuids or []), "md": _now()},
            )
        if write.action == "delete_tag":
            return Envelope(uuid=write.uuid, action=2, kind="Tag4", payload={})
        raise CloudError("ensure_tag envelopes are planned by the tag handler")

    def recurrence(self, mutation: _RecurrenceMutation) -> Envelope:
        write = mutation.write
        payload: dict[str, Any] = {"md": _now()}
        if write.action == "repeat_progress":
            payload.update(
                {
                    "acrd": day_ts(write.recurrence_completed_on)
                    if write.recurrence_completed_on
                    else None,
                    "tir": day_ts(write.recurrence_next_on)
                    if write.recurrence_next_on
                    else None,
                }
            )
        elif write.action == "repeat_next":
            payload = {"icc": write.recurrence_instance_count}
        elif write.action != "repeat" or (
            write.recurrence_rule is not None or write.clear_recurrence_rule
        ):
            payload["rr" if write.action == "repeat" else "rt"] = (
                None
                if write.action == "repeat" and write.clear_recurrence_rule
                else deepcopy(
                    write.recurrence_rule
                    if write.action == "repeat"
                    else list(write.recurrence_links or [])
                )
            )
        if write.recurrence_paused is not None:
            payload["icp"] = write.recurrence_paused
        return Envelope(
            uuid=write.uuid, action=1, kind=self._entity(write), payload=payload
        )

    def edit(self, mutation: _EditMutation) -> Envelope:
        write = mutation.write
        entity = self._entity(write)
        if write.action == "rename_area":
            return Envelope(
                uuid=write.uuid,
                action=1,
                kind=entity,
                payload={"tt": write.title, "md": _now()},
            )
        payload: dict[str, Any] = {"md": _now()}
        if write.action == "tags" and write.tag_uuids is not None:
            payload["tg"] = write.tag_uuids
        elif write.action == "move":
            payload.update(_placement(write, self.library.records.get(write.uuid)))
        else:
            if write.status is not None:
                payload.update(
                    {
                        "ss": _status_code(write.status),
                        "sp": _now() if write.status != "open" else None,
                    }
                )
            if write.title is not None:
                payload["tt"] = write.title
            if write.notes is not None:
                payload["nt"] = _note(write.notes) if write.notes else _empty_note()
            if write.tag_uuids is not None:
                payload["tg"] = write.tag_uuids
            if write.sort_index is not None:
                payload["ix"] = write.sort_index
            if write.today_index is not None:
                payload["ti"] = write.today_index
            if write.someday:
                payload.update(
                    {
                        "st": 2,
                        "sr": None,
                        "tir": None,
                        "ato": None,
                        "rmd": None,
                        "sb": 0,
                    }
                )
            elif write.anytime:
                payload.update(
                    {
                        "st": 1,
                        "sr": None,
                        "tir": None,
                        "ato": None,
                        "rmd": None,
                        "sb": 0,
                    }
                )
            elif write.tonight:
                payload.update(
                    _schedule(
                        write.start
                        or write.owner_today
                        or datetime.now().astimezone().date(),
                        write.remind,
                        today=write.owner_today,
                    )
                )
                payload["sb"] = 1
            elif write.clear_start:
                payload.update(
                    {
                        "st": 1,
                        "sr": None,
                        "tir": None,
                        "ato": None,
                        "rmd": None,
                        "sb": 0,
                    }
                )
            elif write.start is not None:
                payload.update(
                    _schedule(write.start, write.remind, today=write.owner_today)
                )
                payload["sb"] = 1 if write.tonight else 0
            if write.clear_deadline:
                payload["dd"] = None
            elif write.deadline is not None:
                payload["dd"] = day_ts(write.deadline)
            if write.clear_remind:
                payload.update({"ato": None, "rmd": None})
            elif write.remind is not None and write.start is not None:
                payload.update(
                    _schedule(write.start, write.remind, today=write.owner_today)
                )
            if (
                write.into_uuid is not None
                or write.into_kind is not None
                or write.inbox
                or write.anytime
                or write.heading_uuid is not None
                or write.clear_heading
            ):
                payload.update(_placement(write, self.library.records.get(write.uuid)))
        return Envelope(uuid=write.uuid, action=1, kind=entity, payload=payload)


def _inbox_from_list_state(item: Record, state: int) -> bool:
    return state == 0 and item.kind != "project" and not item.heading


def _needs_anytime_list_state(write: Write, current: Record | None) -> bool:
    if (
        write.someday
        or write.start is not None
        or write.tonight
        or write.anytime
        or write.inbox
    ):
        return False
    return current is None or not (
        current.someday or current.start is not None or current.tonight
    )


def _placement(write: Write, current: Record | None = None) -> dict[str, Any]:
    payload: dict[str, Any]
    if (
        write.clear_heading
        and write.into_uuid is None
        and write.into_kind is None
        and not write.inbox
        and not write.anytime
    ):
        return {"agr": []}
    if write.into_kind == "project" and write.into_uuid:
        payload = {
            "pr": [write.into_uuid],
            "ar": [],
            "agr": [write.heading_uuid] if write.heading_uuid else [],
        }
    elif write.into_kind == "area" and write.into_uuid:
        payload = {"ar": [write.into_uuid], "pr": [], "agr": []}
    elif write.kind == "project" or write.anytime:
        return {
            "pr": [],
            "ar": [],
            "agr": [],
            "st": 1,
            "sb": 0,
            "sr": None,
            "tir": None,
            "ato": None,
            "rmd": None,
        }
    else:
        return {
            "pr": [],
            "ar": [],
            "agr": [],
            "st": 0,
            "sb": 0,
            "sr": None,
            "tir": None,
            "ato": None,
            "rmd": None,
        }
    if _needs_anytime_list_state(write, current):
        # Native Inbox is st=0 even when pr or ar is set.
        payload["st"] = 1
    return payload


def _schedule(
    start: date, remind: str | None, *, today: date | None = None
) -> dict[str, Any]:
    today = today or datetime.now().astimezone().date()
    stamp = day_ts(start)
    if start <= today:
        payload: dict[str, Any] = {"st": 1, "sr": stamp, "tir": stamp}
    else:
        payload = {"st": 2, "sr": stamp, "tir": stamp}
    if remind is not None:
        payload["rmd"] = stamp
        payload["ato"] = offset_from_remind(remind)
    return payload


def _create_payload(write: Write) -> dict[str, Any]:
    now = _now()
    if write.kind == "area":
        return {
            "tt": write.title,
            "ix": write.sort_index or 0,
            "tg": write.tag_uuids or [],
            "md": None,
            "xx": {"sn": {}, "_t": "oo"},
        }
    st = (
        1
        if write.kind == "project"
        or write.anytime
        or write.into_kind in {"project", "area"}
        else 0
    )
    sr = None
    tir = None
    pr: list[str] = []
    ar: list[str] = []
    if write.into_kind == "project" and write.into_uuid:
        pr = [write.into_uuid]
    elif write.into_kind == "area" and write.into_uuid:
        ar = [write.into_uuid]
    if write.someday:
        st = 2
    elif write.start is not None or write.tonight:
        st = 1
    schedule: dict[str, Any] = {}
    if write.start is not None:
        schedule = _schedule(write.start, write.remind, today=write.owner_today)
        st = int(schedule["st"])
        sr = schedule.get("sr")
        tir = schedule.get("tir")
    if write.tonight:
        st = 1
        if write.start is not None:
            sr = day_ts(write.start)
            tir = sr
    payload: dict[str, Any] = {
        "tp": 1 if write.kind == "project" else 0,
        "sr": sr,
        "dds": None,
        "rt": list(write.recurrence_links or []),
        "rmd": schedule.get("rmd"),
        "ss": _status_code(write.status or "open"),
        "tr": False,
        "dl": [],
        "icp": bool(write.recurrence_paused),
        "st": st,
        "ar": ar,
        "tt": write.title,
        "do": 0,
        "lai": None,
        "tir": tir,
        "tg": write.tag_uuids or [],
        "agr": [write.heading_uuid] if write.heading_uuid else [],
        "ix": write.sort_index or 0,
        "cd": now,
        "lt": write.leavable,
        "icc": write.recurrence_instance_count or 0,
        "md": None,
        "ti": write.today_index or 0,
        "dd": day_ts(write.deadline) if write.deadline else None,
        "ato": schedule.get("ato"),
        "nt": _note(write.notes) if write.notes else None,
        "icsd": day_ts(write.recurrence_created_through)
        if write.recurrence_created_through
        else None,
        "pr": pr,
        "rp": None,
        "acrd": None,
        "sp": now if write.status in {"done", "dropped"} else None,
        "sb": 1 if write.tonight else 0,
        "rr": deepcopy(write.recurrence_rule),
        "xx": {"sn": {}, "_t": "oo"},
    }
    return payload


def state_cache_path() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "things-orchestrator" / "state.json"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _atomic_write(path: Path, text: str) -> None:
    _ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)


def _record_to_json(item: Record) -> dict[str, Any]:
    return {
        "uuid": item.uuid,
        "kind": item.kind,
        "title": item.title,
        "notes": item.notes,
        "notes_source": item.notes_source,
        "notes_format": item.notes_format,
        "status": item.status,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "trashed": item.trashed,
        "inbox": item.inbox,
        "start": item.start.isoformat() if item.start else None,
        "deadline": item.deadline.isoformat() if item.deadline else None,
        "remind": item.remind,
        "tonight": item.tonight,
        "someday": item.someday,
        "parent_uuid": item.parent_uuid,
        "area_uuid": item.area_uuid,
        "heading_uuid": item.heading_uuid,
        "tag_uuids": item.tag_uuids,
        "recurrence_role": item.recurrence.role,
        "recurrence_type": item.recurrence.repeat_type,
        "recurrence_template_uuid": item.recurrence.template_uuid,
        "recurrence_rule": item.recurrence.rule,
        "recurrence_links": list(item.recurrence.links),
        "recurrence_paused": item.recurrence.paused,
        "recurrence_paused_known": item.recurrence_paused_known,
        "repeater": deepcopy(item.repeater),
        "recurrence_created_through": item.recurrence_created_through.isoformat()
        if item.recurrence_created_through
        else None,
        "recurrence_instance_count": item.recurrence_instance_count,
        "recurrence_instance_count_known": item.recurrence_instance_count_known,
        "recurrence_completed_on": item.recurrence_completed_on.isoformat()
        if item.recurrence_completed_on
        else None,
        "recurrence_next_on": item.recurrence_next_on.isoformat()
        if item.recurrence_next_on
        else None,
        "recurrence_generated_on": item.recurrence_generated_on.isoformat()
        if item.recurrence_generated_on
        else None,
        "recurrence_generated_on_known": item.recurrence_generated_on_known,
        "heading": item.heading,
        "sort_index": item.sort_index,
        "today_index": item.today_index,
        "entity": item.entity,
        "leavable": item.leavable,
        "checklists": [
            {
                "uuid": line.uuid,
                "title": line.title,
                "done": line.done,
                "status": line.status,
                "sort_index": line.sort_index,
            }
            for line in item.checklists
        ],
    }


def _cached_optional_date(payload: dict[str, Any], field: str) -> date | None:
    if field not in payload:
        raise ValueError(f"missing cached {field}")
    raw = payload[field]
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"invalid cached {field}")
    return date.fromisoformat(raw)


def _record_from_json(payload: dict[str, Any]) -> Record:
    kind_name = str(payload.get("kind") or "task")
    kind: Kind = (
        "project"
        if kind_name == "project"
        else "area"
        if kind_name == "area"
        else "task"
    )
    status_name = str(payload.get("status") or "open")
    status: Status = (
        "done"
        if status_name == "done"
        else "dropped"
        if status_name == "dropped"
        else "open"
    )
    raw_rule = payload.get("recurrence_rule")
    if raw_rule is not None and not isinstance(raw_rule, dict):
        raise ValueError("invalid cached recurrence rule")
    raw_links = payload.get("recurrence_links") or []
    if not isinstance(raw_links, list) or any(
        not isinstance(link, str) for link in raw_links
    ):
        raise ValueError("invalid cached recurrence links")
    raw_instance_count = payload.get("recurrence_instance_count")
    instance_count = _native_count(raw_instance_count)
    if instance_count is None:
        raise ValueError("invalid cached recurrence instance count")
    instance_count_known = payload.get("recurrence_instance_count_known")
    if not isinstance(instance_count_known, bool):
        raise ValueError("invalid cached recurrence instance count trust marker")
    paused = payload.get("recurrence_paused")
    if not isinstance(paused, bool):
        raise ValueError("invalid cached recurrence paused state")
    paused_known = payload.get("recurrence_paused_known")
    if not isinstance(paused_known, bool):
        raise ValueError("invalid cached recurrence paused trust marker")
    generated_on_known = payload.get("recurrence_generated_on_known")
    if not isinstance(generated_on_known, bool):
        raise ValueError("invalid cached recurrence origin trust marker")
    leavable = payload.get("leavable")
    if not isinstance(leavable, bool):
        raise ValueError("invalid cached leavable state")
    created_through = _cached_optional_date(payload, "recurrence_created_through")
    completed_on = _cached_optional_date(payload, "recurrence_completed_on")
    next_on = _cached_optional_date(payload, "recurrence_next_on")
    generated_on = _cached_optional_date(payload, "recurrence_generated_on")
    if not generated_on_known and generated_on is not None:
        raise ValueError("untrusted cached recurrence origin has a date")
    if (
        payload.get("recurrence_role") == "instance"
        and generated_on_known
        and leavable
        and generated_on is None
    ):
        raise ValueError("cached generated instance has no occurrence date")
    return Record(
        uuid=str(payload["uuid"]),
        kind=kind,
        title=str(payload.get("title") or ""),
        notes=str(payload.get("notes") or ""),
        notes_source=payload.get("notes_source", "none"),
        notes_format=payload.get("notes_format", "markdown"),
        status=status,
        completed_at=(
            datetime.fromisoformat(payload["completed_at"])
            if payload.get("completed_at")
            else None
        ),
        trashed=bool(payload.get("trashed")),
        inbox=bool(payload.get("inbox")),
        start=date.fromisoformat(payload["start"]) if payload.get("start") else None,
        deadline=date.fromisoformat(payload["deadline"])
        if payload.get("deadline")
        else None,
        remind=str(payload["remind"]) if payload.get("remind") else None,
        tonight=bool(payload.get("tonight")),
        someday=bool(payload.get("someday")),
        parent_uuid=payload.get("parent_uuid"),
        area_uuid=payload.get("area_uuid"),
        heading_uuid=payload.get("heading_uuid"),
        tag_uuids=list(payload.get("tag_uuids") or []),
        recurrence=RecurrenceState(
            role=payload.get("recurrence_role", "none"),
            repeat_type=payload.get("recurrence_type", "none"),
            template_uuid=payload.get("recurrence_template_uuid"),
            rule=raw_rule,
            links=tuple(raw_links),
            paused=paused,
        ),
        repeater=deepcopy(payload.get("repeater")),
        recurrence_created_through=created_through,
        recurrence_instance_count=instance_count,
        recurrence_instance_count_known=instance_count_known,
        recurrence_paused_known=paused_known,
        recurrence_completed_on=completed_on,
        recurrence_next_on=next_on,
        recurrence_generated_on=generated_on,
        recurrence_generated_on_known=generated_on_known,
        heading=bool(payload.get("heading")),
        sort_index=int(payload.get("sort_index") or 0),
        today_index=int(payload.get("today_index") or 0),
        entity=str(payload.get("entity") or ""),
        leavable=leavable,
        checklists=[
            ChecklistLine(
                uuid=str(line["uuid"]),
                title=str(line.get("title") or ""),
                done=bool(line.get("done")),
                status=line.get("status", "done" if line.get("done") else "open"),
                sort_index=int(line.get("sort_index") or 0),
            )
            for line in payload.get("checklists") or []
        ],
    )


def credentials_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root) if root else Path.home() / ".config"
    return base / "things-orchestrator" / "credentials.json"


def load_credentials(
    *,
    path: Path | None = None,
) -> tuple[str, str, str | None]:
    file_email = ""
    file_password = ""
    token = None
    target = path or credentials_path()
    if target.is_file():
        try:
            payload = json.loads(target.read_text())
        except json.JSONDecodeError as error:
            raise CloudError("Things Cloud credentials were unreadable") from error
        if not isinstance(payload, dict):
            raise CloudError("Things Cloud credentials were unreadable")
        file_email = str(payload.get("email") or "")
        file_password = str(payload.get("password") or "")
        raw_token = payload.get("mcp_token")
        token = str(raw_token) if raw_token else None
    if not file_email or not file_password:
        raise CloudError("Run things-orchestrator login in a private terminal")
    return file_email, file_password, token


def save_credentials(
    email: str,
    password: str,
    mcp_token: str,
    *,
    timezone_name: str | None = None,
    path: Path | None = None,
) -> Path:
    target = path or credentials_path()
    payload = (
        json.dumps(
            {
                "email": email,
                "password": password,
                "mcp_token": mcp_token,
                **({"timezone": timezone_name} if timezone_name else {}),
            },
            indent=2,
        )
        + "\n"
    )
    _atomic_write(target, payload)
    return target


def load_timezone(*, path: Path | None = None) -> str | None:
    target = path or credentials_path()
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("timezone")
    return str(value) if value else None
