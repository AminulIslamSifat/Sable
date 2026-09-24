"""Study-mode AI — ask about a PDF, explain selections, save answers as notes.

Complements study.py (library + meta store) by layering AI on top of the
already-extracted ``page_text``:

  * configurable model / thinking mode / persona / custom instructions
  * streaming answers grounded in the document text
  * answers persisted against the exact selection (or page / whole doc)

Stored config lives in ``system/study_ai_config.json``. Saved answers live
inside the document's meta JSON under the ``ai_notes`` key (same store the
reader already loads), so the frontend needs no second fetch.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from engine.config import get_all_models, get_model_config

from connectors import get_connector
from server.utils import _is_api_model, _resolve_api_backend

from ..dependencies import service, sse
from .study import _clean_rects, _load_page_text, _read_meta, _update_meta

router = APIRouter()
logger = logging.getLogger("sable.study.ai")

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_PATH = _ROOT / "system" / "study_ai_config.json"
_INSTRUCTION_DIR = _ROOT / "instruction"

_MAX_CTX_CHARS = 24_000      # context budget for selection / page scope
_MAX_DOC_CTX = 60_000        # context budget for whole-document scope
_MAX_SELECTION_CHARS = 4_000
_MAX_PERSONA_CHARS = 2_500

# Backends whose connectors read a local path and base64-inline the bytes
# themselves (see server/api/routes/chat.py). Everything else needs a real
# provider-side upload before the model can see the file.
_DIRECT_READ_BACKENDS = frozenset({"gemini", "groq", "mistral", "openai"})

_DEFAULT_CONFIG: dict[str, str] = {
    "model": "",
    "thinking_mode": "",
    "persona": "",
    "custom_prompt": "",
}

# Task instructions per action. ``ask`` uses the user's own question on top.
# Replaces the full Sable instruction registry (persona, skills, MCP, tool
# schemas — including chat_title) for study answers. Study questions must not
# drag in the chat harness, so we hand the model a self-contained directive.
_STUDY_SYSTEM = (
    "You are an expert study assistant embedded in a PDF reader. "
    "Answer the user's request directly using the document context provided. "
    "Never call tools, never emit tool tags, and never output a chat_title "
    "block. Just answer in clear, well-structured markdown."
)

# Safety net: strip any chat_title / stray tool tags the model emits anyway.
_TITLE_TAG_RE = re.compile(r"<chat_title>.*?</chat_title>", re.S | re.I)
_TOOL_TAG_RE = re.compile(r"</?(?:action|tool_call|chat_title)\b[^>]*>", re.I)

_ACTION_PROMPTS: dict[str, str] = {
    "explain": (
        "Explain the selected text clearly and simply. Break down what it means, "
        "why it matters, and any implied steps or assumptions."
    ),
    "meaning": (
        "Give the precise meaning of the selected text. Clarify every term and "
        "call out any ambiguity."
    ),
    "summarize": "Summarize the selected text into concise key points as a bullet list.",
    "define": "Define each key term or concept in the selected text, one per line.",
    "ask": "Answer the user's question using the document context above.",
    "summary": (
        "Write a structured summary of this document: a short overview, the main "
        "sections, and the key takeaways."
    ),
    "concepts": "List the key concepts in this document, each with a one-line explanation.",
    "exam": (
        "Generate likely exam questions from this document. Follow each question "
        "with a concise model answer."
    ),
    "glossary": "Build a glossary of the important terms in this document (term — definition).",
}


# ── Config ───────────────────────────────────────────────────────────────────


def _load_config() -> dict[str, str]:
    cfg = dict(_DEFAULT_CONFIG)
    if _CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    if not cfg.get("model"):
        models = get_all_models()
        if models:
            cfg["model"] = models[0]["id"]
    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Meta endpoints ───────────────────────────────────────────────────────────


@router.get("/api/study/ai/models")
def study_ai_models() -> dict[str, Any]:
    """Selectable models + their thinking modes for the study AI config UI."""
    out: list[dict[str, Any]] = []
    for m in get_all_models():
        if m.get("hidden"):
            continue
        out.append({
            "id": m.get("id"),
            "label": m.get("label", m.get("id")),
            # Vision capability lets the frontend decide whether to attach a
            # rendered page image or fall back to extracted text.
            "capabilities": m.get("capabilities") or {},
            "thinking_modes": [
                {"id": t.get("id"), "label": t.get("label", t.get("id"))}
                for t in m.get("thinking_modes", [])
            ],
        })
    return {"models": out}


@router.get("/api/study/ai/personas")
def study_ai_personas() -> dict[str, Any]:
    """Persona names available for study answers (from instruction/*.md)."""
    skip = {"output_format.md", "personal.md"}
    names: list[str] = []
    if _INSTRUCTION_DIR.exists():
        for f in sorted(_INSTRUCTION_DIR.glob("*.md")):
            if f.name in skip or f.name.endswith(".example"):
                continue
            names.append(f.stem)
    return {"personas": names}


@router.get("/api/study/ai/config")
def study_ai_get_config() -> dict[str, str]:
    return _load_config()


@router.put("/api/study/ai/config")
async def study_ai_set_config(payload: dict[str, Any]) -> dict[str, str]:
    cfg = _load_config()
    for key in ("model", "thinking_mode", "persona", "custom_prompt"):
        if key in payload:
            cfg[key] = str(payload.get(key) or "")
    _save_config(cfg)
    return cfg


# ── Prompt building ──────────────────────────────────────────────────────────


def _build_context(meta: dict[str, Any], scope: str, page: int, selection: str) -> str:
    """Slice the document text to fit the requested scope within the budget."""
    page_text = _load_page_text(meta)

    if scope == "selection":
        if not (1 <= page <= len(page_text)):
            return ""
        ctx = (page_text[page - 1] or "").strip()
        if len(ctx) > _MAX_CTX_CHARS:
            idx = ctx.find(selection[:60]) if selection else -1
            if idx > 0:
                start = max(0, idx - _MAX_CTX_CHARS // 2)
                ctx = ctx[start:start + _MAX_CTX_CHARS]
            else:
                ctx = ctx[:_MAX_CTX_CHARS]
        return f"[Page {page}]\n{ctx}"

    if scope == "page":
        if 1 <= page <= len(page_text):
            return f"[Page {page}]\n{(page_text[page - 1] or '').strip()[:_MAX_CTX_CHARS]}"
        return ""

    # Whole document — pack pages until the budget runs out.
    parts: list[str] = []
    total = 0
    for i, text in enumerate(page_text, 1):
        text = (text or "").strip()
        if not text:
            continue
        chunk = f"[Page {i}]\n{text}\n"
        if total + len(chunk) > _MAX_DOC_CTX:
            parts.append(f"… ({len(page_text) - i + 1} more pages truncated)")
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)


def _read_persona(name: str) -> str:
    if not name:
        return ""
    path = _INSTRUCTION_DIR / f"{name}.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:_MAX_PERSONA_CHARS]
    except Exception:
        return ""


def _build_system(custom_prompt: str, persona_text: str) -> str:
    """System directive for study answers — replaces Sable's instruction registry."""
    system = _STUDY_SYSTEM
    if custom_prompt.strip():
        system += "\n\nAdditional instructions from the reader:\n" + custom_prompt.strip()
    if persona_text:
        system += "\n\nAdopt the following persona and tone:\n" + persona_text
    return system


def _build_prompt(
    meta: dict[str, Any],
    action: str,
    question: str,
    selection: str,
    scope: str,
    page: int,
    custom_prompt: str,
    persona_text: str,
) -> tuple[str, str]:
    """Return ``(system_instruction, user_prompt)`` for a study answer.

    Keeping them separate lets API connectors receive the directive as a real
    system instruction while Qwen gets it inline (its session instructions are
    global and can't be overridden per request).
    """
    action_instr = _ACTION_PROMPTS.get(action, _ACTION_PROMPTS["ask"])
    context = _build_context(meta, scope, page, selection)
    system = _build_system(custom_prompt, persona_text)

    blocks = [f'Document: "{meta.get("title", "")}"']
    if context:
        blocks.append("Document context:\n---\n" + context + "\n---")
    if selection:
        blocks.append("Selected text:\n---\n" + selection[:_MAX_SELECTION_CHARS] + "\n---")
    blocks.append("Task: " + action_instr)
    if action == "ask" and question.strip():
        blocks.append("Question: " + question.strip())
    return system, "\n\n".join(blocks)


# ── Model streaming (Qwen scraper path OR API connector path) ────────────────


async def _stream(
    system: str,
    prompt: str,
    model: str,
    thinking_mode: str,
    *,
    files: list[str] | None = None,
    ref_file_ids: list[str] | None = None,
    ref_files: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield normalized events ({type: answer|thinking|error}) for any model.

    Attachment kwargs come from :func:`_resolve_attachments` — never a raw
    local path on its own. ``files`` carries paths only for direct-read
    connectors (they inline the bytes); ``ref_file_ids`` / ``ref_files`` carry
    the provider-side upload result for every other backend.

    Instruction handling is deliberate: study answers get the study directive
    instead of Sable's instruction registry, so the ``chat_title`` tool schema
    (and every other chat-harness tool) never reaches the model.
    """
    if model and _is_api_model(model):
        backend = _resolve_api_backend(model)
        conn = get_connector(backend, model_id=model)
        kwargs: dict[str, Any] = {
            "model": model,
            "thinking_mode": thinking_mode or None,
            "inject_instructions": False,
            "system_instruction": system,
        }
        if files:
            kwargs["files"] = files
        if ref_file_ids:
            kwargs["ref_file_ids"] = ref_file_ids
        if ref_files:
            kwargs["ref_files"] = ref_files
        try:
            agen = conn.stream_chat(prompt, **kwargs)
        except TypeError:
            # Connector doesn't accept system_instruction — drop the registry
            # override but still suppress the default instruction injection.
            kwargs.pop("system_instruction", None)
            agen = conn.stream_chat(prompt, **kwargs)
        async for ev in agen:
            yield ev
        return

    # Qwen/scraper path: its session instructions are global and synced ahead
    # of time, so the study directive is prefixed to the message instead.
    # Any chat_title tag the model emits anyway is stripped downstream.
    qwen_message = f"[STUDY MODE INSTRUCTIONS]\n{system}\n\n{prompt}"
    # Qwen's payload builder expects already-uploaded file objects here, not
    # local paths — an un-uploaded path is silently useless to the model.
    qwen_files = ref_files or None
    async for ev in service.stream_events(
        qwen_message,
        model=model or None,
        thinking_mode=thinking_mode or None,
        files=qwen_files,
    ):
        yield ev


@router.post("/api/study/doc/{doc_id}/ai")
async def study_ai_answer(doc_id: str, payload: dict[str, Any]) -> StreamingResponse:
    """Stream an answer grounded in the document. SSE events:

    ``{type:start}`` → ``{type:thinking|answer}``… → ``{type:done,text}``
    """
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")

    cfg = _load_config()
    model = str(payload.get("model") or cfg.get("model") or "")
    thinking_mode = str(payload.get("thinking_mode") or cfg.get("thinking_mode") or "")
    persona = str(payload.get("persona") if "persona" in payload else cfg.get("persona") or "")
    custom_prompt = str(
        payload.get("custom_prompt") if "custom_prompt" in payload else cfg.get("custom_prompt") or ""
    )
    action = str(payload.get("action") or "ask")
    question = str(payload.get("question") or "")
    selection = str(payload.get("selection") or "")
    scope = str(payload.get("scope") or ("selection" if selection else "page"))
    try:
        page = int(payload.get("page") or 1)
    except (TypeError, ValueError):
        page = 1

    system, prompt = _build_prompt(
        meta, action, question, selection, scope, page, custom_prompt, _read_persona(persona)
    )

    async def gen() -> AsyncGenerator[str, None]:
        parts: list[str] = []
        yield sse({"type": "start", "action": action, "model": model})
        try:
            async for ev in _stream(system, prompt, model, thinking_mode):
                etype = ev.get("type")
                if etype == "answer":
                    text = _strip_title_tags(str(ev.get("text", "")))
                    parts.append(text)
                    yield sse({"type": "answer", "text": text})
                elif etype == "thinking":
                    yield sse({"type": "thinking", "text": ev.get("text", "")})
                elif etype == "error":
                    yield sse({"type": "error", "message": ev.get("message", "error")})
                    return
        except Exception as exc:  # surface any transport failure to the card
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return
        yield sse({"type": "done", "text": _strip_title_tags("".join(parts)).strip()})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _strip_title_tags(text: str) -> str:
    """Remove chat_title / stray tool tags from an answer (belt-and-braces)."""
    if not text:
        return text
    cleaned = _TITLE_TAG_RE.sub("", text)
    return _TOOL_TAG_RE.sub("", cleaned)


# ── Attachments (page image / PDF) ───────────────────────────────────────────

_MAX_ATTACH_BYTES = 20 * 1024 * 1024


def _persist_attachments(atts: list[dict[str, Any]], workdir: Path) -> list[str]:
    """Write base64 attachments to temp files and return their paths.

    Connectors take file *paths* (`files=[...]`), not raw bytes — so image and
    PDF attachments both land on disk here and are cleaned up by the caller.
    """
    paths: list[str] = []
    for i, att in enumerate(atts or []):
        data = str(att.get("data") or "")
        if not data:
            continue
        # Accept a raw base64 string or a data: URL.
        if data.startswith("data:"):
            _head, _, data = data.partition(",")
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception:
            continue
        if not raw or len(raw) > _MAX_ATTACH_BYTES:
            continue
        name = str(att.get("name") or f"attachment-{i}")
        # Keep only the extension from the client name — never trust the path.
        ext = Path(name).suffix.lower() or ("" if att.get("kind") == "image" else ".pdf")
        safe = f"att-{i}{ext}"
        dest = workdir / safe
        dest.write_bytes(raw)
        paths.append(str(dest))
    return paths


def _serialize_conversation(messages: list[dict[str, Any]]) -> str:
    """Render a multi-turn thread as a single prompt string.

    Study answers reuse the plain ``stream_chat(message)`` entry point (no
    provider session), so prior turns are flattened into the prompt. That keeps
    the whole conversation — every user question AND every assistant answer —
    visible to the model on each turn.
    """
    lines: list[str] = []
    for m in messages:
        role = str(m.get("role") or "user")
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {content}")
    return "\n\n".join(lines)


async def _upload_attachment(backend: str, path: str) -> dict[str, Any] | None:
    """Upload one local file to the provider, returning its file object.

    Mirrors the per-backend routing in ``server/api/routes/chat.py`` so Study
    AI attaches files exactly the way the main chat does instead of handing a
    local temp path to a provider that can't read it.
    """
    try:
        if backend == "deepseek":
            return await service.upload_deepseek_file(path)
        if backend == "zai":
            from connectors.zai.client import get_client
            return await get_client().upload_file(path)
        if backend == "gemini_web":
            from connectors.gemini_web.client import get_client
            return await get_client().upload_file(path)
        if backend == "mimo":
            from connectors.mimo.client import get_client
            return await get_client().upload_file(path)
        if backend == "mistral_web":
            from connectors.mistral_web.client import get_client
            return await get_client().upload_file(path)
        if backend == "kimi":
            from connectors.kimi.client import get_client
            return await get_client().upload_file(path)
    except Exception as exc:
        logger.warning("study attach upload failed (%s): %s", backend, exc)
        return None
    # Qwen / scraper models (no api_backend) go through the Qwen OSS uploader.
    try:
        return await service.upload_file(path)
    except Exception as exc:
        logger.warning("study attach Qwen upload failed: %s", exc)
        return None


async def _resolve_attachments(model: str, file_paths: list[str]) -> dict[str, Any]:
    """Route attachments the same way the main chat route does.

    Returns keyword arguments for :func:`_stream`: ``files`` for direct-read
    connectors (they inline the bytes themselves), plus ``ref_file_ids`` and
    ``ref_files`` for every backend that needs a real provider-side upload.
    """
    if not file_paths:
        return {}

    backend = _resolve_api_backend(model) if _is_api_model(model) else None

    # Direct-read connectors take local paths and base64-inline the bytes.
    if backend in _DIRECT_READ_BACKENDS:
        return {"files": list(file_paths)}

    resolved: list[dict[str, Any]] = []
    for path in file_paths:
        meta = await _upload_attachment(backend or "", path)
        if meta:
            resolved.append(meta)
        else:
            logger.warning("study attach: could not upload %s", path)
    if not resolved:
        return {}

    out: dict[str, Any] = {}
    ref_ids = [
        str(m.get("file_id") or m.get("id"))
        for m in resolved
        if m.get("file_id") or m.get("id")
    ]
    if ref_ids:
        out["ref_file_ids"] = ref_ids

    # Some backends need the FULL uploaded object, not just an id: z.ai and
    # Gemini Web put it in their completion body, MiMo/Mistral key off `url`.
    # DeepSeek/Kimi consume the id alone, so they get nothing extra here.
    if backend in ("zai", "gemini_web"):
        full = [m for m in resolved if m.get("id") or m.get("blob")]
    elif backend == "mimo":
        full = [m for m in resolved if m.get("fileUrl")]
    elif backend == "mistral_web":
        full = [m for m in resolved if m.get("url")]
    elif backend is None:
        # Qwen rides the whole object on the payload's `files` array.
        full = resolved
    else:
        full = []
    if full:
        out["ref_files"] = full
    return out


@router.post("/api/study/doc/{doc_id}/chat")
async def study_ai_chat(doc_id: str, payload: dict[str, Any]) -> StreamingResponse:
    """Stream a multi-turn study answer.

    body: {
      messages:   [{role, content}],        # full thread, oldest first
      attachments:[{kind:'image'|'text', name, data}],  # base64
      action?, scope?, page?, selection?,   # optional document grounding
      model?, thinking_mode?, persona?, custom_prompt?
    }

    Unlike the single-shot endpoint, this keeps every turn: the client sends
    the whole conversation and the server returns one more assistant turn.
    """
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")

    cfg = _load_config()
    model = str(payload.get("model") or cfg.get("model") or "")
    thinking_mode = str(payload.get("thinking_mode") or cfg.get("thinking_mode") or "")
    persona = str(payload.get("persona") if "persona" in payload else cfg.get("persona") or "")
    custom_prompt = str(
        payload.get("custom_prompt") if "custom_prompt" in payload else cfg.get("custom_prompt") or ""
    )
    scope = str(payload.get("scope") or "doc")
    selection = str(payload.get("selection") or "")
    try:
        page = int(payload.get("page") or 1)
    except (TypeError, ValueError):
        page = 1

    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages required")
    messages = [m for m in messages if isinstance(m, dict)][-40:]

    system = _build_system(custom_prompt, _read_persona(persona))
    context = _build_context(meta, scope, page, selection)

    blocks = [f'Document: "{meta.get("title", "")}"']
    if context:
        blocks.append("Document context:\n---\n" + context + "\n---")
    blocks.append("Conversation:\n" + _serialize_conversation(messages))
    blocks.append(
        "Task: Answer the user's latest message, using the document context and "
        "the conversation so far. Keep continuity with your earlier answers."
    )
    prompt = "\n\n".join(blocks)

    atts = payload.get("attachments") or []
    if not isinstance(atts, list):
        atts = []

    # Image attachments need a model that can actually see; text attachments
    # (the fallback the client sends for non-vision models) are folded into the
    # prompt so every model still gets the page/PDF content.
    image_atts = [a for a in atts if isinstance(a, dict) and a.get("kind") != "text"]
    text_atts = [a for a in atts if isinstance(a, dict) and a.get("kind") == "text"]
    if text_atts:
        extra = []
        for a in text_atts:
            data = str(a.get("data") or "")
            if data.startswith("data:"):
                _head, _, data = data.partition(",")
                try:
                    data = base64.b64decode(data).decode("utf-8", "ignore")
                except Exception:
                    data = ""
            label = str(a.get("name") or "attachment")
            if data.strip():
                extra.append(f"[{label}]\n{data.strip()[:_MAX_CTX_CHARS]}")
        if extra:
            prompt += "\n\nAttached content:\n---\n" + "\n\n".join(extra) + "\n---"

    try:
        caps = (get_model_config(model) or {}).get("capabilities") or {}
    except Exception:
        caps = {}
    can_see_images = bool(caps.get("image"))
    can_read_docs = bool(caps.get("document"))
    # Keep only attachments this model can actually accept; anything else was
    # already downgraded to a text attachment by the client.
    sendable = [
        a for a in image_atts
        if (can_see_images and a.get("kind") != "document")
        or (can_read_docs and a.get("kind") == "document")
    ]

    async def gen() -> AsyncGenerator[str, None]:
        workdir = Path(tempfile.mkdtemp(prefix="sable-study-att-"))
        file_paths = _persist_attachments(sendable, workdir)
        # Upload to the provider (or keep paths for direct-read connectors) the
        # same way the main chat route does, so a vision model actually receives
        # the page image instead of an unreadable local temp path.
        attach_kwargs = await _resolve_attachments(model, file_paths)
        parts: list[str] = []
        yield sse({"type": "start", "model": model, "attachments": len(file_paths)})
        try:
            async for ev in _stream(system, prompt, model, thinking_mode, **attach_kwargs):
                etype = ev.get("type")
                if etype == "answer":
                    text = _strip_title_tags(str(ev.get("text", "")))
                    parts.append(text)
                    yield sse({"type": "answer", "text": text})
                elif etype == "thinking":
                    yield sse({"type": "thinking", "text": ev.get("text", "")})
                elif etype == "error":
                    yield sse({"type": "error", "message": ev.get("message", "error")})
                    return
        except Exception as exc:  # surface any transport failure to the card
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        yield sse({"type": "done", "text": _strip_title_tags("".join(parts)).strip()})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Saved AI notes (page-anchored answers) ───────────────────────────────────


@router.post("/api/study/doc/{doc_id}/notes")
def study_add_note(doc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist an AI answer against a selection / page / document."""
    # Full thread (all turns), so a saved note shows the whole conversation
    # instead of only the last answer. Falls back to a single turn when the
    # client sends just question/answer.
    raw_msgs = payload.get("messages")
    messages: list[dict[str, str]] = []
    if isinstance(raw_msgs, list):
        for m in raw_msgs[-40:]:
            if not isinstance(m, dict):
                continue
            content = str(m.get("content") or "")[:60000]
            if not content:
                continue
            messages.append({
                "role": "assistant" if str(m.get("role")) == "assistant" else "user",
                "content": content,
            })

    note = {
        "id": uuid.uuid4().hex[:12],
        "page": int(payload.get("page") or 0),
        "rects": _clean_rects(payload.get("rects")),
        "text": str(payload.get("text") or "")[:_MAX_SELECTION_CHARS],
        "action": str(payload.get("action") or "ask")[:40],
        "question": str(payload.get("question") or "")[:2000],
        "answer": str(payload.get("answer") or "")[:60000],
        "messages": messages,
        "model": str(payload.get("model") or "")[:80],
        "scope": str(payload.get("scope") or "selection")[:20],
        "created": time.time(),
    }

    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        notes = meta.get("ai_notes") or []
        notes.append(note)
        meta["ai_notes"] = notes
        return note

    return _update_meta(doc_id, mutate)


@router.patch("/api/study/doc/{doc_id}/notes/{note_id}")
def study_update_note(doc_id: str, note_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        for note in meta.get("ai_notes") or []:
            if note.get("id") == note_id:
                if "answer" in payload:
                    note["answer"] = str(payload["answer"])[:60000]
                if "question" in payload:
                    note["question"] = str(payload["question"])[:2000]
                return note
        raise HTTPException(status_code=404, detail="note not found")

    return _update_meta(doc_id, mutate)


@router.delete("/api/study/doc/{doc_id}/notes/{note_id}")
def study_delete_note(doc_id: str, note_id: str) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        notes = meta.get("ai_notes") or []
        kept = [n for n in notes if n.get("id") != note_id]
        if len(kept) == len(notes):
            raise HTTPException(status_code=404, detail="note not found")
        meta["ai_notes"] = kept
        return {"deleted": True, "id": note_id}

    return _update_meta(doc_id, mutate)
