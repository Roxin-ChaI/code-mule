# Code Mule

> Powered by prompts. Paid in tokens.

> You define the goal. The mule does the coding.

中文名：赛博码农（牛马）

Code Mule is a human-supervised autonomous software development system.

## Problem

Current agentic coding workflows often require a human to perform mechanical coordination:

- copying prompts from a Supervisor to Codex;
- copying execution reports from Codex back to the Supervisor;
- advancing development phases;
- maintaining the current project status; and
- replanning after new or changed requirements.

Code Mule aims to remove this coordination burden while preserving human authority over important decisions.

## Goal

The Boss is responsible only for:

- defining goals;
- querying the project;
- changing requirements;
- making key decisions; and
- approving Human Gates.

The system is responsible for:

- planning;
- task decomposition;
- execution dispatch;
- execution review;
- verification;
- progress tracking;
- change impact analysis; and
- replanning.

## v0.1.0 Scope

The v0.1.0 MVP defines a single-project, single-worker workflow coordinated by a deterministic Orchestrator. It uses structured contracts, versioned plans, durable project state, safe interruption, verification, and fail-closed Human Gates. It is an MVP definition, not a production-ready system.

## Architecture

```text
Boss
  ↕
Supervisor
  ↕
Orchestrator
  ├── Project State Store
  └── Codex Worker
```

## Local Setup

Use the repository-local Python 3.12 environment so Code Mule and its declared runtime dependencies share one interpreter:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

Do not rely on packages installed in the global `python3.12` environment.

## Boss CLI Quick Start

```bash
CODE_MULE_BIN="$(pwd)/.venv/bin/code-mule"
mkdir -p /tmp/code-mule-calculator
cd /tmp/code-mule-calculator
git init
git config user.name "Code Mule Boss"
git config user.email "boss@example.invalid"
touch README.md
git add -- README.md
git commit -m "chore: initialize workspace"

"$CODE_MULE_BIN" init \
  --project-id calculator \
  --name "Calculator" \
  --workspace "$PWD"

export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
# Terminal A (blocking execution)
"$CODE_MULE_BIN" run --objective "Create a tested calculator"

# Terminal B, from the same directory while execution is active
"$CODE_MULE_BIN" status
"$CODE_MULE_BIN" chat
"$CODE_MULE_BIN" change "Add multiply support"

# After Terminal A reaches the CHANGE Safe Point
"$CODE_MULE_BIN" change --apply

# Or cancel an active project instead of applying/completing it
"$CODE_MULE_BIN" stop
```

Commands discover `.code-mule/project-state.json` in the current directory by
default. The workspace must be a clean Git repository with an initial commit.
`run` is blocking; control commands are issued from another terminal against
the same persisted state. `stop` is an alternative terminal path, not a command
to run after the Project is already DONE.

The formal Boss CLI also provides `ask`, `change`, `change --apply`, `pause`,
`resume`, `stop`, `inspect`, `approve`, `reject`, and `resolve`. See
[Boss CLI](docs/cli.md) and [Human Resolution](docs/human-resolution.md) for
action-scoped approval, state behavior, Human Gates,
environment variables, exit codes, and the manual real E2E.
Natural-language control is documented in [Boss Chat](docs/boss-chat.md).

Default CLI output uses readable Boss terminology and hides internal IDs and
raw statuses. Use per-command `--verbose` for an auditable internal view. During
`run` and `change --apply`, an adaptive terminal dashboard shows Project, Task,
Codex Worker, Supervisor, deterministic progress, and recent real activity;
redirected output remains line-oriented with no ANSI controls.

## Manual DeepSeek E2E

This command makes a real DeepSeek API request and may incur charges. It is a Boss-only manual gate and is not run by automated verification or Codex.

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_deepseek_supervisor_e2e.py
```

See [DeepSeek Supervisor Provider](docs/deepseek-supervisor.md) for the runtime and state boundaries.

## Local Codex Worker

Phase 6 provides a Python library boundary for executing one task through the
locally installed `codex app-server`. It uses the executable's stdio JSONL
protocol and contains no API key or token configuration. Approval and user-input
requests fail closed; Code Mule does not answer or approve them automatically.

See [Codex Worker](docs/codex-worker.md) for the tested Codex version, lifecycle,
sandbox boundary, and structured `ExecutionReport` mapping.

## Single-Task Cycle

Phase 7 provides a bounded autonomous cycle for exactly one current task. A
deterministic runtime persists each structured Codex report before Supervisor
REVIEW, reuses one Codex thread for REWORK turns, and stops on CONTINUE, DONE,
HUMAN_REQUIRED, execution limit, or Worker failure. It does not select another
task or apply a Plan automatically.

See [Single-Task Autonomous Cycle](docs/task-cycle.md) for persistence ordering,
evidence rules, Human Gates, local smoke verification, and the Boss-only manual
DeepSeek + Codex E2E command.

## Multi-Task Project Execution

Phase 8 adds a pure deterministic scheduler and a bounded project runtime over
an already-materialized active Plan. It dispatches one Ready Task at a time,
persists before starting Codex, reloads state at every Task boundary, honors
PAUSE and CHANGE safe points, and completes the Project only when the active
Plan satisfies deterministic completion rules. It does not apply Supervisor
Plan proposals or perform CHANGE replanning.

See [Multi-Task Project Execution](docs/project-execution.md) for ordering,
recovery, completion, Human Gate, real local Codex smoke, and the Boss-only
manual DeepSeek + Codex multi-task E2E command.

## Runtime Progress

Phase 8.5 adds real-time execution progress and console observability without
turning presentation into control state. Typed ephemeral events expose project,
task, Codex Worker, and Supervisor stages. The standard-library renderer uses a
live TTY dashboard or line-oriented redirected logs, while ProjectState remains
the Source of Truth. This is runtime/manual-script presentation support, not a
formal CLI or GUI.

See [Runtime Progress and Observability](docs/progress-observability.md) for the
event boundary, truthful Codex activity projection, deterministic percentage,
privacy rules, and renderer lifecycle.

## Autonomous Project Planning

Phase 9 accepts a natural-language Boss objective for an empty IDLE Project,
asks the Supervisor for a structured planning proposal, validates its complete
identity/dependency/traceability graph deterministically, and atomically
materializes versioned Requirements, Milestones, Tasks, and an active Plan.
`AutonomousProjectService` can then hand the RUNNING Project to the existing
single-worker execution runtime. Initial bootstrap remains separate from
CHANGE replanning.

See [Autonomous Project Planning](docs/project-planning.md) for proposal rules,
state schema migration, failure handling, local fake-PLAN verification, and
the Boss-only real DeepSeek + Codex autonomous-project E2E command.

## Boss CHANGE Replanning

Phase 10 accepts a persisted Boss CHANGE while a project is RUNNING, stops at
the next Task Safe Point, requests one structured Impact/Replan proposal, and
validates the complete replacement graph deterministically. It supersedes the
old Plan, preserves completed work by default, materializes Plan vN+1 in one
save, and resumes the existing sequential execution service only after that
save succeeds. Invalid proposals and provider failures fail closed without
repair or retry.

See [Boss CHANGE and Replanning](docs/change-replanning.md) for lifecycle,
versioning, progress, failure behavior, the real local Codex smoke, and the
Boss-only real DeepSeek manual E2E.

## Git Delivery

Phase 16 captures a clean repository baseline before every Task, validates the
Worker's exact path ownership, and creates one deterministic local commit only
after verification and Supervisor acceptance. Commit evidence is persisted
before the Task becomes completed. REWORK attempts remain uncommitted, while
push, tag, and release stay behind a Human Gate.

See [Git Delivery Workflow](docs/git-delivery.md) for the clean-baseline rule,
ownership checks, commit boundary, and disposable local E2E.

## Project-Level Final Verification

Phase 17 makes Task completion distinct from Project completion. After every
active-Plan Task has a delivery commit, Code Mule runs only the deterministic
checks persisted in ProjectState, verifies the repository is clean at the last
Task delivery HEAD, and requests a strict final Supervisor review. The Project
enters `DONE` only when every required check passes and that review approves.

See [Project-Level Final Verification](docs/project-verification.md) for the
completion boundary, safe command execution, failure handling, evidence model,
and disposable local E2E.

## Project Cancellation

Phase 18 adds an explicit Boss STOP lifecycle. An idle project cancels
immediately; an active Task is allowed to reach its normal safe completion
boundary before Code Mule cancels all remaining active-Plan Tasks. Completed
work and local delivery commits are preserved, and cancellation never runs
project final verification or rolls work back.

See [Project Cancellation](docs/project-cancellation.md) for STOP versus PAUSE
and failure, Safe Point behavior, preservation rules, and local E2E coverage.

## Status

Code Mule v0.1.0 is release-candidate ready after the Phase 19 local quality
gate. It includes the Boss CLI and Chat, bounded Supervisor regeneration,
single-owner execution, safe CHANGE and STOP boundaries, typed Human Actions,
per-Task Git delivery, and project-level final verification. ProjectState
schema v8 remains the Source of Truth and migrates snapshots from v1 through
v7.

Automated tests and local release E2E never call DeepSeek. The authenticated
DeepSeek + real Codex release flow remains a Boss-only manual gate. v0.1.0 is a
single-project, single-worker local MVP—not a daemon, parallel runner, remote
deployment system, or automatic push/tag/release tool. See
[Release Readiness](docs/release-readiness.md) and
[Troubleshooting](docs/troubleshooting.md).
