# Project Revisions and Post-Completion Change

Completed Code Mule projects are not closed forever. A Boss may evolve a DONE
project with the normal `code-mule change ...` command. Revisions are linear:

```text
Revision 1 (DONE, Plan v1)
  → code-mule change "persist after refresh"
  → CHANGE_REQUESTED
  → apply → REPLANNING
  → Plan v2, Revision 2
  → new Tasks → verification → final review → DONE
```

## Revision records

Each `ProjectRevision` stores:

- revision number (monotonic; never rewritten after completion);
- started/completed timestamps;
- base revision and originating ChangeRequest;
- Plan id/version;
- baseline and completion Git HEADs;
- verification and final-review status plus result evidence.

Revision 1 is created when the first Plan is materialized and completed when
final verification passes. A CHANGE after DONE starts Revision 2 (then 3, ...).
Mid-flight CHANGE during RUNNING/PAUSED stays inside the current revision and
only advances the Plan version.

## History invariants

- Plan v1 Tasks are never reopened or set back to TODO after Revision 1.
- A modification creates a new Task with typed lineage:
  `supersedes_task_id` and/or `derived_from_task_ids` point to the historical
  Task it changes or builds on. One historical Task ID cannot appear in both
  fields: replacement and derivation are distinct facts.
- Historical work may be reused semantically through impact and lineage
  references, but it is never reused as an executable Task or Milestone in the
  new Revision. Each Revision owns fresh executable Plan objects.
- Revisions 1/2/3 each keep their own Plan, completed Tasks, Git commits,
  verification evidence, and completion HEAD.
- A new Revision never inherits PASS from an earlier verification/final review;
  both reset to `not_run` when Revision N+1 starts.

## Replanning and progress

A post-completion Plan vN+1 contains only the executable Tasks for the new
Revision. Completed historical Tasks are not counted in current progress.
`status` may show a `Reused` count for work carried forward from earlier
versions.

The Supervisor owns the semantic proposal and its fresh Requirement,
Milestone, and Task identifiers. Code Mule owns the persistent Plan identity,
validates every proposed identifier and lineage edge, and atomically persists
the Plan, Revision, executable graph, and ChangeRequest binding.

## Git and workspace safety

Reopening requires the workspace HEAD to equal the completed Revision's
recorded HEAD and the working tree to be clean. Dirty work, staged drift, or an
externally advanced HEAD block the new Revision with a deterministic error.
Code Mule never stashes, resets, cleans, rewrites history, amends commits, or
adopts external commits.

## Recovery

- Change persisted before any model call: re-run `change --apply` with the same
  request.
- Replanning interrupted before Plan materialization: a restartable
  REPLANNING state performs fresh impact analysis and never duplicates a
  Revision or Plan.
- Plan vN+1 persisted: `run` resumes the existing Plan without calling the
  Supervisor again.
- Worker-level interruptions reuse the existing deterministic recovery rules.

## Schema migration

ProjectState schema v13 migrates existing v12 snapshots:

- a historical DONE project becomes a completed Revision 1 with the recorded
  Plan/HEAD and conservative verification evidence (`pass` only when persisted
  evidence shows approval, otherwise `unknown`);
- RUNNING and other active v12 snapshots become an in-progress Revision 1
  without changing their execution behavior;
- historical Plans, Tasks, commits, ChangeRequests, and verification records
  are preserved.
