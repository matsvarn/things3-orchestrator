"""Product skill contract for the A2-product package.

Winning first-action rules stay in SKILL.md. Research, form, and
review live in the three disclosed references.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "plugin/skills/things-orchestrator/SKILL.md"
REFERENCES = SKILL.parent / "references"


def _agent_manifest(skill: Path) -> dict[str, object]:
    payload = yaml.safe_load(
        (skill.parent / "agents/openai.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def _frontmatter(skill: Path) -> dict[str, str]:
    source = skill.read_text(encoding="utf-8")
    payload = yaml.safe_load(source.split("---", 2)[1])
    assert isinstance(payload, dict)
    return payload


def _skill_text() -> str:
    files = [SKILL, *sorted(REFERENCES.glob("*.md"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def test_skill_is_model_invoked_with_matching_ui_metadata() -> None:
    main_meta = _frontmatter(SKILL)
    main = _agent_manifest(SKILL)

    assert set(main_meta) == {"name", "description"}
    assert main_meta["name"] == "things-orchestrator"
    assert "capture" in main_meta["description"]
    assert "review" in main_meta["description"]

    assert main["policy"] == {"allow_implicit_invocation": True}
    assert main["dependencies"] == {
        "tools": [
            {
                "type": "mcp",
                "value": "things",
                "description": "Things Orchestrator",
            }
        ]
    }

    interface = main["interface"]
    assert isinstance(interface, dict)
    assert 25 <= len(interface["short_description"]) <= 64
    assert f"${main_meta['name']}" in interface["default_prompt"]

    skill_dirs = {
        path.name
        for path in (ROOT / "plugin/skills").iterdir()
        if path.is_dir()
    }
    assert skill_dirs == {"things-orchestrator"}


def test_main_skill_keeps_winning_first_actions() -> None:
    lower = SKILL.read_text(encoding="utf-8").lower()

    assert "view=tags" in lower
    assert "ensure_tags" in lower
    assert "into_title" in lower
    assert "do not invent a second tag name" in lower
    assert "start=evening" in lower
    assert "distinctive title token" in lower
    assert "do not create that project" in lower
    assert "do not start a permanent-delete plan" in lower
    assert "renew passport" in lower


def test_form_choice_lives_in_form() -> None:
    source = (REFERENCES / "form.md").read_text(encoding="utf-8")
    lower = source.lower()

    assert "smallest useful form" in lower
    assert "walk these questions in order" in lower
    assert "will one sitting finish it" in lower
    for native_form in (
        "task",
        "things checklist",
        "project",
        "markdown notes",
        "area",
        "start date",
        "deadline",
        "reminder",
        "tag",
        "someday",
    ):
        assert native_form in lower

    assert re.search(r"next\s+(?:one to three|1[^\w]3)\s+useful actions", lower)
    assert "visible action" in lower
    assert "finish criteria" in lower
    assert "put a needed source in that task's `sources`" in lower
    assert "startable" in lower
    assert "result or inclusion of work is undecided" in lower
    assert "complete supported finish path" in lower
    assert "dependency order" in lower
    assert "first task is available now" in lower
    assert "committed later action is a project task" in lower
    assert "order shows dependencies" in lower
    assert "do not mix the two forms" in lower
    assert "two or more distinct stages" in lower
    assert "six or more tasks" in lower
    assert "explicit owner-named headings at any size" in lower
    assert "heading_title" in lower
    assert "a task may add one `finish`" in lower
    assert "split it into its own task" in lower
    assert "each source is `{label, location}`" in lower
    assert "never infer or browse for an area" in lower

    planning_stop = lower.split("stop planning", 1)[1].split("##", 1)[0]
    assert "start" in planning_stop
    assert "context" in planning_stop
    assert "finished" in planning_stop


def test_owner_control_is_in_the_package() -> None:
    lower = _skill_text().lower()

    assert "owner's words" in lower
    assert "natural things terms" in lower
    assert "preserve the owner's dates and importance" in lower
    assert "do not infer urgency" in lower
    assert "time-specific start cue" in lower
    assert "repeating template" in lower
    assert "generated copy for current work" in lower
    assert "stopping repetition" in lower
    assert "ask one short question" in lower
    assert "preserve anything the owner did not ask to change" in lower
    assert "ask one short question in the owner's words" in lower
    assert "keep the plan id" in lower
    assert "private" in lower

    internal_jargon = {
        "commitment",
        "evidence label",
        "blueprint",
        "invariant",
        "domain outcome",
        "target registry",
        "if_revision",
        "legacy arms",
        "contextual arms",
    }
    assert not any(term in lower for term in internal_jargon)


def test_skill_leaves_request_mechanics_to_the_mcp_interface() -> None:
    combined = _skill_text()
    tool_names = re.findall(r"things_[a-z_]+", combined)

    assert set(tool_names) == {"things_read", "things_commit", "things_approve"}
    assert "follow each returned" in combined.lower()

    mechanics = {
        "intent_id",
        "if_revision",
        "scope_revision",
        "plan_id",
        "retry_same",
        "move_contents_to",
        "remove_if_empty",
        "cursor",
    }
    assert not any(term in combined for term in mechanics)
    assert '{"' not in combined


def test_skill_discloses_only_the_three_distinct_judgment_branches() -> None:
    source = SKILL.read_text(encoding="utf-8")
    links = set(re.findall(r"\(references/([^)]+)\)", source))

    assert links == {"research.md", "form.md", "review.md"}
    assert {path.name for path in REFERENCES.iterdir()} == links

    form = (REFERENCES / "form.md").read_text(encoding="utf-8").lower()
    review = (REFERENCES / "review.md").read_text(encoding="utf-8").lower()
    research = (REFERENCES / "research.md").read_text(encoding="utf-8").lower()

    for concept in ("broad task", "vague project", "waiting", "someday", "area"):
        assert concept in form
    assert "one concise question" in form

    for concept in ("inbox", "next action", "duplicates", "waiting", "someday", "area"):
        assert concept in review
    assert "every reviewed item" in review
    assert "preserve all other work" in review

    for concept in ("direct", "sources", "uncertainty", "owner", "markdown", "tasks"):
        assert concept in research
    assert "one complete commit" in research
    assert "source packet is not automatically a dump" in research
    assert "creation is authorized" in research
    assert "two supported results" in research
    assert "batch independent reads" in research
    assert "continual work stays outside" in research
    assert "actual use is not installed inventory" in research
    assert "keep each named client separate" in research
    assert "relevant thread replies" in research
    assert "one durable artifact" in research
    assert "authenticated browser" in research
    assert "do not call `things_read`" in research
    assert "private coverage list" in research
    assert "one fact, uncertainty, or exclusion" in research
    assert "source read removes a `read` task" in research
    assert "not an unfinished evidence result" in research

    assert "view=inbox" in form
    assert "distill" in form

    assert "empty your head" in review
    skill = source.lower()
    assert "overdue" in skill
    assert "today_after" in skill


def test_skill_teaches_the_proven_write_forms() -> None:
    lower = _skill_text().lower()

    for capability in (
        "rename or reorder a heading",
        "repeating template for future copies",
        "generated copy for the current cycle",
        "complete repeat rule",
        "batch both changes",
        "repair tag names and parent relationships",
        "restore accidental cleanup",
        "permanently delete",
        "rich notes",
        "lifecycle=trash",
        "ordinary task or project",
        "remaining descendants go to trash with it",
    ):
        assert capability in lower


def test_skill_teaches_safe_delete_and_merge_forms() -> None:
    lower = _skill_text().lower()

    assert "lifecycle=trash` only for an ordinary task or project delete" in lower
    assert "set the source project to `lifecycle=trash`" in lower
    assert "remaining descendants go to trash with it" in lower
    assert "include the destination" in lower
    assert "every permanent task or project deletion target must already be in trash" in lower
    assert "including tasks and empty projects" in lower
    assert "if it is in trash, the contained records" in lower
    assert "do not use `view=trash` alone" in lower
    assert "lifecycle=delete_permanently` with `delete_contents=true`" in lower
    assert "then approve the plan" in lower
    assert "organize.delete_headings" in lower
    assert "change_tags.delete_permanently" in lower


def test_skill_teaches_the_contextual_short_path() -> None:
    lower = _skill_text().lower()

    for instruction in (
        "purpose=change",
        "purpose=organize",
        "context refs",
        "do not copy revisions",
        "editable draft",
        "ordered sections",
        "unlisted=keep",
        "batch related normal changes",
        "structured recovery",
        "current copy and template",
    ):
        assert instruction in lower

    assert "read once" in lower
    assert "one commit" in lower
    assert "rebuild once" in lower
    assert "response is lost" in lower
    assert "pending or unknown" in lower
    assert "no new facts" in lower
    assert "stale or expired" in lower


def test_specialized_write_forms_live_in_disclosed_references() -> None:
    skill = SKILL.read_text(encoding="utf-8").lower()
    form = (REFERENCES / "form.md").read_text(encoding="utf-8").lower()
    review = (REFERENCES / "review.md").read_text(encoding="utf-8").lower()

    assert "start=evening" in skill
    assert "purpose=recurrence" in skill
    assert "search first" in skill
    assert "select one view" not in skill
    assert "local neighborhood" not in skill
    assert "do not use `view=trash` alone" in skill
    assert "if it is in trash, the contained records" in skill

    assert "lifecycle=trash` is recoverable teardown" in form

    assert "search named existing items and edit them" in review
    assert "create only when asked to add" in review
    assert "delete_headings" in review
    assert "remaining descendants go to trash with it" in review
    assert "within=trash" in review


def test_main_and_reference_files_stay_lean() -> None:
    assert len(SKILL.read_text(encoding="utf-8").splitlines()) < 110
    for reference in REFERENCES.glob("*.md"):
        assert len(reference.read_text(encoding="utf-8").splitlines()) < 70


def test_capture_stops_illegal_first_writes() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    lower = skill.lower()
    form = (REFERENCES / "form.md").read_text(encoding="utf-8").lower()
    review = (REFERENCES / "review.md").read_text(encoding="utf-8").lower()
    research = (REFERENCES / "research.md").read_text(encoding="utf-8").lower()

    assert "split" in lower
    assert "distill" in lower
    assert "do not paste" in lower
    assert "brief" in lower
    assert "thread" in lower
    assert "changelog" in lower
    assert "from this" in lower
    assert "adopt vs skip" in lower
    assert "decide" in lower
    assert "think about" in lower
    assert "source packet is not automatically a dump" in lower
    assert "(references/research.md)" in skill
    assert "before any things call" in lower
    assert "today" in lower and "focus" in lower
    assert "(references/review.md)" in skill
    assert "process inbox" in lower
    assert "(references/form.md)" in skill
    assert "## write" not in lower
    assert "## route" not in lower
    assert "before any create" not in lower
    assert "one sitting" in lower
    assert "two supported readings" in lower

    assert "creation is authorized" in research
    assert "batch independent reads" in research
    assert "continual work stays outside" in research
    assert "owns research, progress, form, and one complete commit" in research

    assert "decide" in form
    assert "distill" in form
    assert "view=inbox" in form

    assert "overdue" in lower
    assert "today_after" in lower
    assert "postpone" in lower
    assert "empty your head" in review
    assert "capture" in review


def test_source_heavy_create_is_one_decisive_quiet_flow() -> None:
    skill = SKILL.read_text(encoding="utf-8").lower()
    research = (REFERENCES / "research.md").read_text(encoding="utf-8").lower()

    assert "before any things call" in skill
    assert "owns research, form, progress, and creation" in skill
    assert "routine creates apply at once" in skill
    assert "do not promise a plan or another approval" in skill
    assert "do not narrate tool loading, retries, or the next lookup" in skill
    assert "complete supported finish path" in skill
    assert "did not preapprove each task title" in skill
    assert "review and mark the proposed rules" in skill
    assert "one opening update" in research
    assert "after that, report only" in research
    assert "one result remains" in research
    assert "batch independent reads" in research
    assert "direct sources, then one suitable fallback" in research
    assert "authenticated browser" in research
    assert "do not call `things_read`" in research
    assert "one complete commit" in research


def test_source_capture_writes_a_human_things_document() -> None:
    skill = SKILL.read_text(encoding="utf-8").lower()
    research = (REFERENCES / "research.md").read_text(encoding="utf-8").lower()
    form = (REFERENCES / "form.md").read_text(encoding="utf-8").lower()

    assert "document=source" in research
    assert "concrete `finish`" in research
    assert "server renders each finish" in research
    assert "collapsed project" in research
    assert "every opened task" in research
    assert "without this chat" in research
    assert "first-person" in research
    assert "concrete `outcome`" in research
    assert "`finished_when`" in research
    assert "`keep_in_mind`" in research
    assert "`start_here`" in research
    assert "`approach`" in research
    assert "structured `sources`" in research
    assert "do not send `notes_markdown`" in research
    assert "not authority to author" in research
    assert "actual tools and standing instructions" in research
    assert "repeated patterns and corrections" in research
    assert "external candidate evidence" in research
    assert "external candidate evidence; comparison; proposed rules" in research
    assert "owner rule review" in research
    assert "draft review" in research
    assert "tests in every active client" in research
    assert "one real-use validation" in research
    assert "ten to twelve tasks under three headings" in research
    assert "do not split mapping from selection" in research
    assert "not one task per family or idea" in research
    assert "put its url or path in `sources`" in research
    assert "never in semantic prose" in research
    assert "add finite fixes only after" in research
    assert "pin, publish, send, or install task is delivery" in research
    assert "every task has `finish`" in research
    assert "every source has a label and location" in research
    assert "next=revise" in research
    assert "without asking the owner" in research
    assert "never offer a later notes pass" in research
    assert "short plain notes" not in form
    assert "owner who will reopen things" in form
    assert "my chats" in form
    assert "each source is `{label, location}`" in form
    assert "write nothing this turn" not in f"{skill}\n{research}"
