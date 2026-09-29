"""Study-mode Humanizer — LLM rewrite + rule-based structural pass.

Two-phase, matching skills/text_humanizer/instruction.md:

  1. **LLM rewrite** — persona-conditioned, using whatever model/thinking mode
     the caller picks (same model registry study_ai.py uses).
  2. **Rule pass** — shells out to skills/text_humanizer/scripts/humanize.py
     for burstiness, perplexity swaps, n-gram collapse, and AI-tell removal.

Exposes:
  GET  /api/study/humanize/models    selectable models + thinking modes
  GET  /api/study/humanize/personas  built-in persona prompts
  GET  /api/study/humanize/config    persisted defaults
  PUT  /api/study/humanize/config    save defaults
  POST /api/study/humanize           { text, persona?, model?, thinking_mode?,
                                       strength?, seed?, skip_llm? }
                                     -> { text, stats, phases }

The rule script is OPTIONAL at runtime — if it is missing, the endpoint still
returns the LLM rewrite and flags ``rule_pass: false`` so the UI can say so.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from engine.config import get_all_models
from connectors import get_connector
from server.utils import _is_api_model, _resolve_api_backend

from ..dependencies import service

router = APIRouter()
logger = logging.getLogger("sable.study.humanize")


async def _humanize_stream(
    system: str,
    prompt: str,
    model: str,
    thinking_mode: str,
):
    """Stateless stream for the humanizer.

    Unlike ``study_ai._stream``, this NEVER attaches the study directive, the
    Maria/Sable persona, or the Sable output-format primer. The model receives
    exactly one instruction — the humanizer's own ``system`` — and nothing else.
    For the scraper path we open a throwaway upstream chat (``stateless=True``)
    so no shared session history bleeds in either.
    """
    if model and _is_api_model(model):
        backend = _resolve_api_backend(model)
        conn = get_connector(backend, model_id=model)
        kwargs: dict[str, Any] = {
            "model": model,
            "thinking_mode": thinking_mode or None,
            "inject_instructions": False,   # no Sable/Maria instruction registry
            "system_instruction": system,   # the humanizer instruction, only
            "layout_mode": "study",         # suppress chat_title handler
        }
        try:
            agen = conn.stream_chat(prompt, **kwargs)
        except TypeError:
            kwargs.pop("layout_mode", None)
            kwargs.pop("system_instruction", None)
            agen = conn.stream_chat(prompt, **kwargs)
        async for ev in agen:
            yield ev
        return

    # Scraper/Qwen path: a fresh throwaway chat, raw send, no reminders.
    # ``stateless=True`` is not on this signature — service.stream_events is
    # the lower-level curl path. Wrap the instruction into the message so it
    # is the ONLY thing the model sees, and rely on create_new_chat() (called
    # when chat_id is None) for a clean session with no shared history.
    msg = f"[SYSTEM INSTRUCTION]\n{system}\n\n[USER MESSAGE]\n{prompt}"
    async for ev in service.stream_events(
        msg,
        model=model or None,
        thinking_mode=thinking_mode or None,
    ):
        yield ev

# Safety net for the Qwen/scraper path: strip stray tool/title syntax from the
# rewrite. The API path emits native tool calls and is handled upstream.
_TITLE_TAG_RE = re.compile(r"<chat_title>.*?</chat_title>", re.S | re.I)
# Any stray tool tags (<action>, <tool_call>, <execute_command>, ...).
_TOOL_TAG_RE = re.compile(r"</?(?:action|tool_call|execute_command|grep|glob|chat_title)\b[^>]*>", re.I)
# A bare <action>[{...}]</action> block, with or without the surrounding tag.
_ACTION_BLOCK_RE = re.compile(r"<action>\s*\[.*?\]\s*</action>", re.S | re.I)


def _overlap_ratio(a: str, b: str) -> float:
    """How much of ``a`` is copied verbatim into ``b``, 0..1.

    Measures shared *bigrams* (adjacent word pairs) rather than single words.
    A light paraphrase reuses most content words but reorders them, so it
    scores low; a lazy copy keeps the same word pairs, so it scores high.
    This is what distinguishes "changed a word" from "didn't touch it".
    """
    wa = re.findall(r"\w+", a.lower())
    wb = re.findall(r"\w+", b.lower())
    if len(wa) < 2 or len(wb) < 2:
        return 0.0
    bigrams_a = set(zip(wa, wa[1:]))
    bigrams_b = set(zip(wb, wb[1:]))
    if not bigrams_a:
        return 0.0
    return len(bigrams_a & bigrams_b) / len(bigrams_a)

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_PATH = _ROOT / "system" / "study_humanize_config.json"
_SCRIPT = _ROOT / "skills" / "text_humanizer" / "scripts" / "humanize.py"

_MAX_INPUT_CHARS = 60_000

# ── Personas ────────────────────────────────────────────────────────────────
# Mirrors the persona prompts in skills/text_humanizer/instruction.md. Kept
# here (not read from disk) so the API can list them without a file walk and
# the UI shows a stable set.

_PERSONAS: dict[str, dict[str, str]] = {
    "peer": {
        "label": "Peer (default)",
        "prompt": (
            "Rewrite as if explaining to a colleague over coffee. Smart but "
            "tired. Skip obvious context. Use contractions. Start sentences "
            "with 'And' or 'But.' Have opinions. Don't summarize at the end. "
            "Just stop when done."
        ),
    },
    "grad": {
        "label": "Grad (academic)",
        "prompt": (
            "Rewrite like a PhD student writing their thesis discussion at 11 "
            "PM. Deep knowledge, exhausted. Direct, slightly irreverent. "
            "Domain jargon without explanation. Occasional tangent, then "
            "self-correction. No bullet points. No 'in summary.'"
        ),
    },
    "chaos": {
        "label": "Chaos (anti-detector)",
        "prompt": (
            "Rewrite this text as a knowledgeable human expert writing a "
            "professional report or analysis — someone who deeply understands "
            "the topic but is typing fast and NOT proofreading. The goal is "
            "to defeat AI detectors (GPTZero, Turnitin, Originality.ai) by "
            "killing both statistical signals: perplexity and burstiness.\n"
            "CRITICAL: keep a FORMAL, professional register. Do NOT make it "
            "casual, chatty, or conversational. No slang, no 'gonna', no "
            "rhetorical questions, no addressing the reader.\n"
            "Structure — attack burstiness:\n"
            "- Vary sentence length wildly: most sentences 8-25 words, but "
            "mix in short jabs (3-6 words) and occasional long runs (up to "
            "40+). Never let several sentences land at the same length.\n"
            "- Include one deliberate sentence fragment.\n"
            "- No equal-length paragraphs. Break the text into many "
            "sentences — do not merge everything into one giant run-on. "
            "Every sentence must end with a full stop, question mark, or "
            "exclamation, and stay readable in length.\n"
            "- Start one sentence with 'And' or 'But'.\n"
            "- Remove all hedging. No conclusion — just stop when done.\n"
            "Vocabulary — attack perplexity:\n"
            "- Formal vocabulary (Thus, can be considered, core input cost, "
            "directly related, prominent, covering around, top rated).\n"
            "- Kill predictable, high-probability phrasing. If it is the "
            "word a model reaches for first, use the one a person would.\n"
            "- Few or NO contractions — a formal report does not use them. "
            "At most one per paragraph if it aids natural rhythm.\n"
            "- Never use: delve, leverage, tapestry, furthermore, moreover, "
            "notably, 'it is important to note', 'it's important to note', "
            "'in conclusion', 'additionally', 'crucial', 'landscape', "
            "'multifaceted', 'nuanced', 'comprehensive', 'seamless', "
            "'robust', 'remarkably', 'plenty of', 'various', 'numerous'.\n"
            "Grammar: this is the key. Leave in the mistakes a real expert "
            "makes when they don't proofread:\n"
            "  * subject-verb slips (\"these towers runs\", \"it must run\")\n"
            "  * a missing article (\"in market with reliable uptime\")\n"
            "  * wrong article (\"An shocking spike\", \"a huge hammer\")\n"
            "  * a capital letter mid-sentence (\"electricity, Which also...\")\n"
            "  * missing possessive apostrophe (\"executives shiny head\")\n"
            "  * a missing plural (\"direct input cost\" where it should be "
            "costs)\n"
            "  * a comma splice where a full stop belonged\n"
            "- Do NOT clean up the grammar mistakes. They are the point. But "
            "keep the vocabulary formal and the sentences substantive.\n"
            "Output only the rewritten text."
        ),
    },
}

_SYSTEM = (
    "You are a rewriting engine whose job is to make machine-written text "
    "pass as human-written prose.\n"
    "RULES:\n"
    "1. REWRITE AGGRESSIVELY. Do not lightly paraphrase or swap a word here "
    "and there — rebuild every sentence from scratch with different word "
    "choice, different structure, and different rhythm. A rewrite that "
    "shares more than a few consecutive words with the original has FAILED.\n"
    "2. PRESERVE ONLY THE FACTS: every number, name, date, and technical term "
    "must survive unchanged. Nothing else must. The exact wording does NOT "
    "need to survive — only the information does.\n"
    "3. Kill predictable, high-probability phrasing. If a phrase is the one "
    "a language model would reach for first, replace it with the one a "
    "person would reach for.\n"
    "4. Do not add information. Do not omit information. Do not summarize.\n"
    "Output ONLY the rewritten text. No preamble, no explanation, no quotes, "
    "no markdown fences, no 'Here is' opener.\n"
    "You have NO tools. Do NOT call any tool or function. Do NOT emit "
    "<action>, <tool_call>, <chat_title>, or any tool/function syntax. "
    "Do NOT set, suggest, or output a chat title — never emit a chat_title "
    "block or a title of any kind. Your entire response is the rewritten "
    "text and nothing else."
)

_DEFAULT_CONFIG: dict[str, str] = {
    "model": "",
    "thinking_mode": "",
    "persona": "peer",
    "strength": "0.6",
    "seed": "",
    "typos": "0",
}


# ── Config ──────────────────────────────────────────────────────────────────


def _load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = dict(_DEFAULT_CONFIG)
    if _CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            logger.warning("humanize config unreadable", exc_info=True)
    # Legacy: the standalone "anti" persona was merged into "chaos", and
    # the free-form "custom" persona was removed. Both fall back gracefully.
    if cfg.get("persona") == "anti":
        cfg["persona"] = "chaos"
    elif cfg.get("persona") == "custom":
        cfg["persona"] = "peer"
    if not cfg.get("model"):
        models = get_all_models()
        if models:
            cfg["model"] = models[0]["id"]
    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Rule pass ───────────────────────────────────────────────────────────────


async def _run_rule_pass(
    text: str, *, strength: float, seed: int | None, typos: float = 0.0,
) -> tuple[str, dict[str, Any] | None]:
    """Shell the humanize.py rule pass. Returns (text, stats|None).

    Failures are non-fatal: the LLM rewrite is still returned, with stats=None
    and the caller told the rule pass didn't run.
    """
    if not _SCRIPT.exists():
        logger.warning("humanize.py not found at %s — skipping rule pass", _SCRIPT)
        return text, None

    cmd = [sys.executable, str(_SCRIPT), "--stats", "--strength", f"{strength:.2f}"]
    if typos > 0:
        cmd += ["--typos", f"{typos:.2f}"]
    if seed is not None:
        cmd += ["--seed", str(seed)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(text.encode("utf-8")), timeout=30
        )
    except asyncio.TimeoutError:
        logger.warning("humanize rule pass timed out")
        return text, None
    except Exception:
        logger.warning("humanize rule pass failed", exc_info=True)
        return text, None

    if proc.returncode != 0:
        logger.warning("humanize.py exited %s: %s", proc.returncode, err.decode("utf-8", "replace")[:400])
        return text, None

    stats: dict[str, Any] | None = None
    stderr_text = err.decode("utf-8", "replace").strip()
    if stderr_text:
        try:
            stats = json.loads(stderr_text)
        except Exception:
            stats = None

    return out.decode("utf-8", "replace").strip(), stats


# ── LLM rewrite ─────────────────────────────────────────────────────────────


async def _llm_rewrite(
    text: str,
    *,
    system: str,
    prompt: str,
    model: str,
    thinking_mode: str,
) -> str:
    """Collect the streaming rewrite into one string.

    Uses the dedicated stateless ``_humanize_stream`` — the model sees only the
    humanizer instruction, never the study directive, Maria persona, or Sable
    output-format primer.
    """
    parts: list[str] = []
    error: str | None = None
    async for ev in _humanize_stream(system, prompt, model, thinking_mode):
        etype = ev.get("type")
        if etype == "answer":
            # Connectors emit {"type": "answer", "text": ...} (see
            # connectors/base.py). Accept "content" too for safety.
            parts.append(ev.get("text") or ev.get("content") or "")
        elif etype in ("chat_title", "skill_start", "skill_end", "skill_output"):
            # Belt-and-braces: the connector suppresses chat_title in study
            # layout, but a stale/older connector build — or a scraper session
            # with a persistent system prompt — could still emit it. A
            # humanizer rewrite must NEVER rename the user's chat or run a
            # tool. Swallow every tool-related event here.
            logger.warning("humanizer: ignored stray %s event", etype)
        elif etype == "error":
            error = ev.get("message") or ev.get("error") or "model error"
    if error and not parts:
        raise RuntimeError(error)
    out = "".join(parts).strip()

    # Strip any tool/title syntax the model emitted as plain text. The API path
    # uses native tool calls (handled above), but the Qwen/scraper path can leak
    # the tag into the answer stream — especially when the upstream session's
    # system prompt still nudges it to call chat_title.
    out = _TITLE_TAG_RE.sub("", out)
    out = _TOOL_TAG_RE.sub("", out)
    out = _ACTION_BLOCK_RE.sub("", out).strip()

    # Guard: a rate-limit / auth / refusal string is NOT a rewrite. Returning it
    # as "humanized text" would silently hand the user a quota message, so treat
    # it as a hard failure instead.
    low = out.lower()
    _BAD = (
        "chat limit", "rate limit", "try again tomorrow", "try again later",
        "quota exceeded", "too many requests", "unauthorized", "not logged in",
        "session expired", "please log in", "sign in to continue",
    )
    if any(b in low for b in _BAD) and len(out) < 400:
        raise RuntimeError(f"model returned a non-answer: {out[:120]!r}")

    return out


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("/api/study/humanize/models")
def humanize_models() -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for m in get_all_models():
        if m.get("hidden"):
            continue
        out.append({
            "id": m.get("id"),
            "label": m.get("label", m.get("id")),
            "thinking_modes": [
                {"id": t.get("id"), "label": t.get("label", t.get("id"))}
                for t in m.get("thinking_modes", [])
            ],
        })
    return {"models": out}


@router.get("/api/study/humanize/personas")
def humanize_personas() -> dict[str, Any]:
    return {
        "personas": [
            {"id": pid, "label": p["label"], "prompt": p["prompt"]}
            for pid, p in _PERSONAS.items()
        ]
    }


@router.get("/api/study/humanize/config")
def humanize_get_config() -> dict[str, Any]:
    return _load_config()


@router.put("/api/study/humanize/config")
async def humanize_set_config(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_config()
    for key in ("model", "thinking_mode", "persona", "strength", "seed"):
        if key in payload:
            cfg[key] = str(payload.get(key) or "")
    # Typos need care: switching TO chaos should raise the default, but an
    # explicit numeric value (including 0) must win. Never persist "".
    if "typos" in payload:
        raw = payload.get("typos")
        if raw in (None, ""):
            raw = cfg.get("typos") or "0"
        cfg["typos"] = str(raw)
    elif cfg.get("persona") == "chaos" and not cfg.get("typos"):
        cfg["typos"] = "0.8"
    _save_config(cfg)
    return cfg


@router.post("/api/study/humanize")
async def humanize_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Humanize text: LLM rewrite (Phase 1) then rule pass (Phase 2).

    Body:
      text*          the input
      persona?       peer | grad | chaos
      model?         model id (falls back to saved config)
      thinking_mode? thinking mode id
      strength?      0..1 rule-pass aggressiveness
      seed?          int for reproducible rule pass
      skip_llm?      true -> rule pass only (fast, deterministic)
    """
    cfg = _load_config()

    text = str(payload.get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > _MAX_INPUT_CHARS:
        raise HTTPException(status_code=413, detail=f"text exceeds {_MAX_INPUT_CHARS} chars")

    persona = str(payload.get("persona") or cfg.get("persona") or "peer")
    model = str(payload.get("model") or cfg.get("model") or "")
    thinking_mode = str(payload.get("thinking_mode") or cfg.get("thinking_mode") or "")

    try:
        strength = float(payload.get("strength", cfg.get("strength", 0.6)))
    except (TypeError, ValueError):
        strength = 0.6
    strength = max(0.0, min(1.0, strength))

    # Typos: an explicit numeric payload wins; otherwise the chaos persona
    # implies a default. Treat "" / None / junk as "not provided" so the
    # chaos default actually fires — an empty string from a stale config
    # must NOT silently become 0.
    typos_raw = payload.get("typos")
    if typos_raw in (None, ""):
        typos = 0.8 if persona == "chaos" else 0.0
    else:
        try:
            typos = float(typos_raw)
        except (TypeError, ValueError):
            typos = 0.8 if persona == "chaos" else 0.0
    typos = max(0.0, min(1.0, typos))

    seed_raw = payload.get("seed", cfg.get("seed", ""))
    seed: int | None = None
    if seed_raw not in (None, "", "null"):
        try:
            seed = int(seed_raw)
        except (TypeError, ValueError):
            seed = None

    skip_llm = bool(payload.get("skip_llm"))
    phases: dict[str, Any] = {"llm": False, "rule": False}
    text_after_llm = text

    # ── Phase 1: LLM rewrite ──
    if not skip_llm:
        pdef = _PERSONAS.get(persona) or _PERSONAS["peer"]
        persona_prompt = pdef["prompt"]

        prompt = (
            f"Persona / style instructions:\n{persona_prompt}\n\n"
            "Rewrite the text below from scratch. Change the wording and "
            "sentence structure aggressively while keeping every fact intact. "
            "Output only the rewritten text.\n\n"
            f"---\n{text}"
        )
        try:
            rewritten = await _llm_rewrite(
                text, system=_SYSTEM, prompt=prompt,
                model=model, thinking_mode=thinking_mode,
            )
        except Exception as exc:
            logger.warning("humanize LLM rewrite failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"LLM rewrite failed: {exc}")
        if not rewritten.strip():
            raise HTTPException(status_code=502, detail="LLM returned an empty rewrite")

        # Distance check: a lazy rewrite shares long runs with the input. If
        # the model barely touched it, push once with an explicit nudge. This
        # is the difference between "lightly paraphrased" and "actually human".
        try:
            _sim = _overlap_ratio(text, rewritten)
        except Exception:
            _sim = 0.0
        if _sim > 0.55:
            logger.info("humanize: rewrite too close (%.2f) — retrying harder", _sim)
            harder = (
                prompt
                + "\n\nYour previous rewrite was too similar to the original. "
                "Rewrite it again, completely differently: new sentence "
                "boundaries, new vocabulary, new rhythm. Keep the facts. "
                "The result must not read like the text above."
            )
            try:
                rewritten2 = await _llm_rewrite(
                    text, system=_SYSTEM, prompt=harder,
                    model=model, thinking_mode=thinking_mode,
                )
                if rewritten2.strip() and _overlap_ratio(text, rewritten2) < _sim:
                    rewritten = rewritten2
            except Exception as exc:
                logger.warning("humanize retry failed (keeping first): %s", exc)

        text_after_llm = rewritten
        phases["llm"] = True
        phases["model"] = model
        phases["persona"] = persona
        phases["rewrite_distance"] = round(1.0 - _sim, 3)

    # ── Phase 2: rule pass ──
    final, stats = await _run_rule_pass(
        text_after_llm, strength=strength, seed=seed, typos=typos,
    )
    phases["rule"] = stats is not None
    phases["strength"] = strength
    phases["typos"] = typos
    phases["seed"] = seed

    return {
        "text": final or text_after_llm,
        "original": text,
        "stats": stats,
        "phases": phases,
    }
