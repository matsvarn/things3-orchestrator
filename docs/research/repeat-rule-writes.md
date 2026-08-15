# Repeat-rule writes

Reviewed 2026-08-16. This note uses the pinned [`evanpurkhiser/things3-cloud`](https://github.com/evanpurkhiser/things3-cloud/tree/1281f43bc677325968a6fdea242a5c39bb04d208) source as the primary protocol source. It does not use Things credentials or perform Cloud writes.

## Result

The wire model can represent a repeat rule. A live disposable-template probe
proved that a sparse update can replace `rr` without changing the template ID.
The pinned CLI does not expose a command that writes either `rr` or `rt`.
Its roadmap still lists both “Resolve recurring items” and “Mark items as
recurring” as open work. This means the source proves serialization shape, but
it does not prove the full Things application lifecycle. See the [`TaskPatch`]
and [`RecurrenceRule`] definitions and the [`ROADMAP.md`] entries.

[`TaskPatch`]: https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/task.rs
[`RecurrenceRule`]: https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/recurrence.rs
[`ROADMAP.md`]: https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/ROADMAP.md

## `rr` fields

`rr` is an object on a recurring template. The pinned source defines these
fields:

| Field | Wire meaning in the source | Observed/default behavior | Confidence |
| --- | --- | --- | --- |
| `tp` | Repeat mode: `0` fixed schedule; `1` after completion | Defaults to `0` | High |
| `fu` | Frequency-unit bit mask | `8` daily, `16` monthly, `256` weekly; defaults to `256` | High for values in source; mask combinations are unproven |
| `fa` | Frequency amount, “every N units” | Defaults to `1` | High |
| `of` | Offset selectors for weekday/day/ordinal rules | Defaults to an empty list of arbitrary JSON maps | Low for the inner schema; the source does not define the map keys |
| `sr` | Recurrence start reference day timestamp | Optional; defaults to `null`/absent | Medium |
| `ia` | Initial anchor day timestamp for recurrence calculations | Optional; defaults to `null`/absent | Medium |
| `ed` | Recurrence end day timestamp | Defaults to `64092211200`, a far-future sentinel | High for the wire default; exact UI meaning is unproven |
| `rc` | Repeat count | Defaults to `0` | Medium; source names it but does not document whether zero means unlimited |
| `ts` | Task-skip behavior metadata | Defaults to `0` | Low; source gives no behavior description |
| `rrv` | Recurrence-rule version | Defaults to `4` | High for the observed version; future versions are possible |

The defaults above come from Rust `serde` defaults in [`recurrence.rs`].
`sr` and `ia` are `Option<i64>` fields, so they do not default to the far-future
sentinel. The existing general protocol note should not be used as a field
schema; it compresses several different defaults into one sentence.

## Fixed versus after-completion

The source names `tp=0` `FixedSchedule` and `tp=1`
`AfterCompletion`. It describes fixed mode as a cadence and after-completion
mode as an interval anchored after the completion date. This establishes the
high-level distinction only. The source does not prove how `sr`, `ia`, `acrd`,
`rc`, `ts`, or `of` interact in every mode. [`task.rs`] shows that
`acrd` is a separate task property named “after-completion reference date”,
but it is not part of the `rr` object and the CLI does not write it.

[`task.rs`]: https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/task.rs

## Template and instance lifecycle

The read model uses two complementary shapes:

* A recurring template has `rr` present and `rt` empty.
* A generated instance has `rr` absent and a non-empty `rt` list that points to
  its template ID.

The source implements these predicates in [`store/entities.rs`]. The store
also filters recurrence templates from normal Someday output, while instances
remain ordinary task records. The source does not define whether changing a
template updates already-created instances, whether it changes only future
instances, or how a completed instance causes the next instance to appear.

[`store/entities.rs`]: https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/store/entities.rs

## Setting, changing, and clearing

At the wire type level, `TaskPatch.recurrence_rule` is a nested optional:

* field omitted: leave the current `rr` unchanged;
* `"rr": { ... }`: set or replace the rule object;
* `"rr": null`: clear the rule.

This follows `Option<Option<RecurrenceRule>>` plus the `serde` attributes in
[`task.rs`]. `TaskPatch.recurrence_template_ids` (`rt`) is a normal optional
list: omit it to leave links unchanged, or send `[]` to remove the links. The
source does not prove that clearing `rr`, clearing `rt`, or doing both in one
patch is accepted by Things Cloud, nor does it prove the required companion
changes to `icsd`, `acrd`, `icp`, dates, status, or generated instances.

A full create snapshot uses `TaskProps`, where `rr` is an optional field and
`rt` is a list. The pinned `new` command creates ordinary tasks with no repeat
rule and does not provide a recurring-task option. The pinned `edit` command
does not populate the recurrence fields. See [`commands/new.rs`],
[`commands/edit.rs`], and [`wire_object.rs`] for the write dispatch and
operation envelope.

[`commands/new.rs`]: https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/new.rs
[`commands/edit.rs`]: https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/edit.rs
[`wire_object.rs`]: https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/wire_object.rs

## Live verification

Live probes used disposable records in Things 3 on 2026-08-16.

- Creating a repeat rule replaced one normal Task with a new template and a
  new generated Task. The Cloud group deleted the original Task and created
  both new records.
- Changing an after-completion interval from one week to two weeks emitted one
  sparse `Task6` update. It contained only `md` and the complete changed `rr`.
- Changing the same template from after-completion to fixed weekly emitted one
  sparse update. Things rewrote anchors and selectors, and also wrote `tir`.
- Changing it back emitted one sparse update. Things again rewrote the full
  rule. It did not write `tir` in that direction.
- The new adapter changed the disposable interval through Cloud, read the exact
  complete rule back, and restored the old interval.

These results prove lossless interval changes on existing templates. They do
not prove safe creation, removal, or pattern changes through the adapter.

## Why the adapter limits writes

The adapter now retains the complete `rr` object and `rt` links. Its public
model exposes recurrence kind, template ID, mode, unit, and interval. It lets a
caller change only the interval on an exact template. It rejects other repeat
writes because Things can rewrite selectors, anchors, and companion fields.

## Minimal safe implementation and probe plan

1. Add a lossless internal `RecurrenceRule` model. Preserve unknown fields and
   unknown numeric enum values. Keep `rr` omitted, object, and explicit `null`
   as separate states.
2. Expose full rule data only in an explicit detail/read operation. Do not
   infer `of`, `rc`, or `ts` from the coarse public recurrence type.
3. Add a template-only mutation first. Require the exact template ID, current
   revision, and explicit confirmation. Reject generated instances until their
   semantics are proven.
4. Use a disposable test account or disposable test records. Probe, one at a
   time: create a fixed weekly rule; create daily/monthly rules; set `fa` and
   `of`; set an end date and count; create after-completion; complete an
   instance; edit the template; clear `rr`; clear `rt`; and repeat after a
   sync/restart.
5. After every commit, read the history and the materialized state. Verify the
   exact `rr`, `rt`, `icsd`, `acrd`, dates, status, and number/identity of
   generated instances. Record whether edits affect existing instances or only
   future generation.
6. Ship only the smallest proven subset. Keep unknown fields and versions
   read-only. Do not claim support for “change repeat rules” until the clear,
   replacement, fixed, after-completion, and template/instance cases all pass
   read-back tests.

No live Cloud probe was run for this report.
