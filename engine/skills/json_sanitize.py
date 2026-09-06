"""Transport-level sanitization for LLM tool call JSON."""
from __future__ import annotations


def sanitize_transport(raw: str) -> str:
    if not raw:
        return raw
    s = raw
    s = _normalize_backslashes(s)
    s = _fix_mixed_quotes(s)
    s = _fix_inner_double_quotes(s)
    return s


def _normalize_backslashes(s: str) -> str:
    BS = chr(92)
    quad = BS * 4
    double = BS * 2
    s = s.replace(quad, double)
    valid = set(chr(34) + BS + chr(47) + 'bfnrtu')
    result = []
    i = 0
    while i < len(s):
        if s[i] == BS and i + 1 < len(s):
            nc = s[i + 1]
            if nc in valid:
                result.append(s[i])
                result.append(nc)
                i += 2
                continue
            elif nc == chr(39):
                result.append(chr(39))
                i += 2
                continue
            else:
                result.append(nc)
                i += 2
                continue
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


def _fix_mixed_quotes(s: str) -> str:
    SQ = chr(39)
    DQ = chr(34)
    BS = chr(92)
    if s.count(SQ) <= s.count(DQ):
        return s
    result = []
    in_str = False
    sc = None
    i = 0
    while i < len(s):
        c = s[i]
        if not in_str:
            if c == SQ:
                in_str, sc = True, SQ
                result.append(DQ)
            elif c == DQ:
                in_str, sc = True, DQ
                result.append(c)
            else:
                result.append(c)
        else:
            if c == BS and i + 1 < len(s):
                result.append(c)
                result.append(s[i + 1])
                i += 2
                continue
            elif c == sc:
                in_str = False
                result.append(DQ)
            elif c == DQ and sc == SQ:
                result.append(BS + DQ)
            else:
                result.append(c)
        i += 1
    return ''.join(result)


def _fix_inner_double_quotes(s: str) -> str:
    """Fix unescaped double quotes inside JSON string values.

    When LLM streaming corrupts apostrophes to bare double quotes inside
    JSON strings (e.g. {"q": "arafat"s laptop"}), this detects and escapes
    the inner quotes so json.loads can parse it.
    """
    DQ = chr(34)
    BS = chr(92)
    _AFTER_CLOSE = frozenset(',}]:')

    result = []
    in_string = False
    i = 0
    while i < len(s):
        c = s[i]
        if not in_string:
            result.append(c)
            if c == DQ:
                in_string = True
            i += 1
        else:
            if c == BS and i + 1 < len(s):
                result.append(c)
                result.append(s[i + 1])
                i += 2
                continue
            if c == DQ:
                j = i + 1
                while j < len(s) and s[j] in ' \t\n\r':
                    j += 1
                if j < len(s) and s[j] in _AFTER_CLOSE:
                    result.append(c)
                    in_string = False
                elif j >= len(s):
                    result.append(c)
                    in_string = False
                else:
                    result.append(BS)
                    result.append(c)
                i += 1
            else:
                result.append(c)
                i += 1
    return ''.join(result)
