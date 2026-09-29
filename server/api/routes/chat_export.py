"""Chat transcript export — Copy Chat (markdown) and Download Chat (PDF).

Both actions share one builder: messages + skill_events are flattened into a
single markdown transcript (user / assistant / thinking / tool call / tool
output / status / critique). Copy returns that markdown verbatim; Download
renders it through the project's own vendored ``marked.min.js`` inside a
headless Chromium and prints to PDF, so the exported document matches the
chat UI instead of a hand-rolled second renderer.
"""

from __future__ import annotations

import base64
import html as _html
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from server.database import get_db, get_messages

logger = logging.getLogger("sable.chat_export")

router = APIRouter()

# Project root -> web/vendor/marked.min.js (same parser the UI uses).
_WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# Prefixes the UI strips before showing a user message.
_MEM_RE = re.compile(r"^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n")
# Real user rows look like "[2026-09-18 11:28:31 | cwd: /path]\nmessage"; older
# rows omit the cwd segment. Capture the timestamp, swallow the rest of the line.
_TS_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\s*\|[^\]]*)?\]\n?"
)

# Internal attrs injected by the engine; never part of what the user asked for.
_ATTR_NOISE = {"_chat_id"}

# "<\/script>" — built without a literal escape so this module compiles warning-free.
_ESCAPED_SCRIPT_CLOSE = "<" + chr(92) + "/script>"


# ── helpers ──────────────────────────────────────────────────────────────────


def _fence(text: str, lang: str = "text") -> str:
    """Wrap text in a code fence that its own content cannot break out of."""
    longest = run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    bar = "`" * max(3, longest + 1)
    return f"{bar}{lang}\n{text}\n{bar}"


def _clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop engine-internal keys so they don't show up as tool arguments."""
    return {k: v for k, v in (attrs or {}).items() if k not in _ATTR_NOISE}


def _fmt_ts(value: str | None) -> str:
    """Normalize any stored timestamp to local 'YYYY-MM-DD HH:MM:SS'.

    User rows carry local time already (parsed out of the content prefix),
    assistant rows carry the UTC ``created_at`` ISO string. Rendering both
    through here keeps one consistent clock in the transcript.
    """
    if not value:
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
        return text  # already local wall-clock from the user prefix
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _safe_filename(name: str) -> str:
    base = re.sub(r'[\/:*?"<>|\x00-\x1f]+', "_", (name or "chat")).strip().strip(".")
    return (base or "chat")[:80]


def _chat_title(chat_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        return None
    return (row["title"] or "Chat") if not isinstance(row, tuple) else (row[0] or "Chat")


# ── event replay ─────────────────────────────────────────────────────────────


def _replay_events(events: list[dict[str, Any]], items: list[dict[str, Any]]) -> None:
    """Flatten an ordered skill_event stream into renderable items.

    Mirrors the ordering rules in web/js/sse.js: thinking, text and tool calls
    appear in the order they streamed, with each tool's output/end folded back
    into its own block by tag id.
    """
    tools: dict[Any, dict[str, Any]] = {}
    critiques: dict[Any, dict[str, Any]] = {}

    for ev in events:
        et = ev.get("type")

        if et == "round_thinking":
            items.append({"t": "thinking", "text": ev.get("text") or ""})

        elif et == "round_text":
            if (ev.get("text") or "").strip():
                items.append({"t": "text", "text": ev.get("text")})

        elif et == "skill_start":
            name = ev.get("name") or "skill"
            if name == "ask_user":
                continue  # rendered from its skill_output payload instead
            data = ev.get("data") or {}
            block = {
                "t": "tool",
                "name": name,
                "args": _clean_attrs(data.get("attrs") or {}),
                "content": data.get("content") or "",
                "outputs": [],
                "end": None,
            }
            tools[ev.get("id")] = block
            items.append(block)

        elif et == "skill_output":
            if ev.get("name") == "ask_user":
                try:
                    payload = json.loads(ev.get("text") or "{}")
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                question = payload.get("question") or ""
                answer = (
                    payload.get("answer")
                    or payload.get("selected")
                    or payload.get("response")
                    or ""
                )
                note = f"❓ **Asked:** {question}" if question else "❓ **Asked the user**"
                if answer:
                    note += f"\n\n**Answer:** {answer}"
                items.append({"t": "note", "text": note})
                continue
            block = tools.get(ev.get("id"))
            if block is not None:
                block["outputs"].append(ev.get("text") or "")

        elif et == "skill_end":
            if ev.get("name") == "ask_user":
                continue
            block = tools.get(ev.get("id"))
            if block is not None:
                block["end"] = ev

        elif et == "permission_request":
            data = ev.get("data") or {}
            cmd = str(data.get("command") or ev.get("name") or "")[:200]
            items.append({"t": "note", "text": f"🔒 **Permission requested:** {cmd}"})

        elif et == "cwd_warning":
            data = ev.get("data") or {}
            items.append({"t": "note", "text": f"⚠️ **CWD warning:** {str(data.get('path') or '')[:200]}"})

        elif et == "critique_start":
            block = {
                "t": "critique",
                "focus": ev.get("focus") or "general",
                "context": ev.get("context") or "",
                "criteria": ev.get("criteria") or "",
                "report": None,
                "error": None,
                "tools": [],
            }
            critiques[ev.get("id")] = block
            items.append(block)

        elif et == "critique_tool":
            block = critiques.get(ev.get("id"))
            if block is not None:
                block["tools"].append(
                    {"tool": ev.get("tool") or "", "output": ev.get("output") or ""}
                )

        elif et == "critique_done":
            block = critiques.get(ev.get("id"))
            if block is not None:
                block["report"] = ev.get("report")
                block["error"] = ev.get("error")

        elif et == "agent_result":
            state = "completed" if ev.get("ok") else "failed"
            items.append({
                "t": "note",
                "text": f"🤖 **Agent {state}:** `{ev.get('agent_id') or ''}`",
            })

        elif et == "file_edit":
            data = ev.get("data") or {}
            path = data.get("path") or data.get("filename") or ""
            items.append({"t": "note", "text": f"📝 **File edit:** `{path}`"})

        elif et == "memory_used":
            memories = ev.get("memories") or []
            if memories:
                items.append({"t": "memory", "items": memories})


def _build_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn stored messages into ordered render blocks."""
    blocks: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role") or ""
        if role == "system":
            continue

        content = msg.get("content") or ""
        events = msg.get("skill_events") or []
        stamp = _fmt_ts(msg.get("created_at"))

        if role == "user":
            text = content
            mem = _MEM_RE.match(text)
            if mem:
                text = text[mem.end():]
            ts = _TS_RE.match(text)
            if ts:
                stamp = _fmt_ts(ts.group(1))
                text = text[ts.end():]
            blocks.append({"kind": "user", "ts": stamp, "text": text.strip()})
            continue

        # assistant (and legacy "tool" rows)
        has_round_text = any(
            e.get("type") == "round_text" and (e.get("text") or "").strip() for e in events
        )
        has_round_thinking = any(e.get("type") == "round_thinking" for e in events)

        items: list[dict[str, Any]] = []

        # Legacy thinking column only shows when no round_thinking events exist,
        # otherwise it would duplicate the replayed stream.
        if not has_round_thinking and (msg.get("thinking") or "").strip():
            items.append({"t": "thinking", "text": msg["thinking"]})

        # When round_text events exist the stored content is the merged blob;
        # the events are the source of truth for ordering (same as the UI).
        if not has_round_text and content.strip():
            items.append({"t": "text", "text": content})

        _replay_events(events, items)

        memories = msg.get("memory_used") or []
        if isinstance(memories, list) and memories:
            items.append({"t": "memory", "items": memories})

        if items:
            blocks.append({"kind": "assistant", "ts": stamp, "items": items})

    return blocks


# ── markdown rendering ───────────────────────────────────────────────────────


def _render_item(item: dict[str, Any]) -> list[str]:
    kind = item["t"]
    out: list[str] = []

    if kind == "text":
        out += [item["text"].strip(), ""]

    elif kind == "thinking":
        body = (item["text"] or "").strip()
        out += ["### 💭 Thinking", ""]
        if body:
            out += [_fence(body, "text"), ""]

    elif kind == "tool":
        out += [f"### ⚡ Tool · `{item['name']}`", ""]
        # Mirror the UI's createSkillCard(): prefer structured attrs, fall back
        # to the raw tag content. Showing both just duplicates the same params.
        args = item.get("args") or {}
        raw_input = (item.get("content") or "").rstrip("\n")
        if args:
            out += [
                "**Arguments**",
                "",
                _fence(json.dumps(args, indent=2, ensure_ascii=False), "json"),
                "",
            ]
        elif raw_input:
            out += ["**Input**", "", _fence(raw_input, "text"), ""]
        output = "".join(item.get("outputs") or []).rstrip("\n")
        if output:
            out += ["**Output**", "", _fence(output, "text"), ""]
        end = item.get("end")
        if end is None:
            out += ["**Status:** ⏳ running", ""]
        else:
            ok = end.get("ok", False)
            duration = end.get("duration_ms")
            status = "✅ done" if ok else "❌ failed"
            if duration is not None:
                status += f" · {duration}ms"
            out += [f"**Status:** {status}", ""]
            if end.get("error"):
                out += [f"**Error:** {end['error']}", ""]

    elif kind == "memory":
        entries = [m for m in item["items"] if isinstance(m, dict) and m.get("value")]
        if entries:
            out += ["### 🧠 Memory used", ""]
            for entry in entries:
                out.append(f"- **{entry.get('key', '')}**: {entry.get('value', '')}")
            out.append("")

    elif kind == "note":
        out += [item["text"], ""]

    elif kind == "critique":
        out += [f"### 🔍 Critique · {item['focus']}", ""]
        if item["context"]:
            out += [f"**Context:** {item['context']}", ""]
        if item["criteria"]:
            out += [f"**Criteria:** {item['criteria']}", ""]
        for tool in item["tools"]:
            out.append(f"- `{tool['tool']}` → {tool['output'][:200]}")
        if item["tools"]:
            out.append("")
        if item["error"]:
            out += [f"**Failed:** {item['error']}", ""]
        elif item["report"]:
            out += [item["report"].strip(), ""]

    return out


def _render_markdown(
    title: str,
    chat_id: str,
    blocks: list[dict[str, Any]],
    exported: str,
) -> str:
    lines = [
        f"# {title}",
        "",
        f"> **Chat ID:** `{chat_id}`  ",
        f"> **Messages:** {sum(1 for b in blocks if b['kind'] == 'user')} user"
        f" / {sum(1 for b in blocks if b['kind'] == 'assistant')} assistant  ",
        f"> **Exported:** {exported}",
        "",
    ]

    for block in blocks:
        lines += ["---", ""]
        if block["kind"] == "user":
            heading = "## 👤 User"
            if block.get("ts"):
                heading += f" · `{block['ts']}`"
            lines += [heading, "", block["text"] or "_(empty)_", ""]
        else:
            heading = "## 🤖 Assistant"
            if block.get("ts"):
                heading += f" · `{block['ts']}`"
            lines += [heading, ""]
            for item in block["items"]:
                lines += _render_item(item)

    return "\n".join(lines).rstrip() + "\n"


def build_transcript(chat_id: str) -> str:
    """Full markdown transcript for a chat. Raises LookupError if unknown."""
    title = _chat_title(chat_id)
    if title is None:
        raise LookupError(chat_id)
    messages = get_messages(chat_id, include_skill_events=True)
    blocks = _build_blocks(messages)
    exported = datetime.now().strftime("%Y-%m-%d %H:%M")
    return _render_markdown(title, chat_id, blocks, exported)


# ── PDF rendering ────────────────────────────────────────────────────────────

_PDF_CSS = """
@page { size: A4; }
* { box-sizing: border-box; }
body {
  font-family: 'DejaVu Sans', Helvetica, Arial, sans-serif;
  font-size: 10.5px; line-height: 1.5; color: #14141a; margin: 0;
}
h1 { font-size: 19px; margin: 0 0 6px; }
h2 {
  font-size: 14px; margin: 16px 0 6px; padding-bottom: 3px;
  border-bottom: 1px solid #e2e2ea; break-after: avoid; page-break-after: avoid;
}
h3 { font-size: 11.5px; margin: 11px 0 5px; color: #33333d; break-after: avoid; page-break-after: avoid; }
p { margin: 6px 0; }
a { color: #1a4fa0; text-decoration: none; word-break: break-all; }
code {
  font-family: 'DejaVu Sans Mono', monospace; font-size: 9px;
  background: #f1f1f5; padding: 1px 3px; border-radius: 3px;
}
pre {
  font-family: 'DejaVu Sans Mono', monospace; font-size: 8.4px; line-height: 1.42;
  background: #f6f6f9; border: 1px solid #e4e4ec; border-radius: 4px;
  padding: 7px 9px; margin: 5px 0;
  white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere;
}
pre code { background: none; padding: 0; font-size: inherit; }
blockquote {
  margin: 6px 0; padding: 3px 12px;
  border-left: 3px solid #c2c2cf; color: #44444f; background: #fafafc;
}
blockquote p { margin: 3px 0; }
ul, ol { margin: 5px 0; padding-left: 22px; }
li { margin: 2px 0; }
table { border-collapse: collapse; width: 100%; margin: 7px 0; }
th, td { border: 1px solid #ccccd6; padding: 4px 7px; font-size: 9.3px; text-align: left; vertical-align: top; }
th { background: #f0f0f5; }
hr { border: none; border-top: 1px solid #e4e4ec; margin: 14px 0; }
img { max-width: 100%; }
"""

_PDF_FOOTER = (
    '<div style="width:100%;font-size:8px;color:#888;text-align:center;'
    'font-family:Helvetica,Arial,sans-serif;">'
    'Page <span class="pageNumber"></span> / <span class="totalPages"></span>'
    "</div>"
)


async def _render_pdf(markdown_text: str, title: str) -> bytes:
    """Render the transcript to PDF with headless Chromium.

    The markdown is base64'd into the page and parsed by the project's own
    vendored marked.min.js, so the PDF matches the chat UI exactly (code
    fences, tables, emoji) instead of going through a second renderer.
    """
    from playwright.async_api import async_playwright

    marked_js = (_WEB_DIR / "vendor" / "marked.min.js").read_text(encoding="utf-8")
    # Defensive: a future marked release could contain a literal </script>,
    # which would close the tag early when the bundle is inlined below.
    marked_js = marked_js.replace("</script>", _ESCAPED_SCRIPT_CLOSE)
    payload = base64.b64encode(markdown_text.encode("utf-8")).decode("ascii")

    document = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_html.escape(title)}</title>"
        f"<style>{_PDF_CSS}</style>"
        f"<script>{marked_js}</script>"
        "</head><body><article id='root'></article>"
        "<script>"
        f"const b='{payload}';"
        "const u=Uint8Array.from(atob(b),c=>c.charCodeAt(0));"
        "const md=new TextDecoder().decode(u);"
        "document.getElementById('root').innerHTML=marked.parse(md);"
        "</script></body></html>"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.set_content(document, wait_until="load")
            pdf = await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "16mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=_PDF_FOOTER,
            )
        finally:
            await browser.close()
    return pdf


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/api/chats/{chat_id}/export/transcript")
def export_transcript(chat_id: str) -> Response:
    """Full chat transcript as markdown — used by "Copy Chat"."""
    try:
        markdown_text = build_transcript(chat_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Chat not found")
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Transcript export failed for %s", chat_id)
        raise HTTPException(status_code=500, detail=f"Transcript export failed: {exc}")
    return Response(content=markdown_text, media_type="text/markdown; charset=utf-8")


@router.get("/api/chats/{chat_id}/export/pdf")
async def export_pdf(chat_id: str) -> Response:
    """Full chat transcript rendered to PDF — used by "Download Chat"."""
    try:
        markdown_text = build_transcript(chat_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Chat not found")
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Transcript build failed for %s", chat_id)
        raise HTTPException(status_code=500, detail=f"Transcript build failed: {exc}")

    title = _chat_title(chat_id) or "Chat"
    try:
        pdf_bytes = await _render_pdf(markdown_text, title)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("PDF render failed for %s", chat_id)
        raise HTTPException(status_code=500, detail=f"PDF render failed: {exc}")

    filename = _safe_filename(title) + ".pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
