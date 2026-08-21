"""Owner-visible MCP text. The model still reads structured Result."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .interface import Result, ReviewSection

OWNER_TEXT_LIMIT = 1200
_PLAN_ID = re.compile(r"plan_[A-Za-z0-9_-]{8,120}")


def owner_text(result: Result) -> str:
    """Plain owner card. Never includes a plan id. No markdown tables."""

    body = _body(result)
    cleaned = _PLAN_ID.sub("the plan", body)
    if len(cleaned) > OWNER_TEXT_LIMIT:
        return cleaned[: OWNER_TEXT_LIMIT - 3] + "..."
    return cleaned


def _body(result: Result) -> str:
    if result.status in {
        "rejected",
        "internal_error",
        "unavailable",
        "stale",
        "pending",
        "partial",
        "unsupported",
        "needs_input",
    }:
        return result.instruction
    if result.plan is not None:
        return _approval_card(result)
    if any(section.key == "get_clear" for section in result.sections):
        return _weekly_card(result)
    if result.status in {"applied", "unchanged"}:
        return result.instruction
    if _is_today(result):
        return _today_card(result)
    if result.items and result.status == "ok":
        return _list_card(result)
    return result.instruction


def _is_today(result: Result) -> bool:
    keys = {section.key for section in result.sections}
    return bool(keys) and keys <= {"overdue", "evening", "today", "waiting"}


def _weekly_card(result: Result) -> str:
    by_key = {section.key: section for section in result.sections}
    clear = by_key.get("get_clear")
    current = by_key.get("get_current")
    week = by_key.get("plan_week")
    inbox = _section_count(clear, "Inbox") if clear else None
    stale = _section_count(current, "Stale starts") if current else None
    overdue = _section_count(current, "overdue deadlines") if current else None
    waiting = _section_count(current, "Waiting") if current else None
    exceptions = len(result.items)
    lines = [f"**Weekly review.** {exceptions} exception(s) need a decision."]
    counts: list[str] = []
    if inbox is not None:
        counts.append(f"**Inbox:** {inbox}")
    if stale is not None:
        counts.append(f"**Stale starts:** {stale}")
    if overdue is not None:
        counts.append(f"**Overdue:** {overdue}")
    if waiting is not None:
        counts.append(f"**Waiting:** {waiting}")
    if counts:
        lines.append(" · ".join(counts))
    load = _day_load(week.signals if week is not None else [])
    if load:
        lines.append(f"**Load:** {load}")
    if inbox:
        lines.append(
            "If Inbox is still above zero, process three titles before you close."
        )
    elif stale or overdue:
        lines.append(
            "If a date is a start, keep the day. If it is only important, ask."
        )
    else:
        lines.append("If nothing needs a decision, stop.")
    lines.append("Ask about any line.")
    return "\n".join(lines)


def _today_card(result: Result) -> str:
    groups = (
        ("overdue", "Overdue"),
        ("evening", "Evening"),
        ("today", "Today"),
        ("waiting", "Waiting"),
    )
    lines = [f"**Today.** {len(result.items)} item(s)."]
    for signal, title in groups:
        titles = [
            item.title
            for item in result.items
            if signal in item.signals
        ]
        if titles:
            shown = "; ".join(titles[:5])
            extra = f" +{len(titles) - 5}" if len(titles) > 5 else ""
            lines.append(f"**{title}:** {shown}{extra}")
    lines.append("If something will not start today, move it off Today.")
    lines.append("Ask about any line.")
    return "\n".join(lines)


def _list_card(result: Result) -> str:
    titles = [item.title for item in result.items[:8]]
    extra = f" +{len(result.items) - 8}" if len(result.items) > 8 else ""
    lines = [
        f"**Matches.** {len(result.items)} item(s).",
        "; ".join(titles) + extra,
        result.instruction,
    ]
    return "\n".join(lines)


def _approval_card(result: Result) -> str:
    assert result.plan is not None
    lines = ["**Needs confirmation.**"]
    for row in result.plan.summary[:5]:
        lines.append(_PLAN_ID.sub("the plan", row))
    omitted = len(result.plan.summary) - 5
    if omitted > 0:
        lines.append(f"+{omitted} more change(s).")
    lines.append("Ask about any line.")
    return "\n".join(lines)


def _section_count(section: ReviewSection, label: str) -> int | None:
    pattern = re.compile(rf"(?i){re.escape(label)}:\s*(\d+)")
    for signal in section.signals:
        match = pattern.search(signal)
        if match is not None:
            return int(match.group(1))
    return None


def _day_load(signals: Sequence[str]) -> str | None:
    for signal in signals:
        if "Things load by day:" not in signal:
            continue
        pairs = re.findall(r"(\d{4}-\d{2}-\d{2}): (\d+)", signal)
        if not pairs:
            return None
        return " ".join(f"{day[8:]}:{count}" for day, count in pairs)
    return None
