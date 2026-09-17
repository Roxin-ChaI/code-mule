# Boss CLI

The `code-mule` command is the formal Boss control surface for one persisted
project.

## Persistent installation

Install once into an isolated application environment. The repository `.venv`
is **not** required afterwards:

```bash
git clone https://github.com/Roxin-ChaI/code-mule.git /path/to/code-mule
cd /path/to/code-mule
bash scripts/install.sh
```

The installer:

- checks for Python 3.12 and fails with a clear message when it is missing;
- creates `~/.local/share/code-mule/venv/` as an isolated application
  environment, separate from any target workspace;
- installs the current package there and creates the stable launcher
  `~/.local/bin/code-mule`;
- when `~/.local/bin` is missing from `PATH`, automatically detects the shell
  and appends one managed Code Mule PATH block to the correct startup file:
  for zsh, `${ZDOTDIR:-$HOME}/.zprofile` plus a guarded
  `${ZDOTDIR:-$HOME}/.zshrc` fallback on macOS (login environment plus
  non-login interactive), or `${ZDOTDIR:-$HOME}/.zshrc` elsewhere; for bash,
  `~/.bash_profile` on macOS or `~/.bashrc` elsewhere;
- never installs into system Python, never modifies `/usr/local`, and never
  deletes an existing environment;
- refreshes an existing Code Mule installation in place (re-run the same
  script; `--reinstall` makes the intent explicit);
- fails closed when `~/.local/bin/code-mule` already belongs to another tool;
- does not overwrite unrelated executables;
- never rewrites, deletes, or reorders existing rc content; it only appends a
  `# >>> code-mule >>>` … `# <<< code-mule <<<` managed block and never repeats
  it;
- creates a one-time backup (for example `~/.zprofile.code-mule.bak` or
  `~/.zshrc.code-mule.bak`) only when an existing non-empty rc file is about to
  be modified;
- fails closed for symlinked or special rc files, prints the manual fallback,
  and still completes the Code Mule installation;
- refuses to follow symlink rc paths and never sources, evaluates, or runs rc
  content;
- migrates an older unguarded Code Mule block from `~/.zshrc` to the guarded
  current block and adds the macOS `~/.zprofile` entry, without touching any
  user content outside the managed markers.

`--no-deps` installs the local CLI without the `openai` model dependency for
deterministic/air-gapped environments; local commands such as `--help`,
`doctor`, `start` preflight, `status`, and `diagnose` still work, while model
commands explain that dependencies are missing.

`--no-configure-shell` installs the CLI without touching any shell rc file and
prints the manual PATH export instead. `--configure-shell` is accepted for
backward compatibility and is equivalent to the default automatic behavior; it
is no longer part of the recommended flow.

After installation, open a **new terminal** and confirm:

```bash
command -v code-mule
code-mule --help
```

The installer cannot update the already-running parent shell, so new Terminal
windows (not the one that ran the installer) are the acceptance target.

## Quick start

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
code-mule doctor

cd /path/to/my-project   # a Git repository with an initial commit
git status --short       # must print nothing
code-mule start --objective "Create a tested calculator"

# Terminal B, from the same directory while run is active
code-mule status
code-mule chat
code-mule change "Add multiply support"

# After run stops at the CHANGE Safe Point
code-mule change --apply
```

The default state file is `.code-mule/project-state.json` under the current
directory, so subsequent commands automatically discover the initialized
project when run there. Pass `--state-file PATH` to every command when using
another location. `start` performs the same deterministic Git preflight as
`init` before any side effects: it never re-initializes an existing project,
never runs `git init`, never creates a Boss commit, and blocks dirty or
HEAD-less repositories with concrete next steps. `init` never overwrites an
existing state file and persists an absolute Worker workspace.

The explicit initialization form remains available:

```bash
code-mule init --project-id calculator --name "Calculator" --workspace "$PWD"
code-mule run --objective "Create a tested calculator"
```

The workspace must be a clean Git repository with an initial commit before
execution because every Task starts from a recorded Git HEAD.
`run` and `change --apply` are blocking execution commands. Use a second
terminal in the same directory for `status`, Chat, CHANGE, PAUSE, or STOP.

## Environment doctor

`doctor` is deterministic and local-only. It checks:

- Code Mule version and CLI location;
- the running Python runtime (3.12);
- the `git` executable;
- the `codex` executable, `codex --version`, and `codex app-server --help`
  (no minimum-version compatibility claim; Codex 0.153.4 remains the
  real-E2E-tested version);
- only whether `DEEPSEEK_API_KEY` exists — it never prints the key, calls the
  API, checks balance, or sends a request; and
- the current workspace Git repository, HEAD, working-tree cleanliness, and
  existing Code Mule project state.

Healthy output uses the stable rows:

```text
DOCTOR
Code Mule   PASS
Python      PASS
Git         PASS
Codex       PASS
DeepSeek    CONFIGURED
Workspace   READY
```

Problems print the failing row plus the exact fix. `--verbose` adds versions,
binary locations, repository facts, and the current project status without
ever printing credentials or raw command output.

## Default output

Boss-facing output uses readable labels and hides storage identifiers and raw
control enums. Interactive terminals receive one shared dashboard layout:

```text
┌────────────────────────────────────────────────────────────┐
│ CODE MULE · Calculator                                     │
├────────────────────────────────────────────────────────────┤
│ Project        Calculator                                  │
│ Status         Running                                     │
│ Plan           v2                                          │
│ Progress       ████████████░░░░░░░░  3 / 5               │
│ Current        Add tests                                   │
│ Safe Point     Task delivered                              │
└────────────────────────────────────────────────────────────┘
```

The layout reads the actual terminal width and uses most of the available
space, capped at a readable maximum. It uses a single column at 60–79 columns
and wraps safely below 60. CJK, combining characters, common emoji, long action
IDs, and commands are measured by display cells and are wrapped rather than
truncated.

`status`, `diagnose`, `recover`, and `inspect` use the same section, label,
padding, border, and wrapping rules. Empty Boss Action panels are omitted.
`--verbose` adds a separate INTERNAL panel; normal output keeps internal IDs,
thread/turn identities, raw enums, and SHAs hidden.

Live `run` output is an append-only activity stream such as `→ Worker started`
and `✓ Task committed`, with bounded waiting updates. It does not repaint a
complete dashboard for every event. The final state is rendered once as the
full dashboard. Project verification displays `Skipped` separately from
`Passed`.

When stdout is piped, redirected, running under CI, or `TERM=dumb`, Code Mule
retains its stable line-oriented representation. This fallback has no cursor
control or ANSI dependency. Non-Unicode output uses a safe ASCII representation.
`NO_COLOR` is honored; current output does not require colour to communicate
state.

Use the deterministic in-memory preview for manual visual checking. It neither
loads ProjectState nor starts DeepSeek or Codex:

```bash
.venv/bin/python scripts/preview_terminal.py --width 40
.venv/bin/python scripts/preview_terminal.py --width 80
.venv/bin/python scripts/preview_terminal.py --width 120 --diagnose
.venv/bin/python scripts/preview_terminal.py --plain
```

Add `--verbose` after a command to expose `project_id`, `active_plan_id`, Plan
version, `current_task_id`, raw statuses, execution stop reason, and HumanAction
identity when applicable:

```bash
.venv/bin/code-mule status --verbose
.venv/bin/code-mule diagnose --verbose
.venv/bin/code-mule inspect --verbose
```

The preview script prints a non-blocking warning when a requested preview width
exceeds the current terminal width, because the shell may wrap the output:

```text
Requested preview width exceeds current terminal width. Output may wrap.
```

## Commands

- `doctor`: runs the deterministic local environment and workspace checks
  described above; it never starts a Worker, never composes a model runtime,
  and never prints credentials.
- `start --objective "..."`: preflights the current directory (existing
  project / dirty / no HEAD / outside Git), then initializes with deterministic
  defaults and runs the objective. With no existing project and no
  `DEEPSEEK_API_KEY`, it fails before creating state and prints the exact
  environment step.
- `run`: an IDLE project requires `--objective`; a RUNNING project continues
  its active Plan. Other states fail closed.
- `status`: prints Plan version, task progress, current Task, blockers, and the
  Human Gate state without writing ProjectState.
- `diagnose`: explains why execution is stopped or active, where a blocker
  occurred, whether recovery is safe, and the exact next Boss command. It is
  strictly read-only and does not acquire an execution lease or call a model.
- `ask "question"`: executes the existing deterministic QUERY and is read-only.
- `change "request"`: persists a ChangeRequest and moves RUNNING, paused, or a
  completed project to CHANGE_REQUESTED without calling DeepSeek. After DONE it
  records the base revision/Plan and the requested next Revision.
- `change --apply`: at a Task Safe Point, performs Impact Analysis, materializes
  the replacement Plan, and resumes execution.
- `pause`: applies the existing Boss PAUSE transition.
- `resume`: resumes only PAUSED_BY_BOSS after workspace, Plan, and task ownership
  recovery checks. It never bypasses HUMAN_REQUIRED.
- `recover`: classifies a persisted interruption, validates the required Git
  continuity, and continues from the last trusted Safe Point. It never resolves
  a HumanAction or restarts an uncertain Worker.
- `stop`: records whole-project cancellation. With no active Task it completes
  immediately; otherwise the current Task reaches a Safe Point and no next Task
  is dispatched. Completed work is preserved and no rollback is performed.
- `inspect`: reads the unique pending typed HumanAction and shows its request,
  risk, scope, and action-specific next command. `--verbose` adds internal IDs.
- `approve ACTION_ID` / `reject ACTION_ID`: closes one exact approval action;
  approval cannot be reused and rejection never executes the operation.
- `answer ACTION_ID "answer"`: answers one pending WORKER_INPUT action without
  starting a Worker. Partial work is preserved. A subsequent explicit `recover`
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
code-mule recover --verbose
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

The current development baseline uses ProjectState schema v15; the v0.1.1 baseline was real-E2E
validated with Codex CLI 0.153.4 (not a minimum-version claim). Historical state
migrations remain supported, but old Code Mule versions may not read new state;
back up state before upgrading. See [release notes](releases/v0.1.1.md).

Completed projects can be reopened with `code-mule change ...` as a new linear
Revision. See [Project Revisions](project-revisions.md) for the revision
record, task lineage, per-revision verification reset, and Git continuity
rules.

## Delivery handoff

After final verification, `deliverable` shows the verified type, entry point,
usage, revision, and Plan version. Runnable local products additionally support
`launch`, `app-status`, and `stop-app`. These commands never infer a command from
README text or model conversation history; they use the typed manifest persisted
for the completed revision.

```bash
code-mule deliverable
code-mule launch
code-mule app-status
code-mule stop-app
```

Libraries and other non-runnable deliverables return their usage without starting
a process. See [Runtime Handoff](runtime-handoff.md) for the manifest contract,
local health checks, PID-reuse protection, and migration boundary.

## Environment

Only model-dependent commands read `DEEPSEEK_API_KEY`. The optional
`CODE_MULE_DEEPSEEK_MODEL` defaults to `deepseek-v4-flash`. The DeepSeek client
uses `DefaultHttpx2Client(trust_env=False)`. `init`, `status`, `diagnose`, `ask`, CHANGE
submission, `pause`, and `resume` do not require an API key.
`doctor` reports whether the key is configured without reading its value.
`start` requires the key before initializing a new project so that a failed
environment never leaves a half-started project that a later `start` would
refuse to re-initialize.

## Exit codes

- `0`: success
- `2`: invalid CLI usage
- `3`: invalid or unrecoverable project state
- `4`: human action required
- `5`: provider or Worker failure
- `6`: environment check failed (`doctor` found missing or broken prerequisites)

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

## Worker transport diagnostics

`code-mule inspect --verbose` prints a `TRANSPORT` section for a stopped Worker
boundary: Codex binary and version, process start and exit (including the
terminating signal), stdout/stderr/stdin channel states, the last protocol
event, thread and turn identity, activity count, terminal and report status,
failure class and cause, cleanup reason, retry safety, and a bounded
metadata-only lifecycle ring.

`code-mule diagnose --verbose` prints `Stage`, `Failure class`, `Cause`, last
trusted event, terminal and report status, workspace state, ownership, and
retry safety. Legacy state with no captured evidence prints `Legacy evidence
insufficient` instead of `Unknown`.

A rejected structured Worker report additionally reports its typed
`report_stage`, `report_code`, and `report_field_path` (for example
`extraction / ambiguous_json_candidate` or `schema / missing_field @
worker report.tests`). Raw final answers are never displayed.

`code-mule doctor --verbose` additionally reports the Codex binary, its
version, and whether `codex app-server` is available. It never starts a model
turn. See [Codex Worker Transport Reliability](codex-worker-reliability.md).

## Manual real Worker soak

The transport reliability release gate is a Boss-only manual procedure:

```bash
.venv/bin/python scripts/real_worker_soak.py --iterations 10
```

It starts 10 real local Codex Worker turns in isolated disposable Git
repositories, requires one file edit, one local command, and one persisted
ExecutionReport per iteration, and reports a reliability summary whose
`Unknown failures` count must be 0. It uses no DeepSeek planning or review and
is never run by automated verification.
