# `engine/search/` — Multi-Provider Web Search

> [!INFO] Scope
> **2,528 lines across 10 files.** Provider table, ranking, and the browser-free HTTP engine were read directly; remaining files at function-contract level.

***

## Two search stacks, not one

| Stack | Entry | Transport |
|:--|:--|:--|
| **API stack** | `core.searxng_search_results()` | SearXNG JSON, or keyed providers |
| **HTTP ladder** | `browser_provider.py` | Direct HTTP against Google/Brave/Bing/DDG — **no browser launched** |

> [!NOTE] `browser_provider.py` is misleadingly named
> Its docstring: *"Multi-engine web search over plain HTTP. No browser is ever launched."* It uses `curl_cffi` with `impersonate="chrome124"` to get browser-shaped TLS without a real Chromium.

***

## Module map

| File | Lines | Role |
|:--|--:|:--|
| `providers.py` | 579 | SearXNG, Brave, DDG, Google PSE, Tavily, Serper implementations |
| `browser_provider.py` | 449 | The HTTP multi-engine ladder + RRF fusion |
| `core.py` | 336 | Orchestrators, config, cache invalidation |
| `image_providers.py` | 327 | Tavily/Serper/Brave/Google PSE image search |
| `sable_provider.py` | 304 | Desktop-shell bridge via a marker file |
| `config.py` | 179 | Settings loader, key rotation |
| `ranking.py` | 139 | Relevance + source quality + recency scoring |
| `query.py` | 132 | Query enhancement, entity extraction |
| `cache.py` | 53 | LRU cache |
| `__init__.py` | 30 | Package exports |

***

## `providers.py` — the provider table

```python
PROVIDER_INFO = {
    "searxng":    ("SearXNG",      False, True),   # (label, needs_key, is_default)
    "brave":      ("Brave Search", True,  False),
    "duckduckgo": ("DuckDuckGo",   False, False),
    "google_pse": ("Google PSE",   True,  False),
    "tavily":     ("Tavily",       True,  False),
    "serper":     ("Serper",       True,  False),
    "disabled":   ("Disabled",     False, False),
}
```

The tuple is `(display label, requires API key, is the default)`. SearXNG is the default because it's self-hosted and keyless.

### DuckDuckGo needs a Chrome UA and throttling

```python
DDG_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
_DDG_MIN_INTERVAL = 4.0
_last_ddg_request_ts = 0.0
```

> [!IMPORTANT] Two comments record hard-won facts
> *"DuckDuckGo's html endpoint returns a 202 anomaly/challenge page for bot, curl and Firefox UAs — only Chrome-style UAs get real results."*
> *"Throttle DDG requests — bursts trip their per-IP anomaly CAPTCHA."* 4 seconds between calls, module-level timestamp.

### SearXNG engine selection

```python
_GENERAL_ENGINES = os.environ.get("SEARXNG_GENERAL_ENGINES", "bing,mojeek,presearch")
_NEWS_HINTS = ("news", "nyheter", "headlines", "breaking", "latest", "today", "idag")
```

The upstream engines are env-configurable. `_NEWS_HINTS` includes Swedish terms (`nyheter`, `idag`) — a hint that the default SearXNG config is multilingual.

***

## `browser_provider.py` — the HTTP ladder with RRF

```python
IMPERSONATE       = "chrome124"
TIMEOUT           = 12
PER_ENGINE_LIMIT  = 10
MIN_RESULTS       = 2
RRF_K             = 10
AGREEMENT_STEP    = 0.3
PRIORITY_STEP     = 0.7
ENGINE_ORDER      = ["google", "brave", "bing", "ddg"]
```

### RRF fusion — the interesting part

Reciprocal Rank Fusion: each engine returns a ranked list; a document's fused score is the sum of `1/(RRF_K + rank)` across all engines that returned it. A page found by three engines outranks a page found once by any single engine.

Two additional weights:

| Weight | Effect |
|:--|:--|
| `AGREEMENT_STEP = 0.3` | Bonus per additional engine that returned the same URL |
| `PRIORITY_STEP = 0.7` | Engine-order bonus — Google > Brave > Bing > DDG |

```python
def _ladder() -> List[float]:
    return [round(PRIORITY_STEP ** i, 4) for i in range(len(ENGINE_ORDER))]
```

> [!TIP] `PRIORITY_STEP ** i` is geometric decay, not linear
> Google gets `1.0`, Brave `0.7`, Bing `0.49`, DDG `0.343`. A linear ladder would make the weakest engine too competitive. Geometric decay matches how much trust actually differs between them.

### `_norm_url()` — dedup key with a deliberate exception

```python
def _norm_url(u: str) -> str:
    """Key for spotting the same page across engines. The query is kept on
    purpose -- YouTube video ids live there."""
    p = urlparse(u)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")
```

> [!IMPORTANT] Query strings are normally stripped for dedup — this one isn't
> `youtube.com/watch?v=abc123` and `youtube.com/watch?v=xyz789` are *different videos* but identical in host+path. Dropping the query would collapse every YouTube result into one.

### `_clean_url()` — Bing's tracking redirect

```python
if engine == "bing" and "bing.com/ck/a" in href:
    raw = parse_qs(urlparse(href).query).get("u", [""])[0]
    if raw.startswith("a1"):
        raw = raw[2:]
    return base64.urlsafe_b64decode(_b64_fix(raw)).decode()
```

Bing wraps every result in a base64-encoded redirect with an `a1` prefix. Unwrapping it is what lets cross-engine dedup work at all — otherwise Bing results never match Google's.

`_b64_fix(s)` adds `=` padding to the right multiple of 4; Bing strips it.

***

## `core.py` — orchestrators

| Function | Does |
|:--|:--|
| `get_search_config()` / `update_search_config(api_key, **kwargs)` | Settings read/write |
| `searxng_search_results(query, count, time_filter)` | Primary search |
| `comprehensive_web_search(...)` | Filtered wrapper |
| `image_search_results(query, count)` | Image path |
| `image_search_for_provider(provider, ...)` / `image_search_traced(...)` | Provider-specific with tracing |
| `invalidate_search_cache(query=None)` | Cache drop |
| `_is_secret_key(name)` | Redact keys from config responses |
| `search` | Public alias for `searxng_search_results` |

### `comprehensive_web_search()` is Phase 1 only

```python
def comprehensive_web_search(query, max_pages=3, max_workers=4, time_filter=None,
                             domain_whitelist=None, domain_blacklist=None, ...):
    results = searxng_search_results(query, count=max_pages * 3, time_filter=time_filter)
    # domain whitelist/blacklist filters
    return results[:max_pages]
```

> [!NOTE] The docstring is explicit
> *"Phase 1: returns raw search results without content fetching. Content extraction stays in `online_search.py`."* Despite accepting `max_workers`, `content_type`, `language`, and `min_content_length`, this function fetches **no page content**. Those parameters are accepted for signature compatibility and largely unused. Fetching lives in the `online_search` tool.

It over-fetches (`max_pages * 3`) then truncates to `max_pages` — correct, because the domain filters can remove most of a result set.

***

## `ranking.py` — scoring

```python
_AGE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")

def recency_score(age_str, now=None) -> float
def rank_search_results(query, results) -> List[dict]
```

Domain sets drive quality scoring:

| Set | Purpose |
|:--|:--|
| `_LOW_VALUE_NEWS_DOMAINS` | Downweight content farms |
| `_TRUSTED_NEWS_DOMAINS` | Upweight established outlets |
| `_NEWS_HINTS` / `_SPORTS_HINTS` + `_SPORTS_HINT_RE` | Query-type detection |

`_has_word(text, term)` does word-boundary matching, so `"news"` doesn't match `"newsworthy"` accidentally... though it deliberately *does* — the hint lists are for query classification, not exclusion.

***

## `query.py` — enhancement

| Function | Does |
|:--|:--|
| `_detect_question_type(query)` | Who/what/when/where/how |
| `_extract_entities(query)` | Named-entity extraction |
| `_split_multi_part(query)` | Break compound questions |
| `_extract_site_filter(query)` | Pull `site:` out |
| `_boost_entities_in_query(base, entities)` | Re-weight the query |
| `enhance_query(original)` → `(query, filter)` | Public entry |
| `build_enhanced_query(query, time_filter)` | Apply time filter |
| `_is_news_query(query)` | Classification |
| `_cache_duration_for_query(query)` | **Cache TTL varies by query type** |

> [!TIP] `_cache_duration_for_query` is the subtle one
> A news query gets a short TTL; a definitional query gets a long one. Caching both for the same duration would either serve stale headlines or waste re-fetches on stable facts.

***

## `config.py` — key rotation

```python
_PRIMARY_SETTINGS  = system/settings.json
_FALLBACK_SETTINGS = tools/online_search/scripts/settings.json
_SAFESEARCH_LEVELS = ("strict", "moderate", "off")
```

| Function | Does |
|:--|:--|
| `_resolve_provider_keys(provider)` | Collect all keys for a provider |
| `get_provider_keys(provider)` | Public |
| `_get_provider_key(provider)` | Current key |
| `reset_key_rotation(provider=None)` | Reset the rotation index |

> [!NOTE] Key rotation, not key fallback
> Multiple keys per provider rotate on failure. `_SETTINGS_FIELD_MAP` and `_ENV_VAR_MAP` let settings-file keys and env-var keys coexist — env wins.

***

## `sable_provider.py` — the desktop bridge

```python
MARKER = Path.home() / ".cache" / "sable" / "search-bridge.json"
TIMEOUT = 45.0
```

Reads a marker file the desktop shell writes, then POSTs to the endpoint it names. `sable_available()` checks the marker exists and is fresh.

| Function | Does |
|:--|:--|
| `sable_multi_search(query, engines)` / `..._traced` | Multi-engine via desktop |
| `sable_image_search(query, count, engines)` / `..._traced` | Image via desktop |
| `_shape_web(data)` / `_shape_image(data)` | Normalize to the common result shape |
| `_report_summary(report)` | Human-readable engine report |
| `_clean_report(raw)` | Parse `EngineReport` list |

> [!TIP] `_traced` variants everywhere
> Every search function has a `_traced` twin returning per-engine diagnostics. When a search returns nothing, the traced version tells you *which engine failed and why* — the untraced one just returns `[]`.

***

## `image_providers.py`

```python
IMAGE_PROVIDERS = { ... }
def has_image_provider(provider) -> bool
def image_provider_unavailable_reason(provider) -> str
def image_search_for(provider, query, count=10) -> List[dict]
```

Four implementations: `tavily_image_search`, `serper_image_search`, `brave_image_search`, `google_pse_image_search`. All normalize through `_norm(title, image, thumb, page)`.

`image_provider_unavailable_reason()` returns a **string explaining why** a provider can't be used (missing key, unsupported) rather than a bare `False` — so the UI can tell the user what to fix.

***

## Gotchas

> [!WARNING] Four things worth knowing
> 1. **`comprehensive_web_search()` does not fetch content.** Half its parameters are unused. The name suggests otherwise.
> 2. **`browser_provider.py` launches no browser.** It's HTTP with browser-shaped TLS. Confusing name, good reason.
> 3. **DDG is rate-limited to one call per 4 s.** Bursting gets a 202 challenge page, which parses as zero results, not an error.
> 4. **`_norm_url()` keeps query strings on purpose.** Changing it to strip them would collapse all YouTube results into one.
