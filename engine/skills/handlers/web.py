
"""Web handlers: openweb, online_search."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Generator
from typing import Any

import httpx

from engine.skills.handlers.common import (
    RESULT_PREVIEW_CHARS,
    SKILLS_DIR,
    _end_event,
    _output_event,
    strip_html,
)

_SEARCH_SCRIPT = SKILLS_DIR / "online_search" / "scripts" / "web_search_batch.py"


def handle_online_search(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    query = content.strip() or attrs.get("query", "")
    if not query:
        yield _output_event(tag_id, "No search query provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty query")
        return

    yield _output_event(tag_id, f"Searching: {query}\n\n")

    try:
        proc = subprocess.run(
            ["python3", str(_SEARCH_SCRIPT), "--json", query],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            yield _output_event(tag_id, f"Search failed: {proc.stderr.strip()}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=proc.stderr.strip()[:500])
            return

        data = json.loads(proc.stdout)
        items = data.get("items", [])
        if not items:
            yield _output_event(tag_id, "No results found.\n")
            yield _end_event(tag_id, name, True, started, {"query": query, "results": []})
            return

        for item in items:
            context = item.get("context", "")
            if context:
                yield _output_event(tag_id, context + "\n")
            elif not item.get("ok"):
                yield _output_event(tag_id, f"Error: {item.get('error', 'unknown')}\n", "stderr")

        yield _end_event(tag_id, name, True, started, {"query": query, "results": items})

    except subprocess.TimeoutExpired:
        yield _output_event(tag_id, "Search timed out (60s)\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Search timed out")
    except json.JSONDecodeError as exc:
        yield _output_event(tag_id, f"Failed to parse search output: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
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
            proc = subprocess.run(
                ["python3", str(_SEARCH_SCRIPT), "--json", query],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                yield _output_event(tag_id, f"Search failed: {proc.stderr.strip()}\n", "stderr")
                yield _end_event(tag_id, name, False, started, error=proc.stderr.strip()[:500])
                return
            data = json.loads(proc.stdout)
            items = data.get("items", [])
            for item in items:
                context = item.get("context", "")
                if context:
                    yield _output_event(tag_id, context + "\n")
            yield _end_event(tag_id, name, True, started, {"site": site, "op": op, "results": items})
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
        yield _output_event(tag_id, preview + "\n")
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
