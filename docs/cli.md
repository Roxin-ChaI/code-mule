# Boss CLI

The `code-mule` command is the formal Boss control surface for one persisted
project. Install the repository and its runtime dependency into Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

## Quick start

```bash
CODE_MULE_BIN="$(pwd)/.venv/bin/code-mule"
mkdir -p /tmp/code-mule-workspace
cd /tmp/code-mule-workspace
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
# Terminal A
"$CODE_MULE_BIN" run --objective "Create a tested calculator"

# Terminal B, from the same directory while run is active
"$CODE_MULE_BIN" status
"$CODE_MULE_BIN" chat
"$CODE_MULE_BIN" change "Add multiply support"

# After run stops at the CHANGE Safe Point
"$CODE_MULE_BIN" change --apply
```

The default state file is `.code-mule/project-state.json` under the current
directory, so subsequent commands automatically discover the initialized
project when run there. Pass
`--state-file PATH` to every command when using another location. `init` never
overwrites an existing state file and persists an absolute Worker workspace.
The workspace must be a clean Git repository with an initial commit before
execution because every Task starts from a recorded Git HEAD.
`run` and `change --apply` are blocking execution commands. Use a second
terminal in the same directory for `status`, Chat, CHANGE, PAUSE, or STOP.

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
.venv/bin/code-mule diagnose --verbose
.venv/bin/code-mule inspect --verbose
```

## Commands

- `run`: an IDLE project requires `--objective`; a RUNNING project continues
  its active Plan. Other states fail closed.
- `status`: prints Plan version, task progress, current Task, blockers, and the
  Human Gate state without writing ProjectState.
- `diagnose`: explains why execution is stopped or active, where a blocker
  occurred, whether recovery is safe, and the exact next Boss command. It is
  strictly read-only and does not acquire an execution lease or call a model.
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
- `answer ACTION_ID "answer"`: answers one pending WORKER_INPUT action without
  starting a Worker. Partial work is preserved. A subsequent explicit `run`
  validates the original Git baseline and continues the same Task in a fresh
  Worker session; it does not resume the closed turn or create a partial commit.
- `resolve ACTION_ID --strategy STRATEGY`: handles non-approval gates with an
  explicit `acknowledge`, `fail_project`, or permitted `retry_task` strategy.

`run` and `change --apply` render live progress. Other commands use concise
structured text. Errors go to stderr. `--debug` before the command adds a
sanitized traceback; credentials are never printed.

## Project diagnosis

`status` answers what the persisted state is. `diagnose` answers why that state
exists and what the Boss should do next:

```bash
code-mule diagnose
code-mule diagnose --verbose
```

The diagnosis is derived only from ProjectState: active Plan and Task progress,
the current or latest completed Task, typed HumanAction, safe verification
metadata, delivery evidence, and control status. Worker input, approval,
external effects, verification failures, workspace blocks, uncertain recovery,
PAUSE, and CHANGE each have deterministic classifications. Unknown or
conflicting facts produce an uncertain diagnosis instead of a guessed cause.

Verbose output may show the pending action ID, typed classification, safe
Worker question, and latest delivery commit. It never shows prompts, model
reasoning, raw protocol payloads, or credentials. Diagnosis never saves state,
creates an event, starts a Worker, changes Git, or resolves a HumanAction.

The TTY dashboard separates Project, current Task, Worker, Supervisor, and the
five most recent real activities. It adapts to terminal width and uses an ASCII
fallback when Unicode symbols are unavailable. Redirected output is ordered,
line-oriented, and contains no ANSI or spinner control characters.

Normal errors identify the failing boundary and give a safe next command.
Tracebacks remain exclusive to global `--debug` mode.

The current v0.1.1 baseline uses ProjectState schema v11 and was real-E2E
validated with Codex CLI 0.153.4 (not a minimum-version claim). Historical state
migrations remain supported, but old Code Mule versions may not read new state;
back up state before upgrading. See [release notes](releases/v0.1.1.md).

## Environment

Only model-dependent commands read `DEEPSEEK_API_KEY`. The optional
`CODE_MULE_DEEPSEEK_MODEL` defaults to `deepseek-v4-flash`. The DeepSeek client
uses `DefaultHttpx2Client(trust_env=False)`. `init`, `status`, `diagnose`, `ask`, CHANGE
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
See [Boss Chat](boss-chat.md) for deterministic natural-language routing.

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
