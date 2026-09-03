# Workflow recipes

These prompts map to the bounded v2 tools. Start with a fresh client session so
the agent reads current Things state.

## Capture one task

> Add "Renew passport" to my Things Inbox.

The agent creates one Inbox Task. It does not invent a date, Project, Area, or
tag.

## Choose today's focus

> Show me Today. Help me choose what to do now, then move the items I name to
> Evening. Do not change anything else.

The agent reads Today first. It updates only the exact items you name and keeps
their existing notes, checklist rows, tags, recurrence, and home.

## Process Inbox together

> Process my Things Inbox with me. Ask when an item has no clear outcome. Apply
> only the decisions I make, and stop when Inbox is empty or we reach an item I
> cannot decide.

The agent reads Inbox, keeps ambiguous items unchanged, and uses bounded
updates, completion, or recoverable Trash for explicit decisions.

## Review current commitments

> Review my Inbox, Today, upcoming deadlines, and active Projects. Give me a
> short list of decisions. Do not change Things until I name the changes.

The agent reads current state and proposes decisions. A later message can apply
the exact changes you accept through the same bounded v2 tools.
