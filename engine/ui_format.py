"""Rich UI block format — visual blocks rendered inline in chat.

This is a RESPONSE FORMAT, not a tool/skill. The model writes markup
between <render_component> tags; the stream parser detects it, this module
parses it to a node tree, and the frontend renders it natively. There is no
skill lifecycle, no middleware, no tool feedback — the turn never pauses.

Two input modes:

  1. MARKUP (preferred) — a JSX-like body the model writes naturally. Parsed
     server-side into the node tree the frontend renders:

         <ui state='{"mode":"Balanced"}'>
           <card title="Runtime">
             <row gap="12">
               <stat value="42" label="Score"/>
               <stat value="96.8%" label="Success"/>
             </row>
           </card>
         </ui>

  2. JSON (legacy) — components=[...] attribute, kept for back-compat.

Media is resolved HERE so the frontend only ever sees concrete sources:

    <img query="cute cats" count="4"/>    -> SearXNG image search
    <img src="https://..."/>              -> direct URL
    <img file="~/pics/a.png"/>            -> local file, inlined as data URI
    <video src="https://..."/>            -> direct video URL
    <link href="https://..." label="X"/>  -> styled link

Presentation only. The frontend renders the components inline; the model gets
no feedback and the turn does not pause.
"""

from __future__ import annotations

import ast
import base64
import json
import mimetypes
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ALLOWED_TYPES = frozenset({
    "image", "card", "table", "callout", "chart", "flow", "keyvalue",
    "layout",
})

MAX_COMPONENTS = 8
MAX_IMAGES_PER_COMPONENT = 8
_MAX_NODE_DEPTH = 20
_MAX_INLINE_FILE_BYTES = 8_000_000  # 8 MB cap on inlined local files

_IMAGE_NODE_KINDS = frozenset({"image", "media", "hero", "avatar"})


# ── markup parsing ──────────────────────────────────────────────────────────

# Tags treated as leaves even when written without a self-closing slash, so a
# missing "/" cannot swallow the rest of the document as children.
_LEAF_TAGS = frozenset({
    "img", "image", "pic", "video", "audio", "icon", "divider", "hr",
    "separator", "spacer", "progress", "bar", "meter", "badge", "tag",
    "stat", "metric", "kpi", "chart", "code", "pre", "quote", "avatar",
    "input", "link", "file", "attachment",
})

# Attributes whose value is a JSON literal (object / array / number).
_JSON_ATTRS = frozenset({
    "state", "options", "items", "pairs", "series", "columns", "rows",
    "data", "actions", "tabs", "badges", "steps", "labels", "values",
    "over", "set", "action", "explanation", "reveal", "template",
    "node", "nodes", "left", "right", "pairs",
})

# Attributes that are plain booleans.
_BOOL_ATTRS = frozenset({
    "wrap", "open", "correct", "ordered", "truthy", "disabled",
    "stretch", "bordered",
})

# Tag name -> node kind. Only names that differ from the kind need an entry.
_KIND_MAP = {
    "img": "image",
    "pic": "image",
    "column": "col",
    "columns": "col",
    "vstack": "col",
    "hstack": "row",
    "card": "box",
    "panel": "box",
    "section": "box",
    "hr": "divider",
    "separator": "divider",
    "h1": "title",
    "h2": "title",
    "h3": "title",
    "h4": "title",
    "subtitle": "text",
    "label": "text",
    "paragraph": "text",
    "p": "text",
    "span": "text",
    "checkbox": "check",
    "switch": "toggle",
    "collapse": "toggle",
    "accordion": "toggle",
    "select": "choices",
    "radio": "choices",
    "options": "choices",
    "bar": "progress",
    "meter": "progress",
    "metric": "stat",
    "kpi": "stat",
    "blockquote": "quote",
    "pre": "code",
    "codeblock": "code",
    "a": "link",
    "attachment": "file",
    "btn": "button",
    "tag": "badge",
    "grid-item": "griditem",
    "griditem": "griditem",
    "b": "text",
    "strong": "text",
    "i": "text",
    "em": "text",
    "small": "text",
    "mark": "text",
}

# Inline tags carry a default style the bare prop would otherwise lose.
_INLINE_STYLE = {
    "b": {"weight": "bold"},
    "strong": {"weight": "bold"},
    "i": {"tone": "muted"},
    "em": {"tone": "muted"},
    "small": {"size": "sm"},
    "mark": {"tone": "accent"},
}


def _strip_fences(s: str) -> str:
    """Drop a wrapping markdown code fence if the model added one."""
    s = (s or "").strip()
    if s.startswith("```"):
        lines = s.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    return s.strip()


# The model writes React-flavoured markup (`{4}`, `{x?A:B}`, `{@body ...}`,
# `onClick={() => ...}`) because that is what the system prompt shows it. HTMLParser
# cannot read any of that, so we normalise it into plain HTML *before* parsing.
_JS_EXPR_RE = re.compile(r"[{]([^{}]*)[}]")


class _ExprScanner:
    """Brace-depth aware scanner for `{...}` / `{{...}}` in markup."""

    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)

    def _consume_braced(self):
        """At self.i pointing at `{`, consume a balanced {..} or {{..}}."""
        if self.i >= self.n or self.s[self.i] != "{":
            return None
        double = self.s[self.i:self.i + 2] == "{{"
        open_tok = "{{" if double else "{"
        close_tok = "}}" if double else "}"
        depth = 0
        j = self.i
        while j < self.n:
            if self.s[j:j + len(open_tok)] == open_tok:
                depth += 1
                j += len(open_tok)
                continue
            if self.s[j:j + len(close_tok)] == close_tok:
                depth -= 1
                j += len(close_tok)
                if depth == 0:
                    inner = self.s[self.i + len(open_tok):j - len(close_tok)]
                    self.i = j
                    return ("json" if double else "expr") + ":" + inner
            j += 1
        return None


def _simplify_expr(expr):
    """Collapse a JS-ish `{...}` fragment into a plain string."""
    e = (expr or "").strip()
    if not e:
        return ""
    if re.fullmatch(r"-?[0-9]+(?:[.][0-9]+)?", e):
        return e
    if "?" in e and ":" in e:
        after = e.split("?", 1)[1]
        parts = after.split(":", 1)
        a = parts[0].strip().strip('"').strip("'").strip("`")
        return a or "—"
    if (e[0:1] == '"' and e[-1:] == '"') or (e[0:1] == "'" and e[-1:] == "'"):
        return e[1:-1]
    return "—"


def _strip_declarations(s):
    out = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "{" and re.match(r"[@](?:body|state|let|const)", s[i + 1:]):
            sc = _ExprScanner(s)
            sc.i = i
            if sc._consume_braced() is not None:
                i = sc.i
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _strip_handlers(s):
    out = []
    i = 0
    n = len(s)
    while i < n:
        m = re.match(r"\s+on[A-Z][A-Za-z0-9]*=", s[i:])
        if m:
            j = i + m.end()
            if j < n and s[j] == "{":
                sc = _ExprScanner(s)
                sc.i = j
                if sc._consume_braced() is not None:
                    i = sc.i
                    continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _rewrite_braces(s):
    out = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "{":
            sc = _ExprScanner(s)
            sc.i = i
            res = sc._consume_braced()
            if res is not None:
                kind, _, inner = res.partition(":")
                prefix = "".join(out)
                am = re.search(r"(\w[\w-]*)=$", prefix)
                if am:
                    attr_name = am.group(1)
                    val = ("{" + inner + "}") if kind == "json" else _simplify_expr(inner)
                    out = [prefix[:am.start()]]
                    out.append(attr_name + '="' + val.replace('"', "&quot;") + '"')
                else:
                    out.append(("{" + inner + "}") if kind == "json" else _simplify_expr(inner))
                i = sc.i
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


_TEXT_LEAF_TAGS = (
    "badge", "tag", "stat", "metric", "kpi", "progress", "bar", "meter",
    "code", "pre", "quote", "icon", "spacer", "divider", "hr", "separator",
    "input", "avatar", "chart", "video", "audio", "file", "attachment", "link",
)


def _collapse_text_leaves(s):
    """<badge>TEXT</badge> -> <badge text="TEXT" /> for text-only leaves."""
    tag_alt = "|".join(_TEXT_LEAF_TAGS)
    pattern = re.compile(
        r"<(" + tag_alt + r")(\s[^<>]*?)?>(?P<inner>[^<>]*?)</>",
        re.IGNORECASE | re.DOTALL,
    )

    def _sub(m):
        tag = m.group(1)
        attrs = (m.group(2) or "").rstrip()
        inner = (m.group("inner") or "").strip()
        if not inner:
            return "<" + tag + attrs + " />"
        if re.search(r"text\s*=", attrs):
            return "<" + tag + attrs + ">" + inner + "</" + tag + ">"
        safe = inner.replace('"', "&quot;")
        return "<" + tag + attrs + ' text="' + safe + '" />'

    return pattern.sub(_sub, s)


def _preprocess_markup(markup):
    """Rewrite React-flavoured markup into something HTMLParser can digest.

    Handles {expr}, {{json}}, attr={expr}, attr={{json}} and onX={...}
    with arbitrary nesting. Never raises.
    """
    try:
        s = markup or ""
        s = _strip_declarations(s)
        s = _strip_handlers(s)
        s = _rewrite_braces(s)
        s = _collapse_text_leaves(s)
        return s
    except Exception:
        return markup or ""


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, Any]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Any] = []


class _UIParser(HTMLParser):
    """Lenient tree builder for the render markup subset."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("__root__", {})
        self.stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Any]]) -> None:
        node = _Node(tag, {k: v for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _LEAF_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Any]]) -> None:
        node = _Node(tag, {k: v for k, v in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data and data.strip():
            self.stack[-1].children.append(data)


def _coerce(name: str, value: Any) -> Any:
    """Turn a raw HTML attribute value into a typed prop value."""
    if value is None:
        return True
    if not isinstance(value, str):
        return value
    if name in _JSON_ATTRS:
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    if name in _BOOL_ATTRS:
        return value.strip().lower() in ("1", "true", "yes", "on", "")
    return value


def _convert(node: _Node, depth: int = 0) -> dict[str, Any] | None:
    """Convert a parsed markup node into a frontend node object."""
    if depth > _MAX_NODE_DEPTH:
        return None
    tag = (node.tag or "").lower()
    kind = _KIND_MAP.get(tag, tag)
    props: dict[str, Any] = {}
    for k, v in (node.attrs or {}).items():
        props[k] = _coerce(k, v)
    props["kind"] = kind
    for k, v in _INLINE_STYLE.get(tag, {}).items():
        props.setdefault(k, v)

    texts: list[str] = []
    kids: list[dict[str, Any]] = []
    for child in node.children:
        if isinstance(child, str):
            t = child.strip()
            if t:
                texts.append(t)
        else:
            r = _convert(child, depth + 1)
            if r:
                kids.append(r)

    text_joined = " ".join(texts).strip()
    if text_joined:
        if kids:
            kids.insert(0, {"kind": "text", "text": text_joined})
        elif "text" not in props and "value" not in props:
            props["text"] = text_joined
    if kids:
        props["children"] = kids
    return props


def _strip_internal(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recurse through children, normalising non-dict entries.

    NOTE: `__expr__<prop>` keys are deliberately KEPT. The parser evaluates a
    dynamic prop once, server-side, which freezes it — but the raw source is
    shipped alongside so the client can re-evaluate it against live state on
    every render (see `hydrate()` in web/js/render-components.js). Stripping
    them here is what made `{running?"success":"danger"}` never change.
    """
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            out.append(n)
            continue
        clean = dict(n)
        kids = clean.get("children")
        if isinstance(kids, list):
            clean["children"] = _strip_internal(kids)
        out.append(clean)
    return out


def _parse_markup(markup: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse render markup into (nodes, state). Never raises.

    Primary path is the DIL parser (understands `{expr}`, `{@body}`,
    `{#if}` / `{#each}`). Falls back to the legacy HTML-ish parser if DIL
    yields nothing, so older markup keeps working.
    """
    try:
        from engine.skills.handlers.dil_parser import parse as _dil_parse
        nodes, state = _dil_parse(markup or "")
        if nodes:
            return _strip_internal(nodes), state
    except Exception:
        pass

    parser = _UIParser()
    try:
        parser.feed(_preprocess_markup(markup or ""))
        parser.close()
    except Exception:
        return [], {}

    top = [c for c in parser.root.children if not isinstance(c, str)]
    state: dict[str, Any] = {}

    if len(top) == 1 and top[0].tag in ("ui", "render", "layout"):
        wrapper = top[0]
        for k, v in (wrapper.attrs or {}).items():
            if k == "state":
                parsed = _coerce("state", v)
                if isinstance(parsed, dict):
                    state = parsed
        top = [c for c in wrapper.children if not isinstance(c, str)]

    nodes: list[dict[str, Any]] = []
    for c in top:
        r = _convert(c)
        if r:
            nodes.append(r)
    return nodes, state


# ── media resolution ────────────────────────────────────────────────────────

def _resolve_images(data: dict[str, Any]) -> dict[str, Any]:
    """Turn an image component's query into real URLs. Never raises.

    Fetches a RESERVE POOL (up to 3x the requested count) so the client can
    swap in a fresh image from the same query when one 404s or is hotlink-
    blocked. `images` is the display slice; `pool` is the full ordered list.
    """
    query = str(data.get("query", "") or "").strip()
    try:
        count = int(data.get("count", 1) or 1)
    except (ValueError, TypeError):
        count = 1
    count = max(1, min(count, MAX_IMAGES_PER_COMPONENT))

    images: list[dict[str, str]] = []
    if query:
        try:
            from engine.search import searxng_image_results
            # Ask for extra candidates — dead links are common (hotlink
            # protection, 404s), so the client needs spares from the SAME query.
            want = max(count, min(count * 3, 12))
            for r in searxng_image_results(query, count=want):
                img = r.get("image") or r.get("thumbnail")
                if not img:
                    continue
                images.append({
                    "image": img,
                    "thumbnail": r.get("thumbnail") or img,
                    "title": r.get("title", ""),
                    "source": r.get("url", ""),
                })
        except Exception:
            images = []

    out = dict(data)
    out["query"] = query
    out["count"] = count
    out["images"] = images[:count]   # what the card actually shows
    out["pool"] = images             # ordered spares for onerror swaps
    return out


def _file_to_data_uri(path: str) -> str | None:
    """Inline a local file as a data URI. Returns None on any failure."""
    try:
        p = Path(str(path)).expanduser()
        if not p.is_file():
            return None
        if p.stat().st_size > _MAX_INLINE_FILE_BYTES:
            return None
        mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except Exception:
        return None


def _is_local_path(s: str) -> bool:
    """True when `s` is a filesystem path rather than a URL / data URI."""
    return bool(s) and not s.startswith(("http://", "https://", "data:", "//", "blob:"))


def _inline_entry(entry: Any) -> Any:
    """Inline a local path inside an image-list entry (str or {image,...})."""
    if isinstance(entry, str):
        if _is_local_path(entry):
            return _file_to_data_uri(entry) or entry
        return entry
    if isinstance(entry, dict):
        for key in ("image", "thumbnail", "src"):
            v = entry.get(key)
            if isinstance(v, str) and _is_local_path(v):
                uri = _file_to_data_uri(v)
                if uri:
                    entry[key] = uri
    return entry


def _first_src(entry: Any) -> str | None:
    if isinstance(entry, dict):
        return entry.get("image") or entry.get("thumbnail") or entry.get("src")
    if isinstance(entry, str):
        return entry
    return None


def _resolve_image_node(n: dict[str, Any]) -> None:
    # `images` may arrive as a JSON *string* from a markup attribute, and its
    # entries may be bare local paths — parse then inline both.
    imgs = n.get("images")
    if isinstance(imgs, str):
        try:
            parsed = json.loads(imgs)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        imgs = parsed if isinstance(parsed, list) else None
        n["images"] = imgs
    if isinstance(imgs, list) and imgs:
        imgs = [_inline_entry(e) for e in imgs]
        n["images"] = imgs
        if not n.get("src"):
            n["src"] = _first_src(imgs[0])
        return

    if n.get("src"):
        s = str(n["src"])
        if _is_local_path(s):
            uri = _file_to_data_uri(s)
            if uri:
                n["src"] = uri
        return

    if n.get("file"):
        uri = _file_to_data_uri(str(n["file"]))
        if uri:
            n["src"] = uri
        return

    q = n.get("query")
    if q:
        resolved = _resolve_images({"query": q, "count": n.get("count", 1)})
        got = resolved.get("images") or []
        if got:
            n["images"] = got
            n["pool"] = resolved.get("pool") or got   # keep reserves
            n["src"] = got[0].get("image") or got[0].get("thumbnail")


def _resolve_file_node(n: dict[str, Any]) -> None:
    if n.get("src") or n.get("href"):
        return
    p = n.get("path") or n.get("file")
    if not p:
        return
    try:
        n["name"] = n.get("name") or Path(str(p)).name
    except Exception:
        pass
    uri = _file_to_data_uri(str(p))
    if uri:
        n["src"] = uri


def _resolve_media(obj: Any, depth: int = 0) -> None:
    """Walk a node tree and resolve image / file sources in place."""
    if depth > _MAX_NODE_DEPTH:
        return
    if isinstance(obj, dict):
        kind = str(obj.get("kind") or obj.get("type") or "").lower()
        if kind in ("image", "img"):
            _resolve_image_node(obj)
        elif kind == "file":
            _resolve_file_node(obj)
        for v in list(obj.values()):
            _resolve_media(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _resolve_media(item, depth + 1)


# ── legacy JSON path ────────────────────────────────────────────────────────

def _parse_components(raw: str) -> list[dict[str, Any]]:
    """Resiliently parse a components array. Mirrors ask_user's strategy."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("components is empty")
    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(raw)
        except (json.JSONDecodeError, ValueError, SyntaxError, TypeError):
            continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    raise ValueError(f"Cannot parse components from: {raw[:120]}")


def _walk_nodes(obj: Any, depth: int = 0) -> None:
    """Resolve image queries inside a legacy JSON layout node tree."""
    if depth > _MAX_NODE_DEPTH:
        return
    if isinstance(obj, dict):
        kind = str(obj.get("kind") or obj.get("type") or "").lower()
        if kind in _IMAGE_NODE_KINDS and obj.get("query") and not obj.get("src"):
            resolved = _resolve_images({"query": obj.get("query"), "count": obj.get("count", 1)})
            imgs = resolved.get("images") or []
            if imgs:
                obj["images"] = imgs
                obj["pool"] = resolved.get("pool") or imgs   # keep reserves
                obj["src"] = imgs[0].get("image") or imgs[0].get("thumbnail")
        for value in list(obj.values()):
            _walk_nodes(value, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_nodes(item, depth + 1)


def _normalize(comp: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one legacy JSON component. Returns (component, None) or (None, reason)."""
    if not isinstance(comp, dict):
        return None, f"not an object ({type(comp).__name__})"
    ctype = str(comp.get("type", "")).strip().lower()
    if not ctype:
        return None, "missing 'type'"
    if ctype not in ALLOWED_TYPES:
        return None, f"unknown type '{ctype}'"
    data = comp.get("data")
    if not isinstance(data, dict):
        return None, f"'{ctype}' data must be an object"
    if ctype == "image":
        data = _resolve_images(data)
        if not data["images"]:
            return None, "image query returned nothing"
    elif ctype == "layout":
        _walk_nodes(data)
        if not (data.get("nodes") or data.get("node")):
            return None, "layout has no nodes"
    return {"type": ctype, "data": data}, None


# ── public entry point ────────────────────────────────────────────────────

def parse_ui_block(content: str, attrs: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Parse a UI block body into the payload the frontend renders.

    Returns ``{"components": [{"type": "layout", "data": {...}}]}`` or
    ``None`` if nothing usable was produced. Never raises. Called directly by
    the stream parser — no skill lifecycle, no middleware, no events.
    """
    attrs = attrs or {}
    payload: dict[str, Any] | None = None
    dropped: list[str] = []

    # ── 1. markup body (preferred) ──
    markup = _strip_fences(content or "")
    if markup and "<" in markup:
        try:
            nodes, state = _parse_markup(markup)
        except Exception:
            nodes, state = [], {}
        if nodes:
            _resolve_media(nodes)
            data: dict[str, Any] = {"nodes": nodes}
            if state:
                data["state"] = state
            payload = {"components": [{"type": "layout", "data": data}]}
        else:
            dropped.append("markup produced no nodes")

    # ── 2. legacy JSON (attribute or bare body) ──
    if payload is None:
        raw = attrs.get("components") or ""
        if not raw and markup.lstrip()[:1] in ("[", "{"):
            raw = markup
        if raw:
            try:
                parsed = _parse_components(raw)
            except ValueError:
                return None
            if len(parsed) > MAX_COMPONENTS:
                parsed = parsed[:MAX_COMPONENTS]
            valid: list[dict[str, Any]] = []
            for comp in parsed:
                norm, reason = _normalize(comp)
                if norm is not None:
                    valid.append(norm)
                elif reason:
                    dropped.append(reason)
            if not valid:
                return None
            payload = {"components": valid}

    if payload is None:
        return None
    if dropped:
        payload["dropped"] = dropped
    return payload
