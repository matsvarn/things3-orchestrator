from __future__ import annotations

import pytest

from things_orchestrator.interface import CreateEntry, ProjectTask, SourceRef
from things_orchestrator.source_document import (
    SourceDocumentError,
    compile_project_document,
    render_project_note,
    render_task_note,
)


def _project() -> CreateEntry:
    return CreateEntry(
        kind="project",
        document="source",
        title="Create Mats Mode as a reusable Agent Skill",
        outcome=(
            "Create one reusable Mats Mode skill from how I already work with agents."
        ),
        finished_when=[
            "I approve the rules before drafting.",
            "The tested skill is pinned in Cursor.",
        ],
        keep_in_mind=["Use recent chats, not installed inventory."],
        tasks=[
            ProjectTask(
                title="Choose representative chats from each active agent client",
                finish="A dated source set from every active agent client.",
                start_here=["Use recent chats that show success and corrections."],
                approach=[
                    "Keep each client separate.",
                    "Record privacy limits.",
                ],
                sources=[
                    SourceRef(
                        label="Optional extractor",
                        location="https://github.com/0xSero/ai-data-extraction",
                    )
                ],
            )
        ],
    )


def test_natural_renderer_is_compact_and_adaptive() -> None:
    project = _project()

    assert render_project_note(project, style="natural") == (
        "## Outcome\n\n"
        "Create one reusable Mats Mode skill from how I already work with agents.\n\n"
        "## Done when\n\n"
        "- I approve the rules before drafting.\n"
        "- The tested skill is pinned in Cursor.\n\n"
        "## Keep in mind\n\n"
        "Use recent chats, not installed inventory."
    )
    assert render_task_note(project.tasks[0], style="natural") == (
        "## Done when\n\n"
        "A dated source set from every active agent client.\n\n"
        "## Start here\n\n"
        "Use recent chats that show success and corrections.\n\n"
        "## Approach\n\n"
        "- Keep each client separate.\n"
        "- Record privacy limits.\n\n"
        "## Sources\n\n"
        "Optional extractor\n"
        "https://github.com/0xSero/ai-data-extraction"
    )


def test_visual_renderer_uses_only_the_fixed_note_markers() -> None:
    project = _project()

    assert render_project_note(project, style="visual") == (
        "## 🎯 Outcome\n\n"
        "Create one reusable Mats Mode skill from how I already work with agents.\n\n"
        "## ✅ Done when\n\n"
        "- I approve the rules before drafting.\n"
        "- The tested skill is pinned in Cursor.\n\n"
        "## 🧭 Keep in mind\n\n"
        "Use recent chats, not installed inventory."
    )
    note = render_task_note(project.tasks[0], style="visual")
    assert note.startswith("## ✅ Done when\n\nA dated source set")
    assert "## 💡 Start here" in note
    assert "## ▶️ Approach" in note
    assert "## 🔗 Sources" in note
    assert not any(marker in project.title for marker in "🎯✅🧭💡▶️🔗")


def test_compiler_renders_all_source_notes_without_legacy_result_labels() -> None:
    rendered = compile_project_document(_project(), style="natural")

    assert rendered.notes_markdown is not None
    assert rendered.tasks[0].notes_markdown is not None
    joined = f"{rendered.notes_markdown}\n{rendered.tasks[0].notes_markdown}"
    assert "## Result" not in joined
    assert "## Guardrails" not in joined
    assert "## Leave with" not in joined


def test_compiler_requires_an_allowed_third_party_source_scheme() -> None:
    project = _project()
    task = project.tasks[0].model_copy(
        update={
            "sources": [
                SourceRef(label="Design note", location="obsidian://open?vault=Work")
            ]
        }
    )
    project = project.model_copy(update={"tasks": [task]})

    with pytest.raises(SourceDocumentError, match="obsidian.*not allowed"):
        compile_project_document(project)

    rendered = compile_project_document(
        project, allowed_source_schemes=("Obsidian",)
    )
    assert "obsidian://open?vault=Work" in (rendered.tasks[0].notes_markdown or "")


def test_ordinary_raw_project_is_not_reformatted() -> None:
    entry = CreateEntry(
        kind="project",
        title="Replace kitchen tap",
        notes_markdown="Measure the sink first.",
        tasks=[ProjectTask(title="Measure the sink")],
    )

    assert compile_project_document(entry) == entry


def test_ordinary_project_meaning_renders_without_nested_tasks() -> None:
    entry = CreateEntry(
        kind="project",
        title="Publish the guide",
        outcome="One concise guide is published.",
        finished_when=["The intended readers can use it."],
    )

    rendered = compile_project_document(entry)

    assert rendered.notes_markdown == (
        "## Outcome\n\nOne concise guide is published.\n\n"
        "## Done when\n\nThe intended readers can use it."
    )


def test_source_document_limits_checklists_and_total_native_rows() -> None:
    base = _project()
    two_checklists = base.model_copy(
        update={
            "tasks": [
                base.tasks[0].model_copy(update={"checklist": ["One"]}),
                ProjectTask(
                    title="Test the skill",
                    finish="The test passes.",
                    checklist=["Two"],
                ),
            ]
        }
    )
    with pytest.raises(SourceDocumentError, match="at most one checklist"):
        compile_project_document(two_checklists)

    too_many_rows = base.model_copy(
        update={
            "tasks": [
                ProjectTask(
                    title=f"Produce result {index}",
                    finish=f"Result {index} exists.",
                    heading_title=f"Stage {index // 4}",
                )
                for index in range(16)
            ]
        }
    )
    with pytest.raises(SourceDocumentError, match="20 native rows"):
        compile_project_document(too_many_rows)
