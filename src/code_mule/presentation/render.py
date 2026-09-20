"""Line-oriented Boss CLI rendering from immutable view models."""

from code_mule.domain.enums import HumanActionCategory, RevisionStatus
from code_mule.domain.models import HumanAction
from code_mule.diagnosis import (
    DiagnosisRecoverability,
    ProjectDiagnosis,
)
from code_mule.project_verification import (
    FinalReviewDecision,
    ProjectVerificationStatus,
    final_review_human_judgment_evidence,
)
from code_mule.state.models import ProjectState
from code_mule.recovery.contracts import WorkerStopCause
from code_mule.transport import WorkerFailureClass

from .models import human_action_view, project_view
from .labels import humanize_identifier, status_label
from .terminal import TerminalDashboard
from .panels import project_dashboard, diagnosis_dashboard, human_dashboard


def render_project_diagnosis(
    diagnosis: ProjectDiagnosis, *, verbose: bool = False, terminal: TerminalDashboard | None = None
) -> tuple[str, ...]:
    recoverability = {
        DiagnosisRecoverability.RECOVERABLE: "Yes",
        DiagnosisRecoverability.NOT_APPLICABLE: "Not applicable",
        DiagnosisRecoverability.UNCERTAIN: "Uncertain",
        DiagnosisRecoverability.TERMINAL: "No (terminal state)",
    }[diagnosis.recoverability]
    current = (
        "None"
        if diagnosis.current_task_id is None
        else f"{diagnosis.current_task_id} · {diagnosis.current_task_title}"
    )
    latest = (
        "None"
        if diagnosis.latest_completed_task_id is None
        else (
            f"{diagnosis.latest_completed_task_id} · "
            f"{diagnosis.latest_completed_task_title}"
        )
    )
    lines = (
        "PROJECT DIAGNOSIS",
        "",
        f"Project        {diagnosis.project_name}",
        f"Status         {status_label(diagnosis.project_status)}",
        f"Revision       {'—' if diagnosis.revision_number is None else diagnosis.revision_number}",
        f"Plan           {'—' if diagnosis.active_plan_version is None else f'v{diagnosis.active_plan_version}'}",
        f"Progress       {diagnosis.completed_tasks} / {diagnosis.total_tasks}",
        f"Current Task   {current}",
        f"Latest Done    {latest}",
        f"Last Safe Point {humanize_identifier(diagnosis.last_safe_point or 'unknown')}",
        f"Stop Reason    {humanize_identifier(diagnosis.stop_reason or 'none')}",
        "",
        "BLOCKER",
        "",
        f"Reason         {diagnosis.blocker_summary}",
        f"Stage          {humanize_identifier(diagnosis.blocker_stage.value)}",
        f"Recoverable    {recoverability}",
    )
    if diagnosis.target_plan_version is not None:
        lines += (
            f"Base Revision  {diagnosis.base_revision or '-'}",
            f"Requested      {diagnosis.requested_revision or '-'}",
            f"Base Plan      v{diagnosis.base_plan_version or '-'}",
            f"Target Plan    v{diagnosis.target_plan_version}",
            "Plan created  "
            + ("Yes" if diagnosis.plan_materialized else "No"),
            "Revision created "
            + (
                "Yes"
                if diagnosis.requested_revision_materialized
                else "No"
            ),
            "Failure        "
            + humanize_identifier(
                diagnosis.failure_category or "unknown_failure"
            ),
        )
        if diagnosis.failure_code is not None:
            lines += (
                "Validation     "
                + humanize_identifier(diagnosis.failure_code),
            )
    elif diagnosis.failure_code is not None:
        lines += (
            "Validation     " + humanize_identifier(diagnosis.failure_code),
        )
    if diagnosis.failure_field_path is not None:
        lines += (f"Field          {diagnosis.failure_field_path}",)
    if diagnosis.failure_summary is not None:
        lines += (f"Detail         {diagnosis.failure_summary}",)
    if diagnosis.verification is not None:
        verification = diagnosis.verification
        lines += (
            f"Check           {verification.check_name}",
            f"Check type      {humanize_identifier(verification.check_type.value)}",
            f"Check status    {humanize_identifier(verification.check_status.value)}",
            f"Required        {'Yes' if verification.required else 'No'}",
        )
    if diagnosis.worker_uncertainty is not None:
        evidence = diagnosis.worker_uncertainty
        lines += (
            "Failure class     "
            + humanize_identifier(diagnosis.worker_failure_class or "not recorded"),
        )
        if evidence.stop_cause in {
            WorkerStopCause.REPORT_PARSE_FAILED,
            WorkerStopCause.TERMINAL_RECEIVED_REPORT_PARSE_FAILED,
        }:
            lines += (
                "Worker turn       "
                + (
                    "Completed"
                    if evidence.trusted_terminal_result
                    else "Not proven complete"
                ),
                "Transport         Healthy",
                "Report            Invalid",
            )
        lines += _worker_uncertainty_lines(evidence, include_context=False)
        if verbose:
            lines += (
                "transport_evidence_present: "
                + str(diagnosis.worker_transport_evidence_available).lower(),
            )
    if diagnosis.no_change_delivery is not None:
        lines += _no_change_delivery_lines(diagnosis.no_change_delivery)
    lines += (
        "",
        "ME ACTION",
        "",
        f"Required       {'Yes' if diagnosis.boss_action_required else 'No'}",
        f"Next           {diagnosis.recommended_next_action.value.title() if diagnosis.recommended_next_action.value == 'none' else diagnosis.recommended_next_action.value}",
    )
    if diagnosis.change_summary is not None:
        lines += (
            "",
            "PENDING CHANGE",
            f"Requested      Revision {diagnosis.requested_revision or '-'}",
            f"Base           Revision {diagnosis.base_revision or '-'} · "
            f"Plan v{diagnosis.base_plan_version or '-'}",
            f"Change         {diagnosis.change_summary}",
        )
    if verbose:
        lines += (
            "",
            "INTERNAL",
            f"project_status: {diagnosis.project_status.value}",
            f"blocker_category: {diagnosis.blocker_category.value}",
            f"blocker_stage: {diagnosis.blocker_stage.value}",
            f"recoverability: {diagnosis.recoverability.value}",
            f"current_task_id: {diagnosis.current_task_id or '-'}",
            f"latest_completed_task_id: {diagnosis.latest_completed_task_id or '-'}",
            f"latest_task_commit: {diagnosis.latest_task_commit or '-'}",
            f"pending_action_id: {diagnosis.pending_action_id or '-'}",
            f"recovery_mode: {diagnosis.recovery_mode or '-'}",
            f"recovery_command: {diagnosis.recovery_command or '-'}",
        )
        if diagnosis.worker_input_question is not None:
            lines += (f"worker_input_question: {diagnosis.worker_input_question}",)
    if terminal is not None and terminal.interactive:
        return diagnosis_dashboard(diagnosis, terminal, lines, verbose=verbose)
    return lines if terminal is None else terminal.legacy(lines)


def render_project(
    state: ProjectState,
    *,
    verbose: bool = False,
    heading: str = "PROJECT",
    execution_stop_reason: str | None = None,
    terminal: TerminalDashboard | None = None,
) -> tuple[str, ...]:
    view = project_view(state)
    lines = (
        heading,
        view.name,
        "",
        f"Status      {view.status}",
        f"Revision    {'—' if view.revision_number is None else view.revision_number}",
        f"Plan        {'—' if view.plan_version is None else f'v{view.plan_version}'}",
        f"Progress    {view.completed_tasks} / {view.total_tasks}",
        *((
            f"Reused      {view.reused_task_count} task(s) from earlier revisions",
        ) if view.reused_task_count else ()),
        f"Current     {view.current_task or 'None'}",
        f"Me action   {view.boss_action or 'None'}",
    )
    verification = next(
        (
            result
            for result in reversed(state.project_verification_results)
            if result.plan_id == state.project.active_plan_id
        ),
        None,
    )
    if verification is not None:
        lines += ("", "FINAL VERIFICATION")
        symbols = {
            ProjectVerificationStatus.PASS: "✓",
            ProjectVerificationStatus.FAIL: "✗",
            ProjectVerificationStatus.TIMEOUT: "✗",
            ProjectVerificationStatus.SKIPPED: "–",
            ProjectVerificationStatus.PENDING: "○",
        }
        lines += tuple(
            f"{symbols[check.status]} {check.name}"
            for check in verification.checks
        )
        if verification.final_review_decision is FinalReviewDecision.APPROVE:
            lines += ("✓ Supervisor final review",)
        elif verification.final_review_decision is FinalReviewDecision.HUMAN_REQUIRED:
            lines += ("✗ Supervisor final review",)
        else:
            lines += ("○ Supervisor final review",)
        failed = next(
            (
                check
                for check in verification.checks
                if check.required
                and check.status is not ProjectVerificationStatus.PASS
            ),
            None,
        )
        if failed is not None:
            lines += (f"Failure     {failed.name}: {failed.safe_summary}",)
    elif (
        view.total_tasks > 0
        and view.completed_tasks == view.total_tasks
        and state.project.status.value != "done"
    ):
        lines += (
            "",
            "FINAL VERIFICATION",
            "○ Project checks have not completed.",
            "○ Supervisor final review is pending.",
        )
    if state.project.status.value == "done":
        lines += ("", "PROJECT COMPLETED")
        revision = next(
            (item for item in reversed(state.revisions) if item.lifecycle_status.value == "completed"),
            None,
        )
        manifest = next(
            (
                item for item in reversed(state.delivery_manifests)
                if revision is not None
                and item.revision_number == revision.revision_number
                and item.plan_version == revision.plan_version
            ),
            None,
        )
        if manifest is not None:
            lines += (
                "",
                "DELIVERY HANDOFF",
                f"Type        {manifest.deliverable_type.value.replace('_', ' ').title()}",
                f"Entry       {manifest.entry_point}",
                f"Runnable    {'Yes' if manifest.runnable else 'No'}",
                f"Next        {'code-mule launch' if manifest.runnable else 'code-mule deliverable'}",
            )
        elif not state.delivery_manifest_required:
            lines += ("", "DELIVERY HANDOFF", "Unavailable for this historical state.")
    if verbose:
        lines += (
            "",
            "INTERNAL",
            f"project_id: {view.project_id}",
            f"project_status: {view.raw_status}",
            f"active_plan_id: {view.active_plan_id or '-'}",
            f"active_plan_version: {view.plan_version or '-'}",
            f"current_task_id: {view.current_task_id or '-'}",
            f"execution_stop_reason: {execution_stop_reason or '-'}",
            f"latest_task_commit: {view.latest_task_commit or '-'}",
            "project_verification_result_id: "
            + ("-" if verification is None else verification.id),
            "final_review_decision: "
            + (
                "-"
                if verification is None or verification.final_review_decision is None
                else verification.final_review_decision.value
            ),
        )
    if terminal is not None and terminal.interactive:
        return project_dashboard(state, terminal, lines, verbose=verbose)
    return lines if terminal is None else terminal.legacy(lines)


def render_change_requested(
    state: ProjectState, request: str, *, verbose: bool = False
) -> tuple[str, ...]:
    view = project_view(state)
    completed_revision_change = any(
        revision.lifecycle_status is RevisionStatus.COMPLETED
        and revision.plan_id == state.project.active_plan_id
        for revision in state.revisions
    ) and state.project.current_task_id is None
    boundary_message = (
        "Completed revision will remain unchanged while the next revision is planned."
        if completed_revision_change
        else "Current task will finish safely before replanning."
    )
    lines = (
        "CHANGE REQUESTED",
        "",
        f'"{request}"',
        "",
        boundary_message,
        f"Plan        {'—' if view.plan_version is None else f'v{view.plan_version}'}",
        "Next        Impact analysis",
        "",
        "Run:",
        "  code-mule change --apply",
    )
    if verbose:
        lines += (
            "",
            f"project_id: {view.project_id}",
            f"project_status: {view.raw_status}",
            f"active_plan_id: {view.active_plan_id or '-'}",
        )
    return lines


def render_change_applied(
    before: ProjectState,
    after: ProjectState,
    *,
    verbose: bool = False,
    execution_stop_reason: str | None = None,
    terminal: TerminalDashboard | None = None,
) -> tuple[str, ...]:
    old = project_view(before)
    new = project_view(after)
    lines = (
        "REPLANNING",
        "✓ Impact analysis completed",
        "✓ Existing work preserved",
        f"✓ Plan v{old.plan_version or '—'} superseded",
        f"✓ Plan v{new.plan_version or '—'} activated",
        "",
        "Execution resumed.",
    )
    if terminal is not None:
        lines = terminal.legacy(lines)
    return lines + render_project(
        after,
        verbose=verbose,
        heading="PROJECT",
        execution_stop_reason=execution_stop_reason,
        terminal=terminal,
    )


def render_human_action(
    action: HumanAction, *, verbose: bool = False, state: ProjectState | None = None,
    terminal: TerminalDashboard | None = None, codex_version: str | None = None,
) -> tuple[str, ...]:
    view = human_action_view(action)
    final_review = (
        None
        if state is None
        else final_review_human_judgment_evidence(state, action)
    )
    no_change = None
    category = "Final review decision" if final_review is not None else view.category
    lines = (
        "ACTION REQUIRED",
        "────────────────────────",
        "",
        f"Category    {category}",
        f"Task        {view.task or 'None'}",
    )
    if final_review is not None:
        revision = final_review.revision
        passed = all(
            not check.required or check.status is ProjectVerificationStatus.PASS
            for check in final_review.checks
        )
        lines += (
            "",
            "FINAL REVIEW",
            "Outcome     Needs human judgment",
            f"Revision    {revision.revision_number if revision is not None else 'Unknown'}",
            f"Plan        v{revision.plan_version if revision is not None else '—'}",
            f"Verification {'Completed' if passed else 'Not passed'}",
            f"Candidate   {final_review.completion_head_candidate}",
            f"Finding     {(final_review.issues[0] if final_review.issues else final_review.summary)}",
        )
        if len(final_review.issues) > 1:
            lines += ("", "Other findings") + tuple(
                f"- {issue}" for issue in final_review.issues[1:]
            )
        lines += (
            "",
            "Evidence",
            *(
                f"- {check.name}: {check.status.value} — {check.safe_summary}"
                for check in final_review.checks
            ),
            "",
            "Code changes Not determined; decision required from me",
            "Accept as-is No; final review has not approved completion",
            "Suggested    Submit a precise change if correction is required",
        )
    if view.capability is not None:
        lines += (
            f"Approval type {view.approval_type}",
            f"Capability  {view.capability}",
            f"{'Purpose' if view.approval_type == 'Native sandbox approval' else 'Application'} "
            f"{view.application or 'Not provided by protocol'}",
            *(
                (f"Command     {action.capability_approval.tool_name or 'Not provided by protocol'}",)
                if view.approval_type == "Native sandbox approval"
                else ()
            ),
            *(
                (f"Target      {action.capability_approval.capability_id or 'Not provided by protocol'}",)
                if view.approval_type == "Native sandbox approval"
                else ()
            ),
            "Scope       " + ", ".join(view.approval_scopes),
            "Native request "
            + (
                "Active"
                if view.native_request_active
                else "Expired"
            ),
            f"Continuation {view.continuation}",
        )
    elif view.approval_type is not None:
        lines += (
            f"Approval type {view.approval_type}",
            "Protocol event Worker structured report (not a native request)",
            "Native request Not applicable",
            f"Continuation {view.continuation}",
        )
    if view.question is not None:
        lines += (f"Question    {view.question}",)
        if view.choices:
            lines += ("", "Choices") + tuple(
                f"- {choice}" for choice in view.choices
            )
    else:
        lines += (f"Request     {view.request}",)
    if action.category is HumanActionCategory.WORKER_VERIFICATION and state is not None:
        lines += _render_verification_failure(state, action, verbose=verbose)
    if (
        action.category is HumanActionCategory.SUPERVISOR_FAILURE
        and state is not None
        and final_review is None
    ):
        replanning = _render_post_completion_replanning_failure(state, action)
        lines += replanning or _render_planning_failure(state, action)
        lines += _render_manifest_handoff_failure(state, action)
    if (
        action.category is HumanActionCategory.RECOVERY_UNCERTAIN
        and state is not None
    ):
        from code_mule.recovery.uncertainty import worker_uncertainty_evidence

        uncertainty = worker_uncertainty_evidence(state, action)
        if uncertainty is not None:
            lines += ("", "RECOVERY UNCERTAIN") + _worker_uncertainty_lines(
                uncertainty, include_context=True
            )
    if state is not None:
        from code_mule.git_delivery.recovery import (
            no_change_delivery_recovery_evidence,
        )

        no_change = no_change_delivery_recovery_evidence(state, action)
        if no_change is not None:
            lines += ("", "NO-CHANGE DELIVERY REVIEW")
            lines += _no_change_delivery_lines(no_change)
    lines += (f"Risk        {view.risk}", "", (
        "No delivery commit was created. Worker changes were preserved."
        if action.category is HumanActionCategory.WORKER_VERIFICATION
        else "No action has been executed."
    ))
    if action.category is HumanActionCategory.WORKER_INPUT:
        lines += (
            "",
            "Answer:",
            f'  code-mule answer {view.action_id} "<answer>"',
        )
    elif view.approvable:
        lines += (
            "",
            "Approve:",
            f"  code-mule approve {view.action_id}",
            "",
            "Reject:",
            f"  code-mule reject {view.action_id}",
        )
    elif action.capability_approval is not None:
        lines += (
            "",
            "The original native request has expired. A later approve command cannot",
            "deliver a response to it or start a fresh Worker safely.",
        )
    elif action.category is HumanActionCategory.WORKER_VERIFICATION:
        lines += (
            "", "No automatic retry or approval is available for this verification block.",
            "Acknowledging the action does not resume execution:",
            f"  code-mule resolve {view.action_id} --strategy acknowledge",
        )
    elif state is not None:
        from code_mule.human import allowed_resolution_strategies

        strategies = allowed_resolution_strategies(state, action)
        if strategies:
            lines += (
                "",
                "Allowed strategies:",
                *(f"  {strategy.value}" for strategy in strategies),
                "",
                "Resolve:",
                *(
                    f"  code-mule resolve {view.action_id} --strategy {strategy.value}"
                    for strategy in strategies
                ),
            )
            if final_review is not None:
                lines += (
                    "",
                    "Request a correction:",
                    '  code-mule change "<my correction>"',
                    "Acknowledging only records awareness and pauses safely; it does not approve completion.",
                )
            elif action.category is HumanActionCategory.RECOVERY_UNCERTAIN:
                lines += ("", "Acknowledge records awareness only. The Human Gate stays pending,",
                          "the Task remains incomplete, and no retry or commit is performed.")
                lines += (
                    (
                        "The verified continue_after_report option remains available after acknowledgement."
                    )
                    if no_change is not None and no_change.continuation_safe
                    else "After acknowledgement, inspect manually or fail the project explicitly.",
                )
        else:
            lines += ("", "No resolve strategy is available for this action.")
    else:
        lines += (
            "",
            "Resolve:",
            f"  code-mule resolve {view.action_id} --strategy <strategy>",
        )
    if verbose:
        lines += (
            "",
            "INTERNAL",
            f"action_id: {view.action_id}",
            f"project_id: {view.project_id}",
            f"task_id: {view.task_id or '-'}",
            f"status: {view.raw_status}",
            f"created_at: {view.created}",
            f"summary: {view.summary}",
        )
        if view.worker_request_method is not None:
            lines += (
                f"worker_request_method: {view.worker_request_method}",
                f"worker_request_id: {view.worker_request_id or '-'}",
            )
        if action.capability_approval is not None:
            details = action.capability_approval
            lines += (
                f"worker_thread_id: {details.thread_id}",
                f"worker_turn_id: {details.turn_id}",
                f"approval_server: {details.server_name}",
                "approval_scopes: "
                + ",".join(scope.value for scope in details.approval_scopes),
                "native_request_active: "
                + str(details.native_request_active).lower(),
            )
        if state is not None:
            lines += _render_worker_failure(state, action)
            from code_mule.recovery.uncertainty import worker_uncertainty_evidence

            evidence = worker_uncertainty_evidence(state, action)
            if evidence is not None:
                lines += _worker_transport_lines(
                    state,
                    action.task_id,
                    stop_cause=evidence.stop_cause.value,
                    report_persisted=evidence.report_persisted,
                    retry_safe=evidence.retry_safe,
                    codex_version=codex_version,
                )
    if terminal is not None and terminal.interactive:
        return human_dashboard(action, state, terminal, lines, verbose=verbose)
    return lines if terminal is None else terminal.legacy(lines)


def _worker_uncertainty_lines(evidence, *, include_context: bool) -> tuple[str, ...]:
    from .labels import humanize_identifier

    lines: tuple[str, ...] = ()
    if include_context:
        lines += (
            f"Revision          {evidence.revision_number or '-'}",
            f"Plan              {'-' if evidence.plan_version is None else f'v{evidence.plan_version}'}",
            f"Task              {evidence.task_id} · {evidence.task_title}",
        )
    lines += (
        f"Attempt           {evidence.attempt}",
        f"Worker started    {'Yes' if evidence.worker_started else 'No'}",
        f"Last trusted      {evidence.last_trusted_stage}",
        "Terminal result   "
        + ("Received" if evidence.trusted_terminal_result else "Missing"),
        f"Report persisted  {'Yes' if evidence.report_persisted else 'No'}",
        "Workspace changed "
        + humanize_identifier(evidence.workspace_state.value),
    )
    if evidence.partial_paths:
        suffix = "" if evidence.partial_paths_complete else " (last trusted report)"
        lines += ("Partial paths" + suffix,)
        lines += tuple(f"- {path}" for path in evidence.partial_paths)
    else:
        lines += ("Partial paths     None recorded",)
    lines += (
        "Commit created    "
        + (evidence.commit_sha if evidence.commit_created else "No"),
        "Ownership status  "
        + humanize_identifier(evidence.ownership_status.value),
        "Stop cause        " + _stop_cause_label(evidence),
        f"Retry safe        {'Yes' if evidence.retry_safe else 'No'}",
    )
    return lines


_LEGACY_INSUFFICIENT = "Legacy evidence insufficient"


def _stop_cause_label(evidence) -> str:
    """Name a typed stop cause; never present legacy absence as Unknown."""

    from code_mule.recovery.contracts import WorkerStopCause
    from .labels import humanize_identifier

    if evidence.stop_cause in {
        WorkerStopCause.REPORT_PARSE_FAILED,
        WorkerStopCause.TERMINAL_RECEIVED_REPORT_PARSE_FAILED,
    }:
        return "Report contract rejected the Worker report"
    if evidence.stop_cause is WorkerStopCause.UNKNOWN:
        return _LEGACY_INSUFFICIENT
    if (
        evidence.stop_cause is WorkerStopCause.UNCLASSIFIED_PROTOCOL_FAILURE
        and not evidence.transport_evidence_available
    ):
        return _LEGACY_INSUFFICIENT
    return humanize_identifier(evidence.stop_cause.value)


def _worker_transport_lines(
    state: ProjectState,
    task_id: str | None,
    *,
    stop_cause: str | None,
    report_persisted: bool,
    retry_safe: bool,
    codex_version: str | None = None,
) -> tuple[str, ...]:
    """Render bounded Worker transport supervision facts for --verbose."""

    from .labels import humanize_identifier

    if task_id is None:
        return ()
    attempts = tuple(
        item for item in state.execution_attempts if item.task_id == task_id
    )
    lines: tuple[str, ...] = ("", "TRANSPORT", "")
    if not attempts:
        return lines + ("No Worker attempt was recorded for this Task.",)
    attempt = max(attempts, key=lambda item: item.attempt)
    diagnostics = attempt.transport
    if diagnostics is None or diagnostics.is_legacy_incomplete:
        lines += (
            "Codex binary      " + (
                "codex app-server"
            ),
            f"Codex version     {codex_version or 'unavailable'}",
            f"Attempt           {attempt.attempt}",
            _LEGACY_INSUFFICIENT,
        )
    else:
        exit_text = (
            "still running"
            if diagnostics.app_server_exit_code is None
            and diagnostics.app_server_exit_signal is None
            else (
                f"signal {diagnostics.app_server_exit_signal}"
                if diagnostics.app_server_exit_signal is not None
                else str(diagnostics.app_server_exit_code)
            )
        )
        last_event = diagnostics.last_protocol_event_type or "none"
        last_event_at = (
            ""
            if diagnostics.last_protocol_event_at is None
            else f" at {diagnostics.last_protocol_event_at.isoformat()}"
        )
        lines += (
            "Codex binary      "
            + (diagnostics.app_server_command or "codex app-server"),
            f"Codex version     {codex_version or 'unavailable (see doctor --verbose)'}",
            f"Process started   {'Yes' if diagnostics.app_server_pid else 'No'}"
            + (
                ""
                if diagnostics.app_server_pid is None
                else f" (pid {diagnostics.app_server_pid})"
            ),
            f"Process exit      {exit_text}",
            f"Stdout            {humanize_identifier(diagnostics.stdout_state.value)}",
            f"Stderr            {humanize_identifier(diagnostics.stderr_state.value)}",
            f"Stdin             {humanize_identifier(diagnostics.stdin_state.value)}",
            f"Last protocol event {last_event}{last_event_at}",
            f"Thread            {diagnostics.thread_id or attempt.thread_id or '-'}",
            f"Turn              {diagnostics.turn_id or attempt.turn_id or '-'}",
            f"Activity count    {diagnostics.activity_count}",
            "Terminal result   "
            + ("Received" if diagnostics.terminal_event_received else "Missing"),
            f"Report persisted  {'Yes' if report_persisted else 'No'}",
            "Failure class     "
            + humanize_identifier(
                diagnostics.failure_class.value
                if diagnostics.failure_class is not None
                else "none"
            ),
            "Failure cause     "
            + humanize_identifier(
                diagnostics.transport_failure_kind.value
                if diagnostics.transport_failure_kind is not None
                else (stop_cause or "none")
            ),
            "Cleanup           "
            + humanize_identifier(diagnostics.cleanup_reason or "not recorded"),
            f"Retry safe        {'Yes' if retry_safe else 'No'}",
        )
        if diagnostics.events:
            lines += ("", "Recent protocol lifecycle")
            lines += tuple(
                f"  {event.at.isoformat()} {event.event_type}"
                + (
                    ""
                    if event.payload_category is None
                    else f" [{event.payload_category}]"
                )
                for event in diagnostics.events
            )
    report_failure = _report_contract_failure(state, task_id)
    if report_failure is not None:
        lines += (
            "",
            "OUTCOME",
            "Worker turn       "
            + (
                "Completed"
                if report_failure.get("terminal_event_received") == "true"
                else "Not proven complete"
            ),
            "Transport         "
            + (
                "Healthy"
                if diagnostics is None
                or diagnostics.failure_class
                not in {
                    WorkerFailureClass.TRANSPORT_FAILURE,
                    WorkerFailureClass.CODEX_PROCESS_FAILURE,
                }
                else humanize_identifier(diagnostics.transport_failure_kind.value)
            ),
            "Report            Invalid",
            "Report stage      " + humanize_identifier(report_failure.get("report_stage", "unknown")),
            "Report code       " + humanize_identifier(report_failure.get("report_code", "unknown")),
        )
        field_path = report_failure.get("report_field_path")
        if field_path is not None:
            lines += (f"Report field      {field_path}",)
    return lines


def _report_contract_failure(
    state: ProjectState, task_id: str
) -> dict[str, str] | None:
    """Return bounded report-contract diagnostics for the latest failure."""

    matches = tuple(
        event
        for event in state.events
        if event.event_type == "task.execution_failed"
        and event.entity_id == task_id
        and "report_code" in event.metadata
    )
    if not matches:
        return None
    return max(matches, key=lambda event: event.timestamp).metadata


def _no_change_delivery_lines(evidence) -> tuple[str, ...]:
    reported = ", ".join(evidence.reported_paths) or "None"
    unstaged = ", ".join(evidence.actual_unstaged_paths) or "None"
    staged = ", ".join(evidence.actual_staged_paths) or "None"
    return (
        f"Task              {evidence.task_id}",
        f"Attempt           {evidence.attempt}",
        f"Expected baseline {evidence.baseline_head}",
        f"Reported changes  {reported}",
        f"Actual unstaged   {unstaged}",
        f"Actual staged     {staged}",
        f"Current HEAD      {evidence.current_head or 'Unavailable'}",
        f"Commit created    {'Yes' if evidence.commit_created else 'No'}",
        "Delivery          No repository changes",
        "Commit            Not required after Supervisor approval",
        "Supervisor review "
        + ("Completed" if evidence.supervisor_reviewed else "Not started"),
        "Continuation safe "
        + ("Yes" if evidence.continuation_safe else "No"),
        "Failure reason    Empty change set was rejected before review",
    )


def _render_manifest_handoff_failure(
    state: ProjectState, action: HumanAction
) -> tuple[str, ...]:
    """Name exactly where the delivery manifest handoff chain stopped."""

    events = tuple(
        event
        for event in state.events
        if event.event_type == "project.verification_failed"
        and event.entity_id == state.project.id
        and event.timestamp == action.created_at
        and "manifest_code" in event.metadata
    )
    if len(events) != 1:
        return ()
    metadata = events[0].metadata
    candidate_present = metadata.get("manifest_candidate_present") == "true"
    lines = (
        "",
        "DELIVERY MANIFEST HANDOFF",
        "Stage         " + humanize_identifier(metadata.get("manifest_stage", "unknown")),
        "Code          "
        + humanize_identifier(metadata.get("manifest_code", "manifest_handoff_failed")),
        "Candidate     " + metadata.get("manifest_candidate_path", "-"),
        "Candidate state "
        + ("Present and read" if candidate_present else "Not found at the repository root"),
    )
    if metadata.get("manifest_field_path"):
        lines += (f"Field         {metadata['manifest_field_path']}",)
    if metadata.get("manifest_revision"):
        lines += (f"Revision      {metadata['manifest_revision']}",)
    if metadata.get("manifest_summary"):
        lines += (f"Reason        {metadata['manifest_summary']}",)
    return lines


def _render_planning_failure(
    state: ProjectState, action: HumanAction
) -> tuple[str, ...]:
    """Render only allowlisted typed PLAN failure evidence."""

    from code_mule.human import (
        planning_failure_is_persisted,
        planning_retry_is_safe,
    )
    from code_mule.supervisor import (
        SupervisorFailureCategory,
        supervisor_failure_is_retryable,
    )

    boundary = state.latest_execution_stop
    events = tuple(
        event
        for event in state.events
        if event.event_type in {"planning.failed", "planning.proposal_rejected"}
        and event.entity_id == state.project.id
        and event.timestamp == action.created_at
    )
    if len(events) != 1 or not planning_failure_is_persisted(state, action):
        return ()
    metadata = events[0].metadata
    from code_mule.planning import PlanningValidationCode

    try:
        category = SupervisorFailureCategory(metadata["failure_category"])
    except (KeyError, ValueError):
        category = None
    attempts_text = "Unknown"
    attempt_count = metadata.get("attempt_count")
    if (
        attempt_count is not None
        and attempt_count.isascii()
        and attempt_count.isdigit()
    ):
        parsed_attempts = int(attempt_count)
        if 1 <= parsed_attempts <= 100:
            attempts_text = str(parsed_attempts)
    retryable = metadata.get("retryable")
    retryable = (
        "Yes"
        if retryable == "true"
        else "No"
        if retryable == "false"
        else "Unknown"
        if category is None
        else "Yes"
        if supervisor_failure_is_retryable(category)
        else "No"
    )
    failure = (
        "Unavailable"
        if category is None
        else humanize_identifier(category.value)
    )
    lines = (
        "",
        "SUPERVISOR PLANNING FAILURE",
        "Stage        Planning",
        "Boundary     " + humanize_identifier(metadata.get("stage", "planning")),
        f"Failure      {failure}",
        f"Attempts     {attempts_text}",
        f"Plan created {'Yes' if state.plans else 'No'}",
        f"Worker started {'Yes' if boundary.worker_started else 'No'}",
    )
    raw_code = metadata.get("validation_code")
    try:
        code = PlanningValidationCode(raw_code)
    except (TypeError, ValueError):
        code = None
    if code is not None:
        lines += (f"Validation   {humanize_identifier(code.value)}",)
    elif category is SupervisorFailureCategory.DETERMINISTIC_VALIDATION_FAILURE:
        lines += ("Validation   Unavailable in legacy evidence",)
    field_path = metadata.get("field_path")
    if (
        field_path is not None
        and len(field_path) <= 160
        and all(
            character.isalnum() or character in "_.[]"
            for character in field_path
        )
    ):
        lines += (f"Field        {field_path}",)
    elif category is SupervisorFailureCategory.DETERMINISTIC_VALIDATION_FAILURE:
        lines += ("Field        Unavailable in legacy evidence",)
    summary = metadata.get("safe_summary")
    if (
        summary is not None
        and 1 <= len(summary) <= 200
        and not any(
            marker in summary.casefold()
            for marker in ("api_key", "password", "credential", "token=")
        )
    ):
        lines += (f"Reason       {summary}",)
    return lines + (
        f"Retryable    {retryable}",
        "Fresh planning "
        + (
            "Safe after explicit resolution"
            if planning_retry_is_safe(state, action)
            else "Not proven safe"
        ),
    )


def _render_post_completion_replanning_failure(
    state: ProjectState, action: HumanAction
) -> tuple[str, ...]:
    """Render bounded, persisted post-completion failure and retry evidence."""

    from code_mule.replanning.recovery import (
        post_completion_replanning_failure_evidence,
        post_completion_replanning_retry_safety,
    )

    evidence = post_completion_replanning_failure_evidence(state, action)
    if evidence is None:
        return ()
    retry = post_completion_replanning_retry_safety(
        state, action, validate_workspace=True
    )
    lines = (
        "",
        "POST-COMPLETION REPLANNING FAILURE",
        "Stage        " + humanize_identifier(evidence.failure_stage),
        f"Base         Revision {evidence.base_revision} · "
        f"Plan v{evidence.base_plan.version}",
        f"Requested    Revision {evidence.requested_revision} · "
        f"Plan v{evidence.target_plan_version}",
        f"Plan created {'Yes' if evidence.plan_materialized else 'No'}",
        "Revision created "
        + ("Yes" if evidence.revision_materialized else "No"),
        f"Worker started {'Yes' if evidence.worker_started else 'No'}",
        "Failure      " + humanize_identifier(evidence.failure_category),
        "Validation   "
        + (
            "Unavailable in legacy evidence"
            if evidence.failure_code is None
            else humanize_identifier(evidence.failure_code)
        ),
    )
    if evidence.field_path is not None:
        lines += (f"Field         {evidence.field_path}",)
    return lines + (
        "Fresh replan "
        + (
            "Safe after explicit resolution"
            if retry.safe
            else "Not proven safe: " + retry.reason
        ),
    )


def _render_verification_failure(state: ProjectState, action: HumanAction, *, verbose: bool) -> tuple[str, ...]:
    from code_mule.domain.worker_verification import (
        UNMET_CHECK_LIMIT,
        WorkerCheckStatus,
        WorkerCheckType,
        safe_check_name,
    )

    events = tuple(event for event in state.events
                   if event.event_type in {"task.verification_blocked", "git.delivery_failed"}
                   and event.entity_id == action.task_id
                   and event.timestamp == action.created_at
                   and event.metadata.get("error_type") == "WorkerVerificationError"
                   and event.metadata.get("stage") == "verification")
    if len(events) != 1:
        return ()
    metadata = events[0].metadata
    lines = ("Failure     Worker verification evidence blocked delivery",)
    statuses = {item.value for item in WorkerCheckStatus}
    types = {item.value for item in WorkerCheckType}
    blocking = _blocking_check_rows(metadata, statuses, types)
    if blocking:
        if len(blocking) > 1:
            lines += (f"Blocked by   {len(blocking)} checks",)
        for index, row in enumerate(blocking, start=1):
            prefix = "Failed check" if len(blocking) == 1 else f"Blocked check {index}"
            lines += (
                f"{prefix} {safe_check_name(row['name'])}",
                f"Status       {row['status']}",
                f"Required     {'yes' if row['required'] == 'true' else 'no'}",
            )
            if verbose:
                lines += (f"Check type   {row['type']}",)
    if verbose:
        lines += ("Failure type WorkerVerificationError", "Failure stage verification")
    return lines


def _blocking_check_rows(
    metadata: dict[str, str], statuses: set[str], types: set[str]
) -> list[dict[str, str]]:
    """Read bounded blocking-check rows, tolerating the single-check form."""

    rows: list[dict[str, str]] = []
    try:
        count = int(metadata.get("unmet_check_count", "1"))
    except (TypeError, ValueError):
        count = 1
    from code_mule.domain.worker_verification import UNMET_CHECK_LIMIT as _LIMIT

    for index in range(1, min(count, _LIMIT) + 1):
        if index == 1 and "unmet_check_1_name" not in metadata:
            name = metadata.get("check_name")
            kind = metadata.get("check_type")
            status = metadata.get("check_status")
            required = metadata.get("check_required")
        else:
            name = metadata.get(f"unmet_check_{index}_name")
            kind = metadata.get(f"unmet_check_{index}_type")
            status = metadata.get(f"unmet_check_{index}_status")
            required = metadata.get(f"unmet_check_{index}_required")
        if (
            isinstance(name, str)
            and kind in types
            and status in statuses
            and required in {"true", "false"}
        ):
            rows.append(
                {"name": name, "type": kind, "status": status, "required": required}
            )
    return rows


def _render_worker_failure(state: ProjectState, action: HumanAction) -> tuple[str, ...]:
    from code_mule.worker.contracts import turn_failure_details_from_metadata

    # Match the action's exact failure boundary, never a different Task/attempt.
    events = tuple(
        event for event in state.events
        if event.event_type == "task.execution_failed"
        and event.entity_id == action.task_id
        and event.timestamp == action.created_at
    )
    if len(events) != 1:
        return ()
    event = events[0]
    details = turn_failure_details_from_metadata(event.metadata)
    if details is None:
        return ()
    age = details.last_activity_age_seconds
    return (
        "", "WORKER FAILURE",
        "Failure type   CodexTurnFailed",
        f"Failure kind   {details.kind.value}",
        f"Turn status    {details.turn_status or '-'}",
        f"Error code     {details.error_code or '-'}",
        f"Will retry     {'-' if details.will_retry is None else 'false'}",
        f"Activity count {details.activity_count}",
        f"Last activity  {'-' if age is None else f'{age:.1f}s before failure'}",
        f"Turn elapsed   {details.turn_elapsed_seconds:.1f}s",
        f"Thread ID      {details.thread_id}",
        f"Turn ID        {details.turn_id}",
        f"Failure time   {event.timestamp.isoformat()}",
    )


def render_project_cancellation_requested(
    state: ProjectState, *, verbose: bool = False
) -> tuple[str, ...]:
    lines = (
        "PROJECT CANCELLATION REQUESTED",
        "",
        "Current task will finish safely.",
        "No new tasks will be dispatched.",
        "Completed work will be preserved.",
    )
    if verbose:
        lines += (
            "",
            f"project_status: {state.project.status.value}",
            f"current_task_id: {state.project.current_task_id or '-'}",
        )
    return lines


def render_project_cancelled(
    state: ProjectState, *, verbose: bool = False
) -> tuple[str, ...]:
    lines = (
        "PROJECT CANCELLED",
        "",
        "Completed work was preserved.",
        "No rollback was performed.",
    )
    if verbose:
        lines += ("", f"project_status: {state.project.status.value}")
    return lines


__all__ = [
    "render_change_applied",
    "render_change_requested",
    "render_human_action",
    "render_project",
    "render_project_diagnosis",
    "render_project_cancelled",
    "render_project_cancellation_requested",
]
