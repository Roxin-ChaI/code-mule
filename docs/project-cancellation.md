# Project Cancellation

Phase 18 gives the Boss an explicit `code-mule stop` command and a typed
cancellation lifecycle. STOP is distinct from PAUSE, FAILED, and DONE:

- PAUSE preserves the intention to resume the current project.
- FAILED records an execution failure.
- STOP records an intentional Boss cancellation and ends as CANCELLED.

## Safe Point

With no active Task, STOP persists `project.cancel_requested` and
`project.cancelled` atomically and the Project becomes CANCELLED immediately.
With an active Task, STOP first persists CANCEL_REQUESTED. The current Codex
turn is not killed: its report, Supervisor review, verification, and accepted
local Task commit may finish. TaskCycle then clears `current_task_id`; no
REWORK turn and no next Task is dispatched. Project execution finalizes the
cancellation at that boundary.

An interrupted or uncertain Worker remains fail-closed as HUMAN_REQUIRED. STOP
does not pretend an external side effect is safely resolved.

## Preservation

Cancellation never runs `git reset`, `git revert`, workspace deletion, or
history deletion. Completed Tasks, Requirements, Plans, events, execution
evidence, and commit SHAs remain facts. Every unfinished Task in the active
Plan becomes CANCELLED. A pending HumanAction remains historical evidence even
when the Project is cancelled.

CANCELLED Projects do not run project-level verification or FINAL_REVIEW and
cannot transition to DONE. Repeating STOP after CANCELLED is idempotent. STOP
from DONE or FAILED is rejected because those outcomes are already terminal.

## CLI and Chat

```text
code-mule stop
```

The chat phrases `停止项目`, `不做了`, and `取消这个项目` deterministically map
to STOP. PAUSE phrases such as `先暂停` and `stop for now` remain PAUSE.
Ambiguous destructive language is UNKNOWN and cannot mutate ProjectState.

The local cancellation E2E uses a disposable Git repository, a fake
Supervisor, and the local Codex Worker. It verifies that an accepted current
Task commit is preserved while the next Task is cancelled. It never pushes,
tags, releases, calls DeepSeek, or rolls back the repository.
