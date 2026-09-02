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

`run` and `change --apply` render live progress. Other commands use concise
structured text. Errors go to stderr. `--debug` before the command adds a
sanitized traceback; credentials are never printed.

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

HUMAN_REQUIRED is a fail-closed Human Gate. Inspect `status` and persisted
events; neither `run` nor `resume` silently clears it.

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
