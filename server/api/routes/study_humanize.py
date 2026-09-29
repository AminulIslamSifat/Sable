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
                                       strength?, seed?, custom_prompt?,
                                       skip_llm? } -> { text, stats, phases }

The rule script is OPTIONAL at runtime — if it is missing, the endpoint still
returns the LLM rewrite and flags ``rule_pass: false`` so the UI can say so.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import re

from fastapi import APIRouter, HTTPException

from engine.config import get_all_models

from .study_ai import _stream

router = APIRouter()
logger = logging.getLogger("sable.study.humanize")

# Safety net for the Qwen/scraper path: strip a stray title tag from the
# rewrite. The API path emits native tool calls and is handled upstream.
_TITLE_TAG_RE = re.compile(r"<chat_title>.*?</chat_title>", re.S | re.I)

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
    "anti": {
        "label": "Anti (detector evasion)",
        "prompt": (
            "Never use: delve, leverage, tapestry, furthermore, moreover, "
            "notably, 'it's important to note', 'in conclusion', "
            "'additionally', 'crucial', 'landscape', 'multifaceted', "
            "'nuanced', 'comprehensive', 'seamless', 'robust'. Vary sentence "
            "length wildly (3 words to 40+). Include a sentence fragment. "
            "One colloquial contraction per paragraph. No equal-length "
            "paragraphs. Start one sentence with 'And' or 'But'. Remove all "
            "hedging. No conclusion."
        ),
    },
    "custom": {
        "label": "Custom",
        "prompt": "",  # filled from the request's custom_prompt
    },
}

_SYSTEM = (
    "You are a rewriting engine. Rewrite the user's text so it reads as "
    "human-written, preserving every fact, number, name, and technical term "
    "EXACTLY as given.\n"
    "Output ONLY the rewritten text. No preamble, no explanation, no quotes, "
    "no markdown fences, no 'Here is' opener. Do not summarize. Do not add "
    "new information. Do not omit information.\n"
    "The rewrite is a style pass — meaning must be identical."
)

_DEFAULT_CONFIG: dict[str, str] = {
    "model": "",
    "thinking_mode": "",
    "persona": "peer",
    "strength": "0.6",
    "seed": "",
}


# ── Config ──────────────────────────────────────────────────────────────────


def _load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = dict(_DEFAULT_CONFIG)
    if _CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            logger.warning("humanize config unreadable", exc_info=True)
    if not cfg.get("model"):
        models = get_all_models()
        if models:
            cfg["model"] = models[0]["id"]
    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Rule pass ───────────────────────────────────────────────────────────────


async def _run_rule_pass(text: str, *, strength: float, seed: int | None) -> tuple[str, dict[str, Any] | None]:
    """Shell the humanize.py rule pass. Returns (text, stats|None).

    Failures are non-fatal: the LLM rewrite is still returned, with stats=None
    and the caller told the rule pass didn't run.
    """
    if not _SCRIPT.exists():
        logger.warning("humanize.py not found at %s — skipping rule pass", _SCRIPT)
        return text, None

    cmd = [sys.executable, str(_SCRIPT), "--stats", "--strength", f"{strength:.2f}"]
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

    Reuses study_ai._stream so the model registry, thinking-mode handling, and
    API-vs-Qwen routing are identical to every other study AI call.
    """
    parts: list[str] = []
    error: str | None = None
    async for ev in _stream(system, prompt, model, thinking_mode):
        etype = ev.get("type")
        if etype == "answer":
            # Connectors emit {"type": "answer", "text": ...} (see
            # connectors/base.py). Accept "content" too for safety.
            parts.append(ev.get("text") or ev.get("content") or "")
        elif etype == "chat_title":
            # Belt-and-braces: the connector suppresses this in study layout,
            # but a stale/older connector build could still emit it. Never let
            # a humanizer rewrite rename the user's chat.
            logger.warning("humanizer: ignored stray chat_title event")
        elif etype == "error":
            error = ev.get("message") or ev.get("error") or "model error"
    if error and not parts:
        raise RuntimeError(error)
    out = "".join(parts).strip()

    # Strip any <chat_title>...</chat_title> tag the model emitted as plain
    # text (the API path uses native tool calls, but the Qwen/scraper path can
    # still leak the tag into the answer stream).
    out = _TITLE_TAG_RE.sub("", out).strip()

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
    _save_config(cfg)
    return cfg


@router.post("/api/study/humanize")
async def humanize_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Humanize text: LLM rewrite (Phase 1) then rule pass (Phase 2).

    Body:
      text*          the input
      persona?       peer | grad | anti | custom
      custom_prompt? used when persona == custom (or appended otherwise)
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
    custom_prompt = str(payload.get("custom_prompt") or "")
    model = str(payload.get("model") or cfg.get("model") or "")
    thinking_mode = str(payload.get("thinking_mode") or cfg.get("thinking_mode") or "")

    try:
        strength = float(payload.get("strength", cfg.get("strength", 0.6)))
    except (TypeError, ValueError):
        strength = 0.6
    strength = max(0.0, min(1.0, strength))

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
        persona_prompt = custom_prompt.strip() if persona == "custom" else pdef["prompt"]
        if persona != "custom" and custom_prompt.strip():
            persona_prompt += "\n\nExtra instructions:\n" + custom_prompt.strip()

        prompt = (
            f"Persona / style instructions:\n{persona_prompt}\n\n"
            f"Rewrite the following text. Output only the rewritten text.\n\n"
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
        text_after_llm = rewritten
        phases["llm"] = True
        phases["model"] = model
        phases["persona"] = persona

    # ── Phase 2: rule pass ──
    final, stats = await _run_rule_pass(text_after_llm, strength=strength, seed=seed)
    phases["rule"] = stats is not None
    phases["strength"] = strength
    phases["seed"] = seed

    return {
        "text": final or text_after_llm,
        "original": text,
        "stats": stats,
        "phases": phases,
    }
