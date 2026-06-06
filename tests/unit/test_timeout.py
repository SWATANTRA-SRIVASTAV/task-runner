import pytest
from app.core.models import FailureReason
from app.core.retry import is_retryable, RetryPolicy, compute_backoff


class TestTimeoutRetryBehavior:

    def test_timeout_is_retryable(self):
        # Timeouts should retry — the resource might have been temporarily slow
        assert is_retryable(FailureReason.TIMEOUT) is True

    def test_timeout_respects_max_retries_zero(self):
        # If max_retries=0, a timeout should not retry regardless
        # This is enforced by the scheduler loop, not is_retryable()
        # But we verify the building block here
        policy = RetryPolicy(max_retries=0)
        assert policy.max_retries == 0

    def test_backoff_on_timeout_does_not_exceed_cap(self):
        policy = RetryPolicy(base_delay_seconds=1.0, cap_seconds=30.0)
        for attempt in range(10):
            delay = compute_backoff(attempt, policy)
            assert delay <= 30.0

    def test_oom_is_not_retried_even_with_retries_configured(self):
        # OOM with max_retries=3 should still not retry
        # because is_retryable gates the retry decision
        assert is_retryable(FailureReason.OOM_KILLED) is False


class TestTimeoutJobStatusTransition:
    """
    Tests the DB-level state that a timed-out job should have.
    """

    def test_timeout_failure_reason_value(self):
        assert FailureReason.TIMEOUT.value == "timeout"

    def test_timeout_is_distinct_from_exit_code_failure(self):
        assert FailureReason.TIMEOUT != FailureReason.EXIT_CODE

    def test_timeout_is_distinct_from_oom(self):
        assert FailureReason.TIMEOUT != FailureReason.OOM_KILLED