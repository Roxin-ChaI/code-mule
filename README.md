# Code Mule

[中文](README.zh-CN.md)

> Powered by prompts. Paid in tokens.

Human-supervised autonomous software engineering with DeepSeek + Codex.

Turn one coding objective into a controlled **PLAN → CODE → REVIEW → VERIFY →
COMMIT** workflow.

![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB)
![Tests 591](https://img.shields.io/badge/tests-591%20passed-2E7D32)
![ProjectState v12](https://img.shields.io/badge/ProjectState-v12-6A5ACD)

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
- **Deterministic recovery** — persist the latest execution boundary, Safe Point,
  and Worker-attempt lifecycle; `code-mule recover` continues only when Git and
  Plan continuity are proven from `ProjectState`.

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

- Python 3.12 (the installer verifies this for you)
- Git and a target repository with at least one commit
- [Codex CLI](https://developers.openai.com/codex/cli) with local authentication
- A DeepSeek API key for Supervisor calls

After installation, `code-mule doctor` checks all of these and prints exactly
what is missing. Real E2E has been validated with **Codex CLI 0.153.4**; that is
a tested version, not a proven minimum version, and `doctor` does not treat an
older CLI as incompatible.

## Quick Start (Boss)

Install once, then work only from your target repository. You never need to
activate a virtual environment or know where Code Mule lives.

### 1. Install Code Mule once

```bash
git clone https://github.com/Roxin-ChaI/code-mule.git /path/to/code-mule
cd /path/to/code-mule
bash scripts/install.sh
```

The installer creates an isolated application environment at
`~/.local/share/code-mule/venv` and a stable launcher at
`~/.local/bin/code-mule`. It never installs into your system Python, never
requires the repository's `.venv`, and does not change your shell configuration
without your explicit `--configure-shell` choice.

If `~/.local/bin` is not on `PATH`, the installer prints one line to add. After
that, open a **new terminal**:

```bash
command -v code-mule
code-mule --help
```

### 2. Check the environment

```bash
code-mule doctor
```

Every row must read `PASS`, `CONFIGURED`, or `READY`. If DeepSeek is
`NOT CONFIGURED`, export the key in your shell (never commit it):

```bash
export DEEPSEEK_API_KEY="<your-deepseek-api-key>"
```

`CODE_MULE_DEEPSEEK_MODEL` optionally overrides the default
`deepseek-v4-flash`. Real Supervisor calls may incur provider charges.

### 3. Start a project in your repository

```bash
cd /path/to/my-project
code-mule start --objective "Create a tested calculator"
```

`start` uses the current directory as the workspace, the directory name as the
project name, and a safe deterministic project id. It initializes
`.code-mule/project-state.json` and immediately runs the objective.

Your repository must be a Git repository with at least one initial commit and a
clean working tree. For a brand-new repository:

```bash
mkdir -p /path/to/my-project
cd /path/to/my-project
git init
printf "# My Project\n" > README.md
git add README.md
git commit -m "chore: initial commit"
```

`start` never re-initializes an existing project, never runs `git init` for you,
and never stashes, resets, cleans, or creates commits. Blocked workspaces stop
with the exact next step.

### 4. Return later

From the same project directory in a new terminal:

```bash
code-mule status
code-mule diagnose
code-mule recover
```

The default state file is `.code-mule/project-state.json` under the current
directory, so commands discover the project automatically. Initialization
registers `.code-mule/` in the repository-local Git exclude without editing the
tracked `.gitignore`, so managed state never contaminates a Task baseline.
`run` and `change --apply` are blocking; use another terminal for live control.

On an interactive terminal, `status`, `diagnose`, `recover`, and HumanAction
output use one responsive, scrollback-friendly dashboard:

```text
┌────────────────────────────────────────────────────────────┐
│ CODE MULE · Calculator                                     │
├────────────────────────────────────────────────────────────┤
│ Status         Running                                     │
│ Plan           v2                                          │
│ Progress       █████████████░░░░░░░  4 / 6                │
│ Current        Implement leaderboard                       │
│ Safe Point     Task delivered                              │
└────────────────────────────────────────────────────────────┘
```

Pipes, redirected output, CI, and `TERM=dumb` keep the stable plain-text form
with no ANSI sequences. `NO_COLOR` is supported; meaning never depends on
colour.

## Advanced / explicit control

Bosses who prefer explicit commands can initialize exactly the same project:

```bash
code-mule init --project-id my-project --name "My Project" --workspace "$PWD"
code-mule run --objective "Create a tested calculator"
```

The contributor flow for building Code Mule itself is documented under
[Contributor / Development Setup](#contributor--development-setup).

## Boss Controls

| Command | Purpose |
| --- | --- |
| `doctor` | Check Code Mule, Python, Git, Codex, DeepSeek, and workspace readiness (local-only) |
| `start --objective "..."` | Initialize the current Git workspace with defaults and run the first objective |
| `run --objective "..."` / `run` | Start planning or continue RUNNING work |
| `status` | Read deterministic Plan and Task progress |
| `diagnose` | Explain blockers, recoverability, and the next Boss action (read-only) |
| `recover` | Continue from a persisted, deterministically validated execution boundary |
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

## Contributor / Development Setup

Contributors working on Code Mule itself keep the existing repository-local
editable install:

```bash
cd /path/to/code-mule
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/code-mule --help
```

This is intentionally **not** the Boss installation path. The repository `.venv`
is for development and manual smoke scripts only:

```bash
.venv/bin/python scripts/preview_terminal.py --width 80
```

Reinstall a persistent launcher after source changes from this repo:

```bash
bash scripts/install.sh
```

The installer refreshes the isolated application environment in place; it never
deletes an existing environment.

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

- **591 automated tests PASS**, including a real shell restart regression that
  runs the persistent launcher without the repository `.venv` on `PATH`;
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
- No PyPI/Homebrew distribution yet; persistent installation is a repository
  bootstrap script, with pipx/PyPI/Homebrew as future distribution options.
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
