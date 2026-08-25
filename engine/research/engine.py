
"""Recursive deep-research engine (fractal tree).

The question spawns sub-topics; each sub-topic either splits into deeper
sub-topics (until `max_depth`) or becomes *atomic* and is researched directly
(web search -> fetch pages -> extract findings).  Findings then synthesize
back UP the tree into a final report.

The engine streams graph events (nodes + status) so the frontend can draw a
live horizontal tree: root (left) -> topics -> pages -> report (right).
Circles are yellow while fetching/reading, green on success, red on failure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from engine.config import OUTPUT_ROOT
from engine.research.llm import qwen_complete, extract_json
from connectors import resolve_backend, get_connector

logger = logging.getLogger("sable.research.engine")

# SEARCH_SCRIPT lives in the project tree, not under OUTPUT_ROOT
from engine.config import _ROOT as _PROJECT_ROOT
SEARCH_SCRIPT = _PROJECT_ROOT / "tools" / "online_search" / "scripts" / "online_search.py"

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _date_context() -> str:
    now = datetime.now().astimezone()
    return (
        f"Today's date is {now.strftime('%B %d, %Y')} ({now.strftime('%Y-%m-%d')}). "
        f"When a query needs a year or refers to 'latest'/'current', use {now.strftime('%Y')}.\n\n"
    )


# ── Prompts ──────────────────────────────────────────────────────────────────

EXPAND_PROMPT = """You are a research strategist decomposing a question into angles.

**Question:** {question}

Return ONLY a JSON object: {{"subtopics": ["...", "..."]}} with 3-5 distinct,
high-level angles that together fully cover the question.
"""

SPLIT_PROMPT = """You are deciding whether a research topic needs further decomposition.

**Overall question:** {question}
**Topic:** {topic}
**Current depth:** {depth} of {max_depth}

If the topic is still broad (covers multiple distinct aspects) AND depth < max_depth,
split it into 2-4 narrower sub-topics. If it is specific/atomic enough to research
directly, do NOT split.

Return ONLY a JSON object: {{"split": true, "subtopics": ["..."]}}  OR  {{"split": false}}
"""

QUERY_PROMPT = """You are planning web searches for a specific research topic.

**Overall question:** {question}
**Topic to research:** {topic}

Generate {num_queries} focused search queries that will find authoritative sources
for THIS topic. Phrase them unambiguously — always include the domain context
(e.g. "neural network") so generic words like "build" can't drift to unrelated results.
Return ONLY a JSON array of query strings.
"""

EXTRACT_PROMPT = """You are extracting relevant findings from a web page for a research topic.

**Research topic:** {topic}

**Page content:**
{content}

Extract the key facts, data points, and insights relevant to the topic. Prefer
concrete, quotable claims. Return ONLY a JSON array:
[{{"finding": "...", "source_url": "...", "source_title": "..."}}]
If nothing relevant, return [].
"""

SYNTH_PROMPT = """You are synthesizing research on a sub-topic from its parts.

**Topic:** {topic}
**Child material:**
{children}

Write a concise but complete synthesis (a few paragraphs) of what is known about
this topic, integrating the child material and noting disagreements or gaps.
Write ONLY the synthesis text.
"""

FINAL_PROMPT = """Write a **long, detailed, comprehensive** research report answering this question.

**Question:** {question}
**Structured synthesis:**
{synthesis}

**All collected findings:**
{findings}

Requirements:
- MINIMUM 1500 words — thorough, magazine-quality
- Use ## headings and ### subheadings mirroring the topic structure
- Multiple detailed paragraphs per section, not just bullets
- Synthesize and analyze — explain WHY things matter, draw comparisons
- Include specific data points and statistics from the evidence
- Cite sources inline as [title](url)
- Add a brief executive summary at the top
- End with a clear conclusion that directly answers the question
"""

ALTERNATIVE_URLS_PROMPT = """You are a research assistant. During research, several web pages either failed to fetch or contained no relevant information for the topic.

**Research question:** {question}
**Current topic focus:** {topic}

**Problem URLs:**
{failed_urls}

Suggest {num_alternatives} alternative URLs that are likely to contain useful, on-topic information.
Choose well-known, reliable sources (Wikipedia, official docs, reputable tutorials/news/research sites).
Return ONLY a JSON array of URL strings. Example: ["https://en.wikipedia.org/wiki/Example", "https://docs.example.org/page"]
"""

SELECT_PAGES_PROMPT = """You are choosing which web pages to read for a research topic.

**Research question:** {question}
**Topic to research:** {topic}

**Candidate pages:**
{candidates}

Pick the {num_pages} pages most likely to contain relevant, authoritative information for THIS topic.
Prefer primary/authoritative sources (Wikipedia, official docs, reputable tutorials) and skip obvious junk
(homepages, unrelated products, login/account pages, off-topic results).
If NONE of the candidates are relevant to the topic, return an empty array: []
Return ONLY a JSON array of the candidate numbers you pick. Example: [1, 4, 7]
"""


class DeepResearcher:
    """Recursive fractal-tree research engine with live graph events."""

    def __init__(
        self,
        question: str,
        model: str | None = None,
        models: Optional[list[str]] = None,
        accounts: Optional[list[str]] = None,
        max_depth: int = 3,
        max_time: int = 1500,
        pages_per_topic: int = 3,
        max_topics: int = 14,
        queries_per_topic: int = 3,
        concurrency: int = 3,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        self.question = question
        # Ordered fallback lists. Legacy single `model` seeds the model list.
        self.models = [m for m in (models or []) if m] or ([model] if model else [])
        self.accounts = [a for a in (accounts or []) if a]
        self._attempt_idx = 0
        self._active_model = self.models[0] if self.models else "default"
        self._active_account = self.accounts[0] if self.accounts else "default"
        self.model = self.models[0] if self.models else model
        self.max_depth = max(1, min(5, int(max_depth)))
        self.max_time = max_time
        self.pages_per_topic = pages_per_topic
        self.max_topics = max_topics
        self.queries_per_topic = queries_per_topic
        self._progress = progress_callback
        self._cancelled = False
        self._start = time.time()

        self.nodes: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self._sem = asyncio.Semaphore(concurrency)
        self._log_path: Optional[Path] = None
        self._trace_path: Optional[Path] = None
        self._trace_lock = asyncio.Lock()
        self.findings: list[dict[str, Any]] = []
        self.sources: list[dict[str, str]] = []
        # Simple fetch summary log: [{site, url, success, status_code}]
        self._fetch_log: list[dict[str, Any]] = []
        # Account/model health tracker: key -> consecutive failure count.
        # Keys are "model:account" for Qwen or "backend:model" for API models.
        # After _HEALTH_FAIL_THRESHOLD consecutive failures, skip that combo.
        self._health_failures: dict[str, int] = {}
        self._HEALTH_FAIL_THRESHOLD = 3

    def _health_key(self, model: Optional[str], account: Optional[str]) -> str:
        backend = resolve_backend(model) if model else None
        if backend:
            return f"{backend}:{model}"
        return f"qwen:{account or 'default'}"

    def _is_healthy(self, model: Optional[str], account: Optional[str]) -> bool:
        return self._health_failures.get(self._health_key(model, account), 0) < self._HEALTH_FAIL_THRESHOLD

    def _record_success(self, model: Optional[str], account: Optional[str]) -> None:
        key = self._health_key(model, account)
        if key in self._health_failures:
            logger.info("health reset | %s was at %d failures", key, self._health_failures[key])
        self._health_failures[key] = 0

    def _record_failure(self, model: Optional[str], account: Optional[str]) -> None:
        key = self._health_key(model, account)
        self._health_failures[key] = self._health_failures.get(key, 0) + 1
        if self._health_failures[key] >= self._HEALTH_FAIL_THRESHOLD:
            logger.warning("health circuit-breaker OPEN | %s after %d consecutive failures",
                           key, self._health_failures[key])

    def cancel(self) -> None:
        self._cancelled = True

    async def _trace(self, stage: str, **fields: Any) -> None:
        """Append one structured entry to the live trace log (JSON-lines)."""
        if not self._trace_path:
            return
        entry = {"ts": datetime.now().astimezone().isoformat(), "elapsed_s": round(time.time() - self._start, 2), "stage": stage}
        entry.update(fields)
        # Truncate huge fields so the log stays readable mid-run.
        for k in ("prompt", "response", "content"):
            if k in entry and isinstance(entry[k], str) and len(entry[k]) > 4000:
                entry[k] = entry[k][:4000] + f"\n… [truncated, {len(entry[k])} chars total]"
        line = json.dumps(entry, ensure_ascii=False, default=str)
        async with self._trace_lock:
            try:
                with open(self._trace_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                logger.warning("trace write failed: %s", e)

    # ── node helpers ─────────────────────────────────────────────────────────
    def _new_id(self) -> str:
        self._counter += 1
        return f"n{self._counter}"

    def add_node(self, parent: Optional[str], depth: int, kind: str, label: str, status: str = "pending") -> dict[str, Any]:
        nid = self._new_id()
        node = {"id": nid, "parent": parent, "depth": depth, "kind": kind,
                "label": label, "status": status, "children": []}
        self.nodes[nid] = node
        if parent and parent in self.nodes:
            self.nodes[parent]["children"].append(nid)
        return node

    async def _emit_node(self, node: dict[str, Any]) -> None:
        await self._emit({"type": "graph_node", "id": node["id"], "parent": node["parent"],
                          "depth": node["depth"], "kind": node["kind"],
                          "label": node["label"], "status": node["status"]})

    async def _set_status(self, node: dict[str, Any], status: str) -> None:
        node["status"] = status
        await self._emit({"type": "graph_status", "id": node["id"], "status": status})

    async def _emit(self, payload: dict[str, Any]) -> None:
        if not self._progress:
            return
        payload.setdefault("phase", "working")
        payload.setdefault("elapsed", int(time.time() - self._start))
        try:
            await self._progress(payload)
        except Exception:
            pass

    async def _emit_progress(self, **kw: Any) -> None:
        topics = sum(1 for n in self.nodes.values() if n["kind"] == "topic")
        pages = sum(1 for n in self.nodes.values() if n["kind"] == "page")
        payload = {"type": "progress", "topics": topics, "pages": pages,
                   "sources": len(self.sources), "findings": len(self.findings),
                   "model": self._active_model, "account": self._active_account}
        payload.update(kw)
        await self._emit(payload)

    async def _complete(self, prompt: str, timeout: int = 180, system_prefix: str = "") -> str:
        """LLM completion with model + account fallback rotation.

        Builds an ordered list of (model, account) attempts. API models
        (deepseek/gemini/…) route through their connector — account is ignored,
        exactly like Context Pass. Qwen models get one attempt per browser-data
        account. On failure we rotate to the next attempt and remember whichever
        combo worked, so the next call starts there.
        """
        logger.info("_complete called | prompt_len=%d timeout=%d models=%s accounts=%s",
                     len(prompt), timeout, self.models, self.accounts)
        models = self.models or [None]
        accounts = self.accounts or [None]
        attempts: List[Tuple[Optional[str], Optional[str]]] = []
        for model in models:
            if model and resolve_backend(model):
                attempts.append((model, None))   # API model → keys, not profiles
            else:
                for account in accounts:
                    attempts.append((model, account))
        if not attempts:
            attempts = [(None, None)]
        # Filter out circuit-broken combos, but keep at least one attempt.
        healthy = [(m, a) for m, a in attempts if self._is_healthy(m, a)]
        pool = healthy if healthy else attempts  # if ALL are broken, try anyway
        # Start from the last-working attempt so we don't re-try dead combos.
        k = self._attempt_idx % len(pool)
        ordered = pool[k:] + pool[:k]
        skipped = len(attempts) - len(pool)
        if skipped:
            logger.info("_complete | skipping %d circuit-broken combos", skipped)
            await self._emit_progress(status=f"skipping {skipped} unhealthy model/account combo(s)")
        last_err: Optional[Exception] = None
        total = len(ordered)
        for i, (model, account) in enumerate(ordered):
            try:
                logger.debug("dispatching attempt %d/%d model=%s account=%s", i + 1, total, model, account)
                text = await self._dispatch(model, account, prompt, timeout, system_prefix)
                self._attempt_idx = (k + i) % len(pool)
                self._active_model = model or "default"
                self._active_account = account or (resolve_backend(model) or "default")
                self._record_success(model, account)
                logger.info("_complete success | model=%s account=%s attempt=%d/%d response_len=%d",
                            model, account, i + 1, total, len(text))
                await self._trace("llm_complete", model=model, account=account, attempt=f"{i+1}/{total}",
                                  prompt=prompt[:2000], response=text[:2000], response_len=len(text))
                return text
            except Exception as e:
                last_err = e
                self._record_failure(model, account)
                logger.warning("research llm attempt %d/%d failed (model=%s account=%s): %s",
                               i + 1, total, model, account, e)
                if i + 1 < total:
                    await self._emit_progress(
                        status=f"retrying ({i + 1}/{total}) {model or 'default'}/{account or 'default'}: {str(e)[:48]}")
        raise last_err or RuntimeError("all model/account fallbacks exhausted")

    async def _dispatch(self, model: Optional[str], account: Optional[str],
                        prompt: str, timeout: int, system_prefix: str) -> str:
        """Route one completion call by backend, mirroring Context Pass.

        All paths enforce a wall-clock timeout so no single call can hang
        the entire research run.
        """
        backend = resolve_backend(model) if model else None
        logger.debug("_dispatch | model=%s backend=%s account=%s timeout=%d", model, backend, account, timeout)
        if backend:
            connector = get_connector(backend, model_id=model)
            logger.info("routing to %s connector for model=%s (timeout=%ds)", backend, model, timeout)
            try:
                result = await asyncio.wait_for(
                    connector.chat(message=prompt, model=model, thinking_mode="fast"),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.error("%s connector timed out after %ds for model=%s", backend, timeout, model)
                raise RuntimeError(f"{backend} connector timed out ({timeout}s)")
            answer = (result or {}).get("answer", "").strip()
            if not answer:
                err = (result or {}).get("error") or "empty response"
                logger.error("%s connector returned empty for model=%s: %s", backend, model, err)
                raise RuntimeError(err)
            logger.info("%s connector ok | model=%s response_len=%d", backend, model, len(answer))
            return answer
        logger.info("routing to qwen_complete | model=%s account=%s", model, account)
        return await qwen_complete(prompt, model=model, timeout=timeout,
                                   system_prefix=system_prefix, account=account)

    def _time_exceeded(self) -> bool:
        return (time.time() - self._start) > self.max_time

    def _stop(self) -> bool:
        return self._cancelled or self._time_exceeded()

    # ── web helpers ──────────────────────────────────────────────────────────
    async def _run_search_script(self, args: list[str], timeout: int = 90) -> tuple[bool, str]:
        cmd = ["python3", str(SEARCH_SCRIPT)] + args
        logger.info("running search script | timeout=%d args=%s", timeout, args)
        await self._trace("search_script_start", timeout=timeout, args=" ".join(args[:10]))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            ok = proc.returncode == 0
            decoded = out.decode("utf-8", errors="replace")
            if not ok:
                stderr_text = err.decode("utf-8", errors="replace")[:500]
                logger.warning("search script failed | rc=%d stderr=%s", proc.returncode, stderr_text)
                await self._trace("search_script_fail", rc=proc.returncode, stderr=stderr_text)
            else:
                logger.info("search script ok | output_len=%d", len(decoded))
                await self._trace("search_script_ok", output_len=len(decoded))
            return ok, decoded
        except asyncio.TimeoutError:
            logger.error("search script timed out after %ds", timeout)
            await self._trace("search_script_timeout", timeout=timeout)
            try:
                proc.kill()
            except Exception:
                pass
            return False, "search timed-out"
        except Exception as e:
            logger.error("search script exception: %s", e)
            await self._trace("search_script_exception", error=str(e))
            return False, str(e)

    async def _search(self, queries: list[str], topic: str) -> list[dict[str, Any]]:
        logger.info("_search | topic=%r queries=%d", topic, len(queries))
        args = ["--json", "--search-only", "--max-results", "8"] + queries
        if self._log_path:
            args += ["--research-log", str(self._log_path), "--topic", topic]
        ok, out = await self._run_search_script(args)
        results: list[dict[str, Any]] = []
        if not ok:
            logger.warning("_search failed for topic=%r", topic)
            return results
        try:
            data = json.loads(out)
        except Exception:
            return results
        seen = {s.get("url") for s in self.sources}
        for item in data.get("items", []):
            if not item.get("ok"):
                continue
            for r in item.get("results", []):
                url = r.get("url", "")
                if url and url not in seen:
                    results.append({"title": r.get("title", ""), "url": url, "snippet": r.get("snippet", "")})
                    seen.add(url)
        return results

    def _domain_from_url(self, url: str) -> str:
        """Extract just the domain name from a URL."""
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc or url
        except Exception:
            return url

    async def _fetch_one(self, url: str, topic: str) -> Optional[dict[str, Any]]:
        logger.info("_fetch_one | url=%s topic=%r", url, topic)
        args = ["--json", "--max-chars", "12000"]
        if self._log_path:
            args += ["--research-log", str(self._log_path), "--topic", topic]
        args += ["--fetch-urls", url]
        await self._trace("fetch_start", url=url, topic=topic)
        ok, out = await self._run_search_script(args, timeout=15)
        if not ok:
            logger.warning("_fetch_one failed | url=%s", url)
            await self._trace("fetch_fail", url=url, reason="script_failed")
            self._fetch_log.append({"site": self._domain_from_url(url), "url": url, "success": False, "status_code": 0})
            return None
        try:
            data = json.loads(out)
        except Exception:
            await self._trace("fetch_fail", url=url, reason="json_parse_error")
            self._fetch_log.append({"site": self._domain_from_url(url), "url": url, "success": False, "status_code": 0})
            return None
        for page in data.get("pages", []):
            status_code = page.get("status_code", 0)
            if page.get("success") and page.get("content"):
                content_len = len(page.get("content", ""))
                await self._trace("fetch_ok", url=url, title=page.get("title", ""), content_len=content_len,
                                  content=page.get("content", "")[:2000])
                self._fetch_log.append({"site": self._domain_from_url(url), "url": url, "success": True, "status_code": status_code})
                return page
            else:
                self._fetch_log.append({"site": self._domain_from_url(url), "url": url, "success": False, "status_code": status_code})
        await self._trace("fetch_fail", url=url, reason="no_successful_page")
        return None

    # ── LLM helpers ──────────────────────────────────────────────────────────
    async def _llm_json(self, prompt: str, timeout: int = 120) -> Any:
        """LLM call that returns parsed JSON. Retries once with extended timeout
        on failure before giving up, so transient errors don't silently degrade
        the research tree."""
        logger.debug("_llm_json | prompt_len=%d timeout=%d", len(prompt), timeout)
        full_prompt = _date_context() + prompt
        for attempt in range(2):
            try:
                t = timeout if attempt == 0 else min(timeout * 2, 300)
                text = await self._complete(full_prompt, timeout=t)
                parsed = extract_json(text)
                if parsed is not None:
                    logger.debug("_llm_json ok | attempt=%d parsed_type=%s", attempt + 1, type(parsed).__name__)
                    return parsed
                # Got a response but couldn't parse JSON — retry once
                logger.warning("_llm_json | attempt=%d got unparseable response (len=%d)", attempt + 1, len(text))
            except Exception as e:
                logger.warning("_llm_json | attempt=%d failed: %s", attempt + 1, e)
                if attempt == 0:
                    await self._emit_progress(status=f"llm_json retry after failure: {str(e)[:40]}")
        logger.warning("_llm_json | all attempts exhausted, returning None")
        return None

    async def _expand_root(self) -> list[str]:
        logger.info("_expand_root | question=%r", self.question)
        prompt = EXPAND_PROMPT.format(question=self.question)
        await self._trace("expand_root_start", question=self.question, prompt=prompt)
        parsed = await self._llm_json(prompt)
        if isinstance(parsed, dict) and isinstance(parsed.get("subtopics"), list):
            topics = [s for s in parsed["subtopics"] if isinstance(s, str) and s.strip()][:5]
            logger.info("_expand_root | got %d subtopics: %s", len(topics), topics)
            await self._trace("expand_root_ok", subtopics=topics)
            return topics
        logger.warning("_expand_root | parse failed, falling back to question as single topic")
        await self._trace("expand_root_fail", raw_response=str(parsed)[:500])
        return [self.question]

    async def _maybe_split(self, topic: str, depth: int) -> Optional[list[str]]:
        logger.debug("_maybe_split | topic=%r depth=%d/%d", topic, depth, self.max_depth)
        if depth >= self.max_depth:
            logger.debug("_maybe_split | max depth reached, no split")
            return None
        if len([n for n in self.nodes.values() if n["kind"] == "topic"]) >= self.max_topics:
            logger.debug("_maybe_split | max topics (%d) reached, no split", self.max_topics)
            return None
        parsed = await self._llm_json(SPLIT_PROMPT.format(
            question=self.question, topic=topic, depth=depth, max_depth=self.max_depth))
        if isinstance(parsed, dict) and parsed.get("split") and isinstance(parsed.get("subtopics"), list):
            subs = [s for s in parsed["subtopics"] if isinstance(s, str) and s.strip()]
            if subs:
                logger.info("_maybe_split | splitting %r into %d subtopics", topic, len(subs[:4]))
                return subs[:4]
        logger.debug("_maybe_split | no split for %r", topic)
        return None

    async def _gen_queries(self, topic: str) -> list[str]:
        logger.debug("_gen_queries | topic=%r num=%d", topic, self.queries_per_topic)
        prompt = QUERY_PROMPT.format(question=self.question, topic=topic, num_queries=self.queries_per_topic)
        await self._trace("gen_queries_start", topic=topic, prompt=prompt)
        parsed = await self._llm_json(prompt)
        if isinstance(parsed, list):
            queries = [q for q in parsed if isinstance(q, str) and q.strip()][: self.queries_per_topic]
            logger.info("_gen_queries | generated %d queries for %r", len(queries), topic)
            await self._trace("gen_queries_ok", topic=topic, queries=queries)
            return queries
        logger.warning("_gen_queries | parse failed, using topic as query")
        await self._trace("gen_queries_fail", topic=topic, raw_response=str(parsed)[:500])
        return [topic]

    async def _reformulate_queries(self, topic: str, original_queries: list[str]) -> list[str]:
        """Reformulate search queries when the original set returned zero results.
        Asks the LLM to try different angles, synonyms, or broader/narrower terms."""
        logger.info("_reformulate_queries | topic=%r original=%d", topic, len(original_queries))
        prompt = (
            f"The following search queries returned ZERO results for research topic:\n"
            f"Topic: {topic}\n"
            f"Original queries: {original_queries}\n\n"
            f"Generate {self.queries_per_topic} NEW alternative search queries that approach "
            f"this topic from different angles. Use synonyms, broader terms, related concepts, "
            f"or rephrased questions. Return a JSON array of strings only."
        )
        parsed = await self._llm_json(prompt, timeout=60)
        if isinstance(parsed, list):
            queries = [q for q in parsed if isinstance(q, str) and q.strip()][: self.queries_per_topic]
            if queries:
                logger.info("_reformulate_queries | got %d reformulated queries", len(queries))
                return queries
        logger.warning("_reformulate_queries | failed, returning originals")
        return original_queries

    async def _suggest_alternatives(self, failed_urls: list[str], topic: str) -> list[str]:
        """Ask the LLM to suggest replacement URLs for ones that failed to fetch."""
        logger.info("_suggest_alternatives | topic=%r failed_count=%d", topic, len(failed_urls))
        want = min(len(failed_urls), self.pages_per_topic)
        prompt = ALTERNATIVE_URLS_PROMPT.format(
            question=self.question,
            topic=topic,
            failed_urls="\n".join(failed_urls),
            num_alternatives=want,
        )
        parsed = await self._llm_json(prompt, timeout=90)
        if isinstance(parsed, list):
            valid = [
                u for u in parsed
                if isinstance(u, str) and u.startswith("http")
            ]
            # Don't re-suggest URLs already in our sources
            known = {s.get("url") for s in self.sources}
            valid = [u for u in valid if u not in known]
            logger.info("_suggest_alternatives | got %d valid alternatives", len(valid))
            return valid[: want]
        logger.warning("_suggest_alternatives | parse failed for topic=%r", topic)
        return []

    async def _select_pages(self, topic: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """LLM picks the best pages to fetch from the candidate pool."""
        logger.info("_select_pages | topic=%r candidates=%d", topic, len(candidates))
        lines = []
        for i, c in enumerate(candidates, 1):
            lines.append(f"[{i}] {c.get('title', '')} — {c['url']}")
            if c.get("snippet"):
                lines.append(f"    {c['snippet'][:200]}")
        num = min(self.pages_per_topic, len(candidates))
        prompt = SELECT_PAGES_PROMPT.format(
            question=self.question, topic=topic,
            candidates="\n".join(lines), num_pages=num,
        )
        await self._trace("select_pages_start", topic=topic, candidates=len(candidates))
        parsed = await self._llm_json(prompt, timeout=90)
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        if isinstance(parsed, list):
            for v in parsed:
                try:
                    idx = int(v)
                except Exception:
                    continue
                if 1 <= idx <= len(candidates):
                    c = candidates[idx - 1]
                    if c["url"] not in seen:
                        seen.add(c["url"])
                        selected.append(c)
            if not selected:
                # Model saw the pool and judged nothing relevant — trust it, retry path will suggest URLs
                logger.info("_select_pages | model judged whole pool irrelevant for %r", topic)
                await self._trace("select_pages_empty", topic=topic)
                return []
        if not selected:
            logger.warning("_select_pages | parse failed, falling back to top %d", self.pages_per_topic)
            selected = candidates[: self.pages_per_topic]
        await self._trace("select_pages_ok", topic=topic, picked=[c["url"] for c in selected])
        return selected[: self.pages_per_topic]

    async def _extract(self, topic: str, page: dict[str, Any]) -> list[dict[str, Any]]:
        url = page.get("url", "")
        content = page.get("content", "")[:12000]
        logger.info("_extract | topic=%r url=%s content_len=%d", topic, url, len(content))
        prompt = EXTRACT_PROMPT.format(topic=topic, content=content)
        await self._trace("extract_start", topic=topic, url=url, content_len=len(content))
        try:
            text = await self._complete(prompt, timeout=120)
            parsed = extract_json(text)
            if isinstance(parsed, list):
                out = []
                for f in parsed:
                    if isinstance(f, dict) and f.get("finding"):
                        f.setdefault("source_url", url)
                        f.setdefault("source_title", page.get("title", ""))
                        out.append(f)
                logger.info("_extract | got %d findings from %s", len(out), url)
                await self._trace("extract_ok", topic=topic, url=url, findings_count=len(out),
                                  findings=[f.get("finding", "")[:200] for f in out])
                return out
            logger.warning("_extract | unexpected parse type=%s for %s", type(parsed).__name__, url)
            await self._trace("extract_fail", topic=topic, url=url, reason="bad_parse_type", parse_type=type(parsed).__name__)
        except Exception as e:
            logger.warning("_extract failed | url=%s error=%s", url, e)
            await self._trace("extract_fail", topic=topic, url=url, reason=str(e))
        return []

    # ── recursion ────────────────────────────────────────────────────────────
    async def _process_topic(self, node: dict[str, Any]) -> None:
        logger.info("_process_topic | id=%s label=%r depth=%d", node["id"], node["label"], node["depth"])
        # Acquire semaphore ONLY for the split-check phase. Children must be
        # able to acquire their own slots independently — holding the sem
        # across gather() causes deadlock when concurrency slots are exhausted.
        async with self._sem:
            if self._stop():
                logger.warning("_process_topic | stopped (cancel=%s timeout=%s) id=%s",
                               self._cancelled, self._time_exceeded(), node["id"])
                await self._set_status(node, "failed")
                return
            subs = await self._maybe_split(node["label"], node["depth"])

        if subs:
            await self._set_status(node, "expanding")
            children = [self.add_node(node["id"], node["depth"] + 1, "topic", s) for s in subs]
            for c in children:
                await self._emit_node(c)
            await self._emit_progress(status=f"decomposing: {node['label'][:40]}")
            # Children acquire their own semaphore slots — no deadlock.
            await asyncio.gather(*(self._process_topic(c) for c in children))
            await self._set_status(node, "done")
            return

        # Leaf topic: hold the semaphore for the actual page research work.
        async with self._sem:
            if self._stop():
                logger.warning("_process_topic | stopped before pages (cancel=%s timeout=%s) id=%s",
                               self._cancelled, self._time_exceeded(), node["id"])
                await self._set_status(node, "failed")
                return
            await self._research_pages(node)

    async def _research_pages(self, node: dict[str, Any]) -> None:
        logger.info("_research_pages | id=%s label=%r", node["id"], node["label"])
        await self._set_status(node, "searching")
        await self._emit_progress(status=f"searching: {node['label'][:40]}")
        queries = await self._gen_queries(node["label"])
        results = await self._search(queries, node["label"])
        if not results:
            # Search returned empty — reformulate queries and retry once before giving up
            logger.warning("_research_pages | no search results for %r, reformulating queries", node["label"])
            await self._emit_progress(status=f"no results, reformulating: {node['label'][:40]}")
            reformulated = await self._reformulate_queries(node["label"], queries)
            if reformulated and reformulated != queries:
                logger.info("_research_pages | retrying with reformulated queries: %s", reformulated)
                results = await self._search(reformulated, node["label"])
            if not results:
                logger.warning("_research_pages | still no results after reformulation for %r", node["label"])
                await self._set_status(node, "failed")
                return
        pool = results[: max(15, self.pages_per_topic)]
        logger.info("_research_pages | got %d results, LLM selecting from %d", len(results), len(pool))
        await self._emit_progress(status=f"selecting pages: {node['label'][:40]}")
        top = await self._select_pages(node["label"], pool)
        await self._set_status(node, "reading")
        # Selector rejected the whole pool -> seed failures so retry suggests real URLs instead of fetching junk
        failed_urls: list[str] = [c["url"] for c in pool] if not top else []
        for r in top:
            if self._stop():
                break
            page_node = self.add_node(node["id"], node["depth"] + 1, "page", r.get("title") or r["url"], "pending")
            await self._emit_node(page_node)
            await self._set_status(page_node, "reading")
            page = await self._fetch_one(r["url"], node["label"])
            if not page:
                logger.warning("_research_pages | fetch failed for %s", r["url"])
                await self._set_status(page_node, "failed")
                failed_urls.append(r["url"])
                continue
            self.sources.append({"url": r["url"], "title": r.get("title", "")})
            found = await self._extract(node["label"], page)
            if found:
                self.findings.extend(found)
                node.setdefault("findings", []).extend(found)
                logger.info("_research_pages | extracted %d findings from %s", len(found), r["url"])
                await self._set_status(page_node, "done")
            else:
                logger.warning("_research_pages | no findings extracted from %s", r["url"])
                await self._set_status(page_node, "failed")
                failed_urls.append(r["url"])  # fetched but useless = failure for retry purposes
            await self._emit_progress(status=f"reading: {r.get('title','')[:40]}")

        # Fallback: if >=30% of pages failed (fetch error OR no findings), retry (max 2 rounds)
        # Also retry if absolute failure count >= 2, even if percentage is low
        max_retries = 2
        for retry_round in range(1, max_retries + 1):
            fail_threshold = max(2, int(len(top) * 0.3)) if top else 2
            if self._stop() or not failed_urls or len(failed_urls) < fail_threshold:
                break
            logger.info("_research_pages | retry %d/%d: %d/%d pages failed, requesting alternatives",
                        retry_round, max_retries, len(failed_urls), len(top))
            await self._emit_progress(
                status=f"retry {retry_round}/{max_retries}: {len(failed_urls)} pages failed, asking model for alternatives")
            alt_urls = await self._suggest_alternatives(failed_urls, node["label"])
            if not alt_urls:
                break
            failed_urls = []  # reset for this retry round
            for alt_url in alt_urls:
                if self._stop():
                    break
                alt_node = self.add_node(node["id"], node["depth"] + 1, "page", alt_url, "pending")
                await self._emit_node(alt_node)
                await self._set_status(alt_node, "reading")
                page = await self._fetch_one(alt_url, node["label"])
                if not page:
                    logger.warning("_research_pages | retry %d fetch failed for %s", retry_round, alt_url)
                    await self._set_status(alt_node, "failed")
                    failed_urls.append(alt_url)
                    continue
                self.sources.append({"url": alt_url, "title": page.get("title", "")})
                found = await self._extract(node["label"], page)
                if found:
                    self.findings.extend(found)
                    node.setdefault("findings", []).extend(found)
                    logger.info("_research_pages | retry %d: extracted %d findings from %s",
                                retry_round, len(found), alt_url)
                    await self._set_status(alt_node, "done")
                else:
                    await self._set_status(alt_node, "failed")
                await self._emit_progress(status=f"retry {retry_round}: reading {alt_url[:50]}")

        await self._set_status(node, "done")

    async def _synthesize_node(self, node: dict[str, Any]) -> str:
        """Bottom-up synthesis; returns the node's synthesis text."""
        logger.info("_synthesize_node | id=%s label=%r children=%d", node["id"], node["label"], len(node.get("children", [])))
        await self._trace("synthesize_start", node_id=node["id"], topic=node["label"], depth=node["depth"])
        if self._stop():
            logger.warning("_synthesize_node | stopped before synthesis id=%s", node["id"])
            node["synthesis"] = ""
            return ""
        child_topics = [self.nodes[c] for c in node["children"] if self.nodes[c]["kind"] == "topic"]
        if child_topics:
            child_syns = await asyncio.gather(*(self._synthesize_node(c) for c in child_topics))
            material = "\n\n".join(f"### {c['label']}\n{s}" for c, s in zip(child_topics, child_syns) if s)
        else:
            fs = node.get("findings", [])
            material = "\n".join(f"- {f['finding']} ({f.get('source_title','')})" for f in fs) or "(no findings)"
        if not material.strip():
            node["synthesis"] = ""
            return ""
        prompt = SYNTH_PROMPT.format(topic=node["label"], children=material[:8000])
        await self._trace("synthesize_prompt", node_id=node["id"], topic=node["label"], prompt=prompt[:2000])
        try:
            node["synthesis"] = await self._complete(prompt, timeout=180)
            logger.info("_synthesize_node | ok id=%s synthesis_len=%d", node["id"], len(node["synthesis"]))
            await self._trace("synthesize_ok", node_id=node["id"], topic=node["label"], synthesis_len=len(node["synthesis"]),
                              synthesis=node["synthesis"][:2000])
        except Exception as e:
            logger.warning("_synthesize_node failed | id=%s error=%s", node["id"], e)
            await self._trace("synthesize_fail", node_id=node["id"], topic=node["label"], error=str(e))
            node["synthesis"] = material[:2000]
        return node["synthesis"]

    async def _final_report(self, root: dict[str, Any]) -> str:
        """Generate the final research report with multi-stage fallback.

        1. Full prompt with synthesis + findings (timeout 240s)
        2. Shorter prompt with just synthesis (timeout 180s)
        3. Raw synthesis material as last resort — never returns empty
        """
        logger.info("_final_report | starting | findings=%d sources=%d", len(self.findings), len(self.sources))
        await self._emit_progress(status="writing final report", phase="writing")
        findings_txt = "\n".join(
            f"- {f['finding']} [src: {f.get('source_url','')}]" for f in self.findings[:120]) or "(none)"
        synthesis_text = root.get("synthesis", "") or ""

        # Attempt 1: full prompt
        prompt = FINAL_PROMPT.format(
            question=self.question,
            synthesis=synthesis_text[:8000],
            findings=findings_txt[:8000],
        )
        try:
            report = await self._complete(prompt, timeout=180)
            if report and len(report.strip()) > 100:
                logger.info("_final_report | full prompt ok | report_len=%d", len(report))
                return report
            logger.warning("_final_report | full prompt returned short response (len=%d)", len(report.strip()))
        except Exception as e:
            logger.warning("_final_report | full prompt failed: %s", e)
            await self._emit_progress(status="report retry: simplifying prompt")

        # Attempt 2: shorter prompt — just synthesize what we have
        short_prompt = (
            f"Write a comprehensive research report answering: {self.question}\n\n"
            f"Use this material:\n{synthesis_text[:6000]}\n\n"
            f"Key findings:\n{findings_txt[:4000]}\n\n"
            f"Requirements: Use ## headings, cite sources as [title](url), minimum 800 words."
        )
        try:
            report = await self._complete(short_prompt, timeout=180)
            if report and len(report.strip()) > 50:
                logger.info("_final_report | short prompt ok | report_len=%d", len(report))
                return report
            logger.warning("_final_report | short prompt returned short response (len=%d)", len(report.strip()))
        except Exception as e:
            logger.warning("_final_report | short prompt failed: %s", e)

        # Attempt 3: assemble from raw material — NEVER return empty
        logger.warning("_final_report | all LLM attempts failed, assembling from raw material")
        await self._emit_progress(status="assembling report from collected material")
        sections = []
        sections.append(f"# {self.question}\n")
        sections.append("## Research Summary\n")
        if synthesis_text:
            sections.append(synthesis_text)
        else:
            sections.append("*(Synthesis could not be generated due to model errors.)*\n")
        if self.findings:
            sections.append("\n## Key Findings\n")
            for f in self.findings[:80]:
                src = f.get("source_title", "") or f.get("source_url", "")
                sections.append(f"- {f['finding']} ({src})")
        if self.sources:
            sections.append("\n## Sources\n")
            for s in self.sources[:50]:
                sections.append(f"- [{s.get('title', 'Untitled')}]({s.get('url', '')})")
        assembled = "\n".join(sections)
        logger.info("_final_report | assembled fallback | len=%d", len(assembled))
        return assembled

    # ── main ─────────────────────────────────────────────────────────────────
    async def research(self) -> str:
        self._start = time.time()
        from engine.platform_paths import tmp_path
        self._log_path = tmp_path(f"sable_research_{int(time.time())}.json")
        # Live trace log — JSON-lines, appended in real-time so you can tail it mid-run.
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.question.lower())[:40]
        self._trace_path = OUTPUT_ROOT / "research" / f"trace_{slug}_{int(time.time())}.jsonl"
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("=== RESEARCH START === | question=%r models=%s accounts=%s max_depth=%d max_time=%d trace=%s",
                     self.question, self.models, self.accounts, self.max_depth, self.max_time, self._trace_path)
        await self._trace("research_start", question=self.question, models=self.models, accounts=self.accounts,
                          max_depth=self.max_depth, max_time=self.max_time)

        root = self.add_node(None, 0, "root", self.question, "expanding")
        await self._emit_node(root)
        await self._emit_progress(status="planning strategy")

        subtopics = await self._expand_root()
        children = [self.add_node(root["id"], 1, "topic", s) for s in subtopics]
        for c in children:
            await self._emit_node(c)
        logger.info("research | expanded to %d top-level topics", len(children))
        await self._set_status(root, "reading")
        await self._emit_progress(status="decomposing question")

        await asyncio.gather(*(self._process_topic(c) for c in children))

        elapsed = time.time() - self._start
        if not self._cancelled:
            logger.info("research | processing complete in %.1fs | nodes=%d findings=%d sources=%d",
                         elapsed, len(self.nodes), len(self.findings), len(self.sources))
            await self._set_status(root, "writing")
            await self._synthesize_node(root)
            report = await self._final_report(root)
            maxd = max((n["depth"] for n in self.nodes.values()), default=0)
            report_node = self.add_node(root["id"], maxd + 1, "report", "Report", "done")
            await self._emit_node(report_node)
            await self._set_status(root, "done")
            await self._emit({"type": "done", "phase": "done", "status": "complete"})
            logger.info("=== RESEARCH DONE === | total_time=%.1fs report_len=%d", time.time() - self._start, len(report))
            await self._trace("research_done", total_time_s=round(time.time() - self._start, 2),
                              report_len=len(report), nodes=len(self.nodes), findings=len(self.findings),
                              sources=len(self.sources), trace_file=str(self._trace_path))
            # Write simple fetch summary alongside the trace
            if self._fetch_log:
                fetch_summary_path = self._trace_path.with_suffix(".fetch_summary.json")
                try:
                    with open(fetch_summary_path, "w", encoding="utf-8") as f:
                        json.dump(self._fetch_log, f, indent=2, ensure_ascii=False)
                    logger.info("fetch summary written to %s (%d entries)", fetch_summary_path, len(self._fetch_log))
                except Exception as e:
                    logger.warning("failed to write fetch summary: %s", e)
            return report
        logger.warning("=== RESEARCH CANCELLED === | elapsed=%.1fs", elapsed)
        await self._trace("research_cancelled", elapsed_s=round(elapsed, 2))
        await self._emit({"type": "error", "phase": "error", "status": "cancelled"})
        return ""
#
