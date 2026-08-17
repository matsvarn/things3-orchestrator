# Talk to it

Setup is [scripts/setup](../scripts/setup) on this Mac, or
[host.md](host.md) on a VPS, plus one client in
[clients.md](clients.md). Then ask in normal language.

New titles are short action phrases in the language of your existing
titles. Named next actions on a new Project are kept verbatim.

Today, Logbook, and reminders use the timezone stored by `login`. After
a permanent move: `uv run things-orchestrator login --timezone Europe/Berlin`.

Never paste the Cloud password, an MCP bearer, or a config snippet into
chat. If the model shows a tool id, answer in words.

## Say

- “Remind me to renew my passport.”
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
- a new Project has no stated outcome
- a permanent delete does not name exact Trash items

## It will ask you to confirm before

Area changes, broad batches, Trash, repeat-rule or future-template
changes, registry cleanup, replacing a rich note, permanent deletion,
or closing a Project that still has open actions.

Routine capture and exact edits apply without that step.

Something broken: [recovery.md](recovery.md).
