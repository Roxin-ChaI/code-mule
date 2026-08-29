# Conceptual Data Model

This document defines domain records and relationships for v0.1.0. It is storage-agnostic and includes no database implementation. Identifiers and timestamps are opaque and implementation-neutral; timestamps should represent an unambiguous instant.

## Project

The aggregate root for one software project.

| Field | Meaning |
| --- | --- |
| `id` | Stable project identifier. |
| `name` | Human-readable project name. |
| `status` | Current Project State. |
| `active_plan_id` | Current validated Plan version, if any. |
| `current_task_id` | Active Task, if any; at most one in v0.1.0. |
| `created_at` | Creation timestamp. |
| `updated_at` | Last validated state-change timestamp. |

## Requirement

| Field | Meaning |
| --- | --- |
| `id` | Stable requirement identifier. |
| `project_id` | Owning Project. |
| `title` | Concise requirement name. |
| `description` | Normalized intent and boundaries. |
| `status` | Lifecycle state of the requirement. |
| `priority` | Relative delivery priority. |
| `acceptance_criteria` | Verifiable conditions for satisfaction. |
| `introduced_by` | Boss or originating ChangeRequest identity. |
| `created_at` | Creation timestamp. |
| `updated_at` | Last update timestamp. |

## Plan

Plans are immutable versions in a project's planning history. Replanning creates a new version rather than silently mutating the active one.

| Field | Meaning |
| --- | --- |
| `id` | Stable Plan-version identifier. |
| `project_id` | Owning Project. |
| `version` | Project-unique, ordered version. |
| `status` | Draft, active, superseded, completed, or rejected lifecycle state. |
| `requirement_ids` | Requirements addressed by this version. |
| `milestone_ids` | Ordered or graph-associated Milestones in this version. |
| `created_at` | Creation timestamp. |

Only one Plan may be active for a Project. Historical versions remain available for audit and impact analysis.

## Milestone

| Field | Meaning |
| --- | --- |
| `id` | Stable Milestone identifier. |
| `plan_id` | Owning Plan version. |
| `title` | Milestone outcome. |
| `status` | Current milestone lifecycle state. |
| `task_ids` | Tasks belonging to the Milestone. |

## Task

Allowed statuses are `PENDING`, `IN_PROGRESS`, `COMPLETED`, `BLOCKED`, `CANCELLED`, and `REOPENED`. A reopened Task preserves its identity and history and becomes eligible for a later execution attempt when dependencies permit.

| Field | Meaning |
| --- | --- |
| `id` | Stable Task identifier. |
| `milestone_id` | Owning Milestone. |
| `title` | Concise scoped outcome. |
| `description` | Worker-ready work definition and boundaries. |
| `status` | One of the allowed Task statuses. |
| `dependencies` | Task IDs that must satisfy declared dependency rules first. |
| `acceptance_criteria` | Verifiable completion conditions. |
| `execution_attempts` | Ordered references or records for Worker attempts. |
| `created_at` | Creation timestamp. |
| `updated_at` | Last update timestamp. |

In v0.1.0, a Project may have at most one `IN_PROGRESS` Task. A Task must not be dispatched with unmet dependencies.

## ChangeRequest

| Field | Meaning |
| --- | --- |
| `id` | Stable change identifier. |
| `project_id` | Affected Project. |
| `description` | Requested addition, removal, or modification. |
| `status` | Submitted, analyzing, accepted, rejected, applied, or failed lifecycle state. |
| `affected_requirement_ids` | Requirements known or proposed to be affected. |
| `created_by` | Boss identity. |
| `created_at` | Creation timestamp. |

## ImpactAnalysis

| Field | Meaning |
| --- | --- |
| `change_request_id` | ChangeRequest being analyzed. |
| `architecture_impact` | Expected architectural consequences. |
| `affected_components` | Components likely to change. |
| `affected_completed_tasks` | Completed Tasks whose results may no longer hold. |
| `affected_in_progress_tasks` | Active work affected at the Safe Point. |
| `affected_pending_tasks` | Pending Tasks requiring change or reassessment. |
| `tasks_to_add` | Proposed new Tasks. |
| `tasks_to_reopen` | Completed Tasks proposed for `REOPENED`. |
| `tasks_to_cancel` | Obsolete Tasks proposed for `CANCELLED`. |
| `recommendation` | Structured recommended replanning action and rationale. |

The analysis is evidence for a new Plan version; it does not itself mutate the Task graph.

## Decision

| Field | Meaning |
| --- | --- |
| `id` | Stable Decision identifier. |
| `task_id` | Reviewed Task; completion-level decisions retain the final/current Task context. |
| `type` | `CONTINUE`, `REWORK`, `HUMAN_REQUIRED`, or `DONE`. |
| `rationale` | Review evidence and reasoning for humans; never parsed as control flow. |
| `created_at` | Creation timestamp. |

The structured `type`, after Orchestrator validation, controls the next legal action. The rationale is explanatory only.

## ExecutionReport

| Field | Meaning |
| --- | --- |
| `id` | Stable report identifier. |
| `task_id` | Executed Task. |
| `attempt` | Monotonic attempt number for that Task. |
| `status` | Structured execution outcome. |
| `files_changed` | Structured inventory of repository changes. |
| `tests` | Commands/check categories and observed results. |
| `static_checks` | Static-analysis categories and observed results. |
| `git_state` | Relevant branch, revision, diff, and cleanliness evidence. |
| `issues` | Known failures, risks, or deviations. |
| `human_action_required` | Whether and why explicit human action is needed. |
| `summary` | Human-readable execution summary. |
| `created_at` | Creation timestamp. |

## QualityStatus

QualityStatus summarizes the evidence relevant to the current Task and Plan. It considers:

- test results;
- lint results;
- type-check results;
- build results; and
- repository cleanliness.

Each category can record applicability, outcome, evidence, and observation time. Domain semantics must describe capability categories rather than hard-code specific tools; projects may use different test runners, linters, type checkers, and build systems.

## ProjectEvent

ProjectEvents form an append-only audit trail.

| Field | Meaning |
| --- | --- |
| `id` | Stable event identifier. |
| `project_id` | Owning Project. |
| `event_type` | Structured event category. |
| `entity_id` | Related entity, when applicable. |
| `timestamp` | Time the event was recorded. |
| `metadata` | Structured contextual facts, versions, actor, and evidence references. |

Events are appended, never silently rewritten. Sensitive values such as secrets must not be stored in event metadata.

## Human Gate record

Human Gates are persisted as part of Project State. A gate identifies the proposed action, exact scope, risk, requester, status, Boss decision, timestamps, and related entity or Task. Approval for one scope must not authorize another. Unresolved or invalid gates remain fail-closed.

## Relationships and invariants

- A Project owns Requirements, versioned Plans, ChangeRequests, QualityStatus, and ProjectEvents.
- A Plan belongs to one Project and owns Milestones; a Milestone belongs to one Plan and owns Tasks.
- `Project.active_plan_id` must reference that Project's single active Plan.
- `Project.current_task_id`, when set, must reference a Task in the active Plan and agree with its status.
- Decisions and ExecutionReports are retained per Task and attempt, preserving review history.
- ChangeRequest effects are traceable through ImpactAnalysis, the replacement Plan version, Task status changes, and ProjectEvents.
- Persisted state, structured reports, and repository evidence must agree before safe pause, resume, or completion.
