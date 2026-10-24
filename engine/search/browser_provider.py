"""Multi-engine web search over plain HTTP. No browser is ever launched.

The provider is still registered under the name ``browser`` because that is what
settings.json and the settings dropdown already point at, but the name is now
historical: nothing here starts a process. Every request is a plain HTTPS fetch
through ``curl_cffi``, which presents a real browser's TLS fingerprint so the
engines that sniff the handshake still answer.

That rules Google and Yandex out -- both need JavaScript to produce results and
there is no way to run it without a browser. Brave and Bing still ship complete
results in the initial HTML, so those are the two engines fused here with
weighted Reciprocal Rank Fusion:

    brave 1.0 > bing 0.7

    score(d) = (sum over engines e of weight(e) / (k + rank_e(d)))
               * (1 + 0.3 * (engines that found d - 1))

Agreement is rewarded twice: every engine that found a page adds another
``weight/(k+rank)`` term, then the multiplier lifts the total. A page only Bing
found keeps its own score and its own slot -- it is never absorbed.

An engine returning fewer than ``MIN_RESULTS`` counts as dead and drops out, and
the engine below it moves up one rung of the priority ladder.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from typing import List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from curl_cffi import requests as creq
from lxml import html as lxml_html

from .providers import NetworkError

logger = logging.getLogger(__name__)

IMPERSONATE = "chrome124"
TIMEOUT = 12
PER_ENGINE_LIMIT = 10
MIN_RESULTS = 2
RRF_K = 10
AGREEMENT_STEP = 0.3
PRIORITY_STEP = 0.7

ENGINE_ORDER = ["google", "brave", "bing", "ddg"]

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def _b64_fix(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def _clean_url(engine: str, href: str) -> str:
    """Turn an engine's tracking link into the real destination."""
    if not href:
        return ""
    if engine == "bing" and "bing.com/ck/a" in href:
        raw = parse_qs(urlparse(href).query).get("u", [""])[0]
        if raw.startswith("a1"):
            raw = raw[2:]
        try:
            return base64.urlsafe_b64decode(_b64_fix(raw)).decode()
        except Exception:
            return href
    return href


def _norm_url(u: str) -> str:
    """Key for spotting the same page across engines. The query is kept on
    purpose -- YouTube video ids live there."""
    p = urlparse(u)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def _ladder() -> List[float]:
    return [round(PRIORITY_STEP ** i, 4) for i in range(len(ENGINE_ORDER))]


def _fetch(url: str) -> str:
    try:
        resp = creq.get(url, headers=_HEADERS, impersonate=IMPERSONATE,
                        timeout=TIMEOUT, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        raise NetworkError(f"request failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise NetworkError(f"HTTP {resp.status_code} from {urlparse(url).netloc}")
    return resp.text


# --------------------------------------------------------------------- engines


def _brave(query: str, time_filter: Optional[str]) -> List[dict]:
    url = "https://search.brave.com/search?q=" + query.replace(" ", "+")
    if time_filter:
        url += "&tf=" + {"day": "pd", "week": "pw", "month": "pm"}[time_filter]
    doc = lxml_html.fromstring(_fetch(url))

    out: List[dict] = []
    seen: set[str] = set()
    for node in doc.xpath('//div[contains(@class,"snippet")][@data-type="web"]'):
        anchors = node.xpath('.//a[starts-with(@href,"http")]')
        if not anchors:
            continue
        href = anchors[0].get("href") or ""
        title_nodes = node.xpath('.//div[contains(@class,"title")]')
        title = ""
        if title_nodes:
            title = (title_nodes[0].get("title") or title_nodes[0].text_content() or "").strip()
        content_nodes = node.xpath('.//div[contains(@class,"content")]')
        snippet = content_nodes[0].text_content().strip() if content_nodes else ""
        if not href or not title:
            continue
        key = _norm_url(href)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": href, "snippet": snippet})
        if len(out) >= PER_ENGINE_LIMIT:
            break
    return out


def _bing(query: str, time_filter: Optional[str]) -> List[dict]:
    url = "https://www.bing.com/search?q=" + query.replace(" ", "+") + "&setlang=en&cc=us"
    if time_filter:
        url += "&filters=ex1%3a%22ez" + {"day": "1", "week": "2", "month": "3"}[time_filter] + "%22"
    doc = lxml_html.fromstring(_fetch(url))

    out: List[dict] = []
    seen: set[str] = set()
    for node in doc.xpath('//li[contains(@class,"b_algo")]'):
        anchors = node.xpath('.//h2//a')
        if not anchors:
            continue
        href = _clean_url("bing", anchors[0].get("href") or "")
        title = (anchors[0].text_content() or "").strip()
        para = node.xpath('.//p')
        snippet = para[0].text_content().strip() if para else ""
        if not href.startswith("http") or not title:
            continue
        key = _norm_url(href)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": href, "snippet": snippet})
        if len(out) >= PER_ENGINE_LIMIT:
            break
    return out


def _ddg(query: str, time_filter: Optional[str]) -> List[dict]:
    """DuckDuckGo's server-rendered endpoint.

    ``html.duckduckgo.com`` ships the whole result list in the first response --
    no JavaScript needed, which is the only reason it can be read here at all.
    The main site and the lite endpoint both return a JS shell.
    """
    url = "https://html.duckduckgo.com/html/?q=" + query.replace(" ", "+")
    if time_filter:
        url += "&df=" + {"day": "d", "week": "w", "month": "m"}[time_filter]
    doc = lxml_html.fromstring(_fetch(url))

    out: List[dict] = []
    seen: set[str] = set()
    # this endpoint marks nothing as result--web: the anchor is the reliable
    # handle, and the snippet lives in its nearest result ancestor
    for anchor in doc.xpath('//a[contains(@class,"result__a")]'):
        href = anchor.get("href") or ""
        # ddg wraps links as //duckduckgo.com/l/?uddg=<encoded>
        if "uddg=" in href:
            if href.startswith("//"):
                href = "https:" + href
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        title = (anchor.text_content() or "").strip()
        containers = anchor.xpath('ancestor::div[contains(@class,"result")][1]')
        snips = containers[0].xpath('.//a[contains(@class,"result__snippet")]') if containers else []
        snippet = snips[0].text_content().strip() if snips else ""
        if not href.startswith("http") or not title:
            continue
        key = _norm_url(href)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": href, "snippet": snippet})
        if len(out) >= PER_ENGINE_LIMIT:
            break
    return out


def _google(query: str, time_filter: Optional[str]) -> List[dict]:
    """Google, through its News RSS feed.

    The HTML search page is unreachable without JavaScript -- it answers a cold
    request with a proof-of-work gate and then a CAPTCHA. The RSS feed is a
    different front door: Google renders it server-side and serves it with no
    gate, no framing rule and no captcha. It is Google's own index, queried the
    way Google intends a machine to query it.

    The honest limit: this feed covers Google News, so results are articles and
    pages Google has indexed as news. It is not the full web index. For general
    queries Bing, Brave and DuckDuckGo carry that weight, and they do it without
    a browser too.
    """
    from urllib.parse import urlencode
    from lxml import etree

    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    if time_filter:
        params["q"] = f"{query} when:" + {"day": "1d", "week": "7d", "month": "30d"}[time_filter]
    url = "https://news.google.com/rss/search?" + urlencode(params)

    try:
        resp = creq.get(url, headers={"Accept": "application/rss+xml,application/xml,*/*;q=0.8"},
                        impersonate=IMPERSONATE, timeout=TIMEOUT, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        raise NetworkError(f"request failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise NetworkError(f"HTTP {resp.status_code} from news.google.com")

    try:
        root = etree.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001
        raise NetworkError(f"bad rss: {type(exc).__name__}") from exc

    out: List[dict] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        href = (item.findtext("link") or "").strip()
        if not title or not href.startswith("http"):
            continue
        # the description carries the source name and a short blurb
        desc = item.findtext("description") or ""
        snippet = re.sub(r"<[^>]+>", "", desc).strip()
        key = _norm_url(href)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": href, "snippet": snippet})
        if len(out) >= PER_ENGINE_LIMIT:
            break
    return out


_RUNNERS = {"brave": _brave, "bing": _bing, "ddg": _ddg, "google": _google}


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "how", "what", "why", "is", "are", "best", "my", "your", "i",
})


def _query_terms(query: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", query.lower())
            if w not in _STOPWORDS and len(w) > 1]


def _relevance(results: List[dict], terms: list[str]) -> float:
    """Fraction of results that mention at least one query term.

    An engine that returns pages with no connection to the query is not returning
    *worse* results, it is returning fabricated ones -- a soft block that serves
    random popular pages to scrapers. Bing does exactly that from a flagged IP.
    Trusting the payload because it parsed is how that poison reaches the merged
    list; this measures the payload instead.
    """
    if not results or not terms:
        return 0.0
    hits = 0
    for r in results:
        blob = (r.get("title", "") + " " + r.get("snippet", "") + " " + r.get("url", "")).lower()
        if any(t in blob for t in terms):
            hits += 1
    return hits / len(results)


MIN_RELEVANCE = 0.3  # below this an engine's payload is treated as fabricated


def _run_engine(name: str, query: str, time_filter: Optional[str]) -> dict:
    started = time.time()
    out = {"name": name, "results": [], "error": None, "ms": 0}
    try:
        results = _RUNNERS[name](query, time_filter)
        terms = _query_terms(query)
        rel = _relevance(results, terms)
        if results and rel < MIN_RELEVANCE:
            out["error"] = f"irrelevant payload: {rel:.0%} of results match the query"
            logger.warning("engine %s returned %d results but only %.0f%% match %r -- discarded",
                           name, len(results), rel * 100, query)
        else:
            out["results"] = results
    except Exception as exc:  # noqa: BLE001 - one dead engine must not kill the search
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("engine %s failed: %s", name, out["error"])
    finally:
        out["ms"] = int((time.time() - started) * 1000)
    return out


def _scrape_all(query: str, time_filter: Optional[str]) -> List[dict]:
    """Run every engine concurrently. Plain threads -- no browser involved."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(ENGINE_ORDER)) as pool:
        futures = [pool.submit(_run_engine, n, query, time_filter) for n in ENGINE_ORDER]
        return [f.result() for f in futures]


# ----------------------------------------------------------------------- fuse


# Words that make a query a news query. Only then does Google lead, because the
# only Google front door reachable without JavaScript is its News RSS feed --
# news articles, not the web index. For any other query that feed is the wrong
# shape: it returns release notes and blog posts when the caller wanted a wiki
# page, and because it sits on the top rung and never shares a URL with the web
# engines, it would take every slot. So for non-news queries Google is demoted
# below the engines that actually return web pages.
NEWS_HINTS = frozenset({
    "news", "latest", "release", "released", "releases", "announces",
    "announced", "update", "updated", "updates", "launch", "launched",
    "breaking", "today", "yesterday", "week", "month", "2024", "2025",
    "2026", "rumor", "rumours", "statement", "cve", "advisory",
})


def _is_news_query(query: str) -> bool:
    return any(w in NEWS_HINTS for w in query.lower().split())


def _fuse(per_engine: List[dict], query: str = "") -> List[dict]:
    """Weighted RRF over the surviving engines, with dead engines promoted away.

    The rung order depends on the query: Google leads only when the query is
    news-shaped, because its reachable feed is a news feed. Otherwise the web
    engines lead and Google contributes what it can from the bottom.
    """
    by_name = {b["name"]: b for b in per_engine}
    rungs = _ladder()

    if _is_news_query(query):
        order = ["google", "bing", "ddg", "brave"]
    else:
        order = ["bing", "ddg", "google", "brave"]
    order = [n for n in order if n in by_name]

    alive = [n for n in order
             if by_name[n]["error"] is None and len(by_name[n]["results"]) >= MIN_RESULTS]

    weights: dict[str, float] = {}
    for i, name in enumerate(alive):
        weights[name] = rungs[i] if i < len(rungs) else round(PRIORITY_STEP ** i, 4)

    merged: dict[str, dict] = {}
    for name in alive:
        weight = weights[name]
        for rank, r in enumerate(by_name[name]["results"], 1):
            key = _norm_url(r["url"])
            entry = merged.get(key)
            if entry is None:
                entry = {
                    "title": r["title"],
                    "url": r["url"],
                    "snippet": r["snippet"],
                    "score": 0.0,
                    "found_in": [],
                }
                merged[key] = entry
            entry["score"] += weight / (RRF_K + rank)
            entry["found_in"].append({"engine": name, "rank": rank, "weight": weight})
            if len(r["snippet"]) > len(entry["snippet"]):
                entry["snippet"] = r["snippet"]
            if len(r["title"]) > len(entry["title"]):
                entry["title"] = r["title"]

    for entry in merged.values():
        n = len(entry["found_in"])
        entry["score"] *= 1.0 + AGREEMENT_STEP * (n - 1)
        entry["found_in"].sort(key=lambda f: (-f["weight"], f["rank"]))
        entry["engines"] = ",".join(f["engine"] for f in entry["found_in"])
        entry["agreement"] = n

    ranked = sorted(merged.values(), key=lambda e: (-e["score"], e["url"]))
    top = ranked[0]["score"] if ranked else 1.0
    for entry in ranked:
        entry["score"] = round(entry["score"] / top, 4) if top else 0.0
    return ranked


# ----------------------------------------------------------------- public API


def browser_multi_search(query: str, count: int = 10,
                         time_filter: Optional[str] = None) -> List[dict]:
    """Search Brave and Bing over HTTP and fuse the rankings.

    Named ``browser_multi_search`` because ``_call_provider`` and settings.json
    already reference the ``browser`` provider; no browser is used.

    Returns the same ``[{title, url, snippet}]`` shape every other provider
    returns, with ``engine`` and ``score`` added for callers that want them.
    """
    query = (query or "").strip()
    if not query:
        return []

    per_engine = _scrape_all(query, time_filter)
    ranked = _fuse(per_engine, query)
    if not ranked:
        live = [b["name"] for b in per_engine if b["error"] is None]
        logger.warning("multi-engine search returned nothing (answered: %s)", live or "none")
        return []

    return [
        {
            "title": r["title"],
            "url": r["url"],
            "snippet": r["snippet"],
            "engine": r["engines"],
            "score": r["score"],
        }
        for r in ranked[: max(1, min(int(count or 10), 50))]
    ]


# honest alias for new call sites
multi_search = browser_multi_search
