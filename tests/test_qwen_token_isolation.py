"""
Tests for per-account Qwen token isolation.

Verifies that get_qwen_tokens_for_account() NEVER returns tokens from a
different account when an explicit account name is provided. This was the
root cause of cross-account JWT leakage (all accounts getting acc0's JWT).
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store():
    """Create a fake multi-account token store with distinct cookies/JWTs."""
    return {
        "browser-data-acc0": [{
            "cookies": "jwt=acc0_cookie",
            "bx_ua": "ua_acc0",
            "bx_umidtoken": "umid_acc0",
            "jwt_token": "jwt_acc0_token",
        }],
        "browser-data-acc28": [{
            "cookies": "jwt=acc28_cookie",
            "bx_ua": "ua_acc28",
            "bx_umidtoken": "umid_acc28",
            "jwt_token": "jwt_acc28_token",
        }],
        "browser-data-acc42": [{
            "cookies": "jwt=acc42_cookie",
            "bx_ua": "ua_acc42",
            "bx_umidtoken": "umid_acc42",
            "jwt_token": "jwt_acc42_token",
        }],
    }


# ---------------------------------------------------------------------------
# Test: Explicit account lookup returns ONLY that account's tokens
# ---------------------------------------------------------------------------

class TestTokenIsolation:
    """get_qwen_tokens_for_account(account=X) must return X's tokens or None."""

    @patch("engine.config.load_qwen_token_store")
    def test_returns_correct_account_tokens(self, mock_load):
        from engine.config import get_qwen_tokens_for_account
        mock_load.return_value = _make_store()

        result = get_qwen_tokens_for_account("browser-data-acc28")
        assert result is not None
        assert result["cookies"] == "jwt=acc28_cookie"
        assert result["jwt_token"] == "jwt_acc28_token"

    @patch("engine.config.load_qwen_token_store")
    def test_does_not_leak_to_other_accounts(self, mock_load):
        """Requesting acc42 must NOT return acc0's tokens."""
        from engine.config import get_qwen_tokens_for_account
        mock_load.return_value = _make_store()

        result = get_qwen_tokens_for_account("browser-data-acc42")
        assert result is not None
        assert result["jwt_token"] == "jwt_acc42_token"
        assert result["jwt_token"] != "jwt_acc0_token"
        assert result["cookies"] != "jwt=acc0_cookie"

    @patch("engine.config.load_qwen_token_store")
    def test_nonexistent_account_returns_none(self, mock_load):
        """Explicit account that doesn't exist must return None, NOT fallback."""
        from engine.config import get_qwen_tokens_for_account
        mock_load.return_value = _make_store()

        result = get_qwen_tokens_for_account("browser-data-acc999")
        assert result is None, (
            f"Expected None for nonexistent account, got: {result}. "
            "This means fallback leaked another account's tokens!"
        )

    @patch("engine.config.load_qwen_token_store")
    def test_empty_store_returns_none(self, mock_load):
        from engine.config import get_qwen_tokens_for_account
        mock_load.return_value = {}

        result = get_qwen_tokens_for_account("browser-data-acc0")
        assert result is None

    @patch("engine.config.load_qwen_token_store")
    def test_account_with_empty_entries_returns_none(self, mock_load):
        """Account exists in store but has no valid entries → None."""
        from engine.config import get_qwen_tokens_for_account
        mock_load.return_value = {
            "browser-data-acc0": [],  # empty list
        }

        result = get_qwen_tokens_for_account("browser-data-acc0")
        assert result is None

    @patch("engine.config.load_qwen_token_store")
    def test_no_fallback_when_explicit_account_missing(self, mock_load):
        """Even if other accounts have tokens, missing explicit account → None."""
        from engine.config import get_qwen_tokens_for_account
        store = _make_store()
        del store["browser-data-acc42"]  # remove acc42
        mock_load.return_value = store

        # acc0 and acc28 still exist, but requesting acc42 must NOT return them
        result = get_qwen_tokens_for_account("browser-data-acc42")
        assert result is None


# ---------------------------------------------------------------------------
# Test: No-argument call falls back to active account only
# ---------------------------------------------------------------------------

class TestNoArgFallback:
    """get_qwen_tokens_for_account() with no arg should use active account."""

    @patch("engine.config._resolve_active_account", return_value="browser-data-acc28")
    @patch("engine.config.load_qwen_token_store")
    def test_returns_active_account_when_no_arg(self, mock_load, mock_active):
        from engine.config import get_qwen_tokens_for_account
        mock_load.return_value = _make_store()

        result = get_qwen_tokens_for_account()
        assert result is not None
        assert result["jwt_token"] == "jwt_acc28_token"

    @patch("engine.config._resolve_active_account", return_value="browser-data-acc999")
    @patch("engine.config.load_qwen_token_store")
    def test_returns_none_when_active_missing(self, mock_load, mock_active):
        """No-arg call with missing active account → None (no wildcard fallback)."""
        from engine.config import get_qwen_tokens_for_account
        mock_load.return_value = _make_store()

        result = get_qwen_tokens_for_account()
        assert result is None
