# Project State Machine

This document specifies the v0.1.0 project lifecycle conceptually. It does not implement a state machine.

## States

- `IDLE`: the project exists but no Plan is being prepared or executed.
- `PLANNING`: requirements are being normalized and a Plan is being produced or validated.
- `RUNNING`: a validated Plan is active; the Orchestrator may dispatch or review one Task at a time.
- `CHANGE_REQUESTED`: a CHANGE is recorded, new dispatch is stopped, and the system is reaching or validating a Safe Point.
- `REPLANNING`: impact has been analyzed and a new Plan version is being produced or validated.
- `CANCEL_REQUESTED`: STOP is recorded while a Task owns execution; no new Task may dispatch and the current Task is reaching a Safe Point.
- `PAUSED_BY_BOSS`: execution is safely paused at the Boss's request.
- `HUMAN_REQUIRED`: automatic progress is stopped pending a specific Boss decision or approval.
- `DONE`: every acceptance criterion for the active Plan has been verified and completion accepted.
- `FAILED`: the project cannot safely continue under current evidence or policy.
- `CANCELLED`: the Boss intentionally ended the project; completed history and delivery evidence remain preserved.

## Legal transitions

| From | To | Trigger / guard |
| --- | --- | --- |
| `IDLE` | `PLANNING` | Start requested with sufficient initial requirements. |
| `IDLE` | `CANCELLED` | Boss STOP is persisted before work starts. |
| `PLANNING` | `RUNNING` | Plan and Task graph validate successfully. |
| `PLANNING` | `HUMAN_REQUIRED` | Planning needs a Boss decision or approval. |
| `PLANNING` | `FAILED` | Planning cannot produce or validate a safe Plan. |
| `RUNNING` | `RUNNING` | A Task cycle yields `CONTINUE` or `REWORK` and invariants remain valid. |
| `RUNNING` | `CHANGE_REQUESTED` | Boss submits CHANGE. |
| `RUNNING` | `PAUSED_BY_BOSS` | PAUSE is requested and a Safe Point is verified. |
| `RUNNING` | `HUMAN_REQUIRED` | A Decision, gate, ambiguity, or inconsistency requires the Boss. |
| `RUNNING` | `DONE` | A validated `DONE` Decision confirms all Plan acceptance criteria. |
| `RUNNING` | `FAILED` | Execution or repository reconciliation fails unrecoverably. |
| `RUNNING` | `CANCEL_REQUESTED` | Boss STOP arrives while a Task is active. |
| `RUNNING` | `CANCELLED` | Boss STOP arrives at an idle Task boundary. |
| `CHANGE_REQUESTED` | `REPLANNING` | The active turn reaches a Safe Point and ImpactAnalysis can begin. |
| `CHANGE_REQUESTED` | `HUMAN_REQUIRED` | A Safe Point or change scope cannot be established automatically. |
| `CHANGE_REQUESTED` | `FAILED` | State cannot be safely reconciled or recovered. |
| `REPLANNING` | `RUNNING` | The new Plan version and updated Task graph validate. |
| `REPLANNING` | `HUMAN_REQUIRED` | Replanning requires a Boss decision or gated approval. |
| `REPLANNING` | `FAILED` | No safe, valid revised Plan can be established. |
| `PAUSED_BY_BOSS` | `RUNNING` | RESUME and state/repository consistency checks succeed. |
| `PAUSED_BY_BOSS` | `CHANGE_REQUESTED` | Boss submits CHANGE while paused. |
| `PAUSED_BY_BOSS` | `HUMAN_REQUIRED` | An in-flight Task reaches its Safe Point with a typed failure or Human Gate after PAUSE was recorded. |
| `HUMAN_REQUIRED` | `RUNNING` | Boss input resolves the issue and all resume guards pass. |
| `HUMAN_REQUIRED` | `PAUSED_BY_BOSS` | Boss elects to pause and repository state is safely reconciled. |
| `HUMAN_REQUIRED` | `FAILED` | Boss rejects a required action or the issue cannot be resolved safely. |
| `CANCEL_REQUESTED` | `CANCELLED` | The active Task reaches a verified Safe Point. |
| `CANCEL_REQUESTED` | `HUMAN_REQUIRED` | The active Task ends with uncertain execution or side-effect ownership. |

`PLANNING`, `REPLANNING`, `CHANGE_REQUESTED`, `PAUSED_BY_BOSS`, and
`HUMAN_REQUIRED` may also transition to `CANCELLED` at an idle safe boundary.
`CHANGE_REQUESTED` and `PAUSED_BY_BOSS` first use `CANCEL_REQUESTED` when an
active Task still owns execution.

`QUERY` never causes a transition, including while a Codex Task is active.

## Transition ownership

Only the Orchestrator may validate and apply transitions. The Boss supplies commands and approvals, the Supervisor proposes structured Decisions and Plans, and the Codex Worker supplies execution evidence; none of them directly changes project status.

For every transition, the Orchestrator must validate the source state, requested trigger, required evidence, target-state invariants, and authorization. It must persist the state change and corresponding ProjectEvent atomically from the control plane's perspective.

## Invalid transitions

An unlisted transition is invalid. The Orchestrator must reject it without mutating state, record the attempted transition and reason, and return a structured error. It must not guess an alternative transition from free text. If the request exposes ambiguity or loss of trustworthy state, the Orchestrator must route to `HUMAN_REQUIRED` or `FAILED` only through an otherwise legal transition.

Terminal states do not implicitly restart. v0.1.0 defines no outgoing transitions from `DONE`, `FAILED`, or `CANCELLED`.

## Fail-closed behavior

Missing approvals, invalid schemas, stale Plan or Task versions, inconsistent repository state, unknown execution results, unmet dependencies, and ambiguous authority prevent dispatch or transition. The last validated state remains authoritative unless a legal fail-closed transition to `HUMAN_REQUIRED` or `FAILED` is recorded.

No action requiring a Human Gate may begin until exact-scope approval exists. No project may claim `DONE`, safely paused, or safely resumed without required evidence.

## Safe Point semantics

When CHANGE, PAUSE, or STOP arrives during an atomic Codex Turn, the Orchestrator immediately records the command and stops dispatching new Tasks, but normally allows that turn to finish. It then:

1. obtains and validates the ExecutionReport;
2. inspects and reconciles repository state;
3. updates Task, quality, and Project State;
4. records audit events; and
5. performs the requested pause, begins replanning, or finalizes cancellation.

STOP never hard-kills the active Codex turn. At its Safe Point, accepted Task
work and its commit remain preserved, unfinished active-Plan Tasks become
`CANCELLED`, and project-level final verification is skipped.

The Safe Point exists only after these steps provide consistent evidence. A failed turn, absent or invalid report, or unknown repository state must lead to `HUMAN_REQUIRED` or `FAILED`; the Orchestrator must not report a safe pause by assumption.

If no Worker turn is active, the Orchestrator may establish the Safe Point immediately after validating persisted and repository state.
