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

Install with Python 3.12:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Initialize a clean Git workspace with an initial commit, then run Code Mule:

```bash
mkdir -p /tmp/code-mule-demo
cd /tmp/code-mule-demo
git init
git config user.name "Code Mule Boss"
git config user.email "boss@example.invalid"
touch README.md
git add -- README.md
git commit -m "chore: initialize workspace"

code-mule init --project-id demo --name "Demo" --workspace "$PWD"

export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
code-mule run --objective "Create a tested calculator"
```

From the same directory, inspect or control the persisted project:

```bash
code-mule status
code-mule chat
```

Commands discover `.code-mule/project-state.json` by default. `run` and
`change --apply` are blocking; use another terminal for live control.

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
| `resolve ID --strategy ...` | Resolve a typed non-approval action |

Use `--verbose` on a command for IDs and raw control values. Use global
`--debug` for a sanitized traceback.

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
- Every Worker turn has a configurable bounded deadline; an uncertain result
  becomes `HUMAN_REQUIRED` and is not automatically rerun.
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
