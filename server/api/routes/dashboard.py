"""Dashboard API — aggregated stats for the dashboard panel."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from server.database.core import get_db

router = APIRouter()


@router.get("/api/dashboard/stats")
def dashboard_stats() -> dict[str, Any]:
    """Aggregate stats: token usage, agent counts, recent activity."""
    with get_db() as conn:
        # Total agents by status
        agent_counts = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) as cnt FROM agent_runs GROUP BY status"
        ).fetchall():
            agent_counts[row["status"]] = row["cnt"]

        # Token usage by provider (from messages table — real tracking)
        token_by_model = []
        for row in conn.execute(
            "SELECT c.provider, "
            "SUM(m.prompt_tokens + m.completion_tokens) as total_tokens, "
            "COUNT(DISTINCT m.chat_id) as run_count "
            "FROM messages m JOIN chats c ON m.chat_id = c.id "
            "WHERE (m.prompt_tokens > 0 OR m.completion_tokens > 0) "
            "AND c.provider IS NOT NULL "
            "GROUP BY c.provider ORDER BY total_tokens DESC LIMIT 10"
        ).fetchall():
            token_by_model.append({
                "model": row["provider"],
                "total_tokens": row["total_tokens"],
                "run_count": row["run_count"],
            })

        # Daily token burn (last 7 days) — from messages table
        daily_tokens = []
        for row in conn.execute(
            "SELECT DATE(m.created_at) as day, "
            "SUM(m.prompt_tokens + m.completion_tokens) as tokens, "
            "COUNT(DISTINCT m.chat_id) as runs "
            "FROM messages m "
            "WHERE (m.prompt_tokens > 0 OR m.completion_tokens > 0) "
            "AND m.created_at >= DATE('now', '-7 days') "
            "GROUP BY day ORDER BY day ASC"
        ).fetchall():
            daily_tokens.append({
                "day": row["day"],
                "tokens": row["tokens"],
                "runs": row["runs"],
            })

        # Chat stats
        chat_stats = conn.execute(
            "SELECT COUNT(*) as total_chats, "
            "(SELECT COUNT(*) FROM messages) as total_messages "
            "FROM chats"
        ).fetchone()

        # Recent chats (last 8)
        recent_chats = []
        for row in conn.execute(
            "SELECT id, title, updated_at, mode, provider FROM chats "
            "ORDER BY updated_at DESC LIMIT 8"
        ).fetchall():
            recent_chats.append(dict(row))

        # Provider breakdown
        provider_breakdown = []
        for row in conn.execute(
            "SELECT provider, COUNT(*) as cnt FROM chats "
            "WHERE provider IS NOT NULL GROUP BY provider ORDER BY cnt DESC"
        ).fetchall():
            provider_breakdown.append({"provider": row["provider"], "count": row["cnt"]})

    return {
        "agent_counts": agent_counts,
        "token_by_model": token_by_model,
        "daily_tokens": daily_tokens,
        "chat_stats": {
            "total_chats": chat_stats["total_chats"],
            "total_messages": chat_stats["total_messages"],
        },
        "recent_chats": recent_chats,
        "provider_breakdown": provider_breakdown,
    }


@router.get("/api/dashboard/agents")
def dashboard_agents(limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    """Recent agent runs across all chats, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, role, task, status, model, tokens_used, "
            "created_at, completed_at, error "
            "FROM agent_runs ORDER BY created_at DESC LIMIT ?",
            (min(limit, 50),),
        ).fetchall()
        agents = []
        for row in rows:
            d = dict(row)
            # Truncate task for display
            if d.get("task") and len(d["task"]) > 120:
                d["task"] = d["task"][:120] + "…"
            agents.append(d)
    return {"agents": agents}


@router.get("/api/dashboard/ops")
def dashboard_ops() -> dict[str, list[dict[str, Any]]]:
    """Scheduled agent ops with health status."""
    from server.database.agents import list_agent_ops
    from server.utils import utcnow

    ops = list_agent_ops()
    now = utcnow()

    enriched = []
    for op in ops:
        health = "disabled"
        if op.get("enabled"):
            last_run = op.get("last_run")
            next_run = op.get("next_run")
            last_result = op.get("last_result", "")

            if last_result and ("error" in str(last_result).lower() or "fail" in str(last_result).lower()):
                health = "failed"
            elif next_run and next_run < now and not last_run:
                health = "missed"
            elif last_run:
                health = "ok"
            else:
                health = "pending"

        enriched.append({**op, "health": health})

    return {"ops": enriched}


@router.get("/api/dashboard/status")
def dashboard_status() -> dict[str, Any]:
    """Counts for tasks, todos, notes, research, calendar events."""
    from pathlib import Path
    from server.database.notes import list_notes

    # Todos (note_type='todo')
    todos = list_notes(note_type="todo")
    total_todo_items = 0
    done_todo_items = 0
    for t in todos:
        items = t.get("items", [])
        total_todo_items += len(items)
        done_todo_items += sum(1 for i in items if i.get("done"))

    # Notes (scan sable_output/notes directory)
    notes_count = 0
    try:
        from engine.config import NOTES_DIR
        if NOTES_DIR.is_dir():
            notes_count = sum(1 for f in NOTES_DIR.iterdir() if f.suffix == ".md")
    except Exception:
        pass

    # Research sessions (scan sable_output/research directory)
    research_count = 0
    try:
        from engine.config import RESEARCH_DIR
        if RESEARCH_DIR.is_dir():
            research_count = sum(1 for f in RESEARCH_DIR.iterdir() if f.suffix == ".md")
    except Exception:
        pass

    # Calendar: today's events count
    from datetime import date
    today = date.today().isoformat()
    calendar_today = 0
    try:
        from server.api.routes.tracknote import get_calendar_events
        y, m = today.split("-")[:2]
        evts = get_calendar_events(int(y), int(m))
        calendar_today = len(evts.get("events", {}).get(today, []))
    except Exception:
        pass

    # Agent task counts
    with get_db() as conn:
        running_tasks = conn.execute(
            "SELECT COUNT(*) as cnt FROM agent_runs WHERE status IN ('spawned', 'running')"
        ).fetchone()["cnt"]
        completed_tasks = conn.execute(
            "SELECT COUNT(*) as cnt FROM agent_runs WHERE status = 'completed'"
        ).fetchone()["cnt"]
        failed_tasks = conn.execute(
            "SELECT COUNT(*) as cnt FROM agent_runs WHERE status IN ('failed', 'timed_out', 'killed')"
        ).fetchone()["cnt"]

    return {
        "tasks": {
            "running": running_tasks,
            "completed": completed_tasks,
            "failed": failed_tasks,
            "total": running_tasks + completed_tasks + failed_tasks,
        },
        "todos": {
            "total_notes": len(todos),
            "total_items": total_todo_items,
            "done_items": done_todo_items,
            "pending_items": total_todo_items - done_todo_items,
        },
        "notes": {
            "count": notes_count,
        },
        "research": {
            "sessions": research_count,
        },
        "calendar": {
            "today_events": calendar_today,
        },
    }


@router.get("/api/dashboard/providers")
def dashboard_providers() -> dict[str, Any]:
    """All providers with keys. Status check is separate endpoint."""
    import json as _json
    from engine.config import _SYSTEM

    settings: dict = {}
    settings_path = _SYSTEM / "settings.json"
    if settings_path.is_file():
        try:
            settings = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    providers: list[dict[str, Any]] = []

    # --- Mistral ---
    try:
        from connectors.mistral.client import get_client as get_mistral_client
        mc = get_mistral_client()
        providers.append({"name": "mistral", "label": "Mistral",
                          "keys": mc.list_keys() if mc.is_available else []})
    except Exception:
        providers.append({"name": "mistral", "label": "Mistral", "keys": []})

    # --- Groq ---
    try:
        from connectors.groq.client import get_client as get_groq_client
        gc = get_groq_client()
        providers.append({"name": "groq", "label": "Groq",
                          "keys": gc.list_keys() if gc.is_available else []})
    except Exception:
        providers.append({"name": "groq", "label": "Groq", "keys": []})

    # --- Gemini ---
    try:
        from connectors.gemini.client import GeminiClient
        gemini = GeminiClient()
        providers.append({"name": "gemini", "label": "Gemini",
                          "keys": gemini.list_keys() if gemini.is_available else []})
    except Exception:
        providers.append({"name": "gemini", "label": "Gemini", "keys": []})

    # --- DeepSeek (system-managed, always alive) ---
    try:
        from connectors.deepseek.client import get_unique_tokens
        ds_tokens = get_unique_tokens()
        ds_keys = []
        for i, tok in enumerate(ds_tokens):
            masked = tok[:8] + "..." + tok[-4:] if len(tok) > 12 else "***"
            ds_keys.append({"index": i, "masked": masked, "active": i == 0})
        providers.append({"name": "deepseek", "label": "DeepSeek", "keys": ds_keys, "system": True})
    except Exception:
        # Fallback: read token store directly if connector import fails
        ds_keys = []
        try:
            ts_path = _SYSTEM / ".deepseek_tokens.json"
            if ts_path.is_file():
                ts_data = _json.loads(ts_path.read_text(encoding="utf-8"))
                seen: set[str] = set()
                idx = 0
                for tokens in ts_data.values():
                    tok_list = tokens if isinstance(tokens, list) else [tokens]
                    for t in tok_list:
                        if t and t != "None" and t not in seen:
                            seen.add(t)
                            masked = t[:8] + "..." + t[-4:] if len(str(t)) > 12 else "***"
                            ds_keys.append({"index": idx, "masked": masked, "active": idx == 0})
                            idx += 1
        except Exception:
            pass
        providers.append({"name": "deepseek", "label": "DeepSeek", "keys": ds_keys, "system": True})

    # --- Qwen (session/cookie-based, system-managed, always alive) ---
    qw_keys: list[dict[str, Any]] = []
    try:
        qw_path = _SYSTEM / ".qwen_tokens.json"
        if qw_path.is_file():
            qw_data = _json.loads(qw_path.read_text(encoding="utf-8"))
            idx = 0
            for account, entries in qw_data.items():
                entry_list = entries if isinstance(entries, list) else [entries]
                for entry in entry_list:
                    # Each entry is a dict with 'cookies' key or a raw cookie string
                    if isinstance(entry, dict):
                        cookies = entry.get("cookies", "")
                    elif isinstance(entry, str):
                        cookies = entry
                    else:
                        continue
                    if cookies:
                        # Mask: show account name + first cookie fragment
                        c_str = str(cookies)
                        preview = c_str[:20] + "..." if len(c_str) > 20 else c_str
                        masked = f"{account}: {preview}"
                        qw_keys.append({"index": idx, "masked": masked, "active": idx == 0})
                        idx += 1
    except Exception:
        pass
    providers.append({"name": "qwen", "label": "Qwen", "keys": qw_keys, "system": True})

    # --- Cloudflare ---
    try:
        from connectors.cloudflare.client import get_client as get_cf_client
        cf = get_cf_client()
        cf_keys: list[dict[str, Any]] = []
        if cf.is_available:
            cf_keys.append({"index": 0, "masked": "configured", "active": True})
        providers.append({"name": "cloudflare", "label": "Cloudflare", "keys": cf_keys})
    except Exception:
        providers.append({"name": "cloudflare", "label": "Cloudflare", "keys": []})

    # --- Tavily ---
    tavily_key = settings.get("tavily_api_key", "")
    tv_keys: list[dict[str, Any]] = []
    if tavily_key:
        tv_keys.append({"index": 0, "masked": "configured", "active": True})
    providers.append({"name": "tavily", "label": "Tavily", "keys": tv_keys})

    # --- Serper ---
    serper_key = settings.get("serper_api_key", "")
    sp_keys: list[dict[str, Any]] = []
    if serper_key:
        sp_keys.append({"index": 0, "masked": "configured", "active": True})
    providers.append({"name": "serper", "label": "Serper", "keys": sp_keys})

    return {"providers": providers}


@router.get("/api/dashboard/providers/status")
def dashboard_provider_status() -> dict[str, Any]:
    """Ping one key per provider to check alive status. Fast."""
    import httpx
    import json as _json
    from concurrent.futures import ThreadPoolExecutor
    from engine.config import _SYSTEM

    timeout = httpx.Timeout(3.0)

    def _ping(url: str, headers: dict | None = None, method: str = "GET", body: dict | None = None) -> bool:
        try:
            if method == "POST" and body:
                r = httpx.post(url, json=body, headers=headers or {}, timeout=timeout)
            else:
                r = httpx.request(method, url, headers=headers or {}, timeout=timeout)
            return r.status_code < 400
        except Exception:
            return False

    settings: dict = {}
    settings_path = _SYSTEM / "settings.json"
    if settings_path.is_file():
        try:
            settings = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # One ping per provider (current/first key only)
    ping_tasks: list[tuple[str, str, dict, str, dict | None]] = []

    # Mistral
    try:
        from connectors.mistral.client import get_client as get_mistral_client
        mc = get_mistral_client()
        if mc.is_available and mc._current_key:
            ping_tasks.append(("mistral", "https://api.mistral.ai/v1/models",
                               {"Authorization": f"Bearer {mc._current_key}"}, "GET", None))
    except Exception:
        pass

    # Groq
    try:
        from connectors.groq.client import get_client as get_groq_client
        gc = get_groq_client()
        if gc.is_available and gc._current_key:
            ping_tasks.append(("groq", "https://api.groq.com/openai/v1/models",
                               {"Authorization": f"Bearer {gc._current_key}"}, "GET", None))
    except Exception:
        pass

    # Gemini
    try:
        from connectors.gemini.client import GeminiClient
        gemini = GeminiClient()
        if gemini.is_available and gemini._current_key:
            ping_tasks.append(("gemini",
                               f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini._current_key}",
                               {}, "GET", None))
    except Exception:
        pass

    # Cloudflare
    try:
        from connectors.cloudflare.client import get_client as get_cf_client
        cf = get_cf_client()
        if cf.is_available:
            ping_tasks.append(("cloudflare",
                               "https://api.cloudflare.com/client/v4/accounts/" + getattr(cf, '_account_id', '') + "/ai/models/search",
                               {"Authorization": f"Bearer {getattr(cf, '_api_token', '')}"}, "GET", None))
    except Exception:
        pass

    # Tavily
    tavily_key = settings.get("tavily_api_key", "")
    if tavily_key:
        ping_tasks.append(("tavily", "https://api.tavily.com/search",
                           {"Content-Type": "application/json"}, "POST",
                           {"api_key": tavily_key, "query": "test", "max_results": 1}))

    # Serper
    serper_key = settings.get("serper_api_key", "")
    if serper_key:
        ping_tasks.append(("serper", "https://google.serper.dev/search",
                           {"X-API-KEY": serper_key, "Content-Type": "application/json"}, "POST",
                           {"q": "test", "num": 1}))

    # Ping concurrently (max 6 tasks)
    status: dict[str, bool] = {"deepseek": True, "qwen": True}  # system-managed
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {}
        for name, url, headers, method, body in ping_tasks:
            futures[pool.submit(_ping, url, headers, method, body)] = name
        for fut, name in futures.items():
            try:
                status[name] = fut.result(timeout=4)
            except Exception:
                status[name] = False

    return {"status": status}
