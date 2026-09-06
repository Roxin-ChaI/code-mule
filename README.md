# Code Mule

[中文](README.zh-CN.md)

> Powered by prompts. Paid in tokens.

Human-supervised autonomous software engineering with DeepSeek + Codex.

Turn one coding objective into a controlled **PLAN → CODE → REVIEW → VERIFY →
COMMIT** workflow.

![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB)
![Tests 498](https://img.shields.io/badge/tests-498%20passed-2E7D32)
![ProjectState v11](https://img.shields.io/badge/ProjectState-v11-6A5ACD)

## Demo

Code Mule turns an objective into reviewed, local Git delivery:

```text
Boss Objective
  → PLAN → Task → Codex Worker → Supervisor Review
  → Verification → Local Git Commit → DONE

PROJECT
Status      Completed
Plan        v1
Progress    6 / 6

PROJECT COMPLETED
```

When a product decision is missing, the Task stops safely:

```text
ACTION REQUIRED
Category    Worker input
Question    How should the leaderboard be persisted?
Choices
- localStorage
- session memory

code-mule answer <action-id> "localStorage"
code-mule run
```

`answer` only records the decision. The next explicit `run` starts a fresh
Worker session and continues the same Task.

## Why Code Mule

Typical agent workflows leave orchestration to the human:

```text
Human → ChatGPT → copy prompt → Codex → copy result → ChatGPT
```

Code Mule makes that loop a controlled engineering workflow:

```text
Boss → Supervisor → Runtime → Codex Worker → Review → Git Delivery
```

The practical differences are:

- durable `ProjectState` instead of hidden conversation state;
- deterministic orchestration instead of prose-driven control;
- typed Human Gates for input, approval, and risky operations; and
- Git-native, reviewed Task delivery.

## How It Works

```text
Objective
    ↓
  PLAN
    ↓
Task → Codex Worker → REVIEW → VERIFY → COMMIT
 ↑                                      ↓
 └────────────── Next Task ─────────────┘
                       ↓
                 FINAL REVIEW
                       ↓
                      DONE
```

The Supervisor proposes typed plans and decisions. The deterministic runtime
owns state transitions, scheduling, verification, and Git delivery. See the
[architecture](docs/architecture.md) for the internal boundaries.

## Features

- **Autonomous planning** — convert one objective into a versioned Plan and
  dependency-aware Tasks.
- **Codex execution** — run one bounded local Worker on one Task at a time.
- **Supervisor review** — review every Task before it can be delivered.
- **Git-native delivery** — turn each accepted Task into one precise local commit.
- **Requirement changes** — route CHANGE through impact analysis and Plan vN+1.
- **Human Gates** — stop safely for Worker Input, approval, or external effects.
- **Durable recovery** — recover from persisted `ProjectState`, not chat history.

## Human Control

```text
CHANGE → Impact analysis → Replan
INPUT  → Boss answer → Fresh-session continuation
RISK   → Typed Human Gate
STOP   → Safe cancellation, no rollback
```

Models do not directly modify project state. Ambiguous side-effect requests do
not execute, and approval is bound to one specific action.

## Prerequisites

- Python 3.12
- Git and a target repository with at least one commit
- [Codex CLI](https://developers.openai.com/codex/cli) with local authentication
- A DeepSeek API key for Supervisor calls

Check the local Codex installation before starting:

```bash
codex --version
codex app-server --help
```

Real E2E has been validated with **Codex CLI 0.153.4**. This is a tested version,
not a proven minimum version.

## Quick Start

### 1. Install Code Mule

Install the tool in its own directory:

```bash
git clone https://github.com/Roxin-ChaI/code-mule.git /path/to/code-mule
cd /path/to/code-mule
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
code-mule --help
```

`/path/to/code-mule` is the tool repository. Keep its virtual environment
activated, or invoke `/path/to/code-mule/.venv/bin/code-mule` directly.

### 2. Prepare the target repository

`--workspace` points to a different Git repository that Code Mule will modify:

```bash
cd /path/to/my-project
git status --short
```

The target must have at least one commit, and the command above must print
nothing. For a new repository, create a minimal baseline:

```bash
mkdir -p /path/to/my-project
cd /path/to/my-project
git init
printf "# My Project\n" > README.md
git add -- README.md
git commit -m "chore: initial commit"
```

The clean baseline identifies changes produced by each Task. Code Mule never
automatically stashes, resets, cleans, or overwrites unrelated work.

### 3. Initialize the workspace

Run this inside the target repository, not the Code Mule source repository:

```bash
code-mule init --project-id my-project --name "My Project" --workspace "$PWD"
```

State defaults to `.code-mule/project-state.json`. Initialization registers
`.code-mule/` in the repository-local Git exclude without editing the tracked
`.gitignore`, so managed state does not contaminate the Task baseline.

### 4. Configure DeepSeek

```bash
export DEEPSEEK_API_KEY="<your-deepseek-api-key>"
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
```

Do not commit the API key. Real Supervisor calls may incur provider charges.

### 5. Run an objective

```bash
code-mule run --objective "Create a tested calculator"
```

### 6. Inspect and control

```bash
code-mule status
code-mule chat
```

Commands discover `.code-mule/project-state.json` from the target repository.
`run` and `change --apply` are blocking; use another terminal for live control.

## Boss Controls

| Command | Purpose |
| --- | --- |
| `run --objective "..."` / `run` | Start planning or continue RUNNING work |
| `status` | Read deterministic Plan and Task progress |
| `chat` | Open the natural-language Boss interface |
| `ask "..."` | Ask a read-only project question |
| `change "..."` / `change --apply` | Record CHANGE, then explicitly replan |
| `pause` / `resume` | Pause or resume at a valid control boundary |
| `stop` | Cancel safely without rolling back completed work |
| `inspect` | Inspect the pending typed HumanAction |
| `approve ID` / `reject ID` | Decide one exact approval action |
| `answer ID "..."` | Record an answer to one Worker Input action |
| `resolve ID --strategy ...` | Resolve a typed non-approval action |

HumanAction commands are action-scoped. `answer` does not start a Worker;
after it records the response and preserves partial work, a later explicit
`run` revalidates the baseline and continues the same Task in a fresh session.
See [Human Resolution](docs/human-resolution.md).

## When to Use Code Mule

Code Mule fits local software work that benefits from:

- multi-Task feature implementation;
- a reviewable, one-Task-per-commit Git history;
- product decisions that must stop for human input;
- requirements that may change while execution is underway;
- persisted state across Worker sessions; and
- local verification before delivery.

## Reliability & Safety

- `ProjectState` is the durable source of truth.
- One execution owner dispatches one Worker at a time.
- Inactivity and hard turn timeouts are bounded independently.
- Interrupted or uncertain execution fails closed and is never silently rerun.
- Typed HumanActions preserve the exact decision boundary.
- Delivery stages only exact Task-owned paths after verification and review.
- Code Mule never automatically stashes, resets, or cleans the target repository.
- Push, tag, release, deployment, and irreversible remote effects remain Human Gates.

Details: [Execution Recovery](docs/execution-recovery.md),
[Git Delivery](docs/git-delivery.md), and
[Project Verification](docs/project-verification.md).

## Validation

Current v0.1.1 release-preparation baseline:

- **498 automated tests PASS**;
- `compileall`, `pip check`, and `git diff --check` PASS;
- real DeepSeek + Codex E2E: Plan v1, 6/6 Tasks, Worker Input → Boss answer
  → fresh-session continuation, one delivery commit per Task, final review
  APPROVE, and a clean Git workspace; and
- real E2E validated with Codex CLI **0.153.4**.

In that disposable demo, unconfigured project-level test/lint/typecheck/build
hooks were **SKIPPED**, and optional browser visual verification was **NOT_RUN**—
neither is claimed as PASS. See the detailed
[v0.1.1 release preparation notes](docs/releases/v0.1.1.md). No v0.1.1 GitHub
Release has been created; the v0.1.0 tag and its historical evidence are unchanged.

## Current Limitations

- One local Worker only; no distributed or parallel execution and no daemon.
- No Web UI.
- An interrupted Codex turn is not transparently reconnected.
- Remote and irreversible side effects remain Human Gates.
- Completed projects cannot currently be reopened with CHANGE; DONE does not
  transition to CHANGE_REQUESTED.
- Project verification runs configured hooks only; it does not invent missing checks.

## Documentation

- [Architecture](docs/architecture.md)
- [Boss CLI](docs/cli.md)
- [Human Resolution](docs/human-resolution.md)
- [Execution Recovery](docs/execution-recovery.md)
- [Git Delivery](docs/git-delivery.md)
- [Project Verification](docs/project-verification.md)
- [Troubleshooting](docs/troubleshooting.md)
- [v0.1.1 Release Notes](docs/releases/v0.1.1.md)
