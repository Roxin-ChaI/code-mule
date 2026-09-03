from contextlib import redirect_stdout
from io import StringIO
import json
import unittest

from scripts import local_supervisor_reliability_e2e


class SupervisorReliabilityLocalE2E(unittest.TestCase):
    def test_invalid_review_exhausted_plan_and_domain_rejection(self):
        output = StringIO()
        with redirect_stdout(output):
            code = local_supervisor_reliability_e2e.main()
        payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["provider"], "deterministic fake")
        self.assertEqual(payload["scenario_a"]["project_status"], "done")
        self.assertEqual(payload["scenario_a"]["review_calls"], 2)
        self.assertEqual(payload["scenario_a"]["worker_sessions"], 1)
        self.assertEqual(payload["scenario_a"]["worker_executions"], 1)
        self.assertEqual(
            payload["scenario_b"]["project_status"], "human_required"
        )
        self.assertEqual(payload["scenario_b"]["plan_calls"], 2)
        self.assertEqual(payload["scenario_b"]["attempt_count"], "2")
        self.assertEqual(
            payload["scenario_c"]["project_status"], "human_required"
        )
        self.assertEqual(payload["scenario_c"]["plan_calls"], 1)
        self.assertEqual(payload["scenario_c"]["retry_events"], 0)


if __name__ == "__main__":
    unittest.main()
