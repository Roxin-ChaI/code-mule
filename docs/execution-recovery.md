# Execution ownership and crash recovery

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

## Stale detection

When the OS lock can be acquired but ProjectState still contains an `ACTIVE`
lease, Code Mule records `execution.stale_detected` and makes one typed recovery
decision:

| Classification | Meaning | Result |
| --- | --- | --- |
| `SAFE_TO_RESUME` | The project is executable but no Task is active. | Mark the old lease stale, record recovery, and admit a new owner. |
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
