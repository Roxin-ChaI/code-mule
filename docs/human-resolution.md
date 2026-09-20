# Human Resolution and Approval

`HUMAN_REQUIRED` is a fail-closed control state backed by a persisted, typed
`HumanAction`. ProjectState remains the source of truth; a reason string or a
model conversation is never the only durable record.

## Categories

- `worker_approval`: Codex requested approval for one operation.
- `worker_input`: native Codex input or a typed Worker report requested a Boss answer.
- `attempt_limit`: a bounded Task or project run reached its limit.
- `supervisor_failure`: planning, replanning, or review could not produce a
  trustworthy structured result.
- `dependency_block`: the active Plan has no runnable Task.
- `workspace_block`: the workspace baseline blocked dispatch before a Worker started.
- `worker_verification`: completed Worker evidence failed a required/optional
  verification rule; this is not an ownership/recovery-uncertainty classification.
- `recovery_uncertain`: ownership or effects of interrupted execution are not
  known safely.
- `external_side_effect`: a Worker report identified a gated external effect.
- `unknown`: a legacy or otherwise unclassified gate; it remains fail-closed.

Each action has a unique ID, project and optional Task scope, requested action,
risk, timestamps, and one of `PENDING`, `APPROVED`, `REJECTED`, or `RESOLVED`.
Closure emits an audit event. Events contain bounded IDs, category, status,
strategy and safe diagnostic fields, not credentials, prompts, or raw responses.

## Inspect and decide

```bash
.venv/bin/code-mule inspect
.venv/bin/code-mule inspect --verbose

.venv/bin/code-mule approve action-123
.venv/bin/code-mule reject action-123
.venv/bin/code-mule resolve action-123 --strategy acknowledge
```

For a pending input action, use `code-mule answer <action-id> "<answer>"`.
Answering records the Boss decision without starting a Worker or discarding
partial changes. A later explicit `code-mule run` checks the original baseline
and partial paths, then continues the same Task in a fresh Worker session.
No partial commit, implicit replan, or transparent same-turn recovery occurs.

`inspect` is read-only. `approve` applies only to `worker_approval` and
`external_side_effect`. It binds approval to the exact pending action ID and
cannot be reused. `reject` records rejection and never executes or retries the
operation.

Non-approval actions use an explicit resolution strategy:

- `acknowledge`: close the action while leaving the project safely stopped.
- `fail_project`: close the action and move the project to `FAILED`.
- `retry_task`: reopen the named Task and return to `RUNNING`; it is restricted
  to attempt-limit, dependency-block, and workspace-block actions with a stopped
  Task. Workspace blocks require the baseline to be restored before dispatch.

`worker_verification` is neither approvable nor retryable by this command.
`inspect --verbose` shows the check status, required flag and verification stage;
acknowledging it leaves the project stopped. See [Worker verification](worker-verification.md).

`resume` never clears a pending HumanAction and cannot bypass
`HUMAN_REQUIRED`.

## Recovery boundary

Codex approval interrupts the current app-server turn. v0.2.0 still does not
provide transparent durable same-turn recovery. Approval is therefore persisted
as `APPROVED`, but the project stays `HUMAN_REQUIRED`: Code Mule does not forge
a session continuation, repeat the operation, or claim the side effect ran.
The approved handoff is explicit input to a later recovery phase.
