# Human Resolution and Approval

`HUMAN_REQUIRED` is a fail-closed control state backed by a persisted, typed
`HumanAction`. ProjectState remains the source of truth; a reason string or a
model conversation is never the only durable record.

## Categories

- `worker_approval`: Codex requested approval for one operation.
- `worker_input`: Codex requested interactive human input.
- `attempt_limit`: a bounded Task or project run reached its limit.
- `supervisor_failure`: planning, replanning, or review could not produce a
  trustworthy structured result.
- `dependency_block`: the active Plan has no runnable Task.
- `recovery_uncertain`: ownership or effects of interrupted execution are not
  known safely.
- `external_side_effect`: a Worker report identified a gated external effect.
- `unknown`: a legacy or otherwise unclassified gate; it remains fail-closed.

Each action has a unique ID, project and optional Task scope, requested action,
risk, timestamps, and one of `PENDING`, `APPROVED`, `REJECTED`, or `RESOLVED`.
Closure emits an audit event. Events contain IDs, category, status, and strategy
only; they do not contain credentials, prompts, or raw provider responses.

## Inspect and decide

```bash
.venv/bin/code-mule inspect
.venv/bin/code-mule inspect --verbose

.venv/bin/code-mule approve action-123
.venv/bin/code-mule reject action-123
.venv/bin/code-mule resolve action-123 --strategy acknowledge
```

`inspect` is read-only. `approve` applies only to `worker_approval` and
`external_side_effect`. It binds approval to the exact pending action ID and
cannot be reused. `reject` records rejection and never executes or retries the
operation.

Non-approval actions use an explicit resolution strategy:

- `acknowledge`: close the action while leaving the project safely stopped.
- `fail_project`: close the action and move the project to `FAILED`.
- `retry_task`: reopen the named Task and return to `RUNNING`; it is restricted
  to attempt-limit and dependency-block actions with a stopped Task.

`resume` never clears a pending HumanAction and cannot bypass
`HUMAN_REQUIRED`.

## Recovery boundary

Codex approval interrupts the current app-server turn. Phase 12 does not have a
durable Worker session/turn recovery contract. Approval is therefore persisted
as `APPROVED`, but the project stays `HUMAN_REQUIRED`: Code Mule does not forge
a session continuation, repeat the operation, or claim the side effect ran.
The approved handoff is explicit input to a later recovery phase.
