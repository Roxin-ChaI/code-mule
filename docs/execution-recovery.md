# Execution ownership and crash recovery

## Persisted execution boundary

Schema v12 stores one latest `ExecutionStopBoundary`, one latest typed
`SafePoint`, and an ordered Worker-attempt lifecycle. The recovery classifier
uses those facts after a process restart; conversation history and an in-memory
Worker object are never recovery evidence.

`code-mule recover` is distinct from `code-mule resume`: `resume` is only the
Boss PAUSE control transition, while `recover` handles an interrupted execution.
Recovery preflight is read-only. It checks the active Plan and Task identity and,
where required, the expected HEAD, staged state, baseline, and owned paths.

| Persisted boundary | Recovery |
| --- | --- |
| Plan materialized; no current Task | Reuse the same Plan and dispatch its next Ready Task. |
| Task selected; Worker never started | Dispatch a fresh Worker for the same Task. |
| Worker input answered | Fresh Worker session, same Task, original baseline and partial paths. |
| Complete report persisted before review | Continue review and delivery without invoking Worker again. |
| Worker started without a trusted terminal report | Block as recovery uncertain; diagnose and resolve explicitly. |
| Planning interrupted before materialization | Discard the incomplete model turn and explicitly start fresh planning from the persisted objective. |

Recovery never creates a replacement Plan when a complete active Plan already
exists. It never resets completed Tasks, repeats their commits, stashes, resets,
cleans, or silently resolves a HumanAction.

Recovery is intentionally narrower than replaying arbitrary execution. Legacy
states with no evidence of an unstarted Worker remain uncertain. Plan
materialization atomically publishes the Plan with `RUNNING`; a Plan paired
with `PLANNING` is inconsistent and is blocked, not rewritten. A persisted
report containing a Worker HumanAction cannot bypass that gate. Review or Git
delivery that already started is not replayed automatically. If a recovered
report receives REWORK, execution stops for explicit human-guided resolution;
it does not silently start another Worker. A failure after a commit but before
its evidence was saved also remains uncertain.

Worker attempts are recorded before session startup. A crash inside startup is
therefore conservatively treated as possibly started, even when no thread ID
has yet been returned. Lifecycle sequence numbers include prepared/interrupted
attempts; the existing completed-report attempt counter retains its previous
meaning. Answered input still uses the existing one-time Task continuation
contract and an explicitly created fresh Worker, not transparent reconnection.

Code Mule permits one execution owner per project. `run`, `change --apply`,
and execution resume acquire ownership before constructing a Supervisor or
starting a Codex Worker. If lease persistence fails, execution does not start.

Read-only commands remain available while an owner is active. `status`, query
forms of `ask` and `chat`, and Boss `change` or `pause` commands continue to use
the persisted ProjectState. A second execution command fails closed before it
can create another Codex thread.

## Lock and lease

The local guard combines two records:

- an OS advisory lock at `.code-mule/execution.lock`, carrying current owner
  metadata and a low-frequency heartbeat;
- a versioned `ExecutionLease` in ProjectState, recording the durable owner,
  PID, acquisition and heartbeat times, status, current Task, Codex thread, and
  attempt.

Every owner has a random `owner_id` in addition to its PID. The OS lock is the
authoritative local liveness signal; file existence alone never means an owner
is alive. This prevents a retained lock file or reused PID from being mistaken
for a live execution. Heartbeat age and PID liveness are diagnostic recovery
inputs, while business scheduling continues to depend only on ProjectState.

The lock heartbeat is updated every 15 seconds. ProjectState is synchronized at
durable execution boundaries—acquisition, planning handoff, Codex session
creation, Task completion, and release—so heartbeat writes cannot overwrite a
concurrent Boss `change` or `pause` update.

Normal completion, a CHANGE safe point, PAUSE, HUMAN_REQUIRED, and handled
Ctrl+C all release the OS lock and mark the lease `RELEASED`.

If execution is interrupted outside a proven recoverable boundary while an active Task remains under `RUNNING`,
`CHANGE_REQUESTED`, `PAUSED_BY_BOSS`, or `CANCEL_REQUESTED`, release first
creates a `RECOVERY_UNCERTAIN` HumanAction and moves to `HUMAN_REQUIRED`.
Releasing the local lock never implies that an in-flight Worker side effect is
safe to repeat.

## Stale detection

When the OS lock can be acquired but ProjectState still contains an `ACTIVE`
lease, Code Mule records `execution.stale_detected` and makes one typed recovery
decision:

| Classification | Meaning | Result |
| --- | --- | --- |
| `SAFE_TO_RESUME` | No Task is active, or persisted recovery evidence proves a selected/unstarted Task or trusted pre-review report. | Mark the old lease stale, record recovery, and admit a new owner; `recover` revalidates state and Git under the lock. |
| `STALE_IDLE_LEASE` | The project is not in an execution state. | Clean up the stale lease; the command's ordinary state guard still applies. |
| `SESSION_RECOVERY_REQUIRED` | An IN_PROGRESS Task has a persisted Codex thread identity. | Create a recovery HumanAction and enter HUMAN_REQUIRED. |
| `SIDE_EFFECT_UNCERTAIN` | An active Task has no trustworthy session boundary. | Create a recovery HumanAction and enter HUMAN_REQUIRED. |

An unsafe recovery never creates a replacement Worker. Code Mule does not
rename, clear, or infer completion for the interrupted Task.

## Codex identity boundary

Immediately after the app-server creates a Codex thread, Code Mule persists its
thread ID together with the Task ID, attempt, and execution owner. Successful
Task completion clears the active Task/thread/attempt fields. A failure before a
structured report preserves that identity for inspection.

Phase 14 does **not** reconnect to an interrupted Codex session. A stored thread
ID is evidence for fail-closed recovery, not a promise that the session can be
resumed. Inspect the pending `RECOVERY_UNCERTAIN` HumanAction before choosing an
explicit resolution; do not use `resume` to bypass it.

## CLI behavior

A competing execution command reports:

```text
PROJECT ALREADY RUNNING
Another execution owner is active.
No Worker was started by this command.
```

Use `code-mule status` for the safe project view. `--verbose` adds owner, lease,
PID, and Codex thread identifiers but never credentials.

An unsafe stale session reports:

```text
RECOVERY REQUIRED
Previous execution ended unexpectedly.
The current Task cannot be safely assumed complete.
```

Use `code-mule inspect`, then resolve the action through the existing typed
human-resolution flow. Code Mule does not retry a Worker or external side effect
automatically.

## Audit events

The ownership lifecycle records minimal metadata in:

- `execution.acquired`
- `execution.released`
- `execution.stale_detected`
- `execution.recovery_required`
- `execution.recovered`

Events contain identifiers and typed classifications only. They do not store
prompts, raw model responses, credentials, or workspace contents.

## Local recovery E2E

The automated local harness uses disposable Git repositories, a deterministic
fake Supervisor, and the real local Codex app-server:

```bash
.venv/bin/python scripts/local_execution_recovery_e2e.py
```

It verifies a competing owner is rejected before Worker creation, a stale Task
boundary resumes to DONE, and an interrupted IN_PROGRESS session becomes
HUMAN_REQUIRED without a duplicate Worker. It does not use DeepSeek or read a
DeepSeek API key.
