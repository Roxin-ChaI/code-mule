# Git Delivery Workflow

Phase 16 makes a local Git commit part of Task completion. Git is controlled by
the deterministic Orchestrator; neither the Worker nor the Supervisor runs Git
delivery commands.

## Clean baseline

Immediately before Worker dispatch, Code Mule resolves the repository root,
records `HEAD`, and reads `git status --short`. The workspace must be clean.
Existing staged, modified, or untracked paths stop execution with a typed
`HumanAction(RECOVERY_UNCERTAIN)`. Code Mule does not stash, reset, clean, or
overwrite those paths.

The ProjectState file and execution lock should be outside the Worker repository,
or their directory must already be ignored. Otherwise they correctly count as
pre-existing or concurrent repository changes.

## Task ownership

After each Worker attempt, Code Mule validates completed verification evidence
and compares the real porcelain status with the union of repository-relative
paths reported by that Task's attempts. The sets must match exactly. HEAD and the
repository root must still match the baseline, and the Worker must not have
staged anything. Unknown, unsafe, or externally changed paths fail closed.

ProjectState stores typed `GitBaseline`, `GitChangeSet`, and `GitCommitResult`
records. It stores path lists and commit evidence, never the complete diff.

## Commit boundary

The order is:

1. Worker completes and reports verification evidence.
2. Code Mule validates Git ownership.
3. Supervisor REVIEW accepts the Task.
4. Code Mule stages only the recorded paths with `git add -- <paths>`.
5. Code Mule verifies staged ownership and creates one local commit.
6. Commit SHA and message are persisted.
7. Only then is the Task marked `COMPLETED`.

`git add .` and `git add -A` are never used. Commit subjects are generated
deterministically from the Task title, with a safe Task-ID fallback. An accepted
implementation Task with no diff does not receive an empty commit.

## REWORK and failures

REWORK keeps the same clean baseline and Worker session. Intermediate attempts
are not committed. The final accepted attempt produces one commit containing the
Task's cumulative owned changes.

Worker or Supervisor failure, a Human Gate, ownership ambiguity, unexpected HEAD
movement, staging failure, or commit failure leaves the Task incomplete and
enters `HUMAN_REQUIRED`. Git delivery is not destructively retried.

## Remote operations

Phase 16 never runs push, force-push, tag, GitHub Release, or deployment
operations. These remain action-scoped Human Gates for a future delivery phase.

## Local E2E

The automated suite uses a deterministic fake Worker with real disposable Git
repositories. A local authenticated Codex smoke uses the same fake Supervisor
and real Git workflow:

```bash
.venv/bin/python scripts/local_git_delivery_e2e.py
```

It creates only disposable repositories, makes no DeepSeek request, and verifies
two ordered Task commits plus a REWORK scenario with zero intermediate commits.
