# v0.1.0 Release Readiness

Phase 19 audits the complete local v0.1.0 lifecycle without adding a new Agent
capability. The audited path covers CLI and Chat, planning, ownership,
Supervisor reliability, Codex execution, TaskCycle, Git delivery, CHANGE,
Human Resolution, final verification, completion, and cancellation.

The local release E2E uses a disposable Git repository, a deterministic fake
Supervisor, and either a fake Worker for automated regression or real local
Codex for the release gate. It proves PLAN → Task commits → CHANGE Safe Point →
Plan v2 → resumed commits → project verification → final review → DONE, then
runs a separate STOP path and proves cancellation preserves the accepted Task
commit without final verification.

```bash
.venv/bin/python scripts/local_release_readiness_e2e.py --fake-worker
.venv/bin/python scripts/local_release_readiness_e2e.py
```

The second command uses the locally authenticated Codex app-server but no
DeepSeek API. The authenticated DeepSeek release chain remains manual:

```bash
.venv/bin/python scripts/manual_release_e2e.py
```

The manual harness projects the existing live progress dashboard and applies a
configurable 240-second absolute deadline to each Codex turn. A timeout or Boss
interrupt is fail-closed: the current result remains uncertain, the app-server
is closed, execution ownership is released, and no replacement Worker is
started. Use `--worker-timeout-seconds SECONDS` to select another positive,
bounded deadline.

## Security boundary

Local edits, declared local checks, exact-path staging, and local commits are
automatic. Push, force-push, tag, release, deployment, paid actions, secrets,
destructive changes, and irreversible external mutations remain Human Gates.
Worker approval and input requests fail closed. Git commands use argv lists,
never shell interpolation, and delivery never uses `git add .` or `git add -A`.
The Codex app-server child environment removes API key, token, secret, password,
and credential variables inherited from the Supervisor/CLI process.

## Residual low-severity limitations

- Real DeepSeek behavior and billing remain a Boss-only manual release gate.
- An approved interrupted Worker operation cannot resume the same Codex turn;
  repository inspection and an explicit recovery strategy are required.
- Execution ownership is local to one project state and host; v0.1.0 is not a
  distributed daemon.
- Verification commands are trusted deterministic project configuration rather
  than an OS-level network sandbox. Their environment excludes credentials and
  commands marked network-enabled fail closed, but only local checks should be
  configured.
