# Autonomous Project Planning

Phase 9 turns a Boss objective into a materialized initial Plan while keeping
reasoning separate from deterministic control. Initial planning remains a
separate service from Phase 10 CHANGE replanning.

## Planning Boundary

The application first creates only Project identity and an empty ProjectState:

```text
Boss objective
  → ProjectPlanningService persists IDLE → PLANNING
  → Supervisor PLAN returns a typed PlanProposal
  → PlanProposalValidator checks the complete proposal
  → PlanMaterializer constructs domain entities in memory
  → one atomic snapshot persists PLANNING → RUNNING
```

The Supervisor proposes `RequirementProposal`, `MilestoneProposal`, and
`TaskProposal` content. It does not generate the domain Plan ID, mutate
ProjectState, select a Task, or start Codex. The deterministic application
supplies Plan and audit-event identities through injected factories.

Every new Task retains `requirement_ids`, so requirement traceability survives
materialization and persistence. A Plan's requirement order is existing
`requirements_considered` first, followed by newly proposed Requirements in
proposal order. Milestone and Task order are also preserved exactly.

## Deterministic Validation

Before any new entity is saved, validation rejects:

- duplicate or cross-entity proposal IDs and collisions with historical state;
- unknown, inactive, or wrong-Project existing Requirements;
- empty Plans, Milestones, or Task acceptance criteria;
- unknown, duplicate, missing, or multi-Milestone Task membership;
- orphan Tasks and Requirements without Task coverage;
- unknown or duplicate Requirement references;
- unknown, duplicate, self-referential, or cyclic Task dependencies.

There is no model-based repair, sorting, deduplication, or second Supervisor
call. A generated Plan version is `max(existing versions) + 1`; initial state
with an active Plan fails closed.

## Materialization and Source of Truth

Validated proposals become ACTIVE Requirements, PENDING Tasks, pending
Milestones, and one ACTIVE versioned Plan. The final snapshot sets
`active_plan_id`, clears `current_task_id`, transitions the Project to RUNNING,
and records `planning.completed` plus `plan.materialized` in the same save.

ProjectState remains the Source of Truth. Planning progress events are
ephemeral presentation facts and never control validation, persistence,
scheduling, or execution.

Persisted state schema v3 adds explicit Requirement replacement linkage and
rich ImpactAnalysis metadata. Loading schemas v1 and v2 uses deterministic
migrations; v1 Tasks receive `requirement_ids=()` and v2 Requirements receive
`supersedes_id=None`. New Tasks must always have at least one Requirement.

## Failure Handling

The initial `planning.started` snapshot is saved before Supervisor PLAN is
called. Provider, incomplete-response, parsing, or deterministic proposal
failure transitions PLANNING to HUMAN_REQUIRED and records a safe failure
category. Prompts, raw model responses, rationale, and credentials are not
stored in failure metadata. Failures are not retried or automatically repaired.

If the initial save fails, the Supervisor is never called. If the final atomic
materialization save fails, no RUNNING Plan or Task is persisted and Codex is
never started.

## Autonomous Composition

`AutonomousProjectService` is a small composition layer:

```text
planning.plan(request)
  → if ready: project_execution.run()
  → otherwise: stop without execution
```

Planning and execution remain independently callable and independently tested.
The composition layer performs no additional LLM reasoning.

## Verification

The non-billable local smoke uses a fixed fake PLAN and fake REVIEW policy with
real local Codex in a disposable Git repository:

```bash
.venv/bin/python scripts/local_codex_autonomous_smoke.py
```

The authenticated path is a Boss-only manual gate. It makes real DeepSeek PLAN
and REVIEW requests, may incur billing, and then runs real local Codex only in
a disposable repository:

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_autonomous_project_e2e.py
```

Neither automated verification nor Codex executes that authenticated command.
It performs no push, tag, release, deployment, or production repository work.
