
"""Web handlers: openweb, online_search."""

from __future__ import annotations

import json
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
        max_chars = int(attrs.get("max_chars", "0") or 0)
        fetch_args: list[str] = ["--fetch-urls"]

        url_list = urls if isinstance(urls, list) else [urls]
        fetch_args.extend(url_list)
        if max_chars > 0:
            fetch_args.append(f"--max-chars={max_chars}")

        yield _output_event(tag_id, f"Fetching {len(url_list)} URL(s):\n")
        for u in url_list:
            yield _output_event(tag_id, f"  → {u}\n")
        yield _output_event(tag_id, "\n")

        try:
            data = _fetch_urls(url_list, max_chars=max_chars or 10000)
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
