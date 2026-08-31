# Single-Task Autonomous Cycle

Phase 7 connects persisted project state, one current task, a Codex Worker
thread, and Supervisor REVIEW through deterministic Python control flow. It
does not schedule another task, apply a Plan, or handle Boss CHANGE commands.

## Structured Worker Evidence

Codex CLI 0.147.0 exposes `outputSchema` in the generated `turn/start` schema.
Code Mule therefore uses native structured output (Strategy A) to constrain the
final agent message. The schema requires every report field, rejects additional
properties, constrains execution and check statuses to enums, and recursively
validates test and static-check records.

The local parser accepts exactly one JSON object. It does not extract JSON from
Markdown, repair malformed output, add defaults, retry the turn, or infer facts
from prose. A malformed report raises `InvalidWorkerReport` and routes the
cycle to the Human Gate.

Validated evidence maps to `ExecutionReport` as follows:

- execution status, summary, files changed, Git state, issues, and the human
  action flag are preserved;
- each test and static check becomes a deterministic `name: status (detail)`
  string compatible with the existing domain model; and
- omitted evidence is impossible because all schema fields are required. A
  check that was not run must explicitly use `not_run`; uncertain state uses
  `unknown`.

## Deterministic Cycle

`TaskCycleService` loads ProjectState and requires a RUNNING project whose
current task exists exactly once and is IN_PROGRESS. It creates one injected
Worker session, and that session initializes one Codex thread. The initial
prompt starts the first turn. A REVIEW result of REWORK sends only the validated
`next_task_prompt` as another turn on that same thread.

Each validated report is saved before Supervisor REVIEW. The matching Task's
`execution_attempts` is updated from the report, and the Supervisor receives a
freshly loaded state containing that report. Each structured review becomes a
persisted `Decision` before its control action is applied.

- CONTINUE completes the current task and clears `current_task_id`, without
  selecting another task.
- DONE also completes the task and clears `current_task_id`, but leaves the
  project RUNNING for a future higher-level completion rule.
- HUMAN_REQUIRED stops execution and performs the validated transition from
  RUNNING to HUMAN_REQUIRED.
- Worker approvals, interactive input, protocol failures, malformed reports,
  and explicit Worker human gates stop at HUMAN_REQUIRED without asking the
  Supervisor to guess.
- REWORK is bounded by `max_attempts`; exhaustion records the limit and returns
  a HUMAN_REQUIRED outcome without starting another turn.

All timestamps and report, decision, and event IDs come from injected
factories. State updates use copy-on-write records and tuple appends. Prompts,
raw SDK responses, credentials, and complete reports are not stored in event
metadata. A failed report save prevents REVIEW; a failed Supervisor request
does not rerun the already persisted Worker turn.

## Verification Paths

The non-billable local smoke command runs a real local Codex app-server against
a disposable repository and uses a fixed fake Supervisor to force REWORK then
CONTINUE:

```bash
.venv/bin/python scripts/local_codex_cycle_smoke.py
```

The Boss-only full E2E makes real DeepSeek API calls and can incur charges:

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_task_cycle_e2e.py
```

The manual script prints an explicit warning, uses a disposable Git repository,
and retains TLS verification while constructing `DefaultHttpx2Client` with
`trust_env=False`. Codex and automated verification must never run this
billable command. Both real E2E compositions use one explicit 360-second Worker
deadline per protocol operation and still perform no automatic retry.
