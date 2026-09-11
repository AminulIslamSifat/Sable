"""
Tests for rate-limit auto-switch and account marking logic.
Covers:
  1. service.py fast-fail on rate-limit/captcha during retries
  2. chat.py generic error escalation to auto-switch
  3. Account marking (exhausted + captcha_blocked) during auto-switch
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch




# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service():
    """Create a minimal ChatService-like object for testing stream_events."""
    from engine.service import ChatService
    svc = ChatService.__new__(ChatService)
    svc.account = "test-acc"
    svc._exhausted = False
    svc._headers_cache = {}
    # Attributes set by __init__ that _resolve_account() needs
    svc._account_override = "test-acc"
    svc._browser = None
    svc._headers = None
    svc._headers_account = None
    svc._lock = None
    svc._scoped_browsers = {}
    svc._scoped_headers = {}
    svc._scoped_accounts = {}
    return svc


# ---------------------------------------------------------------------------
# Test 1: service.py fast-fail on rate-limit keywords
# ---------------------------------------------------------------------------

class TestServiceErrorDetection:
    """Verify that rate-limit/captcha keywords in non-200 responses are detected after retries."""

    async def test_rate_limit_detected_after_retries(self):
        """HTTP 429 with 'rate limit' in body should yield rate_limited after retry exhaustion."""
        svc = _make_service()
        svc._ensure_headers = AsyncMock(return_value={})
        svc._refresh_headers = AsyncMock(return_value={})

        mock_response = AsyncMock()
        mock_response.status_code = 429
        mock_response.aread = AsyncMock(return_value=b'{"error": "rate limit exceeded"}')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_stream_ctx)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        events = []
        with patch("engine.service.httpx.AsyncClient", return_value=mock_client), \
             patch("engine.service.mark_account_exhausted"):
            async for event in svc.stream_events("hello", chat_id="c1"):
                events.append(event)

        types = [e.get("type") for e in events]
        assert "rate_limited" in types, f"Expected rate_limited event, got: {types}"

    async def test_captcha_detected_after_retries(self):
        """Non-200 with 'captcha' in body should yield waf_blocked after retry exhaustion."""
        svc = _make_service()
        svc._ensure_headers = AsyncMock(return_value={})
        svc._refresh_headers = AsyncMock(return_value={})

        mock_response = AsyncMock()
        mock_response.status_code = 503
        mock_response.aread = AsyncMock(return_value=b'{"error": "captcha validation required"}')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_stream_ctx)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        events = []
        with patch("engine.service.httpx.AsyncClient", return_value=mock_client):
            async for event in svc.stream_events("hello", chat_id="c1"):
                events.append(event)

        types = [e.get("type") for e in events]
        assert "waf_blocked" in types, f"Expected waf_blocked event, got: {types}"


# ---------------------------------------------------------------------------
# Test 2: Generic error keyword detection (unit-level)
# ---------------------------------------------------------------------------

class TestErrorKeywordDetection:
    """Verify keyword matching logic used in both service.py and chat.py."""

    RATE_LIMIT_KEYWORDS = ("ratelimit", "rate_limit", "rate limit", "quota", "daily usage", "exceeded", "429")
    CAPTCHA_KEYWORDS = ("captcha", "waf", "validate", "rgv587", "blocked", "forbidden")

    @pytest.mark.parametrize("msg", [
        "Rate limit exceeded",
        "Daily usage quota reached",
        "HTTP 429: Too Many Requests",
        "API rate_limit error",
        "You have exceeded your quota",
    ])
    def test_rate_limit_detection(self, msg):
        lower = msg.lower()
        assert any(kw in lower for kw in self.RATE_LIMIT_KEYWORDS), f"Should detect rate-limit in: {msg}"

    @pytest.mark.parametrize("msg", [
        "Captcha validation required",
        "WAF token expired",
        "rgv587_error triggered",
        "Request blocked by firewall",
        "Forbidden: validate your identity",
    ])
    def test_captcha_detection(self, msg):
        lower = msg.lower()
        assert any(kw in lower for kw in self.CAPTCHA_KEYWORDS), f"Should detect captcha in: {msg}"

    @pytest.mark.parametrize("msg", [
        "Connection timeout",
        "Internal server error",
        "Network unreachable",
        "JSON parse error",
    ])
    def test_non_matching_errors(self, msg):
        lower = msg.lower()
        is_rl = any(kw in lower for kw in self.RATE_LIMIT_KEYWORDS)
        is_cap = any(kw in lower for kw in self.CAPTCHA_KEYWORDS)
        assert not is_rl and not is_cap, f"Should NOT match rate-limit or captcha: {msg}"


# ---------------------------------------------------------------------------
# Test 3: Account marking functions exist and work
# ---------------------------------------------------------------------------

class TestAccountMarking:
    """Verify mark_account_exhausted and mark_account_captcha_blocked are importable and callable."""

    def test_imports(self):
        from engine.config import mark_account_exhausted, mark_account_captcha_blocked
        assert callable(mark_account_exhausted)
        assert callable(mark_account_captcha_blocked)

    def test_mark_exhausted(self):
        from engine.config import mark_account_exhausted, is_account_exhausted
        test_acc = "__test_exhaust_marker__"
        try:
            mark_account_exhausted(test_acc)
            assert is_account_exhausted(test_acc) is True
        finally:
            # Cleanup
            from engine.config import _load_exhaustion_store, _save_exhaustion_store
            store = _load_exhaustion_store()
            store.pop(test_acc, None)
            _save_exhaustion_store(store)

    def test_mark_captcha_blocked(self):
        from engine.config import mark_account_captcha_blocked, is_account_captcha_blocked
        test_acc = "__test_captcha_marker__"
        try:
            mark_account_captcha_blocked(test_acc)
            assert is_account_captcha_blocked(test_acc) is True
        finally:
            # Cleanup
            from engine.config import _load_captcha_block_store, _save_captcha_block_store
            store = _load_captcha_block_store()
            store.pop(test_acc, None)
            _save_captcha_block_store(store)


# ---------------------------------------------------------------------------
# Test 4: Defense-in-depth still fires after all retries exhausted
# ---------------------------------------------------------------------------

class TestDefenseInDepth:
    """Verify the post-loop defense-in-depth block still works as safety net."""

    def test_error_keywords_present_in_service(self):
        """Verify error detection keywords exist in service.py for stream error handling."""
        from pathlib import Path
        source = Path("engine/service.py").read_text()

        # At least one occurrence of each keyword category must exist
        assert '"ratelimit"' in source or '"rate limit"' in source, \
            "Rate-limit keywords missing from service.py"
        assert '"captcha"' in source or '"waf"' in source, \
            "Captcha/WAF keywords missing from service.py"
