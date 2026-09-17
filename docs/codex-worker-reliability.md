# Codex Worker Transport Reliability

This document is the reference for how Code Mule supervises one local Codex
Worker turn, how a stopped turn is classified, and how the real-soak release
gate is run.  It exists because a Worker that fails closed is safe but not
useful: Code Mule must always be able to say *why* a trusted terminal result
never arrived.

## Scope

The reliability boundary is exactly this chain:

```text
Worker task cycle
  → CodexWorkerSession
  → CodexAppServerClient
  → subprocess (`codex app-server`)
  → stdin writer / stdout reader / stderr reader
  → JSON-RPC framing and request correlation
  → thread lifecycle → turn lifecycle → terminal event
  → structured ExecutionReport parse
  → ProjectState persistence
  → subprocess cleanup
```

Everything before the Worker (planning, supervision, Git delivery) is out of
scope.  This stage also does not change fail-closed retry policy.

## Transport supervision

One attempt owns one app-server process.  There is no daemon, no shared
process, and no reuse across attempts; the current scope was kept because no
forensic evidence showed spawn/teardown churn as a cause.

The client supervises four things continuously:

- **Process** — `app_server_pid`, start time, exit code, and terminating
  signal are captured from `poll()` before pipes are touched.
- **stdout** — the reader thread owns the channel state
  (`unopened`/`open`/`eof`/`failed`/`closed`) and pushes either one line, an
  `EOF` marker, or its own exception.  A reader exception is propagated and
  classified, never swallowed.
- **stderr** — always drained on a dedicated thread into a bounded in-memory
  tail.  The app-server can therefore never block on a full stderr pipe.  Raw
  stderr is not persisted.
- **stdin** — a write failure (broken pipe, closed stream) is classified as a
  transport failure instead of surfacing as an unrelated error.

### Process exit versus channel EOF

These are different failures and are never conflated:

| Observation | Classification |
| --- | --- |
| stdout EOF, child exited with code 0 | `stdout_eof` |
| stdout EOF, child exited with a non-zero code | `process_exited` (exit code kept) |
| stdout EOF, child still alive after a bounded grace window | `app_server_disconnected` |
| child killed by a signal | `process_exited` (signal kept) |
| no events at all, deadlines reached | `inactivity_timeout` / `hard_timeout` |

### Cleanup ordering

Cleanup is deterministic and preserves evidence in this order:

1. determine the terminal or failure state;
2. persist bounded transport diagnostics;
3. stop owning the app-server and close stdin;
4. terminate, then kill, and await process exit with a bound;
5. drain and join reader threads with a bound;
6. release the execution lease;
7. refine the persisted attempt with the post-cleanup facts.

Releasing ownership before the failure is classified is never allowed: that
ordering is what loses the evidence.

## Failure classes

A stopped Worker is always attributed to exactly one owner:

| Failure class | Meaning |
| --- | --- |
| `codex_turn_failure` | Codex explicitly reported `turn_failed`, `turn_interrupted`, or a non-retryable `error_notification`, or rejected a request |
| `codex_process_failure` | the app-server could not start, or exited unexpectedly |
| `transport_failure` | pipe, EOF, JSON-RPC, reader, writer, disconnect, or report-parse failure |
| `timeout` | inactivity or hard turn deadline |
| `user_interrupt` | the parent CLI received an interrupt |
| `code_mule_runtime_failure` | Code Mule's own persistence or routing boundary failed |

An unrecognised protocol event is `unclassified_protocol_failure` with a
bounded method name, process status, and channel status.  It is never reported
as `Unknown`.

`Stop cause: Unknown` is reserved for legacy state that recorded no failure
event at all.  Rendering that legacy case prints `Legacy evidence
insufficient`.

## Persisted diagnostics

Schema v16 adds one nullable `transport` object per `ExecutionAttempt`:

```text
app_server_pid            app_server_started_at
app_server_command        app_server_exit_code
app_server_exit_signal    stdout_state / stderr_state / stdin_state
transport_failure_kind    failure_class
last_protocol_event_type  last_protocol_event_at
terminal_event_received   terminal_event_type
thread_id / turn_id       request_id
activity_count            last_activity_at
reader_failure_kind       process_alive_at_failure
cleanup_reason            events[] (≤ 20 metadata-only lifecycle records)
legacy_transport_evidence_incomplete
```

Never persisted: raw stdout, raw stderr, model output, prompts, reasoning,
request or response payload bodies, or credentials.  The event ring records
only a timestamp, a normalised event type, a direction, a correlation id, and
a coarse payload category.

An attempt upgraded from schema ≤ 15 is marked
`legacy_transport_evidence_incomplete`.  Migration never fabricates a stop
cause, process status, or terminal result.

## Terminal evidence and report hand-off

Ordering on a terminal event is fixed:

```text
terminal event received
  → terminal evidence recorded
  → report parse
  → report validation
  → report persisted
  → attempt lifecycle advanced
```

Terminal evidence is captured before parsing, so the two forensic states stay
distinct:

- `terminal_received_report_parse_failed` — Codex completed the turn, but the
  structured ExecutionReport was rejected;
- `report_parse_failed` — the report was rejected without proven terminal
  evidence.

A parser failure can therefore never be reported as “terminal result missing”.

## Inspecting evidence

`code-mule inspect --verbose` prints a `TRANSPORT` section with the Codex
binary, version, process start/exit, channel states, the last protocol event,
thread and turn identity, activity count, terminal and report status, failure
class and cause, cleanup reason, retry safety, and the bounded lifecycle ring.

`code-mule diagnose --verbose` prints the Worker stage, the failure class, the
typed cause, last trusted event, terminal and report status, workspace state,
ownership, and retry safety.  Legacy state prints `Legacy evidence
insufficient` rather than `Unknown`.

`code-mule doctor --verbose` additionally prints the Codex binary, its
version, and whether `codex app-server` is available.  It never starts a model
turn.

## Real soak gate

The release gate is a **manual** procedure.  Codex must never run it as part of
automated verification.

```bash
.venv/bin/python scripts/real_worker_soak.py --iterations 10
```

Each iteration creates an isolated disposable Git repository, runs one bounded
real Codex Worker turn that must read `value.txt`, change it to a deterministic
value, run one local Python verification command, and return the structured
report.  There is no network, no MCP server, no Computer Use, no sandbox
escalation, and no DeepSeek planning or review.  The ExecutionReport and the
bounded transport diagnostics are written per iteration; a failing iteration
keeps its workspace unless `--keep-failures` is omitted on success.

## Acceptance criteria

The gate passes only when all of the following hold across at least 10
consecutive real iterations:

- 10 / 10 iterations complete with a Worker start, a file edit, a local
  command, a terminal result, and a persisted ExecutionReport;
- 0 unexplained missing terminal results;
- 0 `Unknown` / legacy-unknown failures;
- 0 orphaned app-server processes;
- 0 transport reader crashes;
- 0 `RECOVERY_UNCERTAIN` boundaries caused by transport loss.

Genuine typed Codex turn failures are counted separately and never counted as
passes.

## Non-goals

This reliability stage does not add automatic Worker retry, does not lower any
fail-closed rule, does not reset or discard a workspace, does not introduce a
daemon or multi-Worker architecture, and does not continue Phase 25 Runtime
Handoff, Phase 26 TUI, or Phase 27 CLI compatibility work.
