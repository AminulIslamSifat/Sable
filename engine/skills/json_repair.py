"""JSON repair utilities for malformed LLM output."""
from __future__ import annotations
import re


def repair_json(raw: str) -> str:
    """Best-effort repair of common LLM JSON malformations.

    Handles: trailing commas, single quotes, unquoted keys, JS comments,
    control characters in strings, NaN/Infinity literals.
    Returns repaired string — may still be invalid, caller must try/catch.
    """
    s = raw.strip()
    if not s:
        return s

    # 1. Strip JS block comments /* ... */
    pat_block = re.compile('/' + '[*].*?[*]/', re.DOTALL)
    s = pat_block.sub('', s)

    # 2. Strip JS line comments // ...
    s = re.sub(r'(?<!:)//[^\n]*', '', s)

    # 3. Single quotes -> double quotes when singles dominate
    sq, dq, bs = chr(39), chr(34), chr(92)
    if s.count(sq) > s.count(dq):
        out: list[str] = []
        in_s = False
        sc = None
        i = 0
        while i < len(s):
            c = s[i]
            if not in_s:
                if c == sq:
                    in_s, sc = True, sq
                    out.append(dq)
                elif c == dq:
                    in_s, sc = True, dq
                    out.append(c)
                else:
                    out.append(c)
            else:
                if c == bs and i + 1 < len(s):
                    out.append(c)
                    out.append(s[i + 1])
                    i += 2
                    continue
                elif c == sc:
                    in_s = False
                    out.append(dq if sc == sq else c)
                elif c == dq and sc == sq:
                    out.append(bs + dq)
                else:
                    out.append(c)
            i += 1
        s = ''.join(out)

    # 4. Trailing commas before } or ]
    s = re.sub(r',\s*([}\]])', r'\1', s)

    # 5. Unquoted keys: {key: val} -> {"key": val}
    s = re.sub(r'(?<=[{,])\s*([a-zA-Z_]\w*)\s*:', r' "\1":', s)

    # 6. NaN / Infinity -> null
    s = re.sub(r'\b-?NaN\b', 'null', s)
    s = re.sub(r'\b-?Infinity\b', 'null', s)

    # 7. Raw control chars inside double-quoted strings
    def _fix_ctrl(m):
        v = m.group(1)
        v = v.replace(chr(10), bs + 'n')
        v = v.replace(chr(13), bs + 'r')
        v = v.replace(chr(9), bs + 't')
        return dq + v + dq
    s = re.sub(r'"((?:[^"\\]|\\.)*)"', _fix_ctrl, s)

    return s