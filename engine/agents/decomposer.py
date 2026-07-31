
"""Heuristic pre-filter: hints whether a message might benefit from parallel agents.

Advisory only — Maria (the LLM) makes the final spawn decision.
"""
from __future__ import annotations

import re

_PARALLEL_PATTERNS = re.compile(
    r"(research .+ and .+|compare .+, .+, and .+|"
    r"do .+ while .+|simultaneously|in parallel|"
    r"first .+ then .+|step 1.+step 2|"
    r"find .+ and also .+|look up .+ and .+)",
    re.IGNORECASE,
)


def needs_decomposition(message: str) -> bool:
    """Quick heuristic check (<1ms). Returns True if message likely benefits from agents."""
    if _PARALLEL_PATTERNS.search(message):
        return True
    # Multiple bullet points suggesting parallel tasks
    if message.count("\n-") >= 3 or message.count("\n*") >= 3:
        return True
    return False
