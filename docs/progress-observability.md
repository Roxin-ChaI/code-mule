# Runtime Progress and Observability

Phase 8.5 makes long-running Worker and Supervisor operations visible without
changing deterministic execution. Progress is ephemeral, read-only telemetry:
ProjectState remains the Source of Truth, and no control decision reads console
text, spinner state, percentages, renderer state, or display messages.

## Event Architecture

```text
Planning / Replanning / ProjectExecution / TaskCycle / Codex app-server client
  → ProgressEvent
  → ProgressSink
  ├── ConsoleProgressRenderer
  ├── RecordingProgressSink
  └── future presentation adapters
```

Events use injected business clocks and contain typed event kinds, optional
project/task/attempt identity, display text, and minimal string metadata. They
do not contain prompts, model responses, credentials, API keys, headers, full
shell commands, or file diffs. A resilient composite sink preserves delivery
order and records presentation errors while continuing core execution.

## Truthful Codex Activity

The mapping was originally based on the older Codex CLI 0.147.0 schema generated
with `codex app-server generate-json-schema`. The current v0.1.1 real E2E was
validated with Codex CLI 0.153.4, not asserted as a minimum version.
The protocol exposes `turn/started`,
`item/started`, `item/completed`, and `turn/completed`; item variants include
`commandExecution`, `fileChange`, and `agentMessage`. Code Mule projects only
those observed facts.

Raw commands are never displayed. Read/search/list actions may become
`Inspecting repository`; other commands become `Running command`. File changes
show only a safely relativized workspace path, otherwise `Updating file`.
Agent-message activity exposes lifecycle only, never message text. This follows
the official [Codex app-server](https://developers.openai.com/codex/app-server)
boundary and does not invent model reasoning states.

## Deterministic Progress

The active Plan defines `total_tasks`. Only `TaskStatus.COMPLETED` contributes
to `completed_tasks`; CANCELLED Tasks do not. Percentage is exactly:

```text
completed_tasks / total_tasks * 100
```

REWORK attempts, elapsed time, token usage, and model estimates never affect
the percentage. A zero-task presentation reports 0% rather than guessing.

## Console Modes

On an ANSI-capable TTY, `ConsoleProgressRenderer` maintains a compact live
dashboard with distinct Project, current Task, Worker, Supervisor, and Recent
Activity sections. It shows the active Plan version, deterministic progress,
latest safely projected Worker activity, stage/project elapsed time, and the
five most recent real activities. Human-readable status/decision labels come
from the read-only presentation layer; raw enums never drive control. Its lightweight
daemon thread uses `time.monotonic()` only for presentation animation. Context
manager shutdown stops the thread, performs a final redraw, restores the cursor,
and flushes the stream even when execution raises.

Dashboard lines adapt to current terminal width and safely truncate long task
titles or messages. Unicode terminals use `✓`, `→`, and block progress bars;
an explicit compatibility path uses ASCII symbols and bars.

When output is redirected or running in CI, the renderer emits ordered
timestamped lines. It writes no ANSI control sequences and starts no spinner
thread, preserving readable durable logs.

## Manual Runtime Integration

The manual DeepSeek scripts construct the renderer at the composition root.
Task-cycle, project, planning, and replanning scripts share the same sink, so
Worker activity and outstanding Supervisor calls remain visible. CHANGE adds
truthful `change.requested`, Impact Analysis, Plan materialization, and resumed
execution events. This does not change authentication, model, transport,
retry, token, structured-output, or Human Gate policy.

## Presentation Boundary

`code_mule.presentation` converts ProjectState or ProgressSnapshot into immutable
Boss-facing view models and lines. It cannot save state, schedule work, control
runtime execution, or parse console text into decisions. ProjectState remains
the Source of Truth. The same boundary can later support a TUI, WebSocket
stream, or Web dashboard; this phase adds no GUI or remote server.
