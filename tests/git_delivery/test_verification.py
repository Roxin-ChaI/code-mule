import unittest
from dataclasses import replace

from code_mule.domain.worker_verification import (
    WorkerCheckStatus, WorkerCheckType, WorkerVerificationCheck,
)
from code_mule.git_delivery import GitOwnershipError, WorkerVerificationError
from .test_service import RepositoryCase, report


class WorkerVerificationTests(RepositoryCase):
    def test_status_requirement_matrix_for_exact_paths(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "owned.py").write_text("owned\n")
        for kind in WorkerCheckType:
            for status in WorkerCheckStatus:
                for required in (True, False):
                    with self.subTest(kind=kind, status=status, required=required):
                        check = WorkerVerificationCheck("visual", kind, status, required)
                        evidence = replace(
                            report("owned.py"),
                            tests=(f"visual: {status.value}",) if kind is WorkerCheckType.TEST else (),
                            static_checks=(f"visual: {status.value}",) if kind is WorkerCheckType.STATIC_CHECK else (),
                            verification_checks=(check,),
                        )
                        if status is WorkerCheckStatus.PASS or (not required and status is WorkerCheckStatus.NOT_RUN):
                            self.service.prepare_change_set(baseline, evidence, ("owned.py",))
                        else:
                            with self.assertRaises(WorkerVerificationError) as caught:
                                self.service.prepare_change_set(baseline, evidence, ("owned.py",))
                            self.assertEqual(caught.exception.check, check)
                            self.assertNotIsInstance(caught.exception, GitOwnershipError)

    def test_malformed_and_contradictory_evidence_fail_closed(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "owned.py").write_text("owned\n")
        for text in ("test: fail (misleading: pass)", "test: passed", "arbitrary"):
            with self.subTest(text=text), self.assertRaises(WorkerVerificationError):
                self.service.prepare_change_set(baseline, replace(report("owned.py"), tests=(text,)), ("owned.py",))
        check = WorkerVerificationCheck("test", WorkerCheckType.TEST, WorkerCheckStatus.PASS, True)
        with self.assertRaises(WorkerVerificationError):
            self.service.prepare_change_set(baseline, replace(report("owned.py"), tests=("test: fail",), verification_checks=(check,)), ("owned.py",))

    def test_ownership_still_blocks_even_with_optional_not_run(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "external.py").write_text("external\n")
        check = WorkerVerificationCheck("visual", WorkerCheckType.TEST, WorkerCheckStatus.NOT_RUN, False)
        with self.assertRaises(GitOwnershipError):
            self.service.prepare_change_set(baseline, replace(report("owned.py"), tests=("visual: not_run",), static_checks=(), verification_checks=(check,)), ("owned.py",))


if __name__ == "__main__":
    unittest.main()
