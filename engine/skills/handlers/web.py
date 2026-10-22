
"""Web handlers: openweb, online_search."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Generator
from typing import Any

import httpx

from engine.skills.handlers.common import (
    RESULT_PREVIEW_CHARS,
    _end_event,
    _output_event,
    strip_html,
)
from engine.security.prompt_guard import wrap_untrusted

# ponytail: direct import instead of subprocess — these already use engine.search internally
from tools.online_search.scripts.online_search import (
    search_only as _search_only,
    fetch_specific_urls as _fetch_urls,
    comprehensive_web_search as _comprehensive_search,
)


def handle_online_search(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """Dispatch web_search / web_fetch / online_search.

    Tool schemas (tools/online_search/tool.json):
      web_search: {query, max_results?, time_filter?}
      web_fetch: {urls: [str], max_chars?}
      online_search: legacy alias for web_search
    """
    started = time.time()

    # web_fetch: attrs has urls as JSON string (parser stringifies non-str params)
    urls_raw = attrs.get("urls", "")
    if urls_raw:
        try:
            urls = json.loads(urls_raw) if isinstance(urls_raw, str) else urls_raw
            if not isinstance(urls, list):
                urls = [str(urls)]
        except Exception:
            urls = [urls_raw]
    else:
        urls = []

    if urls:
        # Clamp: negative values would silently slice content (text[:-5]) and
        # huge values waste context. Floor 1000, ceiling 100k.
        try:
            max_chars = int(attrs.get("max_chars", "0") or 0)
        except (TypeError, ValueError):
            max_chars = 0
        max_chars = min(100_000, max(1_000, max_chars)) if max_chars else 10_000

        url_list = urls if isinstance(urls, list) else [urls]

        yield _output_event(tag_id, f"Fetching {len(url_list)} URL(s):\n")
        for u in url_list:
            yield _output_event(tag_id, f"  → {u}\n")
        yield _output_event(tag_id, "\n")

        try:
            data = _fetch_urls(url_list, max_chars=max_chars)
            items = data.get("pages", [])
            fetched_count = sum(1 for it in items if it.get("success"))
            yield _output_event(tag_id, f"✓ Fetched {fetched_count}/{len(items)} page(s)\n\n")
            for item in items:
                context = item.get("content", "")
                if context:
                    yield _output_event(tag_id, wrap_untrusted(context, source="web_fetch") + "\n")
                elif not item.get("success"):
                    yield _output_event(tag_id, f"✗ {item.get('url', '?')}: {item.get('error', 'unknown error')}\n", "stderr")
            yield _end_event(tag_id, name, True, started, {"urls": url_list, "results": items})
        except Exception as exc:
            yield _output_event(tag_id, f"Fetch error: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    query = content.strip() or attrs.get("query", "")
    if not query:
        yield _output_event(tag_id, "No search query provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty query")
        return

    max_results = int(attrs.get("max_results", "15") or 15)
    time_filter = attrs.get("time_filter") or None

    # Show what we're doing before the blocking call
    yield _output_event(tag_id, f"🔍 Search: {query}\n")
    if time_filter:
        yield _output_event(tag_id, f"   Time filter: {time_filter}\n")
    yield _output_event(tag_id, "\n")

    try:
        data = _search_only(query, max_results=max_results, time_filter=time_filter)
        results = data.get("results", [])
        if not results:
            yield _output_event(tag_id, "No results found.\n")
            yield _end_event(tag_id, name, True, started, {"query": query, "results": []})
            return

        yield _output_event(tag_id, f"✓ {len(results)} result(s)\n\n")

        lines: list[str] = []
        for r in results:
            lines.append(f"[{r['index']}] {r['title']}")
            lines.append(f"    URL: {r['url']}")
            if r.get("snippet"):
                lines.append(f"    Snippet: {r['snippet']}")
            lines.append("")
        yield _output_event(tag_id, wrap_untrusted("\n".join(lines), source="web_search") + "\n")

        yield _end_event(tag_id, name, True, started, {"query": query, "results": results})

    except Exception as exc:
        yield _output_event(tag_id, f"Search error: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))


def _ctx7_get(path: str, params: dict[str, str], timeout: float = 20.0) -> Any:
    """GET context7.com/api/v1/<path> and decode the JSON body.

    Context7 serves public docs without an API key. If CONTEXT7_API_KEY is set
    in the environment we forward it — that only raises the rate limit.
    """
    url = f"https://context7.com/api/v1/{path.lstrip('/')}"
    headers = {"User-Agent": "sable-agent/1.0"}
    api_key = os.environ.get("CONTEXT7_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    res = httpx.get(url, params=params, headers=headers, timeout=timeout, follow_redirects=True)
    res.raise_for_status()
    return res.json()


def _slug(name: str) -> str:
    """Lowercase alphanumeric-only form, for fuzzy ID matching."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _pick_library(hits: list[dict[str, Any]], library: str) -> dict[str, Any]:
    """Choose the best hit for ``library``, trusting Context7's relevance order.

    Do NOT re-sort by trust. Context7's ``score`` is already relevance-ranked and
    correct: querying 'next.js middleware' puts /vercel/next.js first at 516.
    An earlier version sorted trust-first and demoted it to 5th, behind random
    verified packages — over-engineering that made resolution strictly worse.

    The one thing relevance order gets wrong is a same-name package outranking
    the canonical repo, so an exact ID-slug match is promoted when present.
    """
    target = _slug(library)
    for hit in hits:
        if _slug(str(hit.get("id", "")).rsplit("/", 1)[-1]) == target:
            return hit
    return hits[0]


def handle_fetch_docs(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """fetch_docs — version-accurate library documentation via Context7.

    Two modes:
      * library given  -> resolve it to a Context7 ID, then pull docs on topic.
      * no library     -> search the index and return candidate library IDs.

    The point is to stop hallucinating APIs from stale training data. Everything
    returned carries a source URL back to the upstream repo file.
    """
    started = time.time()

    library = (attrs.get("library") or "").strip()
    topic = (attrs.get("topic") or "").strip() or content.strip()
    version = (attrs.get("version") or "").strip()

    try:
        tokens = int(attrs.get("tokens", "5000") or 5000)
    except (TypeError, ValueError):
        tokens = 5000
    tokens = min(20000, max(1000, tokens))

    if not library and not topic:
        yield _output_event(tag_id, "Provide a library name, a topic, or both\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty library and topic")
        return

    # ── Mode 1: no library -> discover candidate IDs ──────────────────────
    if not library:
        yield _output_event(tag_id, f"📚 Searching Context7 for: {topic}\n")
        try:
            data = _ctx7_get("search", {"query": topic})
        except Exception as exc:
            yield _output_event(tag_id, f"Context7 search failed: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
            return

        hits = data.get("results") or []
        if not hits:
            yield _output_event(tag_id, "No matching libraries.\n")
            yield _end_event(tag_id, name, True, started, {"results": []})
            return

        lines = []
        for h in hits[:10]:
            mark = "✓" if h.get("verified") else " "
            lines.append(f"{mark} {h.get('id')}")
            lines.append(f"    {h.get('title')} — {h.get('description', '')[:110]}")
            bits = []
            if h.get("trustScore"):
                bits.append(f"trust {h['trustScore']}")
            if h.get("stars", -1) and h.get("stars", 0) > 0:
                bits.append(f"{h['stars']}★")
            if h.get("versions"):
                bits.append("versions: " + ", ".join(h["versions"][:4]))
            if bits:
                lines.append(f"    {' · '.join(bits)}")
            lines.append("")

        yield _output_event(tag_id, wrap_untrusted("\n".join(lines), source="context7") + "\n")
        yield _output_event(
            tag_id,
            "Pick an ID above and call fetch_docs again with `library` set.\n",
        )
        yield _end_event(tag_id, name, True, started, {"results": hits[:10]})
        return

    # ── Mode 2: library given -> resolve then pull docs ───────────────────
    lib_id = library
    if not lib_id.startswith("/"):
        yield _output_event(tag_id, f"🔎 Resolving '{library}'…\n")
        try:
            data = _ctx7_get("search", {"query": f"{library} {topic}".strip()})
        except Exception as exc:
            yield _output_event(tag_id, f"Resolution failed: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
            return
        hits = data.get("results") or []
        if not hits:
            yield _output_event(tag_id, f"No library matched '{library}'.\n", "stderr")
            yield _end_event(tag_id, name, False, started, error="No library match")
            return
        pick = _pick_library(hits, library)
        lib_id = pick["id"]
        yield _output_event(tag_id, f"   → {lib_id} ({pick.get('title')})\n\n")

    if version:
        topic = f"{topic} {version}".strip()

    params = {"type": "txt", "tokens": str(tokens)}
    if topic:
        params["topic"] = topic

    yield _output_event(tag_id, f"📖 Fetching docs: {lib_id}\n")
    try:
        res = httpx.get(
            f"https://context7.com/api/v1{lib_id}",
            params=params,
            headers={"User-Agent": "sable-agent/1.0"},
            timeout=30,
            follow_redirects=True,
        )
        res.raise_for_status()
        text = res.text
    except Exception as exc:
        yield _output_event(tag_id, f"Docs fetch failed: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    if not text.strip():
        yield _output_event(tag_id, "Context7 returned no docs for that topic.\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty docs")
        return

    yield _output_event(tag_id, f"✓ {len(text):,} chars\n\n")
    yield _output_event(tag_id, wrap_untrusted(text, source="context7") + "\n")
    yield _end_event(
        tag_id, name, True, started,
        {"library": lib_id, "topic": topic, "chars": len(text)},
    )


def handle_openweb(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    site = attrs.get("site", "")
    op = attrs.get("op", "fetch").lower()
    params_raw = attrs.get("params", "")
    params: dict[str, Any] = {}
    if params_raw:
        try:
            loaded = json.loads(params_raw)
            if isinstance(loaded, dict):
                params = loaded
            else:
                params = {"query": str(loaded)}
        except Exception:
            params = {"query": params_raw}

    if op in {"search", "query"}:
        query = str(params.get("query") or content.strip()).strip()
        if site:
            query = f"site:{site} {query}"
        if not query:
            yield _output_event(tag_id, "No OpenWeb query provided\n", "stderr")
            yield _end_event(tag_id, name, False, started, error="Empty query")
            return
        yield _output_event(tag_id, f"OpenWeb search: {query}\n\n")
        try:
            data = _comprehensive_search(query, max_pages=5, max_chars=10000)
            results = data.get("results", [])
            for item in results:
                context = item.get("context", "") or item.get("snippet", "")
                if context:
                    yield _output_event(tag_id, wrap_untrusted(context, source="web_search") + "\n")
            yield _end_event(tag_id, name, True, started, {"site": site, "op": op, "results": results})
        except Exception as exc:
            yield _output_event(tag_id, f"Search error: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    url = str(params.get("url") or content.strip()).strip()
    if not url and site:
        url = f"https://{site}"
    if not url:
        yield _output_event(tag_id, "No URL provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty URL")
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        res = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:153.0)"},
            timeout=15,
            follow_redirects=True,
        )
        ctype = res.headers.get("content-type", "")
        if "json" in ctype:
            try:
                text = json.dumps(res.json(), indent=2, ensure_ascii=False)
            except Exception:
                text = res.text
        else:
            text = strip_html(res.text)
        preview = text[:RESULT_PREVIEW_CHARS]
        yield _output_event(tag_id, wrap_untrusted(preview, source="web_fetch") + "\n")
        yield _end_event(
            tag_id,
            name,
            True,
            started,
            {"url": url, "status": res.status_code, "content_type": ctype, "chars": len(text)},
        )
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
