
"""Research task registry — background runs, persistence, SSE progress fan-out."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from engine.config import RESEARCH_DIR

logger = logging.getLogger("sable.research.manager")

_SESSION_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)


def _json_path(session_id: str) -> Optional[Path]:
    if not _SESSION_RE.fullmatch(session_id or ""):
        return None
    p = (RESEARCH_DIR / f"{session_id}.json").resolve()
    try:
        p.relative_to(RESEARCH_DIR.resolve())
    except ValueError:
        return None
    return p


def _trim_nodes(nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce the live node map to a small serializable graph snapshot."""
    out = []
    for n in (nodes or {}).values():
        out.append({
            "id": n.get("id"), "parent": n.get("parent"), "depth": n.get("depth"),
            "kind": n.get("kind"), "label": (n.get("label") or "")[:80], "status": n.get("status"),
        })
    return out


def _slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9\s-]", "", text.lower()).strip()
    text = re.sub(r"[\s]+", "-", text)
    return text[:max_len].rstrip("-") or "research"


class ResearchManager:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        # session_id -> set of asyncio.Queue (SSE subscribers)
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start_research(
        self,
        query: str,
        model: str | None = None,
        models: list[str] | None = None,
        browser_data: list[str] | None = None,
        max_depth: int = 3,
        max_time: int = 1500,
        pages_per_topic: int = 3,
        owner: str = "",
    ) -> dict[str, Any]:
        from engine.research.engine import DeepResearcher

        # Ordered fallback lists; legacy single `model` seeds the model list.
        models = [m for m in (models or []) if m] or ([model] if model else [])
        accounts = [b for b in (browser_data or []) if b]
        logger.info("start_research | query=%r models=%s accounts=%s max_depth=%d max_time=%d owner=%s",
                     query, models, accounts, max_depth, max_time, owner)

        session_id = uuid.uuid4().hex[:16]
        entry: dict[str, Any] = {
            "researcher": None,
            "task": None,
            "query": query,
            "model": models[0] if models else None,
            "models": models,
            "browser_data": accounts,
            "owner": owner,
            "status": "running",
            "progress": {"type": "progress", "phase": "starting", "topics": 0, "pages": 0, "sources": 0, "status": "queued"},
            "result": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        }

        async def on_progress(payload: dict[str, Any]) -> None:
            # Only counter-style events update the stored progress snapshot;
            # graph_node/graph_status events are just fanned out to subscribers.
            if payload.get("type") == "progress" or "type" not in payload:
                entry["progress"] = payload
            await self._broadcast(session_id, payload)

        researcher = DeepResearcher(
            question=query,
            models=models,
            accounts=accounts,
            max_depth=max_depth,
            max_time=max_time,
            pages_per_topic=pages_per_topic,
            progress_callback=on_progress,
        )
        entry["researcher"] = researcher

        async def runner() -> None:
            try:
                logger.info("runner started | session=%s", session_id)
                report = await researcher.research()
                entry["status"] = "done"
                entry["result"] = report
                entry["finished_at"] = time.time()
                elapsed = entry["finished_at"] - entry["started_at"]
                logger.info("runner done | session=%s elapsed=%.1fs report_len=%d",
                            session_id, elapsed, len(report))
                await self._broadcast(session_id, {"type": "done", "phase": "done", "status": "complete"})
            except asyncio.CancelledError:
                entry["status"] = "cancelled"
                entry["finished_at"] = time.time()
                logger.warning("runner cancelled | session=%s", session_id)
                await self._broadcast(session_id, {"type": "error", "phase": "error", "status": "cancelled"})
            except Exception as e:
                logger.exception("runner failed | session=%s error=%s", session_id, e)
                entry["status"] = "error"
                entry["error"] = str(e)
                entry["finished_at"] = time.time()
                await self._broadcast(session_id, {"type": "error", "phase": "error", "status": str(e)})
            finally:
                self._persist(session_id, entry)

        entry["task"] = asyncio.create_task(runner())
        self._tasks[session_id] = entry
        # Persist immediately so incomplete sessions survive restarts
        self._persist(session_id, entry)
        return self.public_status(session_id)

    def cancel_research(self, session_id: str) -> bool:
        entry = self._tasks.get(session_id)
        if not entry or entry["status"] != "running":
            logger.debug("cancel_research | session=%s not running (status=%s)",
                         session_id, entry.get("status") if entry else "not found")
            return False
        logger.info("cancel_research | session=%s", session_id)
        researcher = entry.get("researcher")
        if researcher:
            researcher.cancel()
        task = entry.get("task")
        if task and not task.done():
            task.cancel()
        entry["status"] = "cancelled"
        entry["finished_at"] = time.time()
        self._persist(session_id, entry)
        return True

    # ── status / results ─────────────────────────────────────────────────────
    def public_status(self, session_id: str) -> Optional[dict[str, Any]]:
        entry = self._tasks.get(session_id)
        if entry:
            return {
                "session_id": session_id,
                "query": entry["query"],
                "status": entry["status"],
                "progress": entry["progress"],
                "error": entry["error"],
                "started_at": entry["started_at"],
                "finished_at": entry["finished_at"],
            }
        p = _json_path(session_id)
        if p and p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return {
                    "session_id": session_id,
                    "query": data.get("query", ""),
                    "status": data.get("status", "done"),
                    "progress": data.get("progress", {}),
                    "error": data.get("error"),
                    "started_at": data.get("started_at"),
                    "finished_at": data.get("finished_at"),
                }
            except Exception:
                return None
        return None

    def get_status(self, session_id: str) -> Optional[dict[str, Any]]:
        return self.public_status(session_id)

    def get_result(self, session_id: str) -> Optional[dict[str, Any]]:
        entry = self._tasks.get(session_id)
        if entry and entry.get("result") is not None:
            researcher = entry.get("researcher")
            return {
                "result": entry["result"],
                "sources": researcher.sources if researcher else [],
                "findings": researcher.findings if researcher else [],
                "nodes": _trim_nodes(researcher.nodes) if researcher else [],
            }
        p = _json_path(session_id)
        if p and p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return {
                    "result": data.get("result", ""),
                    "sources": data.get("sources", []),
                    "findings": data.get("findings", []),
                    "nodes": data.get("nodes", []),
                }
            except Exception:
                return None
        return None

    def list_active(self, owner: str = "") -> list[dict[str, Any]]:
        out = []
        for sid, entry in self._tasks.items():
            if entry["status"] == "running":
                out.append(self.public_status(sid))
        return out

    def list_incomplete(self) -> list[dict[str, Any]]:
        """Return all sessions that are incomplete (cancelled/error/interrupted).

        Scans both in-memory tasks and persisted JSON files on disk.
        Excludes sessions with status 'done'.
        """
        seen_ids: set[str] = set()
        out: list[dict[str, Any]] = []

        # In-memory first
        for sid, entry in self._tasks.items():
            if entry["status"] in ("cancelled", "error"):
                out.append(self.public_status(sid))
                seen_ids.add(sid)

        # On-disk sessions
        try:
            for p in RESEARCH_DIR.glob("*.json"):
                sid = p.stem
                if sid in seen_ids:
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    status = data.get("status", "unknown")
                    if status == "done":
                        continue
                    # Treat 'running' on disk as interrupted (server died mid-run)
                    if status == "running":
                        status = "interrupted"
                    out.append({
                        "session_id": sid,
                        "query": data.get("query", ""),
                        "status": status,
                        "progress": data.get("progress", {}),
                        "error": data.get("error"),
                        "started_at": data.get("started_at"),
                        "finished_at": data.get("finished_at"),
                    })
                    seen_ids.add(sid)
                except Exception:
                    continue
        except Exception as e:
            logger.warning("list_incomplete | failed to scan disk: %s", e)

        # Sort by started_at descending (most recent first)
        out.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
        return out

    def resume_research(
        self,
        session_id: str,
        model: str | None = None,
        models: list[str] | None = None,
        browser_data: list[str] | None = None,
        max_depth: int = 3,
        max_time: int = 1500,
        pages_per_topic: int = 3,
        owner: str = "",
    ) -> dict[str, Any]:
        """Resume an incomplete research session from its saved checkpoint.

        Loads findings, sources, and completed topics from the persisted JSON,
        then starts a new DeepResearcher with that state injected.
        Returns the new session's public status.
        """
        from engine.research.engine import DeepResearcher

        # Load saved state from disk or memory
        saved: Optional[dict[str, Any]] = None
        entry = self._tasks.get(session_id)
        if entry:
            researcher = entry.get("researcher")
            saved = {
                "query": entry["query"],
                "findings": researcher.findings if researcher else [],
                "sources": researcher.sources if researcher else [],
                "nodes": _trim_nodes(researcher.nodes) if researcher else [],
                "model": entry.get("model"),
                "models": entry.get("models", []),
                "browser_data": entry.get("browser_data", []),
                "max_depth": max_depth,
                "max_time": max_time,
                "pages_per_topic": pages_per_topic,
            }
        else:
            p = _json_path(session_id)
            if p and p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    saved = {
                        "query": data.get("query", ""),
                        "findings": data.get("findings", []),
                        "sources": data.get("sources", []),
                        "nodes": data.get("nodes", []),
                        "model": data.get("model"),
                        "models": data.get("models", []),
                        "browser_data": data.get("browser_data", []),
                    }
                except Exception as e:
                    logger.error("resume_research | failed to load session %s: %s", session_id, e)
                    return {"error": f"Failed to load session: {e}"}

        if not saved:
            return {"error": f"No saved state found for session {session_id}"}

        query = saved["query"]
        if not query:
            return {"error": "Saved session has no query"}

        # Determine which topics were completed (nodes with kind=topic and status=done)
        completed_topics: list[str] = []
        for node in saved.get("nodes", []):
            if node.get("kind") == "topic" and node.get("status") == "done":
                label = node.get("label", "")
                if label:
                    completed_topics.append(label)

        # Use provided params or fall back to saved ones
        use_models = [m for m in (models or []) if m] or saved.get("models") or ([saved["model"]] if saved.get("model") else [])
        use_accounts = [b for b in (browser_data or []) if b] or saved.get("browser_data", [])

        logger.info("resume_research | original=%s query=%r completed_topics=%d findings=%d sources=%d",
                     session_id, query, len(completed_topics),
                     len(saved.get("findings", [])), len(saved.get("sources", [])))

        # Start a new session with resume state injected
        new_session_id = uuid.uuid4().hex[:16]
        new_entry: dict[str, Any] = {
            "researcher": None,
            "task": None,
            "query": query,
            "model": use_models[0] if use_models else None,
            "models": use_models,
            "browser_data": use_accounts,
            "owner": owner,
            "status": "running",
            "resumed_from": session_id,
            "progress": {"type": "progress", "phase": "starting", "topics": 0, "pages": 0, "sources": 0, "status": "resuming"},
            "result": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        }

        async def on_progress(payload: dict[str, Any]) -> None:
            if payload.get("type") == "progress" or "type" not in payload:
                new_entry["progress"] = payload
            await self._broadcast(new_session_id, payload)

        researcher = DeepResearcher(
            question=query,
            models=use_models,
            accounts=use_accounts,
            max_depth=max_depth,
            max_time=max_time,
            pages_per_topic=pages_per_topic,
            progress_callback=on_progress,
            resume_findings=saved.get("findings", []),
            resume_sources=saved.get("sources", []),
            resume_nodes=saved.get("nodes", []),
            resume_completed_topics=completed_topics,
        )
        new_entry["researcher"] = researcher

        async def runner() -> None:
            try:
                logger.info("resume runner started | new_session=%s original=%s", new_session_id, session_id)
                report = await researcher.research()
                new_entry["status"] = "done"
                new_entry["result"] = report
                new_entry["finished_at"] = time.time()
                elapsed = new_entry["finished_at"] - new_entry["started_at"]
                logger.info("resume runner done | session=%s elapsed=%.1fs report_len=%d",
                            new_session_id, elapsed, len(report))
                await self._broadcast(new_session_id, {"type": "done", "phase": "done", "status": "complete"})
            except asyncio.CancelledError:
                new_entry["status"] = "cancelled"
                new_entry["finished_at"] = time.time()
                logger.warning("resume runner cancelled | session=%s", new_session_id)
                await self._broadcast(new_session_id, {"type": "error", "phase": "error", "status": "cancelled"})
            except Exception as e:
                logger.exception("resume runner failed | session=%s error=%s", new_session_id, e)
                new_entry["status"] = "error"
                new_entry["error"] = str(e)
                new_entry["finished_at"] = time.time()
                await self._broadcast(new_session_id, {"type": "error", "phase": "error", "status": str(e)})
            finally:
                self._persist(new_session_id, new_entry)

        new_entry["task"] = asyncio.create_task(runner())
        self._tasks[new_session_id] = new_entry
        # Persist immediately so resumed sessions survive restarts
        self._persist(new_session_id, new_entry)
        return self.public_status(new_session_id)

    def delete_research(self, session_id: str) -> dict[str, Any]:
        """Delete a research session (in-memory + on-disk)."""
        deleted = False

        # Cancel if running
        entry = self._tasks.get(session_id)
        if entry:
            if entry["status"] == "running":
                researcher = entry.get("researcher")
                if researcher:
                    researcher.cancel()
                task = entry.get("task")
                if task and not task.done():
                    task.cancel()
            del self._tasks[session_id]
            deleted = True

        # Remove persisted JSON
        p = _json_path(session_id)
        if p and p.exists():
            try:
                p.unlink()
                deleted = True
                logger.info("delete_research | removed %s", p)
            except Exception as e:
                logger.error("delete_research | failed to remove %s: %s", p, e)

        # Remove associated markdown file if any
        try:
            for md in RESEARCH_DIR.glob("*.md"):
                # Check if this markdown was generated from this session
                # (we can't perfectly match, so just skip — markdown is a deliverable)
                pass
        except Exception:
            pass

        if deleted:
            return {"deleted": True, "session_id": session_id}
        return {"deleted": False, "error": f"Session {session_id} not found"}

    # ── persistence ─────────────────────────────────────────────────────────
    def _persist(self, session_id: str, entry: dict[str, Any]) -> None:
        logger.debug("_persist | session=%s status=%s", session_id, entry.get("status"))
        p = _json_path(session_id)
        if not p:
            logger.warning("_persist | invalid path for session=%s", session_id)
            return
        researcher = entry.get("researcher")
        data = {
            "session_id": session_id,
            "query": entry["query"],
            "model": entry.get("model"),
            "owner": entry.get("owner", ""),
            "status": entry["status"],
            "error": entry.get("error"),
            "progress": entry.get("progress", {}),
            "result": entry.get("result"),
            "sources": researcher.sources if researcher else [],
            "findings": (researcher.findings if researcher else [])[:200],
            "nodes": _trim_nodes(researcher.nodes) if researcher else [],
            "started_at": entry.get("started_at"),
            "finished_at": entry.get("finished_at"),
        }
        try:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("_persist | saved json session=%s path=%s", session_id, p)
        except Exception as e:
            logger.error("_persist failed | session=%s error=%s", session_id, e)
        if entry.get("result"):
            self._save_markdown(session_id, entry)

    def _save_markdown(self, session_id: str, entry: dict[str, Any]) -> None:
        from datetime import datetime
        slug = _slugify(entry["query"])
        md_path = RESEARCH_DIR / f"{slug}.md"
        i = 2
        while md_path.exists():
            md_path = RESEARCH_DIR / f"{slug}-{i}.md"
            i += 1
        fm = (
            "---\n"
            f"title: {entry['query']}\n"
            f"date: {datetime.now().strftime('%Y-%m-%d')}\n"
            "type: research\n"
            f"tags: [deep-research]\n"
            "status: active\n"
            "---\n\n"
        )
        try:
            md_path.write_text(fm + entry["result"], encoding="utf-8")
            logger.info("_save_markdown | saved session=%s path=%s", session_id, md_path)
        except Exception as e:
            logger.error("_save_markdown failed | session=%s error=%s", session_id, e)

    # ── SSE pub/sub ──────────────────────────────────────────────────────────
    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(session_id)
        if subs and q in subs:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(session_id, None)

    async def _broadcast(self, session_id: str, payload: dict[str, Any]) -> None:
        for q in list(self._subscribers.get(session_id, ())):
            try:
                q.put_nowait(payload)
            except Exception:
                pass


_manager: Optional[ResearchManager] = None


def get_research_manager() -> ResearchManager:
    global _manager
    if _manager is None:
        _manager = ResearchManager()
    return _manager
