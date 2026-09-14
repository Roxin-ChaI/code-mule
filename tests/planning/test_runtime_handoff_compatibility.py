"""Local contract E2E from initial PLAN through verified runtime handoff."""

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from code_mule.domain import PlanStatus, ProjectStatus, TaskStatus
from code_mule.git_delivery import GitCommitResult
from code_mule.planning import ProjectPlanningRequest, ProjectPlanningService
from code_mule.project_verification import FinalReviewDecision
from code_mule.project_verification.service import (
    ProjectFinalizationService,
    ProjectVerificationService,
)
from code_mule.runtime_handoff import LaunchDisposition
from code_mule.runtime_handoff.service import ProcessObservation, RuntimeHandoffService
from code_mule.state.store import JsonProjectStateStore
from code_mule.supervisor import (
    MilestoneProposal,
    FinalReviewResult,
    PlanProposal,
    RequirementProposal,
    TaskProposal,
)

from planning.test_validation import empty_state


NOW = datetime(2026, 9, 14, tzinfo=UTC)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class _Planner:
    def plan(self, _request):
        return PlanProposal(
            "Verify the existing standard-library service and hand it off.",
            (
                RequirementProposal(
                    "REQ-SERVICE",
                    "Verified local service",
                    "Preserve and verify the existing HTTP service.",
                    "high",
                    ("The local service and health endpoint are verified",),
                ),
            ),
            (),
            (MilestoneProposal("M-SERVICE", "Runtime handoff", ("T-VERIFY",)),),
            (
                TaskProposal(
                    "T-VERIFY",
                    "Verify existing runtime",
                    (
                        "Add deterministic tests and delivery metadata without "
                        "reimplementing server.py."
                    ),
                    (),
                    ("Tests and revision-scoped delivery metadata are complete",),
                    ("REQ-SERVICE",),
                ),
            ),
            (),
            "The existing implementation needs only minimal verification and handoff work.",
        )


class _FinalSupervisor:
    def final_review(self, _request):
        return FinalReviewResult(
            FinalReviewDecision.APPROVE,
            "Deterministic completion evidence is sufficient.",
            (),
        )


class RuntimeHandoffPlanningCompatibilityTests(unittest.TestCase):
    def test_existing_service_plan_materializes_before_verified_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-q")
            git(root, "config", "user.name", "Code Mule Test")
            git(root, "config", "user.email", "test@example.invalid")
            (root / ".git/info/exclude").write_text(".code-mule/\n", encoding="utf-8")
            (root / "server.py").write_text("print('service fixture')\n", encoding="utf-8")
            git(root, "add", "--", "server.py")
            git(root, "commit", "-q", "-m", "existing service")
            baseline = git(root, "rev-parse", "HEAD")

            source = empty_state()
            source = replace(
                source,
                project=replace(source.project, workspace=str(root)),
                delivery_manifest_required=True,
            )
            (root / ".code-mule").mkdir()
            store = JsonProjectStateStore(root / ".code-mule/project-state.json")
            store.save(source)
            ids = iter(f"event-{index}" for index in range(30))
            planning = ProjectPlanningService(
                store=store,
                supervisor=_Planner(),
                clock=lambda: NOW,
                plan_id_factory=lambda: "PLAN-1",
                event_id_factory=lambda: next(ids),
            )

            outcome = planning.plan(
                ProjectPlanningRequest("project-1", "Preserve and verify server.py")
            )
            planned = store.load()
            self.assertEqual((outcome.plan_version, len(planned.revisions)), (1, 1))
            self.assertEqual(planned.delivery_manifests, ())
            self.assertIs(planned.tasks[0].status, TaskStatus.PENDING)

            (root / "test_server.py").write_text(
                "def test_fixture(): assert True\n", encoding="utf-8"
            )
            manifest = {
                "deliverable_type": "service",
                "runnable": True,
                "entry_point": "server.py",
                "launch_spec": {
                    "command": {"executable": "python3", "args": ["server.py"]},
                    "working_directory": ".",
                    "environment_keys": [],
                    "startup_timeout_seconds": 3,
                    "expected_long_running": True,
                    "requires_args": False,
                    "supports_dynamic_port": False,
                },
                "verification_spec": {
                    "required_paths": ["server.py", "test_server.py"],
                    "launch_smoke_test_supported": True,
                },
                "health_check_spec": {
                    "type": "http",
                    "url": "http://127.0.0.1:8765/health",
                    "command": None,
                    "expected_status": 200,
                    "timeout_seconds": 1,
                },
                "access_spec": {"host": "127.0.0.1", "port": 8765, "path": "/"},
                "stop_spec": {"grace_seconds": 2},
                "required_environment": [],
                "runtime_generated_paths": [],
                "usage": "Launch the verified local service.",
            }
            (root / "code-mule-delivery.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            git(root, "add", "--", "test_server.py", "code-mule-delivery.json")
            git(root, "commit", "-q", "-m", "verify runtime handoff")
            delivered_head = git(root, "rev-parse", "HEAD")
            task = replace(planned.tasks[0], status=TaskStatus.COMPLETED)
            delivery = GitCommitResult(
                task.id,
                str(root),
                baseline,
                delivered_head,
                "test(task): verify runtime handoff",
                ("code-mule-delivery.json", "test_server.py"),
                ("code-mule-delivery.json", "test_server.py"),
                NOW,
            )
            executable = replace(
                planned,
                tasks=(task,),
                git_commit_results=(delivery,),
                plans=(replace(planned.plans[0], status=PlanStatus.ACTIVE),),
            )
            store.save(executable)
            finalizer = ProjectFinalizationService(
                store=store,
                verification=ProjectVerificationService(
                    clock=lambda: NOW,
                    result_id_factory=lambda: "verification-1",
                    runner=lambda command, **kwargs: (0, False),
                    environment={"PATH": "/usr/bin:/bin"},
                ),
                supervisor=_FinalSupervisor(),
                clock=lambda: NOW,
                event_id_factory=lambda: next(ids),
            )
            final = finalizer.finalize(executable)
            self.assertIs(final.project.status, ProjectStatus.DONE)
            self.assertEqual(len(final.delivery_manifests), 1)
            self.assertEqual(final.delivery_manifests[0].id, "manifest-r1-1")

            alive = {41000: True}

            class _Process:
                pid = 41000

            handoff = RuntimeHandoffService(
                store=store,
                clock=lambda: NOW,
                session_id_factory=lambda: "runtime-1",
                event_id_factory=lambda: next(ids),
                environment={"PATH": "/usr/bin:/bin"},
                popen=lambda *args, **kwargs: _Process(),
                process_observer=lambda pid: ProcessObservation(
                    alive[pid], "a" * 64, "b" * 64
                ),
                http_status=lambda url, timeout: 200,
                process_signaler=lambda pid, signal_number: alive.__setitem__(pid, False),
                port_available=lambda port: True,
                sleeper=lambda seconds: None,
            )
            self.assertIs(handoff.launch().disposition, LaunchDisposition.STARTED)
            self.assertEqual(handoff.app_status().status.value, "running")
            self.assertEqual(handoff.stop_app().status.value, "stopped")
            self.assertEqual(git(root, "status", "--short"), "")


if __name__ == "__main__":
    unittest.main()
