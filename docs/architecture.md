# Architecture

## System context

```text
Boss Interface
      ↕
Supervisor Layer
      ↕ structured requests and decisions
Orchestrator Layer ──↔ State Layer
      ↕ validated task and report contracts
Execution Layer
```

The Orchestrator owns control flow. The State Layer owns durable truth. The Supervisor reasons about what should happen, and the Codex Worker performs one validated, scoped Task at a time.

## Boss Interface

The Boss Interface is the sole human entry point. It accepts `QUERY`, `CHANGE`, `PAUSE`, and `RESUME` commands and Human Gate approvals. v0.1.0 does not prescribe a final UI.

The interface normalizes commands into structured requests and presents status, risks, decisions, gates, and audit information without becoming a second source of project truth.

## Supervisor Layer

The Supervisor Layer is the reasoning layer. It interprets requirements, creates versioned Plans and Task graphs, generates scoped Worker instructions, reviews ExecutionReports, analyzes changes, identifies risks, and proposes Decisions.

It must not directly mutate a repository, directly control the state machine, or become the only persistent memory. Its structured output is advisory until the Orchestrator validates it against schemas, state, policies, and invariants.

## Orchestrator Layer

The Orchestrator Layer is the deterministic control plane. It validates all state transitions; handles commands, dispatch, lifecycle, Safe Points, and execution limits; coordinates persistence; invokes the Supervisor and Worker; and enforces Human Gates.

All structured Supervisor and Codex outputs must pass schema and contextual validation before they can alter state. Invalid or ambiguous output fails closed instead of being interpreted from prose.

## Execution Layer

The Execution Layer contains the Codex Worker. v0.1.0 permits at most one active Codex Worker for a project. The Worker receives a bounded Task, works in the repository under explicit permissions, verifies its work, and returns a structured ExecutionReport. It has no authority to change the Plan or approve gated actions.

## State Layer

The State Layer contains the Project State Store, the Source of Truth for identity, requirements, versioned Plans, Task state, execution evidence, quality, changes, decisions, gates, and events. It supports recovery independently of any conversation or model session.

ProjectEvents form an append-only audit trail. Current views may be derived from stored entities and events, but conversation transcripts alone are insufficient.

## Core interaction flows

### Normal execution

1. The Supervisor proposes a versioned Plan and structured Task graph.
2. The Orchestrator validates and persists them.
3. The Orchestrator dispatches one eligible Task to the Codex Worker.
4. The Worker returns a structured ExecutionReport with verification and Git state.
5. The Orchestrator validates and persists the report, then requests Supervisor review.
6. The Supervisor returns a structured Decision.
7. The Orchestrator validates the Decision and performs the legal transition or dispatch.

### Requirement change

1. The Boss submits CHANGE; the Orchestrator persists it and stops new dispatch.
2. The active atomic turn reaches a Safe Point.
3. Repository and execution state are reconciled and persisted.
4. The Supervisor proposes an ImpactAnalysis and a new Plan version.
5. The Orchestrator validates the proposal, updates affected Tasks audibly, and resumes only when safe.

### Human Gate

The Orchestrator detects a gated action before execution, records its exact scope, and enters `HUMAN_REQUIRED`. Only a matching Boss approval permits that action. Rejection or missing approval leaves it unexecuted.

## Architectural principles

1. **State over conversation.** Durable structured state, not session history, defines reality.
2. **Structured contracts over free text.** Machines validate control inputs and outputs explicitly.
3. **Deterministic orchestration over LLM-controlled control flow.** The Orchestrator owns transitions and policy enforcement.
4. **Human authority over irreversible actions.** Consequential external effects require explicit approval.
5. **Safe interruption over hard cancellation.** CHANGE and PAUSE reconcile an atomic turn at a Safe Point.
6. **Versioned plans over mutable undocumented plans.** Replanning creates traceable versions.
7. **Auditable changes over silent replanning.** Changes, analyses, decisions, and transitions are recorded.
8. **One worker first, scale later.** The MVP favors predictable control and recovery over concurrency.

## v0.1.0 execution chain

The release-candidate chain is:

```text
Boss CLI / Chat
  -> Planning
  -> Execution Ownership
  -> Supervisor + Codex Worker
  -> TaskCycle
  -> precise Git delivery
  -> optional CHANGE / replanning
  -> Human Resolution when gated
  -> deterministic final verification
  -> final Supervisor review
  -> DONE or CANCELLED
```

Each layer owns one boundary. The Supervisor reasons but cannot mutate state or
approve a Human Gate. The Orchestrator owns legal transitions. TaskCycle owns
Task attempts and merges them into the latest persisted snapshot. Execution
Ownership prevents a second Worker. Git delivery alone stages and commits exact
Task paths. ProjectState is the sole durable source of project facts.

STOP and PAUSE remain safe-point controls. If an in-flight Task fails after a
PAUSE or CHANGE was persisted, the project enters typed `HUMAN_REQUIRED` rather
than losing the external control state or releasing ownership as if recovery
were certain.
