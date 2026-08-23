"""JSON repair utilities for malformed LLM output.

Uses the `json-repair` package (v0.63+) as the primary repair engine,
with Sable's aggressive extraction cascade as a last-resort fallback.
"""
from __future__ import annotations

import json
import re

from json_repair import repair_json as _pkg_repair_json

# Pre-processing: NaN/Infinity → null BEFORE passing to package.
# The package converts these to quoted strings ("NaN"), but Sable's
# original behavior was to convert them to JSON null. We preserve that.
_NAN_RE = re.compile(r'\b-?NaN\b')
_INF_RE = re.compile(r'\b-?Infinity\b')


def repair_json(raw: str) -> str:
    """Repair malformed JSON string using json-repair package.

    String-to-string contract preserved for downstream parser compatibility.
    Handles: trailing commas, single quotes, unquoted keys, JS comments,
    control characters, NaN/Infinity, truncated strings/brackets, Python
    booleans/None, prose around JSON, and more.

    Returns repaired string — may still be invalid, caller must try/catch.
    """
    if not raw or not raw.strip():
        return raw.strip() if raw else ""

    # Pre-process NaN/Infinity → null to match original Sable behavior.
    # Package would otherwise convert these to quoted strings.
    s = _NAN_RE.sub('null', raw)
    s = _INF_RE.sub('null', s)

    # skip_json_loads=True: we always call json.loads() ourselves after repair,
    # so skip the package's internal validation pass to avoid double-parsing.
    result = _pkg_repair_json(s, return_objects=False, skip_json_loads=True)

    # Package returns empty string for completely unrepairable input.
    # Fall back to original so caller can attempt extraction cascade.
    if not result:
        return raw.strip()

    return result


def aggressive_repair_json(raw: str) -> str:
    """Aggressive repair with extraction cascade.

    1. Try direct repair via json-repair package.
    2. If that produces invalid JSON, extract outermost {} or [] substring
       and retry repair on the extracted portion.
    3. Return whatever we got — caller must still try/catch json.loads().
    """
    repaired = repair_json(raw)

    # Quick validity check
    try:
        json.loads(repaired)
        return repaired
    except (json.JSONDecodeError, TypeError):
        pass

    # Extraction fallback: find outermost JSON structure
    start_obj = repaired.find("{")
    start_arr = repaired.find("[")

    if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
        start = start_arr
        end_char = "]"
    elif start_obj >= 0:
        start = start_obj
        end_char = "}"
    else:
        return repaired

    end = repaired.rfind(end_char)
    if end <= start:
        return repaired

    extracted = repaired[start : end + 1]
    return repair_json(extracted)
