# Project-Level Final Verification

Phase 17 separates Task delivery from Project completion. The last Task commit
does not make the Project `DONE`; it starts a deterministic final-verification
boundary while the Project remains safely `RUNNING`.

## Completion boundary

The fixed sequence is:

1. every active-Plan Task is `COMPLETED`;
2. configured project checks run;
3. the repository and final Task delivery HEAD are verified;
4. the Supervisor performs a structured `FINAL_REVIEW` over persisted evidence;
5. only an `APPROVE` decision makes the Plan and Project complete.

No Worker is dispatched during final verification. If finalization is not
configured, execution fails closed instead of using the former direct-DONE
path.

## Deterministic checks

`ProjectVerificationSpec` is persisted in ProjectState and is the only source
of test, lint, type-check, and build commands. Commands are argument tuples,
run without a shell, with a timeout and a credential-free allowlisted
environment. Code Mule never asks the Supervisor to invent a command. An
unconfigured category is recorded as skipped rather than guessed.

Network-enabled checks are rejected unless a future action-scoped approval
mechanism explicitly supports them. Full stdout and stderr are discarded;
ProjectState retains only the typed status, exit code, and a bounded safe
summary.

## Git verification

Git clean is always required. Code Mule verifies that:

- the workspace is the repository root;
- `HEAD` equals the most recent active-Plan Task delivery commit;
- no staged, modified, or untracked path remains.

Any mismatch creates a Human Action and prevents final review.

## Final Supervisor review

After all required deterministic checks pass, `FINAL_REVIEW` receives the
objective, Requirements, active Plan, Task completion and commit evidence, and
the persisted verification result. Its strict output is either `APPROVE` or
`HUMAN_REQUIRED`; it cannot return `REWORK` or a shell command. Structural
response failures use the Phase 15 bounded-regeneration policy. Final review
never changes code automatically.

## Failure handling

A failed or timed-out check, dirty Git state, provider failure, invalid final
review response after bounded regeneration, or `HUMAN_REQUIRED` decision
creates a typed pending Human Action. The Project remains fail-closed. The Boss
can use `code-mule inspect`; code changes must proceed through an explicit
CHANGE/reopen workflow.

The durable audit trail includes verification started/completed, final review
started/completed, and project completed events. A
`ProjectVerificationResult` is persisted as delivery evidence without raw
command output.

## Local E2E

The following uses disposable repositories, real local Git commits and command
execution, deterministic Workers, and a fake final Supervisor. It makes no
DeepSeek request:

```bash
.venv/bin/python scripts/local_project_verification_e2e.py
```

It covers successful two-Task completion, a required test failure, and a dirty
repository. Only the successful scenario reaches `DONE`.
