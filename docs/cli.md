# Boss CLI

The `code-mule` command is the formal Boss control surface for one persisted
project. Install the repository and its runtime dependency into Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

## Quick start

```bash
mkdir -p /tmp/code-mule-workspace
git init /tmp/code-mule-workspace

.venv/bin/code-mule init \
  --project-id calculator \
  --name "Calculator" \
  --workspace /tmp/code-mule-workspace

export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/code-mule run --objective "Create a tested calculator"
.venv/bin/code-mule status
```

The default state file is `.code-mule/project-state.json`. Pass
`--state-file PATH` to every command when using another location. `init` never
overwrites an existing state file and persists an absolute Worker workspace.

## Default output

Boss-facing output uses readable labels and hides storage identifiers and raw
control enums:

```text
PROJECT
Calculator

Status      Running
Plan        v2
Progress    3 / 5
Current     T4 · Add tests
Boss action None
```

Add `--verbose` after a command to expose `project_id`, `active_plan_id`, Plan
version, `current_task_id`, raw statuses, execution stop reason, and HumanAction
identity when applicable:

```bash
.venv/bin/code-mule status --verbose
.venv/bin/code-mule inspect --verbose
```

## Commands

- `run`: an IDLE project requires `--objective`; a RUNNING project continues
  its active Plan. Other states fail closed.
- `status`: prints Plan version, task progress, current Task, blockers, and the
  Human Gate state without writing ProjectState.
- `ask "question"`: executes the existing deterministic QUERY and is read-only.
- `change "request"`: persists a ChangeRequest and moves RUNNING or paused work
  to CHANGE_REQUESTED without calling DeepSeek.
- `change --apply`: at a Task Safe Point, performs Impact Analysis, materializes
  the replacement Plan, and resumes execution.
- `pause`: applies the existing Boss PAUSE transition.
- `resume`: resumes only PAUSED_BY_BOSS after workspace, Plan, and task ownership
  recovery checks. It never bypasses HUMAN_REQUIRED.
- `stop`: records whole-project cancellation. With no active Task it completes
  immediately; otherwise the current Task reaches a Safe Point and no next Task
  is dispatched. Completed work is preserved and no rollback is performed.
- `inspect`: reads the unique pending typed HumanAction and shows its request,
  risk, scope, and action-specific next command. `--verbose` adds internal IDs.
- `approve ACTION_ID` / `reject ACTION_ID`: closes one exact approval action;
  approval cannot be reused and rejection never executes the operation.
- `resolve ACTION_ID --strategy STRATEGY`: handles non-approval gates with an
  explicit `acknowledge`, `fail_project`, or permitted `retry_task` strategy.

`run` and `change --apply` render live progress. Other commands use concise
structured text. Errors go to stderr. `--debug` before the command adds a
sanitized traceback; credentials are never printed.

The TTY dashboard separates Project, current Task, Worker, Supervisor, and the
five most recent real activities. It adapts to terminal width and uses an ASCII
fallback when Unicode symbols are unavailable. Redirected output is ordered,
line-oriented, and contains no ANSI or spinner control characters.

Normal errors identify the failing boundary and give a safe next command.
Tracebacks remain exclusive to global `--debug` mode.

## Environment

Only model-dependent commands read `DEEPSEEK_API_KEY`. The optional
`CODE_MULE_DEEPSEEK_MODEL` defaults to `deepseek-v4-flash`. The DeepSeek client
uses `DefaultHttpx2Client(trust_env=False)`. `init`, `status`, `ask`, CHANGE
submission, `pause`, and `resume` do not require an API key.

## Exit codes

- `0`: success
- `2`: invalid CLI usage
- `3`: invalid or unrecoverable project state
- `4`: human action required
- `5`: provider or Worker failure

HUMAN_REQUIRED is a fail-closed Human Gate. Use `inspect` and an action-scoped
decision; neither `run` nor `resume` silently clears it. See
[Human Resolution and Approval](human-resolution.md) for categories, audit
events, strategy restrictions, and the Worker session recovery limitation.

## Real manual E2E

The Boss-only script creates a disposable Git repository and drives the real
CLI through init, concurrent run/CHANGE, Safe Point, apply, and final status:

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_cli_e2e.py
```

It makes real DeepSeek requests, uses the locally authenticated Codex
app-server, and may incur charges. Automated verification and Codex do not run
this authenticated script.
