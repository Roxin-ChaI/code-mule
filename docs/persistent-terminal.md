# Persistent Terminal UI

`code-mule ui` replaces the scroll-away dashboard on a TTY with a fixed
three-pane terminal. The UI is a **presentation/controller layer only**: it
reads persisted `ProjectState`, dispatches Boss input through the same command
layer the one-shot CLI uses, and never owns business truth.

## Layout

```text
┌ Status   (fixed) ─────────────────────────────┐
│ Project · Revision · Plan · Status · Progress │
│ Current Task · Safe Point · Runtime           │
├───────────────────────────────────────────────┤
│ Activity (scrollable, tail-anchored)          │
│ worker / supervisor / verification / command  │
├───────────────────────────────────────────────┤
│ Boss     (fixed) HumanAction + boss> input    │
└───────────────────────────────────────────────┘
```

Geometry is pure: `tui.layout.compute_layout` splits the screen so Status and
Boss keep their content height and Activity absorbs the slack, and
`render_screen` returns exactly `rows` lines, each within `cols` display cells.
Separator rules are drawn only when the screen has room. Overflow is anchored:
Status drops from the bottom, Activity and Boss keep their **newest** lines, so
the input prompt is never truncated away.

Display width is cell-accurate, not character-count based: `wrap_cells` and
`display_width` from the Phase 22 presentation layer handle CJK wide forms,
combining marks, emoji ZWJ sequences, and regional indicators, so a resize
never paints past the pane edge.

## Interaction

| Key | Effect |
| --- | --- |
| printable characters | edit the `boss>` input line |
| Enter | dispatch the line through the CLI command layer |
| Up / Down / PgUp / PgDn / End | scroll activity; End resumes auto-follow |
| Ctrl+L | repaint |
| Ctrl+C | leave the UI; project state is not modified |

Commands are parsed with the **same argparse grammar** as the shell CLI
(`cli.parser.build_parser`) and executed through `cli.app.dispatch_arguments`,
so a typed line behaves exactly like the one-shot command. Natural-language
commands (`change`, `ask`, `answer`) accept an unquoted tail.

The UI never approves a HumanAction, never retries a Worker, and never starts a
process on its own. A pending HumanAction is rendered in the Boss pane with its
exact available actions; only an explicit Boss keystroke dispatches one.

`ProjectState` is re-read on every refresh, so planning, Worker activity,
verification, Human Gates, recovery, completion, and runtime changes all appear
without a second source of truth. Dispatch runs on a bounded background thread
so the Activity pane keeps rendering while a command is in flight.

## Non-TTY behaviour

`code-mule ui` requires a TTY. When stdin or stdout is not a terminal it prints
one deterministic text frame instead (`persistent UI unavailable: …`), which
keeps pipes, CI, and scripted runs on the stable Phase 22 output.

## Manual visual acceptance

```bash
.venv/bin/python scripts/tui_demo.py
```

The demo runs the real UI against a synthetic local project and never calls a
model, Worker, network, or shell. Suggested checks: fixed header/footer,
activity scrolling with Up/Down, a resize of the terminal window, a HumanAction
rendered in the Boss pane, `status` / `launch` / `inspect` typed at the prompt,
the UI surviving after a command finishes, and Ctrl+C leaving without a
modified project.

## Testing

Headless coverage in `tests/tui/` asserts 80x24 and 120x40 geometry, fixed
header/footer placement, activity scrolling and auto-follow, HumanAction
rendering, approve/reject/resolve/answer dispatch, continued interaction after
DONE, Ctrl+C safety, the non-TTY fallback, CJK/emoji width, live state refresh,
duplicate suppression, frame repainting across resizes, and that dispatching
commands never writes ProjectState.
