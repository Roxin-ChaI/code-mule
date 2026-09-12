"""Dashboard projections of existing typed facts, without state changes."""

from code_mule.domain.enums import HumanActionStatus
from .labels import humanize_identifier
from .models import project_view
from .terminal import DashboardSection, TerminalDashboard, row


def internal(lines):
    return (DashboardSection("INTERNAL", lines[lines.index("INTERNAL") + 1:]),) if "INTERNAL" in lines else ()


def project_dashboard(state, terminal: TerminalDashboard, lines, *, verbose=False):
    view = project_view(state)
    task = next((item for item in state.tasks if item.id == state.project.current_task_id), None)
    title = {"done": "PROJECT COMPLETED", "cancelled": "PROJECT CANCELLED"}.get(view.raw_status, "CODE MULE · " + view.name)
    content = (row("Project", view.name), row("Status", view.status),
               row("Revision", "—" if view.revision_number is None else view.revision_number),
               row("Plan", "—" if view.plan_version is None else f"v{view.plan_version}"),
               row("Progress", terminal.progress(view.completed_tasks, view.total_tasks)),
               *((row("Reused", f"{view.reused_task_count} task(s) from v{view.plan_version - 1 if view.plan_version else '-'}"),) if view.reused_task_count else ()),
               row("Current", "None" if task is None else task.title),
               row("Safe Point", humanize_identifier(state.latest_safe_point.kind.value if state.latest_safe_point else "unknown")))
    sections = [DashboardSection(title, content)]
    verification = next((item for item in reversed(state.project_verification_results) if item.plan_id == state.project.active_plan_id), None)
    if verification is not None:
        checks = tuple(row(check.name, check.status.value.title()) for check in verification.checks)
        decision = verification.final_review_decision
        checks += (row("Final Review", "Pending" if decision is None else {"approve": "Approved", "human_required": "Action required"}[decision.value]),)
        sections.append(DashboardSection("FINAL VERIFICATION", checks))
    elif view.total_tasks and view.completed_tasks == view.total_tasks:
        sections.append(DashboardSection("FINAL VERIFICATION", ("No persisted verification result is available.",)))
    if view.raw_status == "done":
        revision = next((item for item in reversed(state.revisions) if item.lifecycle_status.value == "completed"), None)
        manifest = next((item for item in reversed(state.delivery_manifests) if revision is not None and item.revision_number == revision.revision_number and item.plan_version == revision.plan_version), None)
        if manifest is not None:
            sections.append(DashboardSection("DELIVERY HANDOFF", (
                row("Type", humanize_identifier(manifest.deliverable_type.value)),
                row("Entry", manifest.entry_point),
                row("Runnable", "Yes" if manifest.runnable else "No"),
                row("Next", "code-mule launch" if manifest.runnable else "code-mule deliverable"),
            )))
        elif not state.delivery_manifest_required:
            sections.append(DashboardSection("DELIVERY HANDOFF", ("Unavailable for this historical state.",)))
    actions = tuple(action for action in state.human_actions if action.status is HumanActionStatus.PENDING)
    if actions:
        sections.append(DashboardSection("BOSS ACTION", (row("Category", view.boss_action),) + tuple(action.summary for action in actions) + ("", "Next: code-mule inspect")))
    if verbose:
        sections.extend(internal(lines))
    return terminal.render(tuple(sections))


def diagnosis_dashboard(diagnosis, terminal, lines, *, verbose=False):
    # Values come from Phase 20/21 diagnosis; no new recoverability inference.
    from .labels import status_label
    diagnosis_rows = (
        row("Blocker", humanize_identifier(diagnosis.blocker_category.value)),
        row("Reason", diagnosis.blocker_summary),
        row("Stage", humanize_identifier(diagnosis.blocker_stage.value)),
        row("Safe Point", humanize_identifier(diagnosis.last_safe_point or "unknown")),
        row("Stop Reason", humanize_identifier(diagnosis.stop_reason or "none")),
        row("Recoverability", humanize_identifier(diagnosis.recoverability.value)),
    )
    if diagnosis.target_plan_version is not None:
        diagnosis_rows += (
            row("Base Revision", diagnosis.base_revision or "—"),
            row("Requested", diagnosis.requested_revision or "—"),
            row("Base Plan", f"v{diagnosis.base_plan_version or '—'}"),
            row("Target Plan", f"v{diagnosis.target_plan_version}"),
            row("Plan materialized", "Yes" if diagnosis.plan_materialized else "No"),
            row("Failure", humanize_identifier(diagnosis.failure_category or "unknown_failure")),
        )
    if diagnosis.final_review_outcome is not None:
        diagnosis_rows += (
            row("Verification", humanize_identifier(diagnosis.project_verification_status or "unknown")),
            row("Final Review", humanize_identifier(diagnosis.final_review_outcome)),
            row("Candidate HEAD", diagnosis.completion_head_candidate or "—"),
        )
    if diagnosis.worker_uncertainty is not None:
        evidence = diagnosis.worker_uncertainty
        diagnosis_rows += (
            row("Worker attempt", evidence.attempt),
            row("Stop cause", humanize_identifier(evidence.stop_cause.value)),
            row("Workspace", humanize_identifier(evidence.workspace_state.value)),
            row("Ownership", humanize_identifier(evidence.ownership_status.value)),
            row("Retry safe", "Yes" if evidence.retry_safe else "No"),
        )
    if diagnosis.no_change_delivery is not None:
        evidence = diagnosis.no_change_delivery
        diagnosis_rows += (
            row("Delivery", "No repository changes"),
            row("Supervisor", "Reviewed" if evidence.supervisor_reviewed else "Not reviewed"),
            row("Continuation", "Safe" if evidence.continuation_safe else "Blocked"),
            row("Commit", "Not required after approval"),
        )
    sections = (
        DashboardSection("PROJECT · " + diagnosis.project_name, (
            row("Status", status_label(diagnosis.project_status)),
            row("Revision", "—" if diagnosis.revision_number is None else diagnosis.revision_number),
            row("Plan", "—" if diagnosis.active_plan_version is None else f"v{diagnosis.active_plan_version}"),
            row("Progress", terminal.progress(diagnosis.completed_tasks, diagnosis.total_tasks)),
            row("Current", diagnosis.current_task_title or "None"))),
        DashboardSection("DIAGNOSIS", diagnosis_rows),
        DashboardSection("NEXT ACTION", (diagnosis.recommended_next_action.value,)),
    )
    if diagnosis.verification is not None:
        check = diagnosis.verification
        sections += (DashboardSection("VERIFICATION", (row("Check", check.check_name), row("Status", humanize_identifier(check.check_status.value)), row("Required", "Yes" if check.required else "No"))),)
    return terminal.render(sections + (internal(lines) if verbose else ()))


def human_dashboard(action, state, terminal, lines, *, verbose=False):
    task = next((item for item in state.tasks if item.id == action.task_id), None) if state else None
    # Keep existing approved command guidance and safe verification diagnostics.
    content = tuple(line for line in lines[1:] if not (line and set(line) <= {"─"}))
    content = tuple(row("Task", task.title if task else "Project") if line.startswith("Task        ") else line for line in content)
    if "INTERNAL" in content:
        content = content[:content.index("INTERNAL")]
    sections = (DashboardSection("ACTION REQUIRED", (row("What happened", action.summary),) + content),)
    return terminal.render(sections + (internal(lines) if verbose else ()))


def recovery_dashboard(state, plan, terminal, *, verbose=False):
    view = project_view(state)
    task = next((item for item in state.tasks if item.id == plan.task_id), None)
    sections = (DashboardSection("RECOVERY READY", (
        row("Project", view.name), row("Plan", "—" if view.plan_version is None else f"v{view.plan_version}"),
        row("Task", task.title if task else "None"),
        row("Safe Point", humanize_identifier(plan.from_safe_point.value)),
        row("Mode", humanize_identifier(plan.recovery_mode.value)),
        row("Workspace", "Validated" if plan.requires_workspace_validation else "Not required"),
        row("Fresh Worker", "Yes" if plan.fresh_worker_required else "No"),
        row("Next", "Continue the approved recovery path"))),)
    if verbose:
        sections += (DashboardSection("INTERNAL", (row("task_id", plan.task_id or "-"), row("mode", plan.recovery_mode.value))),)
    return terminal.render(sections)
