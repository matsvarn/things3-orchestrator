# Weekly review design evidence

Researched 2026-08-21. This note separates source-backed practice from product choices.

## Verdict

The proposed redesign is mostly sound. Two parts need correction.

First, the standard GTD phases are Get Clear, Get Current, and Get Creative. "Focus" is not the third phase.

Second, official sources do not require weekly review and weekly planning to be separate sessions. They do support a strict order. Restore system truth, inspect calendar constraints, then make explicit planning choices.

## Evidence by design claim

| Claim | Finding | Design rule |
| --- | --- | --- |
| Three review phases | Official GTD uses Get Clear, Get Current, and Get Creative. | Keep these names and meanings. Add weekly planning as an optional fourth step. |
| Calendar scan order | GTD reviews the past calendar, then the upcoming calendar, before Waiting and Projects. | Never schedule the week before this scan. If calendar access is missing, ask the owner. |
| Review versus planning | GTD includes calendar and list decisions. Things also supports planning a week in Upcoming. Neither source requires separate sessions. | First make the system current. Then start an explicit, calendar-aware planning step. Do not mix hidden planning into cleanup. |
| Clear phase | GTD asks the owner to empty their head after processing inputs. Reading Things cannot discover uncaptured work. | Ask one short capture question. Do not mark Clear complete from list reads alone. |
| Project coverage | GTD reviews Projects one by one and requires a current next action for each active Project. | Check every active Project. Return exceptions, not a narrated full audit. |
| Date meaning | A Things start date means the day work can begin. A deadline means the last acceptable finish date. | Never translate "active next week" into Monday. Ask for a day or leave the item in Anytime. |
| Today and Upcoming | Today contains work intended for today. Upcoming holds work with a specific future start. Its week view helps prevent overloaded days. | Keep Today small. Show day load before adding dated work. |
| Anytime and Someday | Anytime is active and startable now. Someday has no current plan. | Clearing a stale date to Anytime is correct only when the task remains active and startable. Otherwise use Someday or close it. |
| Someday cadence | GTD reviews Someday/Maybe weekly. Cultured Code suggests every couple of months. | Make cadence a product choice or user preference. Do not claim one universal standard. |
| Exact confirmation | NN/G says confirmations must identify affected items and explain consequences. It also warns against routine confirmations. | Show exact titles for broad or high-impact batches. Keep the main manifest brief, with details available on request. |
| Progressive disclosure | NN/G recommends showing the main choices first and revealing secondary detail on request. | Show a compact exception summary. Let the owner open full lists or rationale. Avoid more than two disclosure levels. |
| Owner control | Microsoft says to scope service when uncertain, explain actions, and support dismissal and correction. | Do not preselect answers for personal priorities. Recommend only from owner-stated criteria, with reasons and uncertainty. |
| Change receipt | NN/G requires timely state feedback. Microsoft says to convey the consequences of actions. | After read-back, report changed items, failures, and unchanged requested items. Omit unrelated Areas and tags. |

## Corrected target flow

1. **Get Clear.** Process Inbox and ask for uncaptured work.
2. **Get Current.** Review action exceptions, past and future calendars, Waiting, active Projects, and checklists.
3. **Get Creative.** Review Someday when due and capture new ideas.
4. **Plan the week, if requested.** Show capacity and day load. Ask which active work gets a real start date.

The agent can present these steps as one guided session. It must preserve their order.

The default view should contain only exceptions and decisions. A full list remains available on request.

## Implications for the failed dogfood run

- The repeated full-audit narration was not required by GTD or Things.
- The run did not complete Get Clear because it never asked for uncaptured work.
- It mixed system cleanup with weekly priority selection.
- It treated "both next week" as "both start Monday." Things does not support that meaning.
- It asked the owner to confirm six unnamed date changes. That failed the specificity rule.
- It recommended personal priorities without owner-stated criteria.
- Its receipt included unchanged Areas and tags. Those facts did not explain the applied batch.

## Sources

- [GTD Weekly Review checklist and explanation](https://gettingthingsdone.com/2018/08/episode-43-the-power-of-the-gtd-weekly-review/)
- [Official GTD Weekly Review checklist PDF](https://gettingthingsdone.com/wp-content/uploads/2014/10/Weekly_Review_Checklist.pdf)
- [GTD Things setup guide sample](https://store.gettingthingsdone.com/wp-content/uploads/2025/05/GTD_Things_SAMPLE_A4.pdf)
- [Things date-based lists](https://culturedcode.com/things/support/articles/4001304/)
- [Things scheduling, start dates, and deadlines](https://culturedcode.com/things/support/articles/2803579/)
- [Things getting-started guidance](https://culturedcode.com/things/support/articles/6378414/)
- [Things weekly-review Shortcut](https://culturedcode.com/things/support/articles/2955145/)
- [Microsoft Guidelines for Human-AI Interaction](https://www.microsoft.com/en-us/research/publication/guidelines-for-human-ai-interaction/)
- [NN/G progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
- [NN/G confirmation dialog guidance](https://www.nngroup.com/articles/confirmation-dialog/)
- [NN/G visibility of system status](https://www.nngroup.com/articles/visibility-system-status/)

## Evidence boundary

GTD and Cultured Code do not prescribe quiet traces, exact manifests, neutral answer order, or change-only receipts. Human-AI and usability guidance supports those rules. Their exact implementation remains a Things Orchestrator product decision.
