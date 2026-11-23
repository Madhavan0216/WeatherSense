"""Resilience primitives: retry with jitter and a lightweight circuit breaker."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(RuntimeError):
    """Raised when calls are blocked because the circuit is open."""


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        self.state: CircuitState = CircuitState.CLOSED
        self.failures: int = 0
        self.last_failure_ts: float = 0.0

    def _transition_if_needed(self) -> None:
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.last_failure_ts
            if elapsed >= self.reset_timeout_seconds:
                self.state = CircuitState.HALF_OPEN

    def call(self, fn: Callable[[], T]) -> T:
        self._transition_if_needed()

        if self.state == CircuitState.OPEN:
            raise CircuitBreakerOpenError("circuit is open")

        try:
            result = fn()
        except Exception:
            self.failures += 1
            self.last_failure_ts = time.monotonic()
            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
            raise

        self.failures = 0
        self.state = CircuitState.CLOSED
        return result


def retry_with_backoff(
    fn: Callable[[], T],
    max_attempts: int = 3,
    base_delay_seconds: float = 0.1,
    max_delay_seconds: float = 2.0,
) -> T:
    """Retry callable with exponential backoff and jitter."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception:
            if attempt >= max_attempts:
                raise
            sleep_for = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            jitter = random.uniform(0.0, sleep_for * 0.25)
            time.sleep(sleep_for + jitter)
