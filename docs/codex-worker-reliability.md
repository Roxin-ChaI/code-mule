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

### App-server protocol facts

Audited against `codex app-server generate-json-schema` on Codex CLI 0.153.4,
not inferred from prose:

- `turn/start` accepts `outputSchema`: *"Optional JSON Schema used to constrain
  the final assistant message for this turn."* Native structured output is
  therefore **SUPPORTED**, and Code Mule already sends it.
- `TurnStatus` is `completed | interrupted | failed | inProgress`, and the only
  terminal turn notification is `turn/completed`; its `turn.status` decides the
  outcome. There is no `turn/failed` or `turn/interrupted` notification.
- `agentMessage` items carry an optional `phase` of `commentary` or
  `final_answer`, and the schema warns that providers do not emit it
  consistently. Code Mule prefers a `final_answer`, falls back to the last
  observed agent message, and never lets commentary text shadow a declared
  final answer.

A schema constraint is a constraint, not a guarantee: in the first real soak
8/10 turns complied and 2/10 still wrapped a correct report in prose and a
```json fence. The deterministic extractor therefore remains the enforcement
point, and the prompt states the envelope explicitly.

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

A terminal outcome is recognised whether Codex reports it as a status on
`turn/completed` or as its own `turn/failed` / `turn/interrupted`
notification; an unrecognised terminal shape is classified, never allowed to
run into a timeout.

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
| `transport_failure` | pipe, EOF, JSON-RPC, reader, writer, or disconnect failure |
| `timeout` | inactivity or hard turn deadline |
| `user_interrupt` | the parent CLI received an interrupt |
| `code_mule_runtime_failure` | Code Mule's own report-contract, parsing, or persistence boundary failed |

An unrecognised protocol event is `unclassified_protocol_failure` with a
bounded method name, process status, and channel status.  It is never reported
as `Unknown`.

A rejected structured Worker report (`report_parse_failed`) is a
`code_mule_runtime_failure`: the report contract is Code Mule's own boundary.
It must never be counted as a transport failure.

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

### Worker report contract map

The contract has one source of truth
(`src/code_mule/worker/report_contract.py`). Each layer must state the same
rule:

```text
Worker is instructed to output:        REPORT_ENVELOPE_INSTRUCTION
        ↓                              (appended to every Worker prompt)
Extractor accepts:                     extract_report_candidate
        ↓
Parser constructs / validator requires: parse_structured_worker_report
        ↓
ExecutionReport persists:              build_execution_report
```

The envelope rule is: the entire final answer is exactly one JSON object, with
no surrounding prose, headings, or Markdown, and no ```json code fence. A
contract-map test asserts that the prompt names exactly the top-level fields
the JSON Schema requires, so the prompt, the schema, and the validator can
never drift apart.

### Envelope extraction

Extraction is deterministic and bounded, and performs **no JSON repair**:

1. the whole final message must be JSON; otherwise
2. exactly one fenced ```json (or bare ```) block; otherwise
3. exactly one embedded balanced `{...}` object.

Two or more distinct candidates fail closed
(`ambiguous_json_candidate`); the parser never guesses between two plausible
reports. Field-level schema and semantic validation stay strict: no missing
fields, no extra fields, no coercion, no defaults.

### Typed report failure diagnostics

Every report rejection carries a bounded, secret-free
`ReportFailureStage` / `ReportValidationCode` / `field_path` pair:

```text
stage   envelope | extraction | json_decode | schema | semantic_validation
code    not_a_string, empty_output, output_too_large, no_json_candidate,
        ambiguous_json_candidate, invalid_json, not_an_object, missing_field,
        extra_field, invalid_enum, invalid_field_type, invalid_check_result,
        invalid_human_action, invalid_semantic_value
```

The persisted failure metadata also records `candidate_found`,
`json_decoded`, `semantic_validation_started`, `final_message_present`,
`report_stage`, `report_code`, and `report_field_path`. Raw final answers are
never persisted.

Ordering on a terminal event is fixed:

```text
TURN_TERMINAL → REPORT_EXTRACTION → REPORT_PARSED → REPORT_VALIDATED → REPORT_PERSISTED
```

The furthest report stage reached is persisted per attempt, and the stage never
regresses, so a failed extraction can never look like a persisted report.
Terminal evidence is recorded before extraction, report parsing, and report
persistence, and the attempt lifecycle advances only after each step succeeds.

Terminal evidence is captured before parsing, so the two forensic states stay
distinct:

- `terminal_received_report_parse_failed` — Codex completed the turn, but the
  structured ExecutionReport was rejected;
- `report_parse_failed` — the report was rejected without proven terminal
  evidence.

A parser failure can therefore never be reported as “terminal result missing”.
When a turn completed and the transport was healthy, a rejection reports
`Worker turn: Completed`, `Transport: Healthy`, and `Report: Invalid` rather
than any transport failure.

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
bounded transport diagnostics are written per iteration. A **failing iteration
keeps its workspace** so evidence is never destroyed by cleanup; successful
workspaces are removed. `--purge-failures` opts out of preserving failures.

Failure buckets follow the Worker failure classes, so a rejected report is
counted under runtime failures, never under transport failures. The summary
also splits report rejections into:

```text
Report extraction failures   envelope, extraction, or JSON decode stages
Report validation failures   schema or semantic-validation stages
```

Artifacts written before typed stages existed show a third line,
`Report stage unknown (legacy evidence)`, rather than being guessed into a
bucket.

The first real soak reads: transport failures 0, runtime failures 2, unknown
failures 0. Both of its failures were verified from the Codex session
recordings to be whole-message JSON decode failures (`json_decode`), i.e.
extraction failures; the new parser reports that stage explicitly, so the same
envelope is provably an extraction failure on the next run.

`--inactivity-timeout` and `--max-turn-seconds` bound each turn without editing
the harness. Interrupting the run (Ctrl+C) preserves every workspace and every
artifact; only a successful iteration's workspace is removed, and
`--purge-failures` is the explicit opt-in that also removes failing ones.
Artifacts are never deleted.

## Acceptance criteria

The gate passes only when all of the following hold across at least 10
consecutive real iterations:

- 10 / 10 iterations complete with a Worker start, a file edit, a local
  command, a terminal result, and a persisted ExecutionReport;
- 0 unexplained missing terminal results;
- 0 `Unknown` / legacy-unknown failures;
- 0 report extraction and 0 report validation failures;
- 0 incomplete deliveries;
- 0 orphaned app-server processes;
- 0 transport reader crashes;
- 0 `RECOVERY_UNCERTAIN` boundaries caused by transport loss.

Genuine typed Codex turn failures are counted separately and never counted as
passes. A failing iteration must be retained as evidence; a gate is never
satisfied by re-running rounds until ten happen to pass.

## Non-goals

This reliability stage does not add automatic Worker retry, does not lower any
fail-closed rule, does not reset or discard a workspace, does not introduce a
daemon or multi-Worker architecture, and does not continue Phase 25 Runtime
Handoff, Phase 26 TUI, or Phase 27 CLI compatibility work.
