# Troubleshooting

## Project state is not found

Run commands from the directory containing `.code-mule/project-state.json`, or
pass the same explicit `--state-file` path. `init` creates the default directory
and refuses to overwrite an existing state file.

## Workspace is dirty

Code Mule requires a clean Git baseline before dispatching each Task. Inspect
`git status --short`; do not ask Code Mule to stash, reset, clean, or mix unknown
changes into a Task commit. Resolve ownership manually, then use the applicable
typed resolution.

## ACTION REQUIRED

Run `code-mule inspect`. Approve or reject only the displayed action ID, or use
one of the explicitly permitted `resolve` strategies. `resume` never bypasses a
pending HumanAction, and approval is not reusable.

## Another execution owner is active

Do not launch another Worker. Use `status` or Chat to inspect progress. If the
owner becomes stale at a Task boundary, Code Mule can classify it safely; an
uncertain in-flight Worker session requires human inspection.

## Provider or structured-response failure

Model-dependent commands require `DEEPSEEK_API_KEY`. Code Mule uses at most two
Supervisor attempts by default and never repairs JSON or relaxes deterministic
validation. Exhaustion creates a typed Supervisor failure gate. Do not paste
credentials, prompts, or raw provider responses into ProjectState.

## Final verification did not complete

Use `inspect` and review the named deterministic check. Task completion alone
does not make the Project DONE. Required checks, final Git cleanliness, and the
final Supervisor review must all pass.

## Manual release E2E

The authenticated release script is Boss-only and may incur DeepSeek charges:

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_release_e2e.py
```

Each Codex turn has a 240-second absolute deadline by default. Boss may choose a
different bounded deadline with `--worker-timeout-seconds SECONDS`. The live
dashboard shows the current Task, safe Codex activity, and elapsed time. Ctrl+C
closes the app-server, releases execution ownership, records recovery uncertainty
for an active Task, prints a concise interruption summary, and exits non-zero;
it never automatically retries the Worker turn.

Automated verification and Codex compile this script but never execute its
authenticated DeepSeek path.
