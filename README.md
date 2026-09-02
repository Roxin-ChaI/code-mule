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
.venv/bin/code-mule init \
  --project-id calculator \
  --name "Calculator" \
  --workspace /absolute/path/to/disposable/repository

export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/code-mule run --objective "Create a tested calculator"
.venv/bin/code-mule status
```

The formal Boss CLI also provides `ask`, `change`, `change --apply`, `pause`,
and `resume`. See [Boss CLI](docs/cli.md) for state behavior, Human Gates,
environment variables, exit codes, and the manual real E2E.

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

## Status

Early development.

The DeepSeek Supervisor provider, local Codex Worker, deterministic task cycle,
bounded multi-task execution, natural-language initial project planning, and
deterministic CHANGE replanning are
in v0.1.0 development. The
Supervisor uses the official `openai` Python SDK only as an OpenAI-compatible
client for the DeepSeek endpoint, while the Worker uses the local Codex
app-server process. ProjectState remains the Source of Truth. Automated tests
do not call real model APIs, and the real DeepSeek + Codex multi-task E2E
remains a Boss-only manual gate. Versioned Plan materialization and CHANGE
replanning are available; parallel/multi-project execution and production
readiness are not yet
available. Real-time progress and console observability are available for the
current runtime and manual verification paths.
