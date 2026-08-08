"""
Search Engine Stress Test for Sable
Tests: Serper, Tavily, SearXNG, DuckDuckGo
Usage: python3 tests/search_stress_test.py [--queries N] [--repeats N]
"""

import sys, os, time, json, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.search.config import _get_search_instance, _get_provider_key
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

# ── Test Queries (mixed difficulty) ──
QUERIES = [
    # Clear / normal
    "Python asyncio tutorial 2025",
    "Linux kernel 6.12 changelog",
    "best mechanical keyboards under 100 dollars",
    "climate change impact on agriculture research paper",
    "how to center a div in CSS",
    # Technical / niche
    "rust lifetime annotation error E0491",
    "hyprland windowrule workspace movetoworkspacesilent",
    "nginx reverse proxy websocket connection reset",
    "systemd service restart loop journalctl",
    "docker compose volume mount permission denied",
    # Ambiguous (single-word, multi-meaning)
    "apple",
    "java",
    "python",
    "mercury",
    "jaguar",
    # Typos / broken input
    "hwo to fi xblue scren wndows 11",
    "wht is quantum computng",
    "best laptap for programing 2025",
    # Conversational / weird
    "why is my cat staring at the wall",
    "can you microwave aluminum foil",
]

DDG_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
SEARXNG_GENERAL_ENGINES = os.environ.get("SEARXNG_GENERAL_ENGINES", "bing,mojeek,presearch")


def test_serper(queries):
    api_key = _get_provider_key("serper")
    if not api_key:
        return [{"query": q, "status": "no_api_key", "results": [], "time": 0} for q in queries]
    results = []
    for q in queries:
        start = time.time()
        try:
            resp = httpx.post(
                "https://google.serper.dev/search",
                json={"q": q, "num": 8},
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                timeout=15,
            )
            elapsed = time.time() - start
            resp.raise_for_status()
            data = resp.json()
            items = [
                {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
                for r in data.get("organic", [])[:8]
            ]
            results.append({"query": q, "status": "ok", "results": items, "time": elapsed})
        except Exception as e:
            results.append({"query": q, "status": str(e)[:80], "results": [], "time": time.time() - start})
    return results


def test_tavily(queries):
    api_key = _get_provider_key("tavily")
    if not api_key:
        return [{"query": q, "status": "no_api_key", "results": [], "time": 0} for q in queries]
    results = []
    for q in queries:
        start = time.time()
        try:
            resp = httpx.post(
                "https://api.tavily.com/search",
                json={"api_key": api_key, "query": q, "max_results": 8, "search_depth": "basic"},
                timeout=20,
            )
            elapsed = time.time() - start
            resp.raise_for_status()
            data = resp.json()
            items = [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:200]}
                for r in data.get("results", [])[:8]
            ]
            results.append({"query": q, "status": "ok", "results": items, "time": elapsed})
        except Exception as e:
            results.append({"query": q, "status": str(e)[:80], "results": [], "time": time.time() - start})
    return results


def test_searxng(queries):
    instance = _get_search_instance()
    results = []
    for q in queries:
        start = time.time()
        try:
            params = {
                "q": q, "format": "json", "language": "en",
                "safesearch": "1", "categories": "general",
            }
            if SEARXNG_GENERAL_ENGINES:
                params["engines"] = SEARXNG_GENERAL_ENGINES
            resp = httpx.get(
                f"{instance}/search", params=params,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SableBot/1.0)"},
                timeout=15,
            )
            elapsed = time.time() - start
            resp.raise_for_status()
            data = resp.json()
            items = [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
                for r in data.get("results", [])[:8] if r.get("url")
            ]
            unresponsive = data.get("unresponsive_engines", [])
            status = "ok" if items else f"empty (unresponsive: {unresponsive})"
            results.append({"query": q, "status": status, "results": items, "time": elapsed})
        except Exception as e:
            results.append({"query": q, "status": str(e)[:80], "results": [], "time": time.time() - start})
    return results


def test_duckduckgo(queries, throttle=4.0):
    results = []
    for q in queries:
        start = time.time()
        try:
            resp = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": q, "kl": "us-en"},
                headers={"User-Agent": DDG_BROWSER_UA},
                timeout=15,
            )
            elapsed = time.time() - start
            if resp.status_code == 202:
                results.append({"query": q, "status": "rate_limited", "results": [], "time": elapsed})
                time.sleep(throttle + 2)
                continue
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            items = []
            for item in soup.select(".result")[:8]:
                title_tag = item.select_one(".result__a")
                snippet_tag = item.select_one(".result__snippet")
                if title_tag:
                    href = title_tag.get("href", "")
                    if "uddg=" in href:
                        parsed = parse_qs(urlparse(href).query)
                        href = parsed.get("uddg", [href])[0]
                    items.append({
                        "title": title_tag.get_text(strip=True),
                        "url": href,
                        "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
                    })
            results.append({"query": q, "status": "ok", "results": items, "time": elapsed})
        except Exception as e:
            results.append({"query": q, "status": str(e)[:80], "results": [], "time": time.time() - start})
        time.sleep(throttle)
    return results


def score_provider(name, results):
    total = len(results)
    ok = sum(1 for r in results if r["status"] == "ok")
    has_results = sum(1 for r in results if r["status"] == "ok" and len(r["results"]) > 0)
    total_items = sum(len(r["results"]) for r in results)
    avg_results = total_items / max(has_results, 1)
    avg_time = sum(r["time"] for r in results) / max(total, 1)

    valid_urls = 0
    empty_snips = 0
    relevance_hits = 0
    for r in results:
        for item in r["results"]:
            p = urlparse(item["url"])
            if p.scheme in ("http", "https") and p.netloc and "localhost" not in p.netloc:
                valid_urls += 1
            if not item.get("snippet", "").strip():
                empty_snips += 1
            q_words = set(r["query"].lower().split())
            t_words = set(item["title"].lower().split())
            if q_words & t_words:
                relevance_hits += 1

    url_q = valid_urls / max(total_items, 1)
    snip_q = 1 - (empty_snips / max(total_items, 1))
    relevance = relevance_hits / max(total_items, 1)
    reliability = ok / max(total, 1)
    fill_rate = has_results / max(total, 1)
    volume = min(avg_results / 6.0, 1.0)
    speed = max(0, 1 - (avg_time / 12))

    composite = (
        reliability * 20 + fill_rate * 15 + volume * 15 +
        speed * 10 + url_q * 15 + snip_q * 10 + relevance * 15
    )

    return {
        "name": name, "composite": round(composite),
        "reliability": round(reliability * 100), "fill_rate": round(fill_rate * 100),
        "avg_results": round(avg_results, 1), "avg_time": round(avg_time, 2),
        "url_quality": round(url_q * 100), "snippet": round(snip_q * 100),
        "relevance": round(relevance * 100), "speed_score": round(speed * 100),
        "ok_queries": ok, "has_results": has_results, "total_items": total_items,
    }


def main():
    parser = argparse.ArgumentParser(description="Sable Search Engine Stress Test")
    parser.add_argument("--queries", type=int, default=20, help="Number of queries to test")
    parser.add_argument("--engines", nargs="+", default=["serper", "tavily", "searxng", "duckduckgo"])
    parser.add_argument("--output", type=str, default=None, help="Save JSON results to file")
    args = parser.parse_args()

    queries = QUERIES[:args.queries]
    all_results = {}
    testers = {
        "serper": test_serper,
        "tavily": test_tavily,
        "searxng": test_searxng,
        "duckduckgo": test_duckduckgo,
    }

    for engine in args.engines:
        if engine in testers:
            print(f"Testing {engine} ({len(queries)} queries)...")
            all_results[engine] = testers[engine](queries)

    # Score
    scores = []
    for engine in args.engines:
        if engine in all_results:
            scores.append(score_provider(engine, all_results[engine]))
    scores.sort(key=lambda x: x["composite"], reverse=True)

    # Print summary
    print("\n" + "=" * 80)
    print(f"{'RANK':<5}{'ENGINE':<14}{'SCORE':<7}{'RELI':<7}{'FILL':<7}{'AVG_R':<7}{'TIME':<7}{'URL':<7}{'SNIP':<7}{'RELEV':<7}")
    print("-" * 80)
    for i, s in enumerate(scores, 1):
        print(f"#{i:<4}{s['name'].upper():<14}{s['composite']:<7}{s['reliability']}%{'':<3}"
              f"{s['fill_rate']}%{'':<3}{s['avg_results']:<7}{s['avg_time']}s{'':<2}"
              f"{s['url_quality']}%{'':<3}{s['snippet']}%{'':<3}{s['relevance']}%")
    print("=" * 80)

    # Per-query table
    print(f"\n{'Query':<45}", end="")
    for e in args.engines:
        print(f"{e[:8]:<10}", end="")
    print("\n" + "-" * 90)
    for i, q in enumerate(queries):
        print(f"{q[:44]:<45}", end="")
        for e in args.engines:
            if e in all_results:
                r = all_results[e][i]
                if r["status"] == "ok":
                    print(f"{len(r['results'])} ({r['time']:.1f}s) ", end="")
                elif "rate_limited" in r["status"]:
                    print(f"{'BLOCKED':<10}", end="")
                else:
                    print(f"{'ERR':<10}", end="")
            else:
                print(f"{'—':<10}", end="")
        print()

    # Save results
    if args.output:
        output = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "scores": scores, "results": all_results}
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults saved to {args.output}")

    return scores


if __name__ == "__main__":
    main()
