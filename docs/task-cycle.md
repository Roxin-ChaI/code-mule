# Single-Task Autonomous Cycle

Phase 7 connects persisted project state, one current task, a Codex Worker
thread, and Supervisor REVIEW through deterministic Python control flow. It
does not schedule another task, apply a Plan, or handle Boss CHANGE commands.

## Structured Worker Evidence

The original Phase 7 schema inspection used the older Codex CLI 0.147.0.
The current v0.1.1 real E2E validated native `outputSchema` with Codex CLI 0.153.4;
this is a tested version, not a minimum-version claim.
Code Mule therefore uses native structured output (Strategy A) to constrain the
final agent message. The schema requires every report field, rejects additional
properties, constrains execution and check statuses to enums, and recursively
validates test and static-check records.

The local parser accepts exactly one JSON object. It does not extract JSON from
Markdown, repair malformed output, add defaults, retry the turn, or infer facts
from prose. A malformed report raises `InvalidWorkerReport` and routes the
cycle to the Human Gate.

Validated evidence maps to `ExecutionReport` as follows:

- execution status, summary, files changed, Git state, issues, and typed human
  action are preserved;
- each test/static check has typed name, status, check type and `required` in
  schema v11; display strings retain sanitized `name: status`, not arbitrary detail;
- schema fields are mandatory, but the contract cannot prove the Worker listed
  every necessary check. `not_run` and `unknown` must be explicit; only optional
  `not_run` is acceptable. See [Worker verification](worker-verification.md).

## Deterministic Cycle

`TaskCycleService` loads ProjectState and requires a RUNNING project whose
current task exists exactly once and is IN_PROGRESS. It creates one injected
Worker session, and that session initializes one Codex thread. The initial
prompt starts the first turn. A REVIEW result of REWORK sends only the validated
`next_task_prompt` as another turn on that same thread.

Each validated report is saved before Supervisor REVIEW. Current delivery first
validates Git ownership and Worker verification evidence; a failure stops before
REVIEW or commit. An accepted Task completes only after its delivery commit.
The matching Task's
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
billable command. Both compositions now configure 120-second inactivity and
900-second hard turn limits. Trusted current-turn activity refreshes only the
inactivity deadline; timeout does not automatically retry the Worker.
