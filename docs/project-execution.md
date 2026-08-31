# Multi-Task Project Execution

Phase 8 executes an already-materialized active Plan through deterministic,
single-worker scheduling. It does not ask the Supervisor to create or apply a
Plan: PlanProposal application and CHANGE replanning remain outside this phase.

## Scheduler and Runtime Boundary

`TaskScheduler` is pure. It resolves only `Project.active_plan_id`, validates
that Plan's ordered Milestone and Task graph, rejects missing dependencies and
cycles, and selects the first Ready Task in declared Milestone/Task order. A
Task is Ready only when it is PENDING or REOPENED and every direct dependency
is COMPLETED. CANCELLED dependencies do not satisfy readiness.

`ProjectExecutionService` owns deterministic control and persistence. Before a
Worker starts, it changes the selected Task to IN_PROGRESS, sets
`Project.current_task_id`, records `task.dispatched`, and saves the snapshot.
A failed save therefore prevents the Worker side effect. A fresh
`TaskCycleService` and Codex thread are used for each dispatched Task.

The initial prompt contains the project name, current Task fields, acceptance
criteria, dependency IDs, brief identities for completed direct dependencies,
and execution constraints. It does not dump ProjectState, historical Plans,
unrelated Tasks, event history, credentials, or API keys. The Worker layer
continues to enforce its native structured output schema.

## Completion and Safe Stops

After every Task cycle, the runtime reloads ProjectState. It marks a Milestone
`completed` exactly once when all of its Tasks are COMPLETED or CANCELLED. Only
the deterministic rule that every active-Plan Task is COMPLETED or CANCELLED
may complete the Plan and transition the Project from RUNNING to DONE. A
Supervisor DONE decision completes only its current Task.

The loop stops without dispatching another Task when it observes:

- PAUSED_BY_BOSS or CHANGE_REQUESTED at a Task boundary;
- HUMAN_REQUIRED or a TaskCycle human gate;
- no Ready Task while the Plan remains incomplete; or
- `max_tasks_per_run`, which persists HUMAN_REQUIRED before returning.

PAUSE and CHANGE are safe-boundary controls in v0.1.0. They do not interrupt an
in-flight atomic TaskCycle. If startup finds `current_task_id` pointing to an
IN_PROGRESS Task, the runtime does not replay it because the previous Codex
session is not persisted; it records `project.execution_recovery_required` and
fails closed to HUMAN_REQUIRED.

## Verification Paths

The non-billable local smoke uses a fixed fake Supervisor, two dependent Tasks,
and real locally authenticated Codex app-server sessions in a disposable Git
repository:

```bash
.venv/bin/python scripts/local_codex_project_smoke.py
```

The Boss-only full multi-task E2E uses real DeepSeek REVIEW calls and can incur
charges. It also uses real local Codex, but only inside a disposable repository:

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_project_execution_e2e.py
```

The manual script prints an explicit warning and uses
`DefaultHttpx2Client(trust_env=False)` without disabling TLS verification.
Automated verification and Codex must never execute this billable command.
