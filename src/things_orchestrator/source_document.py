"""Compile Project Task finishes into native Things notes."""

from __future__ import annotations

import re

from things_orchestrator.interface import CreateEntry, ProjectTask

_PROJECT_SECTIONS = ("## Result", "## Done when", "## Guardrails")
_URL = re.compile(r"https?://\S+")
_MARKDOWN_SECTION = re.compile(r"(?m)^#{1,6}\s+.+$")
_GENERATED_LEAVE_WITH_PREFIX = "## Leave with\n\n"
_DISTILL_SECTION_CHARS = 800
_SOURCE_ARTIFACT_TITLE = re.compile(r"(?i)\b(mode|skill)\b")
_SOURCE_TASK_TITLE = re.compile(
    r"(?i)\b(audit|chat|evidence|research|source|post|reply|changelog)\w*\b"
)


class SourceDocumentError(ValueError):
    """The source Project is incomplete and must be revised before writing."""


def prose_chars(note: str) -> int:
    """Count prose without the generated finish label or full source URLs."""

    body = note
    if body.startswith(_GENERATED_LEAVE_WITH_PREFIX):
        body = body[len(_GENERATED_LEAVE_WITH_PREFIX) :]
    return len(_URL.sub("", body).strip())


def is_source_document(entry: CreateEntry) -> bool:
    """Identify declared source Projects and the rich skeleton escape shape."""

    if entry.document == "source":
        return True
    note = entry.notes_markdown or ""
    source_task_count = sum(
        _SOURCE_TASK_TITLE.search(task.title) is not None for task in entry.tasks
    )
    return (
        entry.kind == "project"
        and len(entry.tasks) >= 6
        and all(task.heading_title is not None for task in entry.tasks)
        and all(section in note for section in _PROJECT_SECTIONS)
        and _SOURCE_ARTIFACT_TITLE.search(entry.title) is not None
        and source_task_count >= 2
    )


def compile_project_document(entry: CreateEntry) -> CreateEntry:
    """Render Task finishes and validate one complete source Project."""

    source = is_source_document(entry)
    if entry.document == "source" and (entry.kind != "project" or not entry.tasks):
        raise SourceDocumentError(
            "Revise this source document before writing. It needs one Project with "
            "Tasks. Do not ask the owner."
        )
    if entry.kind != "project" or not entry.tasks:
        return entry
    if source:
        project_note = entry.notes_markdown or ""
        _require_ordered_sections(project_note)
        _require_distilled_sections(project_note, subject="Project note")
        missing = [task.title for task in entry.tasks if task.finish is None]
        if missing:
            raise SourceDocumentError(
                "Revise this source Project before writing. Every Task needs a finish. "
                "Send the complete Project and all Task finishes in one things_commit. "
                "Do not ask the owner."
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
    rendered: list[ProjectTask] = []
    for task in entry.tasks:
        if task.finish is None:
            rendered.append(task)
            continue
        extra = (task.notes_markdown or "").strip()
        if re.search(r"(?im)^#{1,6}\s+leave with\s*$", extra):
            project_kind = "source Project" if source else "Project"
            raise SourceDocumentError(
                f"Revise this {project_kind} before writing. Put the Task result in "
                "finish, not a second Leave with block. Do not ask the owner."
            )
        note = f"## Leave with\n\n{task.finish}"
        if extra:
            note = f"{note}\n\n{extra}"
        if len(note) > 50_000:
            raise SourceDocumentError(
                f"Revise {task.title}. Its rendered note exceeds 50000 characters. "
                "Do not ask the owner."
            )
        if source:
            _require_distilled_sections(note, subject=task.title)
        rendered.append(task.model_copy(update={"notes_markdown": note}))
    return entry.model_copy(
        update={"document": "source" if source else entry.document, "tasks": rendered}
    )


def _require_ordered_sections(note: str) -> None:
    sections = _markdown_sections(note)
    headings = tuple(heading for heading, _ in sections)
    if headings != _PROJECT_SECTIONS:
        raise SourceDocumentError(
            "Revise this source Project before writing. Project notes need only "
            "Result, Done when, and Guardrails in that order, with no preamble. "
            "Do not ask the owner."
        )
    for heading, content in sections:
        if not content.strip():
            raise SourceDocumentError(
                f"Revise this source Project before writing. "
                f"{heading[3:]} cannot be empty. Do not ask the owner."
            )


def _require_distilled_sections(note: str, *, subject: str) -> None:
    """Reject one pasted prose block while allowing a complete rich document."""

    sections = _markdown_sections(note)
    if any(
        _section_prose_chars(heading, content) > _DISTILL_SECTION_CHARS
        for heading, content in sections
    ):
        raise SourceDocumentError(
            f"Revise {subject}. Keep each Markdown section below "
            "800 characters of prose; labeled full URLs do not count. "
            "Do not ask the owner."
        )


def _section_prose_chars(heading: str, content: str) -> int:
    """Count user heading text and body prose, but not the generated result label."""

    heading_text = heading.lstrip("#").strip()
    if heading.casefold() == "## leave with":
        heading_text = ""
    return len(heading_text) + prose_chars(content)


def _markdown_sections(note: str) -> list[tuple[str, str]]:
    """Parse Markdown headings once for structure and prose limits."""

    matches = list(_MARKDOWN_SECTION.finditer(note))
    if not matches:
        return [("", note)]
    sections: list[tuple[str, str]] = []
    if note[: matches[0].start()].strip():
        sections.append(("", note[: matches[0].start()]))
    for index, match in enumerate(matches):
        content_start = match.end()
        content_end = matches[index + 1].start() if index + 1 < len(matches) else len(note)
        sections.append((match.group(0).strip(), note[content_start:content_end]))
    return sections
