# Code Mule

[中文](README.zh-CN.md)

> Powered by prompts. Paid in tokens.

> You define the goal. The mule does the coding.

An autonomous software engineering runtime that plans, executes, reviews,
verifies, and delivers coding tasks under explicit human control.

## Why Code Mule

Typical agent workflows leave orchestration to the human:

```text
Human → ChatGPT → copy prompt → Codex → copy result → ChatGPT
```

Code Mule runs that engineering loop while the Boss retains authority:

```text
Boss → Supervisor → Runtime → Codex Worker → Review → Git Delivery → Verification
```

The difference is durable `ProjectState`, deterministic orchestration,
structured Supervisor contracts, explicit Human Gates, and safe interruption
and recovery—not another free-form chat wrapper.

## Workflow

```text
Boss Objective
  → PLAN → Task Execution → Supervisor REVIEW → Verification → Git Commit
  → Next Task → Final Verification → DONE

CHANGE         → Safe Point → IMPACT_ANALYSIS → Plan vN+1 → Resume
STOP           → Safe Point → CANCELLED
HUMAN_REQUIRED → inspect → approve / reject / resolve
```

## Core Capabilities

- Structured DeepSeek `PLAN`, `REVIEW`, `IMPACT_ANALYSIS`, and `FINAL_REVIEW`
  drive autonomous planning, multi-task scheduling, and versioned replanning.
- A local Codex Worker executes one bounded Task at a time; the deterministic
  runtime owns dispatch, state transitions, validation, and Safe Points.
- Boss CLI and conversational Chat expose queries, CHANGE, PAUSE/RESUME, STOP,
  and action-scoped Human Resolution without giving the model state authority.
- Single-owner execution, stale-lease detection, bounded Worker deadlines, and
  crash recovery guards prevent duplicate or uncertain Worker execution.
- Retryable Supervisor output failures use bounded full regeneration; domain
  validation failures remain fail-closed and are never repaired heuristically.
- Live terminal progress projects safe Worker/Supervisor activity without
  exposing prompts, reasoning, credentials, or raw model responses.
- Exact Task-owned paths are staged and committed locally; project completion
  additionally requires configured checks, clean Git state, and final review.

## Architecture

```text
Boss CLI / Chat
       ↓
Deterministic Orchestrator
       ↓
Supervisor ───── ProjectState
       ↓
Task Runtime
       ↓
Codex Worker
       ↓
Verification
       ↓
Git Delivery
```

`ProjectState` is the source of truth. Model conversation history is not. The
Supervisor returns typed proposals and decisions; it does not mutate state or
execute Git commands.

## Safety Model

Automatically allowed:

- local workspace edits and configured local verification;
- precise staging with `git add -- <owned paths>`; and
- local Task commits.

Human Gate required:

- push, force-push, tag, and release;
- deployment and remote infrastructure mutation;
- secret or API-key use when an operation requires it;
- paid external actions; and
- destructive or irreversible external side effects.

High-risk side effects are never authorized by parsing model prose. Approval is
bound to one typed HumanAction and cannot be reused.

## Quick Start

### 1. Install Code Mule

Clone and install the Code Mule tool with Python 3.12:

```bash
git clone https://github.com/Roxin-ChaI/code-mule.git /path/to/code-mule
cd /path/to/code-mule
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
which code-mule
code-mule --help
```

`/path/to/code-mule` is the Code Mule tool repository, and its `.venv` contains
the installed CLI. Keep this virtual environment activated when you change to
the target project. In each new terminal, activate it again with
`source /path/to/code-mule/.venv/bin/activate`, or invoke the CLI by its absolute
path: `/path/to/code-mule/.venv/bin/code-mule`.

### 2. Prepare a project workspace

Code Mule repository is the tool itself; `--workspace` points to the target Git
repository that Code Mule will modify. The two directories serve different
purposes:

- `/path/to/code-mule`: the Code Mule tool and its virtual environment.
- `/path/to/my-project`: the target Git workspace that Code Mule modifies.

Do not create Code Mule's `.venv` in the target workspace. If the target project
needs its own `.venv`, that project must manage it in its own `.gitignore`.

The target repository must have at least one commit and a clean working tree.
For an existing project, change to its directory and check the baseline:

```bash
cd /path/to/my-project
git status --short
```

Proceed only when `git status --short` prints nothing. For a new project, create
the minimum Git baseline first:

```bash
mkdir -p /path/to/my-project
cd /path/to/my-project
git init
printf "# My Project\n" > README.md
git add -- README.md
git commit -m "chore: initial commit"
```

This clean baseline lets Code Mule identify the changes produced by each Task.
Code Mule does not automatically stash, reset, or overwrite unrelated local
changes.

### 3. Initialize Code Mule

Run initialization from inside the target project directory:

```bash
code-mule init --project-id my-project --name "My Project" --workspace "$PWD"
```

Here, `$PWD` is the current target project—not the Code Mule source repository.
The default state file is `.code-mule/project-state.json`. The `.code-mule/`
directory is Code Mule runtime state; `code-mule init` registers that managed
directory in the repository-local Git exclude so it does not break the clean
baseline or modify the project's tracked `.gitignore`.

### 4. Configure DeepSeek

```bash
export DEEPSEEK_API_KEY="<your-deepseek-api-key>"
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
```

Do not commit the API key to the target project or any configuration file.

### 5. Run

Start the real CLI workflow with an objective:

```bash
code-mule run --objective "Create a tested calculator"
```

Code Mule drives the lifecycle:

`Objective → PLAN → Tasks → REVIEW → Git Commit → Final Verification → DONE`

### 6. Inspect / Control

From the target project directory, inspect state or start a persistent Boss chat:

```bash
code-mule status
code-mule chat
```

For example, ask about progress, request a change, or stop the project:

```text
You > How far have you got?
You > Add JSON export support.
You > Stop this project.
```

Commands discover `.code-mule/project-state.json` by default. `run` and
`change --apply` are blocking; use another terminal for live control.

### Troubleshooting

Verify which installation provides the CLI:

```bash
which code-mule
```

The expected path is `/path/to/code-mule/.venv/bin/code-mule`. If it is missing,
activate the Code Mule virtual environment again. Then, from
`/path/to/my-project`, check Git cleanliness:

```bash
git status --short
```

The expected result is no output, including immediately after `code-mule init`.
Any ordinary untracked, modified, or staged project file remains visible and
must be handled explicitly; Code Mule never stashes, resets, or cleans it.

## Boss Controls

| Command | Purpose |
| --- | --- |
| `run --objective "..."` | Plan a new project; `run` resumes RUNNING work |
| `status` | Show deterministic project and Task status |
| `chat` | Start the natural-language Boss interface |
| `ask "..."` | Read-only project query |
| `change "..."` | Record a requirement change |
| `change --apply` | Run impact analysis, materialize Plan vN+1, and resume |
| `pause` / `resume` | Pause at a control boundary or resume safely |
| `stop` | Cancel at a Safe Point without rollback |
| `inspect` | Inspect the pending HumanAction |
| `approve ID` / `reject ID` | Resolve one exact approval action |
| `answer ID "..."` | Answer one exact Worker input request |
| `resolve ID --strategy ...` | Resolve a typed non-approval action |

Use `--verbose` on a command for IDs and raw control values. Use global
`--debug` for a sanitized traceback.

When a Worker needs information, Code Mule stops in `HUMAN_REQUIRED` and
`inspect` shows the bounded question and choices. Answer that exact action with
`code-mule answer <action-id> "<answer>"`, then explicitly run `code-mule run`.
Code Mule starts a fresh Worker session, verifies that the partial workspace
changes still match the original clean Git baseline, and continues the same
Task. It does not resume the closed Codex session or create a partial commit.

## CHANGE Example

```text
You > Add JSON export support.

Code Mule records CHANGE, lets the current Task reach a Safe Point, runs
IMPACT_ANALYSIS, materializes Plan v2, and resumes only after `change --apply`.
```

## Boss Chat

Examples accepted by `code-mule chat` include:

- “现在做到哪一步了？”
- “还有几个任务？”
- “当前有什么问题？”
- “再加一个 JSON 导出功能。”
- “这个项目不做了。”

Read-only facts come from `ProjectState`; the model does not estimate progress
or directly mutate project state. Ambiguous side-effect requests ask for
clarification.

## Verification and Delivery

```text
Task:
Worker → verification → REVIEW → git add -- <owned paths> → local commit
       → COMPLETED

Project:
all Tasks complete → tests/lint/typecheck/build/git-clean → FINAL_REVIEW → DONE
```

Only configured project checks run; absent check categories are skipped rather
than invented. Task `COMPLETED` does not imply Project `DONE`.

## Reliability

- Exactly one execution owner may dispatch work for a project.
- Stale leases and interrupted Tasks are classified before recovery.
- Every Worker turn has a configurable inactivity timeout plus a non-refreshable
  hard maximum duration. Trusted current-turn activity refreshes only the idle
  limit.
- Either timeout fails closed to `HUMAN_REQUIRED`; partial work is not deleted,
  reset, or automatically rerun.
- Retryable structural Supervisor failures receive bounded fresh regeneration,
  never JSON repair or validator bypass.
- App-server processes and reader threads close on success, timeout, or Ctrl+C.

## v0.1.0 Validation

Release-candidate baseline:

- 441 automated tests PASS on Python 3.12.13;
- `code-mule==0.1.0` fresh editable install and `pip check` PASS;
- real local Codex full-system E2E PASS; and
- real DeepSeek + real Codex release E2E PASS.

The authenticated release E2E verified:

```text
Objective → real PLAN → real Codex Worker → real REVIEW → CHANGE → Safe Point
→ real IMPACT_ANALYSIS → Plan v2 → Task commits → Project Verification
→ real FINAL_REVIEW → DONE
```

Final evidence: Plan v2, 3/3 Tasks completed, 3 Task commits, 3 unique Codex
sessions, verification PASS, `FINAL_REVIEW` APPROVE, clean workspace, released
execution leases, and no duplicate Worker.

## Current Scope and Limitations

- One local Worker and local-machine execution ownership; no distributed
  scheduler, daemon, parallel Worker, or Web UI.
- An interrupted Codex turn is not transparently reconnected; uncertain work
  requires inspection and explicit resolution.
- Push, tag, release, deployment, and other gated external effects remain
  manual Human Gate operations.
- Verification commands are trusted deterministic project configuration, not
  an OS-level network sandbox.

## Documentation

- [Architecture](docs/architecture.md)
- [Boss CLI](docs/cli.md)
- [Boss Chat](docs/boss-chat.md)
- [Human Resolution](docs/human-resolution.md)
- [Execution Recovery](docs/execution-recovery.md)
- [Git Delivery](docs/git-delivery.md)
- [Project Verification](docs/project-verification.md)
- [Project Cancellation](docs/project-cancellation.md)
- [Release Readiness](docs/release-readiness.md)
- [Troubleshooting](docs/troubleshooting.md)
