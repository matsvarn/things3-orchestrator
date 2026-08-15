# Things Cloud capability audit

Reviewed 2026-08-16 against the current adapter, its tests, the pinned
`things3-cloud` sources, and live disposable-record probes.

## Result

The adapter has good coverage for core Task, Project, Area, tag assignment,
checklist, heading, and recoverable Trash operations. It now keeps complete
repeat-rule objects and repeat-template links. It can safely change only the
interval of an existing repeat template.

The adapter does not implement all Things Cloud fields or lifecycles. The
largest remaining gaps need more live proof before they become public writes.

| Capability | Read | Write | Status |
| --- | --- | --- | --- |
| Task and Project title, notes, status | Yes | Yes | Good |
| Project and Area placement | Yes | Yes | Good |
| Start, deadline, reminder, evening | Main facts | Yes | Companion fields are partial |
| Tags on items | Yes | Yes | Good |
| Tag administration | Title and parents | Create or reuse | Rename, delete, hierarchy, and order are missing |
| Checklist rows | Core fields | Create, edit, order, remove | Timestamps and multi-parent state are not modeled |
| Headings | Yes | Create, rename, assign, clear | Reorder, move, and remove are missing |
| Trash | Yes | Move to Trash | Restore is missing |
| Permanent Task or Project deletion | N/A | No | Intentionally blocked |
| Repeat rule | Mode, unit, interval | Interval only | Pattern and lifecycle changes are missing |
| Repeat-template links | Yes | No | Read-only |
| Rich structured notes | Text and format signal | No | Writes are rejected |

## Safety decisions

- Delete requests move Tasks and Projects to recoverable Trash. A permanent
  delete of a non-empty Project left its Tasks with dangling parent IDs in a
  live probe. Permanent deletion stays unavailable.
- Heading removal stays unavailable until assigned-Task behavior is proven.
- Repeat creation is not one update. Things deleted the original Task, then
  created a template and a generated Task. The adapter does not reproduce this
  lifecycle.
- Repeat mode and pattern changes can rewrite selectors, anchors, and companion
  fields. Only interval changes preserve all other rule data exactly.
- Generated repeat instances remain read-only. Completing one can affect the
  next-instance lifecycle, which the Cloud service alone might not perform.

## Reliability gaps

The adapter does not retain all `Task6` companion fields. Important examples
are `tir`, `rmd`, `icsd`, `acrd`, `icp`, and `icc`. Read-back verifies the
fields used by current public writes, but it is not a lossless full protocol
model.

Timeout reconciliation scans a bounded number of history pages. Very large
backlogs can return an unknown outcome after a successful write. The cache
stores folded state, not an append-only raw history log.

The current request headers and generated-ID algorithm differ from the pinned
upstream client. Both work in live use. They should remain monitored because
the Cloud protocol is unofficial.

## Recommended order

1. Retain and test recurrence companion fields before pattern changes.
2. Add a public restore-from-Trash operation.
3. Prove generated-instance completion on a host without Things.app running.
4. Prove heading removal and reorder behavior.
5. Add tag rename, hierarchy, order, and safe deletion.
6. Improve reconciliation and raw-history recovery.

