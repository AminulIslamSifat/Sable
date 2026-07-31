
from __future__ import annotations

import time


class CircuitBreaker:
    """Per-backend circuit breaker. States: closed → open → half-open → closed."""

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 60):
        self.failures = 0
        self.threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.last_failure: float = 0
        self.state = "closed"

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure > self.reset_timeout:
                self.state = "half-open"
                return True
            return False
        return True  # half-open: allow one probe

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure = time.time()
        if self.failures >= self.threshold:
            self.state = "open"


class LoopDetector:
    """Detects repeated identical tool calls. Returns False when stuck."""

    def __init__(self, max_consecutive: int = 3, max_total: int = 10):
        self.history: list[str] = []
        self.max_consecutive = max_consecutive
        self.max_total = max_total

    def check(self, tool_name: str, tool_args: str) -> bool:
        sig = f"{tool_name}:{tool_args}"
        self.history.append(sig)
        if len(self.history) > self.max_total:
            return False
        if len(self.history) >= self.max_consecutive:
            recent = self.history[-self.max_consecutive:]
            if len(set(recent)) == 1:
                return False
        return True
