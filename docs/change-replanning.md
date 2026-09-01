# Boss CHANGE and Replanning

Phase 10 implements one pending Boss ChangeRequest at a deterministic Task Safe
Point. ProjectState remains the Source of Truth; the Supervisor proposes an
Impact/Replan result, while deterministic code validates and applies it.

## Lifecycle and Safe Point

```text
Boss CHANGE
  → persist ChangeRequest + boss.change + CHANGE_REQUESTED
  → finish the already-owned TaskCycle, if any
  → stop before dispatching another Task
  → require current_task_id=None
  → persist CHANGE_REQUESTED → REPLANNING
  → request one Supervisor Impact Analysis
  → validate the complete replacement graph
  → atomically save Plan vN+1 and RUNNING
  → resume sequential project execution
```

CHANGE never hard-cancels an active Codex turn. TaskCycle reloads persisted
state after Worker completion so a ChangeRequest written during the turn is
preserved. Multiple simultaneous ChangeRequests are not supported.

QUERY remains read-only in CHANGE_REQUESTED and REPLANNING.

## Structured Impact and Validation

The proposal carries exact IDs for the ChangeRequest, affected Requirements and
Tasks, lifecycle changes, dependency changes, and replacement Milestones. New
Requirement and Task bodies are typed nested proposals. Requirement updates
create a new entity with `supersedes_id`; they never overwrite historical
content.

Validation rejects unknown IDs, historical ID collisions, duplicate or
conflicting reopen/cancel operations, invalid status classifications, missing
traceability, unavailable or duplicate dependencies, self-dependencies,
cycles, incomplete Milestone membership, and active Requirements without Task
coverage. CANCELLED Tasks cannot satisfy dependencies. There is no fuzzy ID
matching, automatic repair, or second Supervisor request.

## Versioned Atomic Materialization

The old active Plan becomes SUPERSEDED. The new Plan ID comes from an injected
deterministic factory and its version is `max(history) + 1`. Unaffected
COMPLETED Tasks keep identity, status, and execution history. A completed Task
changes to REOPENED only when `tasks_to_reopen` names it explicitly. Pending
Tasks may be reused; cancelled Tasks are not restored automatically. New
Milestones define membership for every Task retained by the replacement Plan.

Requirements, Tasks, Milestones, Plan versions, ImpactAnalysis, ChangeRequest,
Project status, and audit events are constructed in memory before one final
`store.save()`. Execution resumes only after that save succeeds. A save failure
leaves the previously persisted REPLANNING snapshot in place and starts no
Worker.

Persisted ProjectState schema v3 records `Requirement.supersedes_id` and the
expanded ImpactAnalysis metadata. Deterministic migrations preserve v1 and v2
snapshots.

## Failure and Progress

Provider or parser failure records `replanning.failed` and transitions to
HUMAN_REQUIRED. Proposal rejection records `replanning.proposal_rejected` and
also transitions to HUMAN_REQUIRED. Neither path retries the provider.

Presentation-only progress events cover change requested, replanning start,
Supervisor impact start/completion, materialization, completion, and failure.
They expose lifecycle facts such as `Plan v1 → v2`; they do not expose or
fabricate hidden model reasoning.

## Verification

The non-billable local smoke uses fake PLAN, Impact, and REVIEW responses with
real locally authenticated Codex Workers in a disposable Git repository:

```bash
.venv/bin/python scripts/local_codex_change_replanning_smoke.py
```

It creates add/subtract work, injects CHANGE after the first Task, proves no
second Task dispatch occurs before replanning, creates Plan v2, preserves the
completed Task, adds multiply work, resumes Codex, and independently runs the
generated tests.

The complete real DeepSeek + Codex path is a Boss-only manual gate. It makes
billable authenticated requests and is never run by automated verification or
Codex:

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_change_replanning_e2e.py
```

Both paths use disposable repositories. Neither pushes, tags, releases,
deploys, or mutates the Code Mule production repository.
