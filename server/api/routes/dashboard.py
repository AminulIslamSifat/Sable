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

        # Token usage by model (top 10)
        token_by_model = []
        for row in conn.execute(
            "SELECT model, SUM(tokens_used) as total_tokens, COUNT(*) as run_count "
            "FROM agent_runs WHERE tokens_used > 0 AND model IS NOT NULL "
            "GROUP BY model ORDER BY total_tokens DESC LIMIT 10"
        ).fetchall():
            token_by_model.append({
                "model": row["model"],
                "total_tokens": row["total_tokens"],
                "run_count": row["run_count"],
            })

        # Daily token burn (last 7 days)
        daily_tokens = []
        for row in conn.execute(
            "SELECT DATE(created_at) as day, SUM(tokens_used) as tokens, COUNT(*) as runs "
            "FROM agent_runs WHERE tokens_used > 0 "
            "AND created_at >= DATE('now', '-7 days') "
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
