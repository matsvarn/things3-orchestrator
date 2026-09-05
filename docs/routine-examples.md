# Set up a Things routine from an example

Choose a name below, create that routine in Grok Bot or Hermes, and paste the
linked prompt as its complete instruction. Every prompt stands on its own.
You do not need to combine it with the generic setup prompt or install a skill.

These examples use the current eight-tool Things MCP. They are proposed
workflows with acceptance examples, not a claim that each has been tested in
your receiver. All four prompts are currently unverified in live receiver runs.
Before publishing this catalog, verify task enrichment and at least one
scheduled report through an actual Grok or Hermes run. Record the receiver,
prompt tested, observed result, and coverage limits; keep the remaining
examples labeled unverified. Start with a test run and inspect its actual
tool calls and result.

## Choose a routine

| Routine name and complete prompt | Trigger | What you get | Changes Things? |
| --- | --- | --- | --- |
| [Things task enrichment](../plugin/skills/things-orchestrator/references/routine-enrichment.md) | New task with direct `AI` tag | Clear title, native deadline from text, useful notes, suitable existing home, and checklist steps only where useful | Selected task only |
| [Things morning plan](../plugin/skills/things-orchestrator/references/routine-morning-plan.md) | Suggested weekdays at 08:00 | Up to three focus tasks, other urgent commitments, and upcoming preparation | No |
| [Things deadline check](../plugin/skills/things-orchestrator/references/routine-deadline-check.md) | Suggested weekdays at 16:00 | Overdue, due-today, and next-seven-day native deadlines | No |
| [Things weekly Project review](../plugin/skills/things-orchestrator/references/routine-weekly-review.md) | Suggested Fridays at 15:00 | Project next-action gaps and Inbox decisions, with concrete proposals | No |

Use task enrichment for general captures, including reply drafts when the
task supplies the message context. The scheduled reports can coexist
with task enrichment. Start with the reports you will actually read;
morning plan and deadline check intentionally overlap on urgent work.

## Connect Things to the receiving agent

Follow [Connect a client](clients.md) first. Verify that the receiver can read a
current task through `things_get`. A working interactive chat does not prove
that a scheduled or webhook run receives the same MCP connection. Test that
execution path as well.

The read-only prompts limit behavior, but do not remove write tools from a
connector. They explicitly authorize reports only. None of these examples
requires email access, calendar access, web research, or sending messages to
other people.

## Create a webhook routine

1. Name the routine **Things task enrichment**.
2. Open the linked prompt and copy the entire `text` block into the receiver's
   instruction field. Replace the generic instruction rather than appending it.
3. Follow [Run the built-in AI task routine](routines.md) for Grok's webhook
   setup or Hermes's webhook subscription and MCP toolset access.
4. Test with a fresh task and assign `AI` directly while creating it. Use the
   harmless example at the bottom of the selected prompt page.
5. Inspect the selected task in Things and verify the allowed changes.

The current worker has one configured receiver and one fixed `AI` trigger. It
does not route by routine name, support several webhook destinations, or
recognize tags such as `AI-draft` as separate triggers. Use one enrichment
receiver rather than sending the same event to competing writers.
Adding `AI` to an older task does not trigger a new event.

Replacing a saved prompt changes future receiver runs. Updating the Things
Orchestrator server does not replace that saved prompt.

## Create a scheduled routine

The receiver owns the schedule. These three routines do not need the optional
Things Orchestrator worker, its `always_on` profile, an `AI` tag, or a webhook.
The Things MCP server must remain reachable when the receiver runs.

In Grok Bot, create a routine with the table's exact name, choose a schedule
and timezone, and paste the linked prompt as its instruction. Choose the owning
Bot conversation as the result destination. The
[official Grok routine guide](https://docs.x.ai/grok-bot/skills-routines-and-automations)
also describes creating a scheduled routine by asking the Bot, and checking
its next run and history.

In Hermes, ask it to create a scheduled job with that name, your schedule and
timezone, and the complete linked prompt as the job instruction. Choose your
current private chat as the result destination and ensure the gateway or cron
runner stays running. The
[official Hermes scheduled-task guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
describes natural-language creation, schedules, and result delivery.

For example, send the following setup request to either receiver, followed in
the same message by the entire **Things morning plan** prompt:

```text
Create a scheduled job named "Things morning plan" for weekdays at 08:00 Europe/Berlin. Return the result in this private conversation. Use the following text as the complete instruction for each run, and show me the saved schedule and next run:
```

Change `Europe/Berlin` to your timezone. The example prompt is the run
instruction; the setup request creates the schedule. Do not put the setup
request inside the recurring instruction.

Run each scheduled routine once and verify that it can use Things MCP in that
execution. Check the expected result on its prompt page. The report must
disclose failed or incomplete reads, and no Things item should change.

## Know what the examples cover

The reports use the available named views and exact Project membership reads.
They do not promise an unrestricted library query, calendar planning, email
search, historical trend detection, or notification deduplication. Each
scheduled run produces a fresh report. The prompt pages spell out their read
coverage and handling of missing information.

Keep the concrete acceptance example beside each prompt when you customize it.
After changing instructions or connectors, test the result again. A webhook
delivery acknowledgement or a scheduled job being saved does not prove that
the intended work completed.
