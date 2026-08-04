
from __future__ import annotations

import time
from collections import defaultdict


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
                # Transition to half-open: THIS call is the single probe
                self.state = "half-open"
                return True
            return False
        # half-open: probe already in flight, block additional calls
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure = time.time()
        if self.state == "half-open":
            # Probe failed — go back to open
            self.state = "open"
        elif self.failures >= self.threshold:
            self.state = "open"


class LoopDetector:
    """Detects repeated identical tool calls. Returns False when stuck.

    Tracks per-tool totals (not global) to avoid false positives when
    multiple different tools are used legitimately.
    """

    def __init__(self, max_consecutive: int = 3, max_total: int = 10):
        self.history: list[str] = []
        self.per_tool_counts: dict[str, int] = defaultdict(int)
        self.max_consecutive = max_consecutive
        self.max_total = max_total

    def check(self, tool_name: str, tool_args: str) -> bool:
        sig = f"{tool_name}:{tool_args}"
        self.history.append(sig)
        self.per_tool_counts[tool_name] += 1

        # Per-tool cap: one tool called too many times total
        if self.per_tool_counts[tool_name] > self.max_total:
            return False

        # Consecutive identical calls (any tool)
        if len(self.history) >= self.max_consecutive:
            recent = self.history[-self.max_consecutive:]
            if len(set(recent)) == 1:
                return False
        return True
