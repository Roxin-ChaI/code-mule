# v0.1.0 MVP Requirements

## Purpose and scope

Code Mule v0.1.0 is a human-supervised autonomous software development system for one project and one active Codex Worker. It automates planning, dispatch, review, verification, status tracking, requirement-change analysis, and replanning while keeping the Boss in authority over consequential actions.

This document specifies the MVP behavior. It does not claim production readiness.

## 1. Boss

The Boss is the sole human project owner. The Boss must not be required to relay messages between the Supervisor and Codex Worker.

The Boss has four command types.

### QUERY

QUERY is read-only and may request current progress, the current task, completed and remaining tasks, blockers, quality status, recent changes, the current plan, or risks.

A QUERY must not mutate Project State, trigger a project-state transition, or interrupt an active Codex Task.

### CHANGE

CHANGE adds, removes, or modifies requirements. A CHANGE must never be sent directly to the current Codex Worker. The system must:

1. create a `ChangeRequest`;
2. stop dispatching new Tasks;
3. allow the active atomic task to reach a Safe Point;
4. perform an `ImpactAnalysis`;
5. identify affected completed, in-progress, and pending Tasks;
6. create a new Plan version;
7. update the Task graph;
8. REOPEN completed Tasks when necessary;
9. BLOCK or CANCEL obsolete Tasks when necessary; and
10. resume execution only from the validated new Plan.

### PAUSE

PAUSE requests a safe project pause. The system must not pause by forcibly leaving the repository working state inconsistent or unknown. It must reach and record a Safe Point before entering `PAUSED_BY_BOSS`.

### RESUME

RESUME may resume only a project paused in `PAUSED_BY_BOSS`. Before resuming, the Orchestrator must verify that persisted Project State and repository state agree. A mismatch must fail closed.

## 2. Supervisor

The Supervisor is the reasoning layer. It is responsible for:

- understanding and normalizing requirements;
- project planning and milestone generation;
- task decomposition and dependency reasoning;
- Codex prompt generation;
- ExecutionReport review;
- progress reporting;
- change impact analysis and replanning;
- risk identification;
- Human Gate decisions; and
- completion decisions.

The Supervisor must not modify the repository, control the state machine directly, or hold the only copy of project state. Every Supervisor output that affects control flow must be structured and validated by the Orchestrator.

## 3. Orchestrator

The Orchestrator is a deterministic control layer, not an LLM Agent. It is responsible for:

- validating and applying state-machine transitions;
- command handling and message routing;
- task dispatch and lifecycle control;
- persistence coordination;
- Supervisor invocation and Codex lifecycle management;
- Safe Point handling and Boss interrupts;
- Human Gate enforcement and execution limits; and
- validation of structured contracts and schemas.

Control flow must not depend on parsing free-form text.

## 4. Codex Worker

The Codex Worker is the execution layer. For a scoped Task, it may inspect the repository, modify files, run tests and static checks, inspect the Git diff, perform explicitly allowed local Git operations, and return a structured `ExecutionReport`.

The Codex Worker must not change the overall project goal, approve requirements, alter the Plan, bypass a Human Gate, or decide final project completion.

## 5. Project State Store

The Project State Store is the system Source of Truth. It must persist:

- project identity and requirements;
- the current Plan version, Milestones, Tasks, and dependencies;
- the current Task;
- ChangeRequests and ImpactAnalyses;
- Supervisor Decisions and ExecutionReports;
- QualityStatus;
- ProjectEvents; and
- Human Gates.

Conversation history must not be the sole source of project state. The system must be recoverable from the Project State Store after loss of a Supervisor or Codex session, application restart, or model change.

## Supervisor Decision Contract

Every review cycle must emit a structured `Decision` with exactly one of these control types:

- `CONTINUE`: the current Task passed and the next eligible Task may be dispatched.
- `REWORK`: the current Task did not pass and requires a new Codex attempt.
- `HUMAN_REQUIRED`: Boss input or approval is required; automatic execution must stop.
- `DONE`: all acceptance criteria for the current Plan are satisfied.

The Orchestrator must validate the Decision and its context. It must never infer a control decision from keywords in reasoning text.

## Human Gate

In v0.1.0, explicit human approval is required by default for:

- `git push` and force push;
- tag creation and GitHub Release creation;
- destructive file deletion;
- destructive database migration;
- production deployment;
- secret or API-key use;
- paid external API execution;
- remote infrastructure mutation; and
- any irreversible external side effect.

Human Gates are fail-closed. Missing, expired, ambiguous, or scope-mismatched approval must prevent the action. Approval must identify the proposed action and must be recorded in the audit trail.

## Safe Point

An atomic Codex Turn is the smallest execution unit that the Orchestrator does not interrupt under normal CHANGE or PAUSE handling. When either command arrives, the Orchestrator stops new dispatches and normally waits for the active turn to finish. It then obtains and validates the `ExecutionReport`, inspects repository state, persists the resulting Project State and audit event, and only then pauses or replans.

If execution fails, no valid report is available, or repository state is unknown or inconsistent, the project must enter `HUMAN_REQUIRED` or `FAILED`. The system must not claim a safe pause without evidence.

## v0.1.0 explicit non-goals

- multiple Codex Workers;
- multi-agent software-company simulation;
- Web GUI;
- Jira or Linear integration;
- automatic GitHub Releases;
- production deployment;
- organization or team permissions;
- concurrent scheduling across multiple projects; and
- autonomous secret management.
