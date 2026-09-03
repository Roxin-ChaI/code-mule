import unittest

from code_mule.supervisor import (
    SupervisorAttemptResult,
    SupervisorCallFailure,
    SupervisorFailureCategory,
    SupervisorOperation,
    SupervisorRetryPolicy,
    supervisor_failure_is_retryable,
)


class SupervisorReliabilityContractTests(unittest.TestCase):
    def test_default_policy_means_initial_call_plus_one_regeneration(self):
        policy = SupervisorRetryPolicy()
        self.assertEqual(policy.max_attempts, 2)
        self.assertEqual(policy.retry_delay_seconds, 0.0)
        with self.assertRaises(ValueError):
            SupervisorRetryPolicy(max_attempts=0)
        with self.assertRaises(ValueError):
            SupervisorRetryPolicy(retry_delay_seconds=-0.1)

    def test_retryability_is_defined_only_by_typed_category(self):
        retryable = {
            SupervisorFailureCategory.TRANSPORT_TIMEOUT,
            SupervisorFailureCategory.TEMPORARY_CONNECTION_FAILURE,
            SupervisorFailureCategory.INCOMPLETE_MAX_OUTPUT_TOKENS,
            SupervisorFailureCategory.MALFORMED_STRUCTURED_RESPONSE,
            SupervisorFailureCategory.SCHEMA_CONTRACT_VIOLATION,
            SupervisorFailureCategory.DECISION_CONTRACT_VIOLATION,
        }
        actual = {
            category
            for category in SupervisorFailureCategory
            if supervisor_failure_is_retryable(category)
        }
        self.assertEqual(actual, retryable)

    def test_attempt_result_invariants_reject_ambiguous_outcomes(self):
        success = SupervisorAttemptResult(
            SupervisorOperation.REVIEW,
            1,
            True,
            None,
            False,
        )
        self.assertTrue(success.succeeded)
        with self.assertRaises(ValueError):
            SupervisorAttemptResult(
                SupervisorOperation.REVIEW,
                0,
                False,
                SupervisorFailureCategory.CONTENT_FILTER,
                False,
            )
        with self.assertRaises(ValueError):
            SupervisorAttemptResult(
                SupervisorOperation.REVIEW, 1, False, None, False
            )
        with self.assertRaises(ValueError):
            SupervisorAttemptResult(
                SupervisorOperation.REVIEW,
                1,
                True,
                SupervisorFailureCategory.CONTENT_FILTER,
                False,
            )

    def test_call_failure_exposes_safe_typed_metadata_only(self):
        failure = SupervisorCallFailure(
            operation=SupervisorOperation.PLAN,
            failure_category=SupervisorFailureCategory.CONTENT_FILTER,
            attempt_count=1,
            retryable=False,
            exhausted=False,
        )
        self.assertEqual(failure.attempt_count, 1)
        self.assertFalse(failure.retryable)
        self.assertNotIn("raw", str(failure).lower())


if __name__ == "__main__":
    unittest.main()
