"""
Retry engine with exponential backoff and full jitter.

Why jitter? Without it, multiple jobs that fail at the same instant all
retry at the same intervals — they hit the Docker daemon in synchronized
waves (thundering herd). Full jitter spreads retries randomly across the
window, smoothing the load.

Reference: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/

Formula: sleep = random(0, min(cap, base * 2 ** attempt))
"""

import asyncio
import logging
import random
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Failure reasons that are worth retrying vs ones that aren't.
# OOM kills are NOT retried by default — if a job needs more memory,
# retrying with the same limits will just OOM again. The caller should
# increase ResourceLimits.memory_mb instead.
from app.core.models import FailureReason

RETRYABLE_REASONS = {
    FailureReason.EXIT_CODE,
    FailureReason.DOCKER_ERROR,
    FailureReason.TIMEOUT,
}

NON_RETRYABLE_REASONS = {
    FailureReason.OOM_KILLED,   # structural — same limits = same kill
    FailureReason.CANCELLED,    # explicit user intent
}


@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay_seconds: float = 1.0
    cap_seconds: float = 60.0


def is_retryable(reason: FailureReason) -> bool:
    return reason in RETRYABLE_REASONS


def compute_backoff(attempt: int, policy: RetryPolicy) -> float:
    """
    Full jitter backoff. Returns seconds to wait before the next attempt.

    attempt=0 means the first retry (after the initial failure).

    Example with base=1, cap=60:
      attempt 0: sleep in [0, 1]
      attempt 1: sleep in [0, 2]
      attempt 2: sleep in [0, 4]
      attempt 3: sleep in [0, 8]
      attempt 6: sleep in [0, 60]  (capped)
    """
    window = min(policy.cap_seconds, policy.base_delay_seconds * (2 ** attempt))
    delay = random.uniform(0, window)
    logger.debug(
        "Retry attempt %d: sleeping %.2fs (window=%.2fs)",
        attempt, delay, window,
    )
    return delay


async def wait_before_retry(attempt: int, policy: RetryPolicy) -> None:
    delay = compute_backoff(attempt, policy)
    await asyncio.sleep(delay)
