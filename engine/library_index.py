"""Resumable OCR + AI indexing for the study library.

A single per-document *index job* owns two passes, each with its own cursor
persisted to disk so a stop (user pause, crash, restart) resumes exactly where
it left off:

    OCR pass   page-at-a-time   render page -> OCR engine -> merge text
    AI pass    chunk-at-a-time  chunk -> embed -> append vector

OCR runs automatically on upload for any page whose extracted text is blank
(a scanned/image-only page), using BanglaOCR first with a hard per-page
timeout, then falling back to the local SableOCR engine. The AI (embedding)
pass is *manual only* — it is heavyweight and the user drives it.

Job state lives in ``library/jobs/<doc-id>.json``; rendered page images are
cached under ``library/ocr_pages/<doc-id>/`` so a resumed OCR pass never
re-renders pages it already did. Both are derived data — losing them costs
time, never truth (the PDF and its page text remain the source of truth).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from engine.config import (
    LIBRARY_EMBED_DIR,
    LIBRARY_JOBS_DIR,
    LIBRARY_META_DIR,
    LIBRARY_OCR_DIR,
    LIBRARY_PDF_DIR,
    LIBRARY_TEXT_DIR,
)
from engine import library_search as _search

logger = logging.getLogger("sable.study.index")

# Per-page OCR timeout for the cloud engine — a slow page must never wedge the
# whole job. On timeout we fall straight through to the local engine.
BANGLA_OCR_PAGE_TIMEOUT = 5.0

# Concurrency caps. Cloud OCR is network-bound (higher fan-out is fine); the
# local engine is CPU-bound and shares cores with the rest of the app.
_CLOUD_CONCURRENCY = 4
_LOCAL_CONCURRENCY = 2
# AI OCR drives the vision model through the SCRAPER path, which is backed by a
# real browser session and capped by engine.browser_governor.MAX_BROWSERS (2).
# Running pages 4-wide deadlocks on browser slots: two pages hold the slots and
# never release them while two more wait forever — CPU pins, progress freezes.
# Vision OCR therefore runs SERIALLY. It is slower but it actually finishes.
_AI_OCR_CONCURRENCY = 1

# A page with fewer than this many non-space chars counts as "needs OCR".
_MIN_TEXT_CHARS = 12

# How many times a page may fail OCR before it is retired as permanently
# un-OCR-able. Without this a genuinely blank/image-only page keeps re-entering
# the "needs indexing" set forever: it fails, is not marked done, looks
# unindexed, gets re-queued on the next scan, fails again — an infinite loop
# that burns browser slots and CPU. After this many strikes the page is moved
# to ``failed_permanent`` and never auto-picked again (manual re-index still
# works, and clears the strikes).
_MAX_OCR_STRIKES = 3

# The scraper harness can leak a chat_title / tool-call block into model
# output even when the prompt never asked for one. AI OCR must strip these or
# the garbage ends up in page text and, worse, in the embeddings.
import re as _re
_TITLE_TAG_RE = _re.compile(r"<chat_title>.*?</chat_title>", _re.S | _re.I)
_TOOL_TAG_RE = _re.compile(r"</?(?:action|tool_call|chat_title)\b[^>]*>", _re.I)
# A bare tool-call JSON block the model emits WITHOUT tags, e.g.
#   [{"name": "chat_title", "arguments": {...}}]
# The tag strip above misses this; it must go too or it lands in page text.
_BARE_TOOL_JSON_RE = _re.compile(
    r"\[\s*\{\s*\"(?:name|tool|function)\"\s*:.*?\}\s*\]",
    _re.S | _re.I,
)

_JOB_VERSION = 1

# Progress callback: (event: dict) -> None. Fire-and-forget; must not raise.
ProgressCb = Callable[[dict[str, Any]], None]

# One index job per document at a time. The auto-OCR task (spawned on upload)
# and a manual stream job can otherwise race on the same text file: the second
# one sees the first's ``done_pages`` and skips pages whose text isn't written
# yet. The lock serialises them so each pass sees a consistent state.
_doc_locks: dict[str, asyncio.Lock] = {}
_doc_locks_guard = asyncio.Lock()

# Cooperative cancellation. A background job has no request to cancel, so
# stopping it means setting a flag the running pass polls between pages/chunks.
# Cancelling the asyncio task outright would leave a half-written checkpoint.
_stop_requests: set[str] = set()

# Documents with a job actually executing in THIS process. A job file that
# says "running" but has no entry here belongs to a process that died (server
# restart) — reporting it as running forever would be a lie.
_active_jobs: set[str] = set()


def request_stop(doc_id: str) -> bool:
    """Ask a running job to stop at the next checkpoint. Returns whether one
    was actually live; a stop request for an idle doc is harmless but useless."""
    if not doc_id:
        return False
    _stop_requests.add(doc_id)
    return doc_id in _active_jobs


def _mark_stage_done(doc_id: str, stage: str) -> None:
    """Record that a pipeline stage finished (even if it had no work to do)."""
    job = load_job(doc_id)
    done = [str(s) for s in (job.get("completed") or [])]
    if stage not in done:
        done.append(stage)
    job["completed"] = done
    save_job(job)


def clear_stop(doc_id: str) -> None:
    """Drop any pending stop request — called when a job (re)starts."""
    _stop_requests.discard(doc_id)


def is_stop_requested(doc_id: str) -> bool:
    return doc_id in _stop_requests


def is_live(doc_id: str) -> bool:
    """True when a job for this doc is executing right now, in this process."""
    return doc_id in _active_jobs


async def _doc_lock(doc_id: str) -> asyncio.Lock:
    async with _doc_locks_guard:
        lock = _doc_locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            _doc_locks[doc_id] = lock
        return lock


def _jobs_dir() -> Path:
    LIBRARY_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return LIBRARY_JOBS_DIR


def _job_path(doc_id: str) -> Path:
    return _jobs_dir() / f"{doc_id}.json"


def _ocr_pages_dir(doc_id: str) -> Path:
    d = LIBRARY_OCR_DIR / doc_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _text_path(doc_id: str) -> Path:
    return LIBRARY_TEXT_DIR / f"{doc_id}.json"


def _pdf_path(doc_id: str) -> Path:
    return LIBRARY_PDF_DIR / f"{doc_id}.pdf"


# ── Job state ────────────────────────────────────────────────────────────────


def _empty_job(doc_id: str) -> dict[str, Any]:
    return {
        "version": _JOB_VERSION,
        "doc_id": doc_id,
        "status": "idle",              # idle | running | paused | done | error
        "stage": "",                   # ocr | search | ai (what's running now)
        # Ordered stage ids this job will run, so the UI can show one honest
        # overall percentage instead of guessing from a single stage.
        "pipeline": [],
        # Stage ids that have actually finished. Completion is recorded
        # explicitly rather than inferred from counters, because a stage can
        # legitimately have nothing to do (an OCR pass on a text PDF) and would
        # otherwise never reach 100%.
        "completed": [],
        "ocr": {"cursor": 0, "total": 0, "engine": "", "lang": "", "done_pages": []},
        "ai": {"cursor": 0, "total": 0, "model": "", "thinking_mode": ""},
        "search": {"done": 0, "total": 0},
        "error": "",
        "updated": time.time(),
    }


def load_job(doc_id: str) -> dict[str, Any]:
    """Read a doc's job state, creating an empty one when absent/corrupt."""
    path = _job_path(doc_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_job(doc_id)
                base.update(data)
                base.setdefault("ocr", _empty_job(doc_id)["ocr"])
                base.setdefault("ai", _empty_job(doc_id)["ai"])
                return base
        except Exception:
            pass
    return _empty_job(doc_id)


def save_job(job: dict[str, Any]) -> None:
    """Persist job state atomically (write-to-tmp + replace)."""
    doc_id = str(job.get("doc_id") or "")
    if not doc_id:
        return
    job["updated"] = time.time()
    target = _job_path(doc_id)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:
        logger.warning("could not persist job for %s: %s", doc_id, exc)


def delete_job(doc_id: str) -> None:
    """Drop a doc's job file.

    Used when a document disappears (deleted, or an upload cancelled mid-
    pipeline). Without this the Indexing tab would keep offering a Resume
    button for a document that no longer exists.
    """
    if not doc_id:
        return
    _stop_requests.discard(doc_id)
    try:
        _job_path(doc_id).unlink(missing_ok=True)
    except Exception:
        logger.warning("could not delete job file for %s", doc_id, exc_info=True)


def job_exists(doc_id: str) -> bool:
    """Whether a job file actually exists on disk.

    ``load_job()`` deliberately invents an empty job when the file is missing,
    which is convenient for callers mid-pipeline but lies to anything trying to
    ask "has this document ever been indexed?". This is the honest check.
    """
    if not doc_id:
        return False
    try:
        return _job_path(doc_id).exists()
    except Exception:
        return False


def has_embeddings(doc_id: str) -> bool:
    """True when the AI (embedding) pass has produced vectors for this doc."""
    if not doc_id:
        return False
    try:
        return (LIBRARY_EMBED_DIR / f"{doc_id}.json").exists()
    except Exception:
        return False


def list_jobs() -> list[dict[str, Any]]:
    """Every persisted job, newest-updated first."""
    _jobs_dir()
    jobs: list[dict[str, Any]] = []
    for path in LIBRARY_JOBS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("doc_id"):
                jobs.append(data)
        except Exception:
            continue
    jobs.sort(key=lambda j: -(j.get("updated") or 0))
    return jobs


# ── Page text helpers ────────────────────────────────────────────────────────


def _load_pages(doc_id: str) -> list[str]:
    path = _text_path(doc_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(t or "") for t in data]
        except Exception:
            pass
    return []


def _save_pages(doc_id: str, pages: list[str]) -> None:
    LIBRARY_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    target = _text_path(doc_id)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def _doc_title(doc_id: str) -> str:
    """Best-effort title for the search index (the meta store owns it)."""
    try:
        meta_path = LIBRARY_META_DIR / f"{doc_id}.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                return str(meta.get("title") or "")
    except Exception:
        pass
    return ""


def page_needs_ocr(text: str) -> bool:
    """True when a page carries too little text to be useful (scanned page)."""
    return len((text or "").strip()) < _MIN_TEXT_CHARS


def pages_needing_ocr(pages: list[str]) -> list[int]:
    """1-based page numbers whose text is effectively empty."""
    return [i + 1 for i, t in enumerate(pages) if page_needs_ocr(t)]


# ── Page rendering ───────────────────────────────────────────────────────────


def _render_page_png(pdf_path: Path, page_number: int, dpi: int = 200) -> bytes:
    """Rasterize one PDF page to PNG bytes. Raises on failure."""
    import pymupdf  # type: ignore

    doc = pymupdf.open(str(pdf_path))
    try:
        if page_number < 1 or page_number > doc.page_count:
            raise ValueError(f"page {page_number} out of range")
        page = doc.load_page(page_number - 1)
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()


def _cached_page_png(doc_id: str, page_number: int) -> bytes:
    """Rendered page PNG, cached on disk so a resumed pass never re-renders."""
    cache = _ocr_pages_dir(doc_id) / f"{page_number:05d}.png"
    if cache.exists():
        try:
            return cache.read_bytes()
        except Exception:
            pass
    png = _render_page_png(_pdf_path(doc_id), page_number)
    try:
        cache.write_bytes(png)
    except Exception:
        pass
    return png


# ── OCR engines ──────────────────────────────────────────────────────────────


async def _ocr_bangla_ocr(png: bytes, filename: str) -> str:
    """Cloud BanglaOCR, bounded to BANGLA_OCR_PAGE_TIMEOUT wall-clock.

    The API 403s on a direct connection from some networks, so — exactly like
    the panel route — we try direct first, then a few free proxies. The whole
    attempt (proxy fetch + every try) shares ONE deadline: a slow page must
    never wedge the job, it just falls through to the local engine.
    """
    try:
        import httpx

        from server.api.routes.ocr import (
            BANGLA_OCR_URL,
            OCR_HEADERS,
            _fetch_proxies,
        )
    except Exception:
        return ""

    deadline = asyncio.get_event_loop().time() + BANGLA_OCR_PAGE_TIMEOUT

    def _remaining() -> float:
        return deadline - asyncio.get_event_loop().time()

    # Candidate endpoints: direct first, then a small proxy sample.
    candidates: list[str | None] = [None]
    try:
        async with httpx.AsyncClient() as probe:
            ps = await asyncio.wait_for(_fetch_proxies(probe), timeout=min(2.0, max(0.5, _remaining())))
        if ps:
            import random
            candidates.extend(random.sample(ps, min(4, len(ps))))
    except Exception:
        pass

    for proxy in candidates:
        budget = _remaining()
        if budget <= 0.3:
            break
        try:
            timeout = min(budget, BANGLA_OCR_PAGE_TIMEOUT)
            if proxy:
                async with httpx.AsyncClient(proxy=proxy, timeout=timeout) as client:
                    resp = await client.post(
                        BANGLA_OCR_URL,
                        files={"file": (filename, png, "image/png")},
                        headers=OCR_HEADERS,
                    )
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        BANGLA_OCR_URL,
                        files={"file": (filename, png, "image/png")},
                        headers=OCR_HEADERS,
                    )
            if resp.status_code != 200:
                continue
            data = resp.json()
            text = str(data.get("full_text") or "").strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def _ocr_local(png: bytes, filename: str, lang: str) -> str:
    """Local SableOCR (pytesseract + bnunicodenormalizer). Synchronous/CPU-bound."""
    try:
        from server.api.routes.ocr_engines import get_runner

        runner = get_runner("sableocr")
        result = runner(png, filename, lang or "eng+ben")
        return str(result.get("full_text") or "").strip()
    except Exception as exc:
        logger.debug("local OCR failed for %s: %s", filename, exc)
        return ""


# OCR context sync happens once per OCR run, not per page. The Qwen session
# instruction set is global; re-syncing for every page would be wasteful and
# would fight concurrent chats. Reset between runs so a later AI OCR run with a
# different model re-syncs.
_OCR_CONTEXT_LOCK = asyncio.Lock()
_OCR_CONTEXT_SYNCED: str = ""  # model id the current context was synced for

# One Qwen chat session reused for every page of an OCR run. Opening a fresh
# session per page exhausts the browser governor's account pool: page 1 grabs
# the session, page 2 waits the full 120s acquire timeout, the run dies having
# done a single page. Reusing one chat makes every page ride the same session.
_OCR_CHAT_ID: str = ""


def reset_ocr_session() -> None:
    """Forget the reused OCR chat + synced context. Call at the start of a run
    so a fresh run opens a fresh session rather than inheriting a dead one."""
    global _OCR_CHAT_ID, _OCR_CONTEXT_SYNCED
    _OCR_CHAT_ID = ""
    _OCR_CONTEXT_SYNCED = ""


async def _ensure_ocr_context(model: str) -> None:
    """Sync a bare, tool-less instruction set for AI OCR.

    Why this exists: the Qwen scraper session carries a global instruction set
    that includes the chat_title tool. The model sees that tool and calls it
    while transcribing, leaking a tool-call block into the page text. Prompt
    prefixes and output stripping treat the symptom; syncing a context with NO
    tools removes the cause.

    API-connector models (gemini/groq/etc.) don't use the scraper session and
    are skipped — their calls carry an explicit system prompt already.
    """
    global _OCR_CONTEXT_SYNCED
    if not model:
        return
    async with _OCR_CONTEXT_LOCK:
        if _OCR_CONTEXT_SYNCED == model:
            return  # already synced for this run
        try:
            from server.utils import _is_api_model
            if _is_api_model(model):
                _OCR_CONTEXT_SYNCED = model  # API models: nothing to sync
                return
        except Exception:
            pass
        try:
            from server.api.dependencies import service
            # A minimal, tool-less directive. layout_mode="chat" still allows
            # chat_title — so we pass custom_instructions that replace the
            # whole set with OCR-only guidance.
            instructions = (
                "You are an OCR engine. Transcribe page images to text "
                "EXACTLY as they appear. Output only the transcription. "
                "Never call tools. Never emit tool tags or a chat title."
            )
            ok = await service.sync_context(custom_instructions=instructions)
            if ok:
                _OCR_CONTEXT_SYNCED = model
                logger.info("AI OCR: synced tool-less context for %s", model)
            else:
                logger.warning("AI OCR: context sync returned False for %s", model)
        except Exception as exc:
            logger.warning("AI OCR: context sync failed: %s", exc)


async def _ocr_vision(doc_id: str, page_number: int, model: str, thinking_mode: str = "") -> str:
    """OCR one page with a vision model — the AI OCR path.

    Renders the page to PNG, hands it to the chosen model as an attachment,
    and asks for the verbatim text only. This is the override path: when AI OCR
    is selected it is the ONLY engine tried for a page, so a weak vision model
    never silently produces worse text than BanglaOCR would have.

    Returns the transcribed text, or "" when the model is unavailable / the
    call fails. Never raises.
    """
    if not model:
        return ""
    try:
        png = await asyncio.to_thread(_cached_page_png, doc_id, page_number)
    except Exception as exc:
        logger.warning("AI OCR: render page %d for %s failed: %s", page_number, doc_id, exc)
        return ""

    # Write the rendered PNG to a temp file so the attachment resolver can
    # upload it exactly the way study chat attaches a page image.
    import tempfile
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f"aiocr_{doc_id}_{page_number}_", suffix=".png", delete=False
        ) as fh:
            fh.write(png)
            tmp_path = fh.name

        # Imported lazily: library_index is imported by routes that already
        # import study_ai, and a top-level import here would cycle.
        from server.api.routes.study_ai import _resolve_attachments, _stream

        # Root-cause fix: the Qwen scraper session carries a GLOBAL instruction
        # set that includes the chat_title tool. No amount of prompt-prefixing
        # removes it — the model sees the tool and calls it mid-transcription.
        # Sync a bare, tool-less context for OCR so there is simply no tool to
        # call. Done once per batch (see _OCR_CONTEXT_SYNCED), not per page.
        await _ensure_ocr_context(model)

        system = (
            "You are an OCR engine. Transcribe the attached page image to text "
            "EXACTLY as it appears. Preserve line breaks, headings, lists, and "
            "tables. Output only the transcription — no commentary, no markdown "
            "fences, no explanation, no tool calls. If the page is blank, "
            "output nothing."
        )
        prompt = "Transcribe this page image to plain text, verbatim."

        kwargs = await _resolve_attachments(model, [tmp_path])
        global _OCR_CHAT_ID
        chunks: list[str] = []
        async for ev in _stream(
            system, prompt, model, thinking_mode,
            chat_id=(_OCR_CHAT_ID or None), **kwargs,
        ):
            etype = ev.get("type")
            if etype == "answer":
                chunks.append(str(ev.get("text") or ""))
            elif etype == "meta":
                # First page opens the session; the rest reuse this chat_id so
                # they never hit the browser governor's acquire timeout.
                cid = str(ev.get("chat_id") or "")
                if cid and not _OCR_CHAT_ID:
                    _OCR_CHAT_ID = cid
            elif etype == "error":
                logger.warning("AI OCR page %d failed: %s", page_number, ev.get("message"))
                return ""
        text = "".join(chunks).strip()
        # The scraper harness can still emit a <chat_title> / <action> tool
        # block even though OCR never asks for one (qwen does this unprompted).
        # Strip it — otherwise the garbage lands in page text AND embeddings.
        text = _TITLE_TAG_RE.sub("", text)
        text = _TOOL_TAG_RE.sub("", text)
        text = _BARE_TOOL_JSON_RE.sub("", text)
        # Strip a stray code fence if the model wrapped its answer anyway.
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        return text.strip()
    except Exception as exc:
        logger.warning("AI OCR page %d for %s failed: %s", page_number, doc_id, exc)
        return ""
    finally:
        if tmp_path:
            try:
                import os
                os.unlink(tmp_path)
            except Exception:
                pass


async def ocr_page(
    doc_id: str,
    page_number: int,
    lang: str = "eng+ben",
    *,
    ai_ocr: bool = False,
    model: str = "",
    thinking_mode: str = "",
) -> dict[str, Any]:
    """OCR one page.

    Default chain: BanglaOCR first (5s cap), then local SableOCR fallback.

    When ``ai_ocr`` is set, the vision model is the FINAL word: no BanglaOCR,
    no SableOCR fallback. An AI OCR page that fails is left blank rather than
    silently filled by a different engine — the user asked for AI OCR and gets
    exactly that (or an explicit failure to retry).

    Returns ``{page, text, engine, ok}``. Never raises.
    """
    fname = f"{doc_id}_page{page_number}.png"

    # ── AI OCR override: vision model only ──────────────────────────────────
    if ai_ocr:
        text = await _ocr_vision(doc_id, page_number, model, thinking_mode)
        if text:
            return {"page": page_number, "text": text, "engine": "ai", "ok": True}
        return {"page": page_number, "text": "", "engine": "ai", "ok": False}

    # ── Default chain: BanglaOCR -> SableOCR ────────────────────────────────
    try:
        png = await asyncio.to_thread(_cached_page_png, doc_id, page_number)
    except Exception as exc:
        logger.warning("render page %d for %s failed: %s", page_number, doc_id, exc)
        return {"page": page_number, "text": "", "engine": "", "ok": False}

    text = await _ocr_bangla_ocr(png, fname)
    if text:
        return {"page": page_number, "text": text, "engine": "banglaocr", "ok": True}

    # Fallback to the local engine on timeout/failure/empty result.
    text = await asyncio.to_thread(_ocr_local, png, fname, lang)
    if text:
        return {"page": page_number, "text": text, "engine": "sableocr", "ok": True}

    return {"page": page_number, "text": "", "engine": "", "ok": False}


# ── OCR pass ─────────────────────────────────────────────────────────────────


async def run_ocr_pass(
    doc_id: str,
    *,
    pages: list[int] | None = None,
    lang: str = "eng+ben",
    ai_ocr: bool = False,
    model: str = "",
    thinking_mode: str = "",
    on_progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """OCR every needed page, checkpointing after each one.

    ``pages`` overrides the target set; otherwise every page whose text is
    blank is OCR'd. The cursor lives in the job file, so re-invoking after a
    stop continues from the first page not yet in ``done_pages``.

    ``ai_ocr`` switches the engine to the chosen vision model and disables the
    BanglaOCR/SableOCR chain entirely — the AI is the final word on OCR.
    """
    job = load_job(doc_id)
    text_pages = _load_pages(doc_id)

    # Fresh session for this run: the reused OCR chat is per-run, so a new run
    # opens its own session instead of inheriting a closed one.
    if ai_ocr:
        reset_ocr_session()

    prev_ocr = job.get("ocr", {}) or {}
    # Pages that already failed _MAX_OCR_STRIKES times are retired. They are
    # never auto-targeted again — this is what breaks the infinite re-index
    # loop on a permanently-blank page. A manual run can still pass them in
    # via ``pages`` (which also clears their strikes below).
    permanent = set(int(p) for p in (prev_ocr.get("failed_permanent") or []))
    strikes: dict[int, int] = {
        int(k): int(v) for k, v in (prev_ocr.get("strikes") or {}).items()
    }

    if ai_ocr:
        # AI OCR means AI OCR. Every page, full stop — no blank-page scanning,
        # no resume skipping, and no honouring a stale ``pages`` list either.
        # A caller that passed pages=[4] (a leftover blank-page target) must
        # NOT narrow an AI OCR run down to one page. Retired pages are skipped
        # unless the caller named them explicitly.
        targets = list(range(1, len(text_pages) + 1))
        if pages is not None:
            targets = pages
            for p in pages:
                permanent.discard(int(p))  # manual pick re-arms the page
                strikes.pop(int(p), None)
        targets = [p for p in targets if p not in permanent]
        done: set[int] = set()  # ignore prior run's cursor — clean slate
    else:
        targets = pages if pages is not None else pages_needing_ocr(text_pages)
        if pages is not None:
            for p in pages:
                permanent.discard(int(p))  # manual pick re-arms the page
                strikes.pop(int(p), None)
        # A blank page that already retired must not be auto-picked.
        targets = [p for p in targets if p not in permanent]
        # Drop pages already done in a previous run — that's the resume.
        done = set(int(p) for p in (prev_ocr.get("done_pages") or []))
    todo = [p for p in targets if p not in done]

    engine_label = "ai" if ai_ocr else "banglaocr->sableocr"
    job["status"] = "running"
    job["stage"] = "ocr"
    job["ocr"] = {
        "cursor": len(done),
        "total": len(targets),
        "engine": engine_label,
        "lang": lang,
        "ai_ocr": ai_ocr,
        "model": model if ai_ocr else "",
        "thinking_mode": thinking_mode if ai_ocr else "",
        "done_pages": sorted(done),
        "failed_pages": sorted(permanent),
        "failed_permanent": sorted(permanent),
        "strikes": {str(k): v for k, v in strikes.items()},
    }
    save_job(job)

    if on_progress:
        on_progress({"stage": "ocr", "done": len(done), "total": len(targets), "page": None})

    if not todo:
        # Nothing to OCR. Mark the stage complete so it counts as a finished
        # slice of the bar, but leave the status alone — run_job decides
        # whether the whole job is finished.
        job["stage"] = ""
        save_job(job)
        _mark_stage_done(doc_id, "ocr")
        if on_progress:
            on_progress({"stage": "ocr", "done": len(done), "total": len(targets), "finished": True})
        return {"ocr": {"pages": len(done), "total": len(targets)}}

    # AI OCR drives a browser-backed scraper session (see _AI_OCR_CONCURRENCY);
    # it must run serially or it deadlocks on browser slots. The default chain
    # is stateless cloud/local OCR and can safely fan out.
    concurrency = _AI_OCR_CONCURRENCY if ai_ocr else _CLOUD_CONCURRENCY
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    engines: dict[str, int] = {"banglaocr": 0, "sableocr": 0, "ai": 0, "failed": 0}
    failed: set[int] = set()

    async def _one(page_number: int) -> None:
        async with sem:
            if should_stop and should_stop():
                return
            result = await ocr_page(
                doc_id, page_number, lang,
                ai_ocr=ai_ocr, model=model, thinking_mode=thinking_mode,
            )
            async with lock:
                if result["ok"]:
                    # Grow the array if the PDF has more pages than we knew about.
                    while len(text_pages) < page_number:
                        text_pages.append("")
                    text_pages[page_number - 1] = result["text"]
                    _save_pages(doc_id, text_pages)
                    engines[result["engine"]] = engines.get(result["engine"], 0) + 1
                    # Only a SUCCESSFUL page is marked done — a page that failed
                    # both engines stays in the todo set so the next run retries
                    # it instead of silently leaving a blank forever.
                    done.add(page_number)
                    strikes.pop(page_number, None)  # success clears the record
                    permanent.discard(page_number)
                else:
                    engines["failed"] += 1
                    failed.add(page_number)
                    # Count the strike. At the threshold the page is retired so
                    # it never auto-enters the queue again — this is what stops
                    # a permanently-blank page from re-indexing forever.
                    strikes[page_number] = strikes.get(page_number, 0) + 1
                    if strikes[page_number] >= _MAX_OCR_STRIKES:
                        permanent.add(page_number)
                        logger.info(
                            "OCR page %d of %s retired after %d failed attempts",
                            page_number, doc_id, strikes[page_number],
                        )
                job["ocr"]["done_pages"] = sorted(done)
                job["ocr"]["failed_pages"] = sorted(failed)
                job["ocr"]["failed_permanent"] = sorted(permanent)
                job["ocr"]["strikes"] = {str(k): v for k, v in strikes.items()}
                job["ocr"]["cursor"] = len(done)
                save_job(job)
                if on_progress:
                    on_progress({
                        "stage": "ocr",
                        "done": len(done),
                        "total": len(targets),
                        "page": page_number,
                        "engine": result["engine"],
                    })

    await asyncio.gather(*[_one(p) for p in todo])

    stopped = bool(should_stop and should_stop())
    # Only a stop settles the job here. Declaring "done" at the end of one pass
    # made the UI flash "Indexed" between OCR and the embedding pass, and left
    # the status wrong while later stages were still running. run_job owns the
    # terminal status.
    job["stage"] = ""
    if stopped:
        job["status"] = "paused"
    save_job(job)
    if not stopped:
        _mark_stage_done(doc_id, "ocr")

    if on_progress:
        on_progress({
            "stage": "ocr", "done": len(done), "total": len(targets),
            "finished": not stopped, "paused": stopped, "engines": engines,
            "failed": sorted(failed),
        })
    return {"ocr": {"pages": len(done), "total": len(targets), "engines": engines, "failed": sorted(failed)}}


# ── AI (embedding) pass ──────────────────────────────────────────────────────


async def run_ai_pass(
    doc_id: str,
    *,
    on_progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Chunk + embed a document's page text, checkpointing per chunk.

    The embeddings are produced by ``engine.library_rag`` (local fastembed
    model). This pass takes NO model — the vision model belongs to AI OCR, not
    to embedding, and conflating the two was a bug.
    """
    from engine import library_rag as _rag

    job = load_job(doc_id)
    pages = _load_pages(doc_id)
    if not pages:
        job["ai"] = {"cursor": 0, "total": 0}
        job["status"] = "error"
        job["error"] = "no page text to embed"
        save_job(job)
        return {"ai": {"chunks": 0, "error": "no page text"}}

    # Chunk the document, then embed in slices so each slice can checkpoint.
    chunks = _rag._chunk_pages(pages)  # noqa: SLF001 — intentional reuse of chunker
    total = len(chunks)
    job["status"] = "running"
    job["stage"] = "ai"
    cursor = int(job.get("ai", {}).get("cursor") or 0)
    if cursor > total:
        cursor = 0
    job["ai"] = {"cursor": cursor, "total": total}
    save_job(job)

    # Persist chunk metadata + vectors as we go, appending to any partial files.
    embed_dir = _rag._embed_dir()  # noqa: SLF001
    meta_path = embed_dir / f"{doc_id}.json"
    vec_path = embed_dir / f"{doc_id}.npy"

    existing: list[dict[str, Any]] = []
    if cursor and meta_path.exists():
        try:
            prev = json.loads(meta_path.read_text(encoding="utf-8"))
            existing = list(prev.get("chunks") or [])
        except Exception:
            existing = []

    model_obj = _rag._load_embedder()  # noqa: SLF001
    vectors: list[Any] = []
    if cursor and vec_path.exists() and model_obj is not None:
        try:
            import numpy as np
            vectors = list(np.load(vec_path))
        except Exception:
            vectors = []

    slice_size = 32
    if on_progress:
        on_progress({"stage": "ai", "done": cursor, "total": total})

    while cursor < total:
        if should_stop and should_stop():
            break
        batch = chunks[cursor:cursor + slice_size]
        if model_obj is not None:
            try:
                import numpy as np
                vecs = list(model_obj.embed([c["text"] for c in batch]))
                vectors.extend(vecs)
            except Exception as exc:
                logger.warning("embedding batch failed for %s: %s", doc_id, exc)
        existing.extend(batch)
        cursor += len(batch)

        # Persist checkpoint: chunk meta + vectors together.
        try:
            meta_path.write_text(
                json.dumps({"version": 1, "doc_id": doc_id, "built": time.time(), "chunks": existing},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            if vectors:
                import numpy as np
                np.save(vec_path, np.asarray(vectors, dtype="float32"))
        except Exception as exc:
            logger.warning("checkpoint write failed for %s: %s", doc_id, exc)

        job["ai"]["cursor"] = cursor
        save_job(job)
        if on_progress:
            on_progress({"stage": "ai", "done": cursor, "total": total})

    stopped = bool(should_stop and should_stop())
    # Same rule as the OCR pass: a stop pauses, completion is run_job's call.
    job["stage"] = ""
    if stopped:
        job["status"] = "paused"
    save_job(job)
    if not stopped:
        _mark_stage_done(doc_id, "ai")
    if on_progress:
        on_progress({"stage": "ai", "done": cursor, "total": total, "finished": not stopped, "paused": stopped})
    return {"ai": {"chunks": cursor, "total": total, "embedded": bool(vectors)}}


# ── Orchestration ────────────────────────────────────────────────────────────


async def run_job(
    doc_id: str,
    *,
    do_ocr: bool = True,
    do_search: bool = True,
    do_ai: bool = False,
    ai_ocr: bool = False,
    pages: list[int] | None = None,
    lang: str = "eng+ben",
    model: str = "",
    thinking_mode: str = "",
    on_progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the requested passes for one document, in order (OCR then AI).

    Serialised per document: an auto-OCR task and a manual job must never run
    at the same time, or the second sees the first's cursors mid-write and
    skips pages whose text isn't committed yet.
    """
    lock = await _doc_lock(doc_id)
    async with lock:
        # A stop request left over from a previous run must not abort this one.
        clear_stop(doc_id)

        # Record the stage plan up front. The UI weights each stage equally, so
        # the overall percentage never depends on a total we don't know yet
        # (chunk count only exists after the AI pass has chunked the document).
        # AI OCR rewrites page text, which makes any existing embeddings STALE
        # (they were built from the pre-OCR text). Force the embedding pass on
        # so the index reflects what OCR actually produced. Without this the
        # job ran ocr+search only and the vectors kept pointing at old garbage.
        if ai_ocr and do_ocr:
            do_ai = True

        pipeline: list[str] = []
        if do_ocr:
            pipeline.append("ocr")
        if do_search:
            pipeline.append("search")
        if do_ai:
            pipeline.append("ai")
        # Register as live BEFORE the status is written. A poll landing between
        # the two would otherwise see "running" with no live task and downgrade
        # the job to paused — a flicker the user would read as a stalled job.
        _active_jobs.add(doc_id)
        job = load_job(doc_id)
        job["pipeline"] = pipeline
        job["status"] = "running"
        job["error"] = ""
        save_job(job)
        out: dict[str, Any] = {}
        try:
            if do_ocr:
                out.update(await run_ocr_pass(
                    doc_id, pages=pages, lang=lang,
                    ai_ocr=ai_ocr, model=model, thinking_mode=thinking_mode,
                    on_progress=on_progress, should_stop=should_stop,
                ))

            if not (should_stop and should_stop()) and do_search:
                out.update(await run_search_pass(doc_id, on_progress=on_progress))

            if not (should_stop and should_stop()) and do_ai:
                # The embedding pass runs on the local fastembed model and
                # takes no model/thinking_mode — passing the vision model here
                # was cosmetic and misleading.
                out.update(await run_ai_pass(
                    doc_id,
                    on_progress=on_progress, should_stop=should_stop,
                ))
        finally:
            # Always settle the job, however we left the loop — an early
            # return on stop must not leave it stuck on "running" forever.
            _active_jobs.discard(doc_id)
            clear_stop(doc_id)
            final = load_job(doc_id)
            final["stage"] = ""
            if final.get("status") == "running":
                final["status"] = "done"
            save_job(final)

        return out


def _reindex_search(doc_id: str) -> None:
    """Mirror a doc's page text into the FTS index.

    Idempotent (delete-then-insert for this doc) and cheap, so it is safe to
    call on every job run. Without this the search index only ever reflected
    what extraction produced at upload time -- meaning an OCR pass that filled
    in a scanned document's text left it permanently unsearchable.
    """
    try:
        pages = _load_pages(doc_id)
        if pages:
            _search.index_document(doc_id, _doc_title(doc_id), pages)
    except Exception:
        logger.warning("search reindex failed for %s", doc_id, exc_info=True)


async def run_search_pass(
    doc_id: str,
    *,
    on_progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """Mirror page text into the FTS index, reporting progress.

    Cheap next to OCR or embedding, but it still walks every page, so it gets
    a stage of its own to keep the overall progress bar honest.
    """
    job = load_job(doc_id)
    pages = _load_pages(doc_id)
    total = len(pages)
    job["status"] = "running"
    job["stage"] = "search"
    job["search"] = {"done": 0, "total": total}
    save_job(job)
    if on_progress:
        on_progress({"stage": "search", "done": 0, "total": total})

    ok = False
    try:
        _search.index_document(doc_id, _doc_title(doc_id), pages)
        ok = True
    except Exception:
        logger.warning("search reindex failed for %s", doc_id, exc_info=True)

    job = load_job(doc_id)
    job["search"] = {"done": total if ok else 0, "total": total}
    job["stage"] = ""
    save_job(job)
    if ok:
        _mark_stage_done(doc_id, "search")
    if on_progress:
        on_progress({
            "stage": "search", "done": total if ok else 0,
            "total": total, "finished": True, "ok": ok,
        })
    return {"search": {"pages": total if ok else 0, "total": total}}


def status_for(doc_id: str) -> dict[str, Any]:
    """Compact job status for the UI (one doc).

    Also folds the per-stage counters into a single ``percent`` so a card or
    tab can show one honest number for the whole job instead of a stage-local
    figure that jumps backwards when the next stage starts.
    """
    job = load_job(doc_id)
    ocr = job.get("ocr", {}) or {}
    ai = job.get("ai", {}) or {}
    search = job.get("search", {}) or {}

    ocr_done = len(ocr.get("done_pages") or [])
    ocr_total = int(ocr.get("total") or 0)
    search_done = int(search.get("done") or 0)
    search_total = int(search.get("total") or 0)
    ai_cursor = int(ai.get("cursor") or 0)
    ai_total = int(ai.get("total") or 0)

    # Only count a stage toward the overall figure when it is actually part of
    # this job's plan; otherwise a stale cursor from an earlier run would
    # inflate the total forever.
    pipeline = [str(s) for s in (job.get("pipeline") or [])]
    if not pipeline:
        pipeline = [s for s, t in (("ocr", ocr_total), ("search", search_total), ("ai", ai_total)) if t]

    # Stage-weighted, NOT unit-weighted. A stage's unit total is often unknown
    # until that stage begins (the chunk count only exists once the document
    # has been chunked), so dividing by a running sum made the bar hit 100%
    # during OCR and then fall backwards when search joined the denominator.
    # Each stage owns an equal slice instead, and a finished stage is recorded
    # explicitly so one with nothing to do still counts as complete.
    completed = {str(s) for s in (job.get("completed") or [])}
    stage_state = {
        "ocr": (ocr_done, ocr_total),
        "search": (search_done, search_total),
        "ai": (ai_cursor, ai_total),
    }
    fractions: list[float] = []
    for stage in pipeline:
        if stage in completed:
            fractions.append(1.0)
            continue
        done_n, total_n = stage_state.get(stage, (0, 0))
        if total_n <= 0:
            # Not done and nothing to measure yet: a not-yet-started stage.
            fractions.append(0.0)
        else:
            fractions.append(min(1.0, done_n / total_n))

    if job.get("status") == "done":
        percent = 100
    elif not fractions:
        percent = 0
    else:
        percent = int(round(sum(fractions) / len(fractions) * 100))

    status = str(job.get("status") or "idle")
    # A job file can claim "running" while no task is executing it: the server
    # was restarted mid-job, or the task died. Reporting that as running would
    # show a bar that never moves, so downgrade it to paused (resumable).
    if status == "running" and not is_live(doc_id):
        status = "paused"

    return {
        "doc_id": doc_id,
        "status": status,
        "stage": job.get("stage", "") if status == "running" else "",
        "pipeline": pipeline,
        "completed": sorted(completed),
        "live": is_live(doc_id),
        "stop_requested": is_stop_requested(doc_id),
        "ocr_done": ocr_done,
        "ocr_total": ocr_total,
        "ocr_failed": len(ocr.get("failed_pages") or []),
        "ocr_failed_permanent": len(ocr.get("failed_permanent") or []),
        "search_done": search_done,
        "search_total": search_total,
        "ai_cursor": ai_cursor,
        "ai_total": ai_total,
        "percent": percent,
        "updated": job.get("updated", 0),
    }
