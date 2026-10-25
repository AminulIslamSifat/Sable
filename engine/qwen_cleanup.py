"""Qwen chat history cleanup — programmatic deletion of all chats for any account.

Uses BrowserManager to launch the account's browser profile, hydrate fresh
WAF tokens (bx-ua, bx-umidtoken, JWT cookie), then calls the DELETE endpoint.
"""

from __future__ import annotations
from engine.config import PERSISTENT_ROOT  # frozen-build writable root

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.qwen_cleanup")

_SYSTEM_DIR = PERSISTENT_ROOT / "system"
_ACCOUNTS_JSON = _SYSTEM_DIR / "accounts.json"
DELETE_URL = "https://chat.qwen.ai/api/v2/chats/"


def _load_accounts() -> dict[str, Any]:
    """Load accounts.json, returning empty dict on failure."""
    try:
        return json.loads(_ACCOUNTS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_accounts(data: dict[str, Any]) -> None:
    """Atomically write accounts.json."""
    tmp = _ACCOUNTS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_ACCOUNTS_JSON)


def update_last_cleared(profile_name: str) -> None:
    """Set last_cleared timestamp for an account to now (UTC ISO)."""
    accounts = _load_accounts()
    if profile_name not in accounts:
        accounts[profile_name] = {}
    accounts[profile_name]["last_cleared"] = datetime.now(timezone.utc).isoformat()
    _save_accounts(accounts)


def get_last_cleared(profile_name: str) -> datetime | None:
    """Return the last_cleared datetime for an account, or None."""
    accounts = _load_accounts()
    ts = accounts.get(profile_name, {}).get("last_cleared")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


async def delete_all_chats_for_profile(profile_name: str) -> dict[str, Any]:
    """Launch browser with the given profile, grab fresh headers, DELETE all chats.

    Returns {"success": bool, "profile": str, "error": str | None}.
    """
    from engine.session import BrowserManager
    from curl_cffi.requests import AsyncSession

    profile_path = _SYSTEM_DIR / profile_name
    if not profile_path.is_dir():
        return {"success": False, "profile": profile_name, "error": f"Profile dir not found: {profile_name}"}

    bm = BrowserManager(user_data_dir=str(profile_path), headless=True)
    try:
        logger.info("Starting browser for %s to clear Qwen history...", profile_name)
        await bm.start()
        headers = await bm.get_fresh_headers()

        # Use curl_cffi like the rest of Sable does — matches Chrome TLS fingerprint
        async with AsyncSession(impersonate="chrome", timeout=30) as client:
            resp = await client.request(
                "DELETE",
                DELETE_URL,
                headers=headers,
            )
            body = resp.text[:500]
            status = resp.status_code

        if status == 200:
            try:
                data = json.loads(body)
                if data.get("success"):
                    update_last_cleared(profile_name)
                    logger.info("✅ Cleared Qwen history for %s", profile_name)
                    return {"success": True, "profile": profile_name, "error": None}
                else:
                    err = data.get("data", {}).get("details", body)
                    logger.warning("Qwen DELETE returned success=false for %s: %s", profile_name, err)
                    return {"success": False, "profile": profile_name, "error": str(err)}
            except json.JSONDecodeError:
                return {"success": False, "profile": profile_name, "error": f"Non-JSON response: {body}"}
        else:
            logger.warning("Qwen DELETE HTTP %d for %s: %s", status, profile_name, body)
            return {"success": False, "profile": profile_name, "error": f"HTTP {status}: {body}"}

    except Exception as e:
        logger.error("Failed to clear history for %s: %s", profile_name, e)
        return {"success": False, "profile": profile_name, "error": str(e)}
    finally:
        await bm.close()


async def delete_all_chats_all_profiles() -> list[dict[str, Any]]:
    """Iterate over all browser-data-accN profiles and clear Qwen history for each.

    Processes sequentially to avoid launching 70+ browsers simultaneously.
    Returns list of result dicts.
    """
    results = []
    profiles = sorted(
        [p.name for p in _SYSTEM_DIR.iterdir() if p.is_dir() and p.name.startswith("browser-data-acc")],
        key=lambda x: int(x.split("acc")[1]) if x.split("acc")[1].isdigit() else 0,
    )

    logger.info("Starting bulk Qwen history clear for %d profiles", len(profiles))
    for profile in profiles:
        result = await delete_all_chats_for_profile(profile)
        results.append(result)

    succeeded = sum(1 for r in results if r["success"])
    logger.info("Bulk clear complete: %d/%d succeeded", succeeded, len(profiles))
    return results
