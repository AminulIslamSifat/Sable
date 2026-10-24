"""sable search provider — multi-engine search driven by the desktop shell.

The actual searching happens in the Electron main process: one hidden
Chromium tab per engine, results read out of the live DOM, then fused with
weighted Reciprocal Rank Fusion. This module is only the client.

Availability is gated on a marker file the shell writes while its search
bridge is listening (~/.cache/sable/search-bridge.json). No marker means the
app is running as a plain web page, so this provider simply does not exist —
it is never offered in Settings and any stale selection falls through.

The marker carries a per-run bearer token; the bridge refuses requests
without it, so nothing else on the machine can drive the shell.

Two entry points per search type: the plain one returns just the results,
the ``*_traced`` one also returns the bridge's per-engine report so Settings
can show which engine answered and which was blocked.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MARKER = Path.home() / ".cache" / "sable" / "search-bridge.json"
TIMEOUT = 45.0

EngineReport = Dict[str, Any]


def _read_marker() -> Optional[Dict[str, Any]]:
    """Return the bridge descriptor, or None when the shell isn't running."""
    if not MARKER.is_file():
        return None
    try:
        data = json.loads(MARKER.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a corrupt marker means unavailable
        logger.debug("sable search marker unreadable: %s", exc)
        return None
    if not isinstance(data, dict) or not data.get("url") or not data.get("token"):
        return None
    # A marker outlives a crashed shell; a dead pid means the bridge is gone.
    pid = data.get("pid")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except OSError:
            return None
    return data


def sable_available() -> bool:
    """True when the desktop shell's search bridge is up and reachable."""
    return _read_marker() is not None


# ── report helpers ──────────────────────────────────────────────────────────


def _clean_report(raw: Any) -> List[EngineReport]:
    """Normalise the bridge's per-engine report into a stable shape.

    The bridge sends one entry per engine it tried, including the ones that
    were CAPTCHA-gated or returned nothing — that is exactly what the Settings
    test panel needs to show.
    """
    out: List[EngineReport] = []
    if not isinstance(raw, list):
        return out
    for e in raw:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        try:
            count = int(e.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        try:
            ms = int(e.get("ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        out.append(
            {
                "id": str(e.get("id")),
                "ok": bool(e.get("ok")) and count > 0,
                "gated": bool(e.get("gated")),
                "count": count,
                "ms": ms,
                "error": (str(e["error"]) if e.get("error") else None),
            }
        )
    return out


def _report_summary(report: List[EngineReport]) -> str:
    """One-line log summary: ``google=8, yandex=FAIL(captcha)``."""
    if not report:
        return "no engines"
    return ", ".join(
        f"{e['id']}={e['count']}" if e["ok"] else f"{e['id']}=FAIL({e['error'] or 'none'})"
        for e in report
    )


# ── transport ───────────────────────────────────────────────────────────────


def _post(endpoint: str, payload: Dict[str, Any], what: str) -> Dict[str, Any]:
    """POST to the bridge and return the decoded body, or raise NetworkError."""
    from .providers import NetworkError

    marker = _read_marker()
    if not marker:
        raise NetworkError("sable search bridge is not running (desktop shell not active)")

    body = json.dumps(payload).encode("utf-8")
    req = _urllib_request(
        marker["url"].rstrip("/") + endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Sable-Token": marker["token"],
        },
        method="POST",
    )

    try:
        with _urllib_urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise NetworkError(f"sable {what} bridge request failed: {exc}") from exc

    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise NetworkError(f"sable {what} bridge returned non-JSON: {raw[:200]}") from exc

    if not isinstance(data, dict):
        raise NetworkError(f"sable {what} bridge returned an unexpected payload")
    if data.get("error"):
        raise NetworkError(f"sable {what} bridge: {data['error']}")
    return data


# ── shaping ─────────────────────────────────────────────────────────────────


def _shape_web(data: Dict[str, Any]) -> List[dict]:
    out: List[dict] = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if not url:
            continue
        out.append(
            {
                "title": (r.get("title") or "").strip(),
                "url": url,
                "snippet": (r.get("snippet") or "").strip(),
                "engine": r.get("engine") or [],
                "score": r.get("score"),
            }
        )
    return out


def _shape_image(data: Dict[str, Any]) -> List[dict]:
    out: List[dict] = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        image = (r.get("image") or "").strip()
        if not image:
            continue
        out.append(
            {
                "title": (r.get("title") or "").strip(),
                "image": image,
                "thumbnail": (r.get("thumbnail") or image).strip(),
                "url": (r.get("url") or "").strip(),
                "engine": r.get("engine") or [],
                "score": r.get("score"),
            }
        )
    return out


# ── web search ──────────────────────────────────────────────────────────────


def sable_multi_search_traced(
    query: str,
    count: int = 10,
    time_filter: Optional[str] = None,
    engines: Optional[List[str]] = None,
) -> tuple[List[dict], List[EngineReport]]:
    """Fused multi-engine web search, with the per-engine report."""
    query = (query or "").strip()
    if not query:
        return [], []

    payload: Dict[str, Any] = {"query": query, "count": max(1, min(int(count or 10), 50))}
    if time_filter:
        payload["time_filter"] = time_filter
    if engines:
        payload["engines"] = engines

    started = time.monotonic()
    data = _post("/search", payload, "search")
    out = _shape_web(data)
    report = _clean_report(data.get("engines"))

    logger.info(
        "sable search %r -> %d fused results in %.2fs (%s)",
        query,
        len(out),
        time.monotonic() - started,
        _report_summary(report),
    )
    return out, report


def sable_multi_search(
    query: str,
    count: int = 10,
    time_filter: Optional[str] = None,
    engines: Optional[List[str]] = None,
) -> List[dict]:
    """Run a fused multi-engine search through the desktop shell.

    Returns the same ``[{title, url, snippet}]`` shape every other provider
    returns, with ``engine`` and ``score`` added for callers that want them.
    Raises ``NetworkError`` when the bridge is missing or fails, so the caller
    can fall through its chain instead of silently returning nothing.
    """
    results, _report = sable_multi_search_traced(query, count, time_filter, engines)
    return results


# ── image search ────────────────────────────────────────────────────────────


def sable_image_search_traced(
    query: str,
    count: int = 10,
    engines: Optional[List[str]] = None,
) -> tuple[List[dict], List[EngineReport]]:
    """Fused image search, with the per-engine report."""
    query = (query or "").strip()
    if not query:
        return [], []

    payload: Dict[str, Any] = {"query": query, "count": max(1, min(int(count or 10), 40))}
    if engines:
        payload["engines"] = engines

    started = time.monotonic()
    data = _post("/images", payload, "image")
    out = _shape_image(data)
    report = _clean_report(data.get("engines"))

    logger.info(
        "sable image search %r -> %d fused images in %.2fs (%s)",
        query,
        len(out),
        time.monotonic() - started,
        _report_summary(report),
    )
    return out, report


def sable_image_search(query: str, count: int = 10, engines: Optional[List[str]] = None) -> List[dict]:
    """Fused image search through the desktop shell.

    Returns ``[{title, image, thumbnail, url}]``. Google Images carries double
    weight in the fusion because it has the widest image index.

    Raises ``NetworkError`` when the bridge is missing or fails, so the caller
    can fall through to another provider instead of silently returning nothing.
    """
    results, _report = sable_image_search_traced(query, count, engines)
    return results


# ── urllib indirection (kept for testability) ───────────────────────────────


def _urllib_request(*args, **kwargs):
    from urllib.request import Request

    return Request(*args, **kwargs)


def _urllib_urlopen(*args, **kwargs):
    from urllib.request import urlopen

    return urlopen(*args, **kwargs)
