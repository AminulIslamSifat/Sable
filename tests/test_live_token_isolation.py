"""
Live integration test: verifies that switching between browser profiles
produces distinct JWT tokens (different user IDs).

Run with: pytest tests/test_live_token_isolation.py -v -s
Requires: WSL + Windows Chrome setup (or will skip if unavailable).
"""
import base64
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.session import BrowserManager

ACCOUNTS = ["browser-data-acc0", "browser-data-acc28", "browser-data-acc42"]
BASE_DIR = os.path.join(os.path.dirname(__file__), "..", "system")


def decode_jwt_user_id(jwt_token: str) -> str | None:
    """Extract user ID from JWT payload without verification."""
    try:
        parts = jwt_token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        return data.get("id")
    except Exception:
        return None


@pytest.mark.anyio
async def test_distinct_jwt_per_account():
    """Each account profile must produce a JWT with a unique user ID."""
    results: dict[str, str] = {}
    bm = BrowserManager(headless=True)

    try:
        for account in ACCOUNTS:
            profile_dir = os.path.join(BASE_DIR, account)
            if not os.path.isdir(profile_dir):
                pytest.skip(f"Profile {account} not found at {profile_dir}")

            bm.user_data_dir = profile_dir
            await bm.start()
            headers = await bm.get_fresh_headers()
            jwt = headers.get("authorization", "").removeprefix("Bearer ")
            if not jwt:
                pytest.fail(f"No JWT extracted for {account}")

            user_id = decode_jwt_user_id(jwt)
            if not user_id:
                pytest.fail(f"Could not decode JWT user ID for {account}")

            print(f"\n{account}: user_id={user_id}")
            results[account] = user_id

            # Close between accounts to force fresh launch
            await bm.close()

    finally:
        await bm.close()

    # Verify all user IDs are distinct
    user_ids = list(results.values())
    assert len(set(user_ids)) == len(user_ids), (
        f"JWT user IDs are NOT distinct! {results}"
    )
    print(f"\n✅ All {len(results)} accounts have distinct JWT user IDs")
