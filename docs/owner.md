# Talk to it

Setup is [scripts/setup](../scripts/setup) on this Mac, or
[host.md](host.md) on a VPS, plus one client in
[clients.md](clients.md). Then ask in normal language.

New titles are short action phrases in the language of your existing
titles. Named Project Tasks are kept verbatim. An accepted Project plan keeps
all committed Tasks visible in dependency order. Multi-stage Projects use
native Things headings when they improve scanning.

You can send a source thread, links, and one clear result in the same request.
If you also ask it to create the work, it researches and creates in one turn.
It does not browse for an Area or place the Project in one unless you named it.

## Project note style

The server stores structured meaning before it renders new Project notes.
Project notes start with the outcome. Task notes start with the result that the
Task produces. The renderer adds only sections that have content.

The default `natural` style uses visible Markdown headings such as `## Outcome`,
`## Done when`, `## Start here`, and `## Sources`. The
`visual` style adds fixed markers to the same meaning:

- 🎯 outcome
- ✅ completion or Task result
- 🧭 shared constraints
- 💡 starting context
- ▶️ approach
- 🔗 sources

Set the saved style on the server:

```console
uv run things-orchestrator configure --note-style natural
uv run things-orchestrator configure --note-style visual
```

The next Project uses the new style without a restart. An explicit request for
one Project can override the style without changing the saved preference.
Updates, login, token rotation, setup, and rollback preserve the preference in
`${XDG_CONFIG_HOME:-$HOME/.config}/things-orchestrator/preferences.json`.
Existing Things items stay unchanged. The supported styles are `natural` and
`visual`; there is no legacy presentation mode. The command writes the file
atomically with private permissions and preserves unknown future keys. An
invalid preference stops the next Project before any Things write and tells
you to run `configure` again.

Approve third-party app links by their URL schemes:

```console
uv run things-orchestrator configure --source-schemes obsidian x-devonthink-item
```

Pass `--source-schemes` with no values to clear the allowlist. Web, file,
and read-only Things links are built in. Do not add their schemes. The command
case-folds schemes and rejects invalid or unsafe values.

Today, Logbook, and reminders use the timezone stored by `login`. After
a permanent move: `uv run things-orchestrator login --timezone Europe/Berlin`.

Never paste the Cloud password, an MCP bearer, or a config snippet into
chat. If the model shows a tool id, answer in words.

## Say

- “Remind me to renew my passport.”
- “Tag Buy milk Errands and put it in Kitchen.”
- “Project: Replace kitchen tap. Next: Find three taps, Measure the sink, Order one.”
- “What should I focus on in Things today?”
- “Start the contract on 4 September.” / “Deadline for the contract is 4 September.”
- “Remind me about the invoice at 09:00.”
- “Make Water plants every two weeks.” / “Stop repeating Water plants but keep the task.”
- “Add headings Research and Buy to Replace kitchen tap.”
- “Tag the invoice Waiting. Put Waiting under Admin.”
- “Add this note to Contract.”
- “Trash Old draft.” / “Restore the task I just trashed.”
- “Permanently delete Old draft from Trash.”
- “Review my Areas and suggest one cleanup.”

Evening is a start, not a reminder. “Remind me about X” updates existing
X. It will not invent a second copy of the same title.

## It will ask, and change nothing, when

- a date is not named as start or deadline
- a reminder has no clock time
- two items match
- a new Project has no stated finish
- a mashed title, pasted brief, or Decide / Think about row
- two supported readings lead to different results or Things forms
- a permanent delete does not name exact Trash items

## It will ask you to confirm before

Area changes, broad batches, Trash, repeat-rule or future-template
changes, registry cleanup, replacing a rich note, permanent deletion,
or closing a Project that still has open actions.

Routine capture and exact edits apply without that step.

Something broken: [recovery.md](recovery.md).
