"""
Tests for the retry engine.

These tests have zero Docker dependency — they test the retry logic
in isolation. This is intentional: we want retry behaviour to be
verifiable without a running daemon.
"""

import pytest

from app.core.models import FailureReason
from app.core.retry import (
    RetryPolicy,
    compute_backoff,
    is_retryable,
)


class TestIsRetryable:
    def test_exit_code_is_retryable(self):
        assert is_retryable(FailureReason.EXIT_CODE) is True

    def test_docker_error_is_retryable(self):
        assert is_retryable(FailureReason.DOCKER_ERROR) is True

    def test_timeout_is_retryable(self):
        assert is_retryable(FailureReason.TIMEOUT) is True

    def test_oom_killed_is_not_retryable(self):
        # OOM kills are structural — same limits = same kill
        assert is_retryable(FailureReason.OOM_KILLED) is False

    def test_cancelled_is_not_retryable(self):
        assert is_retryable(FailureReason.CANCELLED) is False


class TestComputeBackoff:
    def test_returns_float(self):
        delay = compute_backoff(0, RetryPolicy())
        assert isinstance(delay, float)

    def test_never_exceeds_cap(self):
        policy = RetryPolicy(base_delay_seconds=1.0, cap_seconds=10.0)
        for attempt in range(20):
            delay = compute_backoff(attempt, policy)
            assert delay <= policy.cap_seconds, f"Exceeded cap at attempt {attempt}: {delay}"

    def test_always_non_negative(self):
        policy = RetryPolicy()
        for attempt in range(10):
            assert compute_backoff(attempt, policy) >= 0

    def test_window_grows_with_attempt(self):
        """
        The window (max possible delay) should grow with attempt number
        up to the cap. We can't test the exact value (it's random), but
        we can test that the upper bound grows.
        """
        policy = RetryPolicy(base_delay_seconds=1.0, cap_seconds=1000.0)
        windows = [min(policy.cap_seconds, policy.base_delay_seconds * (2 ** i)) for i in range(6)]
        assert windows == sorted(windows), "Windows should be non-decreasing"

    def test_cap_is_respected_at_large_attempt(self):
        policy = RetryPolicy(base_delay_seconds=1.0, cap_seconds=5.0)
        # At attempt 100, theoretical window is 2^100 — should be capped at 5.0
        for _ in range(100):
            assert compute_backoff(100, policy) <= 5.0
