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
Separator rules are drawn only when the screen has room.

Each pane is aligned deliberately:

- **Status** is top-aligned and drops its oldest lines only when it must.
- **Activity** is top-aligned, so content starts at the top of its region and
  the blank space stays below it. When the content is taller than the region it
  becomes a tail window: the newest lines stay visible and scrolling moves the
  window, not the alignment.
- **Boss** is bottom-aligned so `boss>` always sits on the last row, and its
  content is tail-kept so the prompt is never truncated away.

### Boss area

The Boss pane stays compact — one short line per fact, never an expanded
command:

```text
HUMAN ACTION · Worker input
Request: Confirm the manifest entry point before delivery
Choices: yes | no            (only when the Worker offered choices)
Actions: fail_project | acknowledge | answer
boss> _
```

The exact action id and the full `resolve <id> --strategy …` /
`answer <id> …` invocations stay discoverable through `code-mule inspect` and
the Activity pane. Dispatch is unchanged: typed lines still go through the
shared parser and command layer.

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
model, Worker, network, or shell. It seeds **60 numbered activity events**
(`event 001 … event 060`) so scrolling is unmistakable, and enables the
demo-only key readout.

### Reading the demo

The first row of the Activity pane is a live scroll indicator:

```text
Activity 41-60 / 60 · Follow ON     # auto-following the newest events
Activity 21-40 / 60 · Follow OFF    # scrolled back
```

The last resolved key name appears on the same line, while demo debugging is on:

```text
Activity 41-60 / 60 · Follow ON · Key: KEY_PPAGE
Activity 41-60 / 60 · Follow ON · Key: —          # no key pressed yet
```

The readout starts at an em dash, updates on every key that passes through the
key handler, and lives on the Activity status line so it never competes with the
Boss input row. Only whitelisted resolved names appear — never an escape
payload; an unmapped byte sequence reads `Key: UNKNOWN`. If a macOS Terminal does
not map a key combination to the expected curses key, the readout shows
`Key: UNKNOWN` (or the key it actually sent), which is the evidence needed to
decide whether any mapping should change. No escape sequence is guessed.

Suggested checks: fixed header/footer, `↑`/`↓` line scrolling, `Fn+↑`/`Fn+↓`
page scrolling, `Fn+→` (End) returning to auto-follow, `Ctrl+L` repaint,
`Ctrl+C` exit, a resize of the terminal window, a HumanAction rendered in the
Boss pane, `status` / `launch` / `inspect` typed at the prompt, and the UI
surviving after a command finishes.

## Testing

Headless coverage in `tests/tui/` asserts 80x24 and 120x40 geometry, fixed
header/footer placement, activity scrolling and auto-follow, HumanAction
rendering, approve/reject/resolve/answer dispatch, continued interaction after
DONE, Ctrl+C safety, the non-TTY fallback, CJK/emoji width, live state refresh,
duplicate suppression, frame repainting across resizes, and that dispatching
commands never writes ProjectState.
