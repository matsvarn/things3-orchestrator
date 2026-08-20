"""Validate semantic Project notes and render native Things Markdown."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from things_orchestrator.interface import CreateEntry, NoteStyle, ProjectTask, SourceRef

_WEB_SCHEMES = frozenset({"http", "https"})
_NATIVE_SCHEMES = frozenset({"file", "things"})
_SOURCE_NOTE_LIMIT = 50_000


class SourceDocumentError(ValueError):
    """The Project document is incomplete and must be revised before writing."""


def prose_chars(note: str) -> int:
    """Count visible prose while excluding full web URL tokens."""

    return sum(
        len(token)
        for token in note.split()
        if not token.casefold().startswith(("http://", "https://"))
    )


def is_source_document(entry: CreateEntry) -> bool:
    """Return true only for the explicit source-document contract."""

    return entry.document == "source"


def is_stripped_source_skeleton(entry: CreateEntry) -> bool:
    """Detect the old Mats-style payload after semantic notes were stripped."""

    if entry.kind != "project" or entry.document is not None or len(entry.tasks) < 6:
        return False
    note = (entry.notes_markdown or "").casefold()
    required = ("## result", "## done when", "## guardrails")
    if not all(section in note for section in required):
        return False
    if any(has_task_meaning(task) or task.notes_markdown for task in entry.tasks):
        return False
    if not all(task.heading_title for task in entry.tasks):
        return False
    source_words = (
        "chat",
        "source",
        "evidence",
        "post",
        "thread",
        "pstack",
        "changelog",
    )
    source_tasks = sum(
        any(word in task.title.casefold() for word in source_words)
        for task in entry.tasks
    )
    return "mode" in entry.title.casefold() and source_tasks >= 2


def has_project_meaning(entry: CreateEntry) -> bool:
    """Return true when a Project uses semantic note fields."""

    return bool(entry.outcome or entry.finished_when or entry.keep_in_mind)


def has_task_meaning(task: ProjectTask) -> bool:
    """Return true when a nested Task uses semantic note fields."""

    return bool(task.finish or task.start_here or task.approach or task.sources)


def compile_project_document(
    entry: CreateEntry,
    *,
    style: NoteStyle = "natural",
    allowed_source_schemes: Iterable[str] = (),
) -> CreateEntry:
    """Validate and render one semantic Project document."""

    if entry.kind != "project":
        return entry

    source = is_source_document(entry)
    if source:
        if entry.outcome is None or not entry.finished_when:
            raise SourceDocumentError(
                "Revise this source Project before writing. Add its outcome and "
                "finished_when checks. Do not ask the owner."
            )
        missing = [task.title for task in entry.tasks if task.finish is None]
        if missing:
            raise SourceDocumentError(
                "Revise this source Project before writing. Every Task needs a finish. "
                "Send the complete Project in one things_commit. Do not ask the owner."
            )
        if len(entry.tasks) >= 6:
            headings = list(
                dict.fromkeys(
                    task.heading_title
                    for task in entry.tasks
                    if task.heading_title is not None
                )
            )
            if not 2 <= len(headings) <= 4:
                raise SourceDocumentError(
                    "Revise this source Project before writing. Six or more Tasks need "
                    "two to four contiguous headings. Do not ask the owner."
                )
        checklist_tasks = [task for task in entry.tasks if task.checklist]
        if len(checklist_tasks) > 1 or any(
            len(task.checklist) > 3 for task in checklist_tasks
        ):
            raise SourceDocumentError(
                "Revise this source Project before writing. Use at most one "
                "checklist with at most three rows. Do not ask the owner."
            )
        heading_count = len(
            dict.fromkeys(
                task.heading_title
                for task in entry.tasks
                if task.heading_title is not None
            )
        )
        native_rows = (
            1
            + heading_count
            + len(entry.tasks)
            + sum(len(task.checklist) for task in entry.tasks)
        )
        if native_rows > 20:
            raise SourceDocumentError(
                "Revise this source Project before writing. Keep the Project, "
                "headings, Tasks, and checklist rows to 20 native rows or fewer. "
                "Do not ask the owner."
            )

    allowed = {scheme.casefold() for scheme in allowed_source_schemes}
    rendered_tasks: list[ProjectTask] = []
    for task in entry.tasks:
        if not has_task_meaning(task):
            rendered_tasks.append(task)
            continue
        for reference in task.sources:
            custom = _custom_source_scheme(reference)
            if custom is not None and custom not in allowed:
                raise SourceDocumentError(
                    f"Revise {task.title}. Source scheme '{custom}' is not allowed. "
                    "Configure it on the serving host, then retry the same commit. "
                    "Do not ask the owner."
                )
        note = render_task_note(task, style=style)
        if len(note) > _SOURCE_NOTE_LIMIT:
            raise SourceDocumentError(
                f"Revise {task.title}. Its rendered note exceeds 50000 characters. "
                "Do not ask the owner."
            )
        rendered_tasks.append(task.model_copy(update={"notes_markdown": note}))

    project_note = entry.notes_markdown
    if has_project_meaning(entry):
        project_note = render_project_note(entry, style=style)
        if len(project_note) > _SOURCE_NOTE_LIMIT:
            raise SourceDocumentError(
                "Revise this Project. Its rendered note exceeds 50000 characters. "
                "Do not ask the owner."
            )

    return entry.model_copy(
        update={"notes_markdown": project_note, "tasks": rendered_tasks}
    )


def render_project_note(entry: CreateEntry, *, style: NoteStyle) -> str:
    """Render semantic Project fields with one fixed style grammar."""

    parts: list[str] = []
    if entry.outcome:
        label = "## 🎯 Outcome" if style == "visual" else "## Outcome"
        parts.append(f"{label}\n\n{entry.outcome}")
    if entry.finished_when:
        label = "## ✅ Done when" if style == "visual" else "## Done when"
        parts.append(_adaptive(label, entry.finished_when))
    if entry.keep_in_mind:
        label = "## 🧭 Keep in mind" if style == "visual" else "## Keep in mind"
        parts.append(_adaptive(label, entry.keep_in_mind))
    return "\n\n".join(parts)


def render_task_note(task: ProjectTask, *, style: NoteStyle) -> str:
    """Render semantic Task fields with one fixed style grammar."""

    parts: list[str] = []
    if task.finish:
        label = "## ✅ Done when" if style == "visual" else "## Done when"
        parts.append(f"{label}\n\n{task.finish}")
    if task.start_here:
        label = "## 💡 Start here" if style == "visual" else "## Start here"
        parts.append(_adaptive(label, task.start_here))
    if task.approach:
        label = "## ▶️ Approach" if style == "visual" else "## Approach"
        parts.append(_adaptive(label, task.approach))
    if task.sources:
        label = "## 🔗 Sources" if style == "visual" else "## Sources"
        source_lines = [f"{source.label}\n{source.location}" for source in task.sources]
        parts.append(f"{label}\n\n" + "\n\n".join(source_lines))
    return "\n\n".join(parts)


def _adaptive(label: str, values: list[str]) -> str:
    if len(values) == 1:
        return f"{label}\n\n{values[0]}"
    return f"{label}\n\n{_bullets(values)}"


def _bullets(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def _custom_source_scheme(reference: SourceRef) -> str | None:
    location = reference.location
    if location.startswith(("/", "~/")):
        return None
    scheme = urlsplit(location).scheme.casefold()
    if scheme in _WEB_SCHEMES | _NATIVE_SCHEMES:
        return None
    return scheme
