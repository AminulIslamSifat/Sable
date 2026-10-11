"""DIL markup parser.

Parses the React-flavoured UI language the model writes into a plain node tree
that the frontend `SableComponents.render` draws.

Grammar handled:

    <tag attr="v" attr2={expr} flag>
    {expression}                 -> interpolated text / attribute value
    {@body const [x,setX] = DIL.useState(v)}   -> state declaration
    {#if cond} ... {:else if c} ... {:else} ... {/if}
    {#each ARRAY as item, i} ... {/each}

Everything is best-effort: unknown syntax degrades to text, never raises.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["parse"]


# ── tokenizer ──────────────────────────────────────────────────────────────

class _Tok:
    __slots__ = ("kind", "val")

    def __init__(self, kind: str, val: str) -> None:
        self.kind = kind
        self.val = val

    def __repr__(self) -> str:  # pragma: no cover
        return "Tok(%r,%r)" % (self.kind, self.val)


def _tokenize(src: str) -> list[_Tok]:
    out: list[_Tok] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "<":
            # Find the real tag end: skip `>` that appear inside {expr} or quotes
            # (e.g. `onClick={()=>setRunning(!running)}`).
            j, depth, q = i + 1, 0, None
            while j < n:
                ch = src[j]
                if q:
                    if ch == q:
                        q = None
                elif ch in "\"'":
                    q = ch
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    if depth > 0:
                        depth -= 1
                elif ch == ">" and depth == 0:
                    break
                j += 1
            if j >= n:
                out.append(_Tok("text", src[i:]))
                break
            out.append(_Tok("tag", src[i:j + 1]))
            i = j + 1
            continue
        if c == "{":
            depth, j = 0, i
            while j < n:
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                out.append(_Tok("text", src[i:]))
                break
            inner = src[i + 1:j].strip()
            if inner.startswith("#if"):
                out.append(_Tok("if", inner[3:].strip()))
            elif inner.startswith("#each"):
                out.append(_Tok("each", inner[5:].strip()))
            elif inner.startswith(":else if"):
                out.append(_Tok("elseif", inner[8:].strip()))
            elif inner == ":else":
                out.append(_Tok("else", ""))
            elif inner == "/if":
                out.append(_Tok("endif", ""))
            elif inner == "/each":
                out.append(_Tok("endeach", ""))
            elif inner.startswith("@"):
                out.append(_Tok("decl", inner))
            else:
                out.append(_Tok("expr", inner))
            i = j + 1
            continue
        j = i
        while j < n and src[j] not in "<{":
            j += 1
        out.append(_Tok("text", src[i:j]))
        i = j
    return out


# ── expression evaluator ───────────────────────────────────────────────────

_PRIM = {"true": True, "false": False, "null": None, "undefined": None}


def _split_top(s: str, sep: str) -> list[str]:
    """Split on `sep` at bracket/paren/brace/quote depth 0.

    `sep` may be multi-character (`===`, `!==`, `<=` ...). The old one-char
    compare could never match those: splitting `tab === "A"` on `=` produced
    four pieces, the comparison handler in `_eval` bailed on `len(parts) == 2`,
    and the expression fell through to `_literal` -- a truthy `("raw", ...)`
    tuple. Net effect: every `{#if}` branch rendered at once.
    """
    out, depth, buf, q = [], 0, [], None
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            i += 1
            continue
        if ch == '"' or ch == "'":
            q = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif depth == 0 and s.startswith(sep, i):
            out.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def _loads_loose(s: str) -> Any:
    """Parse JSON that may use single quotes and bare object keys."""
    candidates = [
        s,
        s.replace("'", '"'),
        re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', s),
        re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', s.replace("'", '"')),
    ]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return [] if s.lstrip()[:1] == "[" else {}


def _literal(s: str) -> Any:
    s = s.strip()
    if not s:
        return None
    if s in _PRIM:
        return _PRIM[s]
    if (s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'"):
        return s[1:-1]
    if re.fullmatch(r"-?[0-9]+", s):
        return int(s)
    if re.fullmatch(r"-?[0-9]+[.][0-9]+", s):
        return float(s)
    if s[0] in "[{" and s[-1] in "]}":
        return _loads_loose(s)
    return ("raw", s)


def _dig(obj: dict, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _lookup(path: str, state: dict, scope: dict) -> Any:
    parts = path.split(".")
    if parts[0] in scope:
        cur: Any = scope
    else:
        cur = state
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _eval(expr: str, state: dict, scope: dict) -> Any:
    """Best-effort evaluation of the tiny expression subset the model emits."""
    e = (expr or "").strip()
    if not e:
        return None

    # literal fast paths (so a bare `4` is never treated as a variable)
    if e in _PRIM:
        return _PRIM[e]
    if re.fullmatch(r"-?[0-9]+", e):
        return int(e)
    if re.fullmatch(r"-?[0-9]+[.][0-9]+", e):
        return float(e)
    if (e[0] == '"' and e[-1] == '"') or (e[0] == "'" and e[-1] == "'"):
        return e[1:-1]
    if e[0] in "[{" and e[-1] in "]}":
        return _literal(e)

    # Arrow functions / inline handlers (`()=>setRunning(!running)`) are not
    # evaluable — return the source verbatim so the client can bind them.
    # Must run BEFORE the equality checks, which would otherwise split on the
    # `=` in `=>` and return garbage (usually `False`).
    #
    # ANCHORED: only a *top-level* arrow literal bails. An arrow nested inside a
    # call — `missions.filter(m=>m.status==="Completed").length` — must fall
    # through to the array-method handling below, not be stringified.
    if re.match(r"^\s*(\([^)]*\)|[A-Za-z_]\w*)\s*=>", e):
        return e

    # ternary (top level)
    q = _split_top(e, "?")
    if len(q) == 2:
        if _eval(q[0], state, scope):
            branches = _split_top(q[1], ":")
            if len(branches) == 2:
                return _eval(branches[0], state, scope)
        else:
            branches = _split_top(q[1], ":")
            if len(branches) == 2:
                return _eval(branches[1], state, scope)

    # equality / comparison -- split on the FULL operator token, never op[0].
    # `_split_top` now understands multi-char separators, so `===` is matched
    # whole instead of shattering on the first `=`.
    for op in ("===", "!==", "==", "!=", "<=", ">=", "<", ">"):
        parts = _split_top(e, op)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            a = _eval(parts[0], state, scope)
            b = _eval(parts[1], state, scope)
            if op in ("===", "=="):
                return a == b
            if op in ("!==", "!="):
                return a != b
            if op == "<=":
                return a <= b
            if op == ">=":
                return a >= b
            if op == "<":
                return a < b
            return a > b

    if e.startswith("!"):
        return not _eval(e[1:], state, scope)

    # arithmetic (lowest precedence; +/- then */), left-to-right
    for op in ("+", "-", "*", "/"):
        parts = _split_top(e, op)
        if len(parts) > 1 and all(p.strip() for p in parts):
            vals = [_eval(p, state, scope) for p in parts]
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
                acc = vals[0]
                for v in vals[1:]:
                    if op == "+":
                        acc += v
                    elif op == "-":
                        acc -= v
                    elif op == "*":
                        acc *= v
                    else:
                        acc = acc / v if v else 0
                return acc

    # array methods FIRST — they end in `.length` too, and the generic handler
    # below would strip that suffix and recurse into a filter() call that no
    # longer matches anything.
    # The arrow param is captured and required to prefix the field, so both
    # `m => m.status === "Completed"` and `m => m.status` parse.
    m = re.fullmatch(
        r"(.+?)\.filter\(\s*(\w+)\s*=>\s*\w+\.([\w.]+)\s*===?\s*(.+?)\s*\)\.length",
        e,
    )
    if m:
        base = _eval(m.group(1), state, scope)
        if isinstance(base, list):
            field, want = m.group(3), _eval(m.group(4), state, scope)
            return len([it for it in base if isinstance(it, dict) and _dig(it, field) == want])
    m2 = re.fullmatch(
        r"(.+?)\.filter\(\s*(\w+)\s*=>\s*\w+\.([\w.]+)\s*\)\.length",
        e,
    )
    if m2:
        base = _eval(m2.group(1), state, scope)
        if isinstance(base, list):
            field = m2.group(3)
            return len([it for it in base if isinstance(it, dict) and _dig(it, field)])
    m3 = re.fullmatch(r"(.+?)\.map\(.+?\)\.length", e)
    if m3:
        base = _eval(m3.group(1), state, scope)
        if isinstance(base, list):
            return len(base)

    if e.endswith(".length"):
        v = _eval(e[:-7], state, scope)
        return len(v) if isinstance(v, (list, str, dict)) else 0

    if re.fullmatch(r"[A-Za-z_][\w.]*", e):
        return _lookup(e, state, scope)

    return _literal(e)


# ── attribute parsing ──────────────────────────────────────────────────────

def _parse_attrs(raw: str) -> dict[str, tuple[str, Any]]:
    attrs: dict[str, tuple[str, Any]] = {}
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i].isspace():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and (raw[j].isalnum() or raw[j] in "-_"):
            j += 1
        name = raw[i:j]
        if not name:
            break
        i = j
        while i < n and raw[i].isspace():
            i += 1
        if i < n and raw[i] == "=":
            i += 1
            while i < n and raw[i].isspace():
                i += 1
            if i < n and raw[i] == "{":
                depth, j = 0, i
                while j < n:
                    if raw[j] == "{":
                        depth += 1
                    elif raw[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                attrs[name] = ("expr", raw[i + 1:j])
                i = j + 1
            elif i < n and raw[i] in "\"'":
                q = raw[i]
                j = i + 1
                while j < n and raw[j] != q:
                    j += 1
                attrs[name] = ("str", raw[i + 1:j])
                i = j + 1
            else:
                j = i
                while j < n and not raw[j].isspace() and raw[j] != ">":
                    j += 1
                attrs[name] = ("str", raw[i:j])
                i = j
        else:
            attrs[name] = ("bool", True)
    return attrs


# ── tree builder ───────────────────────────────────────────────────────────

_TEXT_TAG_PROPS = {"text", "title", "badge", "tag"}

# Attributes that name a handler / state binding. When `_eval` can't resolve one
# (e.g. a bare `setTab`), the raw source is kept so the client can bind it.
_HANDLER_ATTR = re.compile(r"^(on[A-Z]\w*|action|send|bind|state|toggle|set)$")


def parse(src: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse DIL markup -> (nodes, state). Never raises."""
    try:
        toks = _tokenize(src or "")
    except Exception:
        return [], {}

    state: dict[str, Any] = {}
    pos = [0]

    # Hook declarations. Pure hooks (state/id/ref) are resolved eagerly so the
    # first paint is real; viewport- and theme-dependent ones are stashed under
    # `__hook__<name>` for the client runtime to fill in at mount.
    _HOOK_RE = re.compile(r"DIL[.]use(\w+)\s*[(](.*)[)]\s*$", re.S)

    for t in toks:
        if t.kind != "decl":
            continue
        h = _HOOK_RE.search(t.val)
        if not h:
            continue
        hook, args = h.group(1), h.group(2).strip()

        if hook == "State":
            nm = re.search(r"\[\s*(\w+)\s*,\s*(\w+)\s*\]", t.val)
            if nm:
                state[nm.group(1)] = _literal(args)
                state["__setter__" + nm.group(1)] = nm.group(2)
            continue

        nm = re.search(r"(?:const|let|var)\s+(\w+)\s*=", t.val)
        if not nm:
            continue
        name = nm.group(1)
        if hook == "Id":
            state[name] = "dil-0"
        elif hook == "Ref":
            state[name] = {"current": _literal(args) if args else None}
        elif hook == "Breakpoint":
            state[name] = "desktop"
        elif hook == "Theme":
            state[name] = {"dark": True, "mode": "dark"}
        else:  # Memo / Callback / Effect -> client resolves
            state[name] = None
        state["__hook__" + name] = {"hook": hook, "args": args}

    # Evaluate any `useMemo(() => EXPR, deps)` eagerly so the FIRST paint has a
    # real value, not null. The client re-runs it on state change.
    for t in toks:
        if t.kind != "decl":
            continue
        m = _HOOK_RE.search(t.val)
        if not m or m.group(1) != "Memo":
            continue
        nm = re.search(r"(?:const|let|var)\s+(\w+)\s*=", t.val)
        if not nm:
            continue
        body = re.match(r"\s*\(?[^)]*?\)?\s*=>\s*(.+?)(?:,\s*\[[^\]]*\])?\s*$", m.group(2), re.S)
        if body:
            state[nm.group(1)] = _eval(body.group(1).strip(), state, {})

    # Pre-seed state from a `<ui state='{...}'>` wrapper BEFORE walking the tree,
    # so `{#each items}` / `{mode}` inside the wrapper resolve on first paint.
    # (Previously this was lifted after parse_children ran, so the wrapper's
    # own children saw an empty store.)
    for t in toks:
        if t.kind == "tag" and t.val.lstrip("<").startswith("ui"):
            m = re.search(r"state\s*=\s*(['\"])(.*?)\1", t.val, re.S)
            if m:
                parsed = _literal(m.group(2))
                if isinstance(parsed, dict):
                    state.update(parsed)
            break

    def peek() -> _Tok | None:
        return toks[pos[0]] if pos[0] < len(toks) else None

    def advance() -> _Tok:
        t = toks[pos[0]]
        pos[0] += 1
        return t

    def coalesce(nodes):
        """Merge raw text runs; keep inter-run spacing so inline children
        (<bold>x</bold>) stay separated, and hoist text-only children."""
        out = []
        buf = []

        def flush(final):
            if not buf:
                return
            joined = re.sub(r"[ \t\r\n]+", " ", "".join(buf)).lstrip()
            if final:
                joined = joined.rstrip()
            if joined:
                out.append({"kind": "text", "text": joined})
            buf.clear()

        def _is_inline(nd):
            k = nd.get("kind")
            if k == "__text__":
                return True
            # a bare text fragment (only kind+text) belongs to the inline run
            if k == "text":
                return set(nd.keys()) <= {"kind", "text"}
            return False

        for nd in nodes:
            if _is_inline(nd):
                # A standalone `{expr}` (its own __text__ node carrying
                # `__expr__`) must NOT be merged into a plain-text run — that
                # would strip the source and freeze the value. Emit it on its
                # own so the client can re-evaluate it each render.
                if nd.get("__expr__") is not None:
                    flush(False)
                    out.append(nd)
                    continue
                buf.append(nd.get("text") or "")
                continue
            flush(False)
            kids = nd.get("children")
            if isinstance(kids, list) and len(kids) == 1 and isinstance(kids[0], dict) and kids[0].get("kind") in ("__text__", "text"):
                child = kids[0]
                joined = re.sub(r"[ \t\r\n]+", " ", child.get("text") or "").strip()
                if joined or child.get("__expr__") is not None:
                    nd = {k: v for k, v in nd.items() if k != "children"}
                    if joined:
                        nd.setdefault("text", joined)
                    # Carry the expression up too, otherwise the hoisted text
                    # freezes at its parse-time value (`__expr__` is what the
                    # client re-evaluates on state change).
                    if child.get("__expr__") is not None:
                        nd["__expr__text"] = child["__expr__"]
            out.append(nd)
        flush(True)
        return out

    def parse_children(scope: dict) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while pos[0] < len(toks):
            t = peek()
            if t is None:
                break
            if t.kind == "tag" and t.val.startswith("</"):
                break
            if t.kind in ("endif", "endeach", "elseif", "else"):
                break
            if t.kind == "decl":
                advance()
                continue
            if t.kind == "text":
                advance()
                if t.val.strip():
                    out.append({"kind": "__text__", "text": t.val})
                continue
            if t.kind == "expr":
                advance()
                v = _eval(t.val, state, scope)
                # ALWAYS keep the raw source alongside the resolved value. The
                # initial value is baked in for first paint, but the client
                # re-evaluates `__expr__` against live state on every render —
                # without it, `{running?"ONLINE":"PAUSED"}` would freeze at its
                # parse-time value.
                # `0` is a legitimate value — `v in (None, "", False)` would
                # wrongly blank it (`0 == False`). Check explicitly.
                blank = v is None or v == "" or v is False
                node: dict[str, Any] = {"kind": "__text__", "text": "" if blank else str(v)}
                node["__expr__"] = t.val
                out.append(node)
                continue
            if t.kind == "if":
                advance()
                out.extend(parse_if(t.val, scope))
                continue
            if t.kind == "each":
                advance()
                out.extend(parse_each(t.val, scope))
                continue
            if t.kind == "tag":
                nd = parse_tag(scope)
                if nd is not None:
                    out.append(nd)
                continue
            advance()
        return out

    def parse_tag(scope: dict) -> dict[str, Any] | None:
        raw = advance().val
        inner = raw[1:-1].strip()
        selfclose = inner.endswith("/")
        if selfclose:
            inner = inner[:-1].rstrip()
        if not inner:
            return None
        parts = inner.split(None, 1)
        tag = parts[0].lower()
        if tag.startswith("/"):
            return None
        attrs = _parse_attrs(parts[1]) if len(parts) > 1 else {}
        props: dict[str, Any] = {"kind": tag}
        for k, v in attrs.items():
            if v[0] == "expr":
                # Evaluate eagerly for the current scope AND stash the raw
                # expression. An enclosing {#each} re-evaluates it later against
                # the loop-item scope, which is what makes `{agent.icon}` work.
                val = _eval(v[1], state, scope)
                # Handlers (`onChange={setTab}`) and bare state setters aren't
                # resolvable values — keep the source string so the client can
                # map it to a state key instead of getting `None`.
                if val is None and _HANDLER_ATTR.match(k):
                    val = v[1].strip()
                props[k] = val
                props["__expr__" + k] = v[1]
            elif v[0] == "bool":
                props[k] = True
            else:
                props[k] = v[1]
        if selfclose:
            return props
        kids = coalesce(parse_children(scope))
        if pos[0] < len(toks) and peek().kind == "tag" and peek().val.startswith("</"):
            advance()
        if kids:
            props["children"] = kids
        return props

    def parse_if(cond: str, scope: dict) -> list[dict[str, Any]]:
        branches = [(cond, parse_children(scope))]
        while pos[0] < len(toks):
            t = peek()
            if t is None:
                break
            if t.kind == "elseif":
                advance()
                branches.append((t.val, parse_children(scope)))
            elif t.kind == "else":
                advance()
                branches.append(("true", parse_children(scope)))
            elif t.kind == "endif":
                advance()
                break
            else:
                break
        # Emit a RE-EVALUABLE conditional instead of collapsing to the winner.
        #
        # The old `return coalesce(body)` for the first truthy branch baked the
        # parse-time result into the tree and THREW THE OTHER BRANCHES AWAY.
        # The client never received them, so `{#if tab==="B"}` could never
        # appear no matter what the user clicked -- `tab` would flip but the
        # body stayed frozen on whatever branch won at parse time.
        #
        # Now every branch ships with its raw condition; `children` still holds
        # the initial winner so first paint is correct even before the client
        # resolves anything.
        branch_nodes = [
            {"cond": c, "nodes": coalesce(body)} for c, body in branches
        ]
        chosen: list[dict[str, Any]] = []
        for c, body in branches:
            if _eval(c, state, scope):
                chosen = coalesce(body)
                break
        return [{"kind": "__if__", "branches": branch_nodes, "children": chosen}]

    def parse_each(header: str, scope: dict) -> list[dict[str, Any]]:
        m = re.match(r"(.+?)\s+as\s+(\w+)\s*(?:,\s*(\w+))?$", header, re.DOTALL)
        if not m:
            return []
        arr_e, item_v, idx_v = m.group(1).strip(), m.group(2), m.group(3)
        arr = _eval(arr_e, state, scope)
        if not isinstance(arr, list):
            arr = []
        body = parse_children(scope)
        if pos[0] < len(toks) and peek().kind == "endeach":
            advance()
        out: list[dict[str, Any]] = []
        for i, item in enumerate(arr):
            sub = dict(scope)
            sub[item_v] = item
            if idx_v:
                sub[idx_v] = i
            for nd in body:
                out.append(_rerender(nd, sub))
        return coalesce(out)

    def _rerender(node: dict[str, Any], sub: dict) -> dict[str, Any]:
        if node.get("kind") == "__text__":
            raw = node.get("__expr__")
            if raw is not None:
                v = _eval(raw, state, sub)
                # `0` is a value, not blank — `v in (None, False)` would blank
                # it since `0 == False`. Check explicitly.
                blank = v is None or v == "" or v is False
                return {
                    "kind": "__text__",
                    "text": "" if blank else str(v),
                    "__expr__": raw,
                }
            return node
        if node.get("kind") == "__if__":
            # Inside an {#each} the branch condition may reference the loop var,
            # which only the server can resolve. Re-pick the winner here against
            # the sub-scope, and carry every branch through unchanged so the
            # client can still re-evaluate top-level state.
            branches = node.get("branches") or []
            chosen: list[dict[str, Any]] = []
            for br in branches:
                if _eval(br.get("cond", ""), state, sub):
                    chosen = coalesce([_rerender(c, sub) for c in br.get("nodes", [])])
                    break
            return {"kind": "__if__", "branches": branches, "children": chosen}
        props: dict[str, Any] = {}
        for k, v in node.items():
            if k == "children":
                continue
            if k.startswith("__expr__"):
                real = k[len("__expr__"):]
                props[real] = _eval(v, state, sub)
                # Keep the source too. The client re-evaluates it against live
                # state, so an expression that only touches top-level state
                # (e.g. `running?"success":"surface-tertiary"` on a dot inside
                # an {#each}) keeps reacting. If it references the loop var
                # (`agent.icon`) the client can't resolve it and silently keeps
                # this baked value — so no regression either way.
                props[k] = v
            else:
                props[k] = v
        kids = node.get("children")
        if kids:
            new_kids = coalesce([_rerender(c, sub) for c in kids])
            # If every child is plain text, hoist into the parent's `text` prop —
            # that is where text-bearing leaves (<text>, <title>, <badge>) look.
            # Only hoist PURE inline text into `text`. If a real <text>/<box>
            # element is present, keep children so the layout stacks them.
            if new_kids and all(
                isinstance(c, dict) and c.get("kind") == "__text__"
                for c in new_kids
            ):
                joined = " ".join((c.get("text") or "").strip() for c in new_kids).strip()
                if joined:
                    props.setdefault("text", joined)
                    return props
            props["children"] = new_kids
        return props

    nodes = coalesce(parse_children({}))

    # `<ui state='{...}'>` wrapper: lift the state object into the reactive
    # store so `{mode}` inside resolves and `<button set='{"mode":"x"}'>`
    # mutates the same key. Previously the attr was passed through as an
    # opaque string and the wrapper's state was invisible to the whole tree.
    if len(nodes) == 1 and isinstance(nodes[0], dict):
        wrapper = nodes[0]
        if wrapper.get("kind") in ("ui", "layout", "render"):
            raw_state = wrapper.get("state")
            if isinstance(raw_state, str):
                parsed = _literal(raw_state)
                if isinstance(parsed, dict):
                    raw_state = parsed
            if isinstance(raw_state, dict):
                state.update(raw_state)
                wrapper.pop("state", None)
            # If the wrapper's only job was holding state, unwrap it so the
            # client isn't handed a stray `ui` node it has no handler for.
            kids = wrapper.get("children")
            if kids and not raw_state:
                nodes = kids
            elif kids and wrapper.get("title") is None and wrapper.get("text") is None:
                nodes = kids

    return nodes, state
