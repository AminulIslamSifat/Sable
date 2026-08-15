#!/usr/bin/env python3
"""Standalone batch web search.

Run this file directly with a list of queries and it will search the web for
one query at a time, using the same high-level behavior as the Odysseus
project: provider selection, SearXNG-first search, freshness inference,
fallbacks, and fetched page content in the output.

Examples:

    python3 web_search_batch.py "latest python release" "best local LLM"
    python3 web_search_batch.py --queries-file queries.txt
    cat queries.txt | python3 web_search_batch.py --stdin
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import html.parser
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lxml import html as lxml_html

# Add project root to sys.path so we can import engine.search
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from engine.search import search as unified_search
from engine.search.config import _get_search_settings

APP_DIR = Path(__file__).parent.resolve()
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULT_SEARXNG_INSTANCE = "http://localhost:8080"
GENERAL_ENGINES = os.environ.get("SEARXNG_GENERAL_ENGINES", "bing,mojeek,presearch")
USER_AGENT = os.environ.get(
    "WEB_FETCH_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
)

DAY_HINTS = ("today", "latest", "breaking", "this morning", "right now", "currently")
WEEK_HINTS = ("this week", "past week", "recent news", "last few days")
MONTH_HINTS = ("this month", "past month")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class TextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "br", "div", "li", "section", "article", "tr", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        else:
            self._chunks.append(text)

    def get_text(self) -> str:
        text = " ".join(self._chunks)
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


NOISE_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "nav",
    "header",
    "footer",
    "aside",
    "menu",
    "dialog",
}
TEXT_BLOCK_TAGS = {"p", "br", "div", "li", "section", "article", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
NOISE_HINTS = (
    "cookie",
    "consent",
    "banner",
    "modal",
    "popup",
    "advert",
    "ad-",
    "sponsor",
    "promo",
    "toolbar",
    "sidebar",
    "nav",
    "menu",
    "footer",
    "header",
    "breadcrumb",
    "subscribe",
    "share",
    "pagination",
    "related",
    "comment",
    "reaction",
    "filter",
    "search",
    "login",
)


def load_settings() -> dict[str, Any]:
    """Load search settings from system settings.json, falling back to skill-local file."""
    try:
        system_settings = _get_search_settings()
        if system_settings:
            return system_settings
    except Exception:
        pass
    # Fallback to skill-local settings.json
    defaults = {
        "search_provider": "searxng",
        "search_fallback_chain": ["duckduckgo"],
        "search_url": "",
        "search_result_count": 5,
        "search_safesearch": "strict",
    }
    if not SETTINGS_FILE.exists():
        return defaults
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    if not isinstance(data, dict):
        return defaults
    defaults.update(data)
    return defaults


def infer_time_filter(query: str) -> str | None:
    q_lc = (query or "").lower()
    if any(kw in q_lc for kw in DAY_HINTS):
        return "day"
    if any(kw in q_lc for kw in WEEK_HINTS):
        return "week"
    if any(kw in q_lc for kw in MONTH_HINTS):
        return "month"
    return None


def http_get(url: str, *, params: dict[str, Any] | None = None, timeout: int = 15) -> tuple[int, str, str]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except Exception:
            text = body.decode("utf-8", errors="replace")
        return response.status, content_type, text


def extract_visible_text(html_text: str) -> tuple[str, str]:
    parser = TextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass
    title = html.unescape(parser.title).strip()
    text = html.unescape(parser.get_text()).strip()
    return title, text


def _clean_html_tree(tree) -> None:
    for tag in NOISE_TAGS:
        for element in tree.xpath(f".//{tag}"):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)


def _node_text_length(node) -> int:
    text = " ".join(piece.strip() for piece in node.itertext() if piece and piece.strip())
    return len(re.sub(r"\s+", " ", text).strip())


def _node_noise_score(node) -> int:
    combined = " ".join(
        value.lower()
        for value in (
            node.get("id") or "",
            node.get("class") or "",
            node.get("role") or "",
            node.tag or "",
        )
    )
    return sum(1 for hint in NOISE_HINTS if hint in combined)


def _node_penalty(node) -> int:
    link_count = len(node.xpath(".//a"))
    control_count = len(node.xpath(".//button|.//input|.//select|.//textarea|.//label|.//nav|.//aside|.//footer|.//header"))
    return link_count * 8 + control_count * 25 + _node_noise_score(node) * 30


def _score_node(node) -> tuple[int, int]:
    text_len = _node_text_length(node)
    if text_len <= 0:
        return (0, 0)
    score = text_len - _node_penalty(node)
    if node.tag in {"main", "article"}:
        score += 250
    role = (node.get("role") or "").lower()
    if role == "main":
        score += 200
    return (score, text_len)


def _render_text_lines(node) -> str:
    lines: list[str] = []

    def append_line(value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            lines.append(value)

    for element in node.iter():
        if element.tag in {"script", "style", "noscript", "template"}:
            continue
        if element.tag == "br":
            lines.append("")
            continue
        if element.tag in TEXT_BLOCK_TAGS:
            text = " ".join(piece.strip() for piece in element.itertext() if piece and piece.strip())
            append_line(text)

    if not lines:
        text = " ".join(piece.strip() for piece in node.itertext() if piece and piece.strip())
        append_line(text)

    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank:
            if not previous_blank:
                cleaned.append("")
            previous_blank = True
            continue
        cleaned.append(line)
        previous_blank = False
    return "\n".join(cleaned).strip()


def extract_main_content(html_text: str) -> tuple[str, str]:
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return extract_visible_text(html_text)

    title = ""
    title_candidates = tree.xpath("//title/text()")
    if title_candidates:
        title = html.unescape("".join(title_candidates)).strip()

    _clean_html_tree(tree)

    candidates = []
    candidates.extend(tree.xpath("//main|//article|//*[@role='main']"))
    candidates.extend(tree.xpath("//section|//div|//td"))
    candidates = [node for node in candidates if _node_text_length(node) >= 120]

    best_node = max(candidates, key=_score_node, default=tree)
    text = _render_text_lines(best_node)

    if not title:
        fallback_title = tree.xpath("//meta[@property='og:title']/@content | //meta[@name='title']/@content")
        if fallback_title:
            title = html.unescape(fallback_title[0]).strip()

    if not text:
        return extract_visible_text(html_text)
    return title, text


def truncate(text: str, limit: int = 3000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[...truncated]"


def clean_query(query: str) -> str:
    return " ".join((query or "").split()).strip()


def read_queries_from_stream(text: str) -> list[str]:
    queries: list[str] = []
    for line in (text or "").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        queries.append(item)
    return queries


def read_queries_from_file(path_text: str) -> list[str]:
    if path_text == "-":
        return read_queries_from_stream(sys.stdin.read())
    path = Path(path_text)
    if not path.exists():
        raise FileNotFoundError(f"no such query file: {path_text!r}")
    return read_queries_from_stream(path.read_text(encoding="utf-8"))


def collect_queries(args) -> list[str]:
    queries: list[str] = []
    for item in args.queries or []:
        item = clean_query(item)
        if item:
            queries.append(item)
    if args.queries_file:
        queries.extend(read_queries_from_file(args.queries_file))
    if args.stdin:
        queries.extend(read_queries_from_stream(sys.stdin.read()))
    elif not queries and not sys.stdin.isatty():
        queries.extend(read_queries_from_stream(sys.stdin.read()))
    return [q for q in queries if q]


# NOTE: Inline search provider functions (searxng_search, duckduckgo_search, etc.)
# have been removed. All search now goes through engine.search.unified_search().
# See engine/search/providers.py for individual provider implementations.


def _normalize_url(raw_url: str) -> str:
    """Decode DDG redirect wrappers and fix protocol-relative URLs."""
    url = html.unescape(raw_url).strip()
    # Decode DuckDuckGo redirect wrapper: //duckduckgo.com/l/?uddg=ENCODED&rut=...
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc in ("duckduckgo.com", "www.duckduckgo.com") and parsed.path == "/l/":
        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get("uddg", [""])[0]
        if target:
            url = urllib.parse.unquote(target)
            parsed = urllib.parse.urlparse(url)
    # Fix protocol-relative URLs (//example.com/...)
    if url.startswith("//"):
        url = "https:" + url
    elif not parsed.scheme:
        url = "https://" + url
    return url


def fetch_webpage_content(url: str, *, timeout: int = 10, max_chars: int = 5000) -> dict[str, Any]:
    url = _normalize_url(url)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.status
            body = response.read()
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                text = body.decode(charset, errors="replace")
            except Exception:
                text = body.decode("utf-8", errors="replace")
            if content_type == "text/html" or "<html" in text[:2000].lower():
                title, visible = extract_main_content(text)
                if not title:
                    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
                    title = html.unescape(re.sub(r"<.*?>", "", m.group(1), flags=re.S)).strip() if m else ""
                return {
                    "success": True,
                    "url": url,
                    "title": title,
                    "content": truncate(visible, max_chars),
                    "status_code": status_code,
                }
            return {
                "success": True,
                "url": url,
                "title": "",
                "content": truncate(text.strip(), max_chars),
                "status_code": status_code,
            }
    except urllib.error.HTTPError as e:
        return {"success": False, "url": url, "error": str(e), "content": "", "title": "", "status_code": e.code}
    except Exception as e:
        return {"success": False, "url": url, "error": str(e), "content": "", "title": "", "status_code": 0}


def comprehensive_web_search(query: str, *, max_pages: int = 5, max_chars: int = 10000, time_filter: str | None = None) -> dict[str, Any]:
    settings = load_settings()
    count = max(max_pages, int(settings.get("search_result_count") or 5))
    raw_results = unified_search(query, count=count, time_filter=time_filter)
    provider_name = settings.get("search_provider", "searxng")
    if not raw_results:
        return {
            "query": query,
            "provider": provider_name,
            "time_filter": time_filter,
            "ok": False,
            "error": "no search results",
            "sources": [],
            "context": f"No search results found for: {query}",
        }

    # Results already ranked by engine.search
    ranked = [SearchResult(title=r.get("title",""), url=r.get("url",""), snippet=r.get("snippet","")) for r in raw_results]
    sources = [{"url": item.url, "title": item.title} for item in ranked[:max_pages]]

    fetched: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max_pages or 1)) as executor:
        future_map = {
            executor.submit(fetch_webpage_content, source["url"], timeout=10, max_chars=max_chars): source
            for source in sources
        }
        for future in concurrent.futures.as_completed(future_map):
            source = future_map[future]
            try:
                page = future.result()
            except Exception as e:
                fetched.append({"url": source["url"], "title": source["title"], "success": False, "error": str(e), "content": ""})
                continue
            if page.get("success") and page.get("content"):
                fetched.append(page)

    parts: list[str] = []
    parts.append("```sources")
    for idx, source in enumerate(sources, 1):
        parts.append(f"[{idx}] {source['title']}")
        parts.append(f"    {source['url']}")
    parts.append("```")
    parts.append("")
    parts.append("=" * 70)
    parts.append("WEB SEARCH RESULTS AND FETCHED CONTENT")
    parts.append(f"Query: {query}")
    parts.append(f"Provider: {provider_name}")
    parts.append(f"Searched {len(ranked)} results, fetched {len(fetched)} pages")
    parts.append("=" * 70)
    parts.append("")
    parts.append("SEARCH RESULTS SUMMARY:")
    parts.append("-" * 50)
    for idx, result in enumerate(ranked, 1):
        parts.append(f"\n[{idx}] {result.title}")
        parts.append(f"    URL: {result.url}")
        if result.snippet:
            parts.append(f"    Snippet: {truncate(result.snippet, 220)}")

    if fetched:
        parts.append("\n" + "=" * 70)
        parts.append("FETCHED PAGE CONTENT:")
        parts.append("-" * 50)
        for idx, page in enumerate(fetched, 1):
            title = page.get("title") or ""
            parts.append(f"\n[CONTENT {idx}] From: {page.get('url', '')}")
            if title:
                parts.append(f"Title: {title}")
            parts.append(truncate(page.get("content", ""), max_chars))

    return {
        "query": query,
        "provider": provider_name,
        "time_filter": time_filter,
        "ok": True,
        "sources": sources,
        "context": "\n".join(parts).strip(),
    }


def render_text(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return textwrap.dedent(
            f"""
            === Query: {result.get('query', '')} ===
            Error: {result.get('error', 'unknown error')}
            """
        ).strip()
    header = [
        f"=== Query: {result['query']} ===",
        f"Provider: {result.get('provider', '')}",
        f"Time filter: {result.get('time_filter') or '(none)'}",
        "",
        result.get("context", ""),
    ]
    return "\n".join(header).strip()


def search_only(query: str, *, max_results: int = 15, time_filter: str | None = None) -> dict[str, Any]:
    """Phase 1: search and return ranked results without fetching page content."""
    settings = load_settings()
    provider_name = settings.get("search_provider", "searxng")
    raw_results = unified_search(query, count=max_results, time_filter=time_filter)
    if not raw_results:
        return {
            "query": query,
            "provider": provider_name,
            "time_filter": time_filter,
            "ok": False,
            "error": "no search results",
            "results": [],
        }

    # Results already ranked by engine.search
    ranked = [SearchResult(title=r.get("title",""), url=r.get("url",""), snippet=r.get("snippet","")) for r in raw_results]
    results = [
        {"index": idx, "title": item.title, "url": item.url, "snippet": truncate(item.snippet, 220)}
        for idx, item in enumerate(ranked[:max_results], 1)
    ]
    return {
        "query": query,
        "provider": provider_name,
        "time_filter": time_filter,
        "ok": True,
        "results": results,
    }


def fetch_specific_urls(urls: list[str], *, max_chars: int = 10000) -> dict[str, Any]:
    """Phase 2: fetch content from specific URLs chosen by the model."""
    fetched: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(urls) or 1)) as executor:
        future_map = {
            executor.submit(fetch_webpage_content, url, timeout=10, max_chars=max_chars): url
            for url in urls
        }
        for future in concurrent.futures.as_completed(future_map):
            url = future_map[future]
            try:
                page = future.result()
            except Exception as e:
                fetched.append({"url": url, "success": False, "error": str(e), "content": "", "title": ""})
                continue
            fetched.append(page)

    # Preserve original URL order
    url_order = {url: idx for idx, url in enumerate(urls)}
    fetched.sort(key=lambda p: url_order.get(p.get("url", ""), 999))

    parts: list[str] = []
    parts.append("=" * 70)
    parts.append(f"FETCHED PAGE CONTENT ({len(fetched)} pages)")
    parts.append("=" * 70)
    for idx, page in enumerate(fetched, 1):
        title = page.get("title") or ""
        parts.append(f"\n[CONTENT {idx}] From: {page.get('url', '')}")
        if title:
            parts.append(f"Title: {title}")
        if page.get("success") and page.get("content"):
            parts.append(truncate(page["content"], max_chars))
        else:
            parts.append(f"Error: {page.get('error', 'no content')}")

    return {
        "ok": True,
        "fetched_count": len(fetched),
        "pages": fetched,
        "context": "\n".join(parts).strip(),
    }


def load_research_state(path: str) -> dict[str, Any]:
    """Load a deep-research state file, or return an empty structure if missing/invalid."""
    p = Path(path)
    if not p.exists():
        return {"topics": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"topics": {}}
    if not isinstance(data, dict) or "topics" not in data:
        return {"topics": {}}
    return data


def save_research_state(path: str, state: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def research_init(path: str, topics: list[str]) -> dict[str, Any]:
    """Create a fresh state file with one entry per sub-topic, all marked 'open'."""
    state = {"topics": {}}
    for name in topics:
        state["topics"][name] = {"status": "open", "queries": [], "fetched_urls": []}
    save_research_state(path, state)
    return state


def research_mark(path: str, topic: str, status: str) -> dict[str, Any]:
    """Update a sub-topic's status: open | answered | thin | conflicting."""
    state = load_research_state(path)
    entry = state["topics"].setdefault(topic, {"status": status, "queries": [], "fetched_urls": []})
    entry["status"] = status
    save_research_state(path, state)
    return state


def render_research_status(state: dict[str, Any]) -> str:
    if not state.get("topics"):
        return "No research state yet."
    lines = ["RESEARCH STATE", "-" * 50]
    for name, info in state["topics"].items():
        lines.append(f"[{info.get('status', 'open')}] {name}")
        if info.get("queries"):
            lines.append(f"    queries run ({len(info['queries'])}): {', '.join(info['queries'])}")
        if info.get("fetched_urls"):
            lines.append(f"    urls fetched: {len(info['fetched_urls'])}")
    return "\n".join(lines)


def _record_queries(state: dict[str, Any], topic: str, queries: list[str]) -> None:
    entry = state["topics"].setdefault(topic, {"status": "open", "queries": [], "fetched_urls": []})
    for q in queries:
        if q not in entry["queries"]:
            entry["queries"].append(q)


def _record_fetched(state: dict[str, Any], topic: str, urls: list[str]) -> None:
    entry = state["topics"].setdefault(topic, {"status": "open", "queries": [], "fetched_urls": []})
    for u in urls:
        if u not in entry["fetched_urls"]:
            entry["fetched_urls"].append(u)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone batch web search")
    p.add_argument("queries", nargs="*", help="One or more search queries")
    p.add_argument("--queries-file", help="Read queries from a text file (one per line); use '-' for stdin")
    p.add_argument("--stdin", action="store_true", help="Read queries from stdin, one per line")
    p.add_argument("--max-pages", type=int, default=5, help="Maximum pages to fetch per query")
    p.add_argument("--max-results", type=int, default=15, help="Maximum search results to return (search-only mode)")
    p.add_argument("--time-filter", choices=["day", "week", "month", "year"], help="Force a freshness filter")
    p.add_argument("--json", action="store_true", help="Print JSON instead of text")
    p.add_argument("--search-only", action="store_true", help="Only return search results (titles/URLs), skip fetching")
    p.add_argument("--fetch-urls", nargs="+", metavar="URL", help="Fetch specific URLs directly (phase 2)")
    p.add_argument("--max-chars", type=int, default=10000, help="Max characters per fetched page (default 10000)")
    p.add_argument("--research-log", metavar="PATH", help="Path to a deep-research state JSON file, used to track sub-topic progress across search rounds")
    p.add_argument("--topic", help="Sub-topic name this --search-only/--fetch-urls call belongs to (used with --research-log)")
    p.add_argument("--research-init", nargs="+", metavar="TOPIC", help="Create a new state file at --research-log with these sub-topic names, all marked 'open'")
    p.add_argument("--research-mark", nargs=2, metavar=("TOPIC", "STATUS"), help="Set a sub-topic's status in --research-log: open|answered|thin|conflicting")
    p.add_argument("--research-status", action="store_true", help="Print the current state summary from --research-log and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Research-state bookkeeping actions (no search performed)
    if args.research_init or args.research_mark or args.research_status:
        if not args.research_log:
            sys.stderr.write("error: --research-init/--research-mark/--research-status require --research-log PATH\n")
            return 1
        if args.research_init:
            state = research_init(args.research_log, args.research_init)
            sys.stdout.write(f"Initialized {args.research_log} with {len(args.research_init)} topic(s)\n")
            sys.stdout.write(render_research_status(state) + "\n")
            return 0
        if args.research_mark:
            topic, status = args.research_mark
            state = research_mark(args.research_log, topic, status)
            sys.stdout.write(render_research_status(state) + "\n")
            return 0
        state = load_research_state(args.research_log)
        sys.stdout.write(render_research_status(state) + "\n")
        return 0

    # Phase 2: fetch specific URLs directly
    if args.fetch_urls:
        urls = args.fetch_urls
        state = None
        if args.research_log and args.topic:
            state = load_research_state(args.research_log)
            already = set(state["topics"].get(args.topic, {}).get("fetched_urls", []))
            skipped = [u for u in urls if u in already]
            urls = [u for u in urls if u not in already]
            if skipped:
                sys.stderr.write(f"note: skipping {len(skipped)} already-fetched URL(s) for topic '{args.topic}'\n")
            if not urls:
                sys.stdout.write("All requested URLs already fetched for this topic; nothing to do.\n")
                return 0
        result = fetch_specific_urls(urls, max_chars=args.max_chars)
        if state is not None:
            _record_fetched(state, args.topic, urls)
            save_research_state(args.research_log, state)
        if args.json:
            json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(result["context"] + "\n")
        return 0

    try:
        queries = collect_queries(args)
    except Exception as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    if not queries:
        sys.stderr.write("error: provide queries as arguments, --queries-file, or stdin\n")
        return 1

    # Phase 1: search-only mode
    if args.search_only:
        items = [
            search_only(query, max_results=args.max_results, time_filter=args.time_filter or infer_time_filter(query))
            for query in queries
        ]
        if args.research_log and args.topic:
            state = load_research_state(args.research_log)
            _record_queries(state, args.topic, queries)
            save_research_state(args.research_log, state)
        if args.json:
            json.dump({"count": len(items), "items": items}, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            for item in items:
                if not item["ok"]:
                    sys.stdout.write(f"Error: {item.get('error', 'unknown')}\n")
                    continue
                sys.stdout.write(f"=== Query: {item['query']} ===\n")
                sys.stdout.write(f"Provider: {item.get('provider', '')}\n\n")
                for r in item["results"]:
                    sys.stdout.write(f"[{r['index']}] {r['title']}\n")
                    sys.stdout.write(f"    {r['url']}\n")
                    if r.get("snippet"):
                        sys.stdout.write(f"    {r['snippet']}\n")
                    sys.stdout.write("\n")
        return 0

    # Legacy: full search + fetch
    items = [
        comprehensive_web_search(query, max_pages=args.max_pages, max_chars=args.max_chars, time_filter=args.time_filter or infer_time_filter(query))
        for query in queries
    ]

    if args.json:
        json.dump({"count": len(items), "items": items}, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        for index, item in enumerate(items, 1):
            sys.stdout.write(render_text(item))
            if index < len(items):
                sys.stdout.write("\n\n")
        sys.stdout.write("\n")
    return 0


# ─── Tool Handler (stdin JSON dispatch) ───────────────────────────────────────

def _tool_web_search(args: dict) -> dict:
    query = args.get("query", "").strip()
    if not query:
        return {"error": "'query' is required"}
    max_results = min(int(args.get("max_results", 10)), 20)
    time_filter = args.get("time_filter") or infer_time_filter(query)
    return search_only(query, max_results=max_results, time_filter=time_filter)


def _tool_web_fetch(args: dict) -> dict:
    urls = args.get("urls", [])
    if not urls:
        return {"error": "'urls' is required (non-empty list)"}
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split(",") if u.strip()]
    if not urls:
        return {"error": "'urls' resolved to empty list"}
    max_chars = int(args.get("max_chars", 10000))
    return fetch_specific_urls(urls, max_chars=max_chars)


_TOOL_COMMANDS = {
    "web_search": _tool_web_search,
    "web_fetch": _tool_web_fetch,
}


def tool_main() -> int:
    """Entry point for tool dispatch via stdin JSON."""
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "No input"}))
        return 1
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        return 1

    command = req.get("command", "")
    if command not in _TOOL_COMMANDS:
        print(json.dumps({"error": f"Unknown command: {command}. Valid: {list(_TOOL_COMMANDS.keys())}"}))
        return 1

    try:
        result = _TOOL_COMMANDS[command](req.get("args", {}))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": f"{command} failed: {e}"}))
        return 1
    return 0


if __name__ == "__main__":
    # If stdin is piped JSON (tool mode), dispatch; otherwise run CLI
    if not sys.stdin.isatty():
        raise SystemExit(tool_main())
    raise SystemExit(main())