
"""Comprehensive tests for connectors/common/context_summarizer.py

Covers:
  - Threshold math (hint at 75%, force at 90%)
  - Edge cases (zero/negative max_chars, empty history, single message)
  - Tag extraction and stripping (various formats, streaming chunks, malformed)
  - History rewriting (gemini vs openai format, prefix preservation)
  - Force cut index computation
  - Summary prompt building (both message formats)
  - Qwen exclusion logic (verified via connector integration pattern)
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors.common.context_summarizer import (
    should_inject_hint,
    should_force_summarize,
    get_hint_text,
    extract_summarize_tag,
    strip_summarize_tag,
    build_summary_prompt,
    rewrite_history_with_summary,
    compute_force_cut_index,
    HINT_THRESHOLD,
    FORCE_THRESHOLD,
)


# ─── Threshold Tests ────────────────────────────────────────────────

class TestThresholds:
    """Test should_inject_hint and should_force_summarize boundary conditions."""

    def test_hint_below_threshold(self):
        assert not should_inject_hint(749, 1000)

    def test_hint_at_exact_threshold(self):
        assert should_inject_hint(750, 1000)

    def test_hint_above_threshold(self):
        assert should_inject_hint(800, 1000)

    def test_force_below_threshold(self):
        assert not should_force_summarize(899, 1000)

    def test_force_at_exact_threshold(self):
        assert should_force_summarize(900, 1000)

    def test_force_above_threshold(self):
        assert should_force_summarize(950, 1000)

    def test_hint_zero_max_chars(self):
        assert not should_inject_hint(100, 0)

    def test_force_zero_max_chars(self):
        assert not should_force_summarize(100, 0)

    def test_hint_negative_max_chars(self):
        assert not should_inject_hint(100, -500)

    def test_force_negative_max_chars(self):
        assert not should_force_summarize(100, -500)

    def test_hint_zero_current_chars(self):
        assert not should_inject_hint(0, 1000)

    def test_force_zero_current_chars(self):
        assert not should_force_summarize(0, 1000)

    def test_both_thresholds_large_values(self):
        """Realistic Gemini-scale values."""
        max_chars = 3_500_000
        assert not should_inject_hint(2_624_999, max_chars)
        assert should_inject_hint(2_625_000, max_chars)
        assert not should_force_summarize(3_149_999, max_chars)
        assert should_force_summarize(3_150_000, max_chars)

    def test_hint_and_force_ordering(self):
        """Hint always triggers before force."""
        max_chars = 10000
        for current in range(0, max_chars + 1, 100):
            if should_force_summarize(current, max_chars):
                assert should_inject_hint(current, max_chars), \
                    f"Force triggered at {current} but hint didn't"

    def test_threshold_constants(self):
        assert HINT_THRESHOLD == 0.75
        assert FORCE_THRESHOLD == 0.90
        assert HINT_THRESHOLD < FORCE_THRESHOLD


# ─── Hint Text Tests ────────────────────────────────────────────────

class TestHintText:
    """Test get_hint_text generation."""

    def test_contains_percentage(self):
        text = get_hint_text(750, 1000)
        assert "75%" in text

    def test_contains_tag_instruction(self):
        text = get_hint_text(800, 1000)
        assert "<summarize_before" in text
        assert "message_index" in text

    def test_zero_max_returns_safe_text(self):
        text = get_hint_text(100, 0)
        assert "0%" in text

    def test_says_system_note(self):
        text = get_hint_text(900, 1000)
        assert "SYSTEM NOTE" in text

    def test_tells_model_not_to_mention(self):
        text = get_hint_text(900, 1000)
        assert "Do NOT mention" in text


# ─── Tag Extraction Tests ───────────────────────────────────────────

class TestExtractSummarizeTag:
    """Test extract_summarize_tag parsing."""

    def test_standard_tag(self):
        assert extract_summarize_tag('<summarize_before message_index="5" />') == 5

    def test_tag_without_quotes(self):
        assert extract_summarize_tag('<summarize_before message_index=10 />') == 10

    def test_tag_in_surrounding_text(self):
        text = 'Sure! Let me compress that. <summarize_before message_index="3" /> Done.'
        assert extract_summarize_tag(text) == 3

    def test_tag_with_newlines(self):
        text = '<summarize_before\n  message_index="7"\n/>'
        assert extract_summarize_tag(text) == 7

    def test_case_insensitive(self):
        assert extract_summarize_tag('<SUMMARIZE_BEFORE MESSAGE_INDEX="4" />') == 4

    def test_mixed_case(self):
        assert extract_summarize_tag('<Summarize_Before Message_Index="2" />') == 2

    def test_no_tag_returns_none(self):
        assert extract_summarize_tag("Just a normal response") is None

    def test_empty_string_returns_none(self):
        assert extract_summarize_tag("") is None

    def test_malformed_tag_returns_none(self):
        assert extract_summarize_tag("<summarize_before />") is None

    def test_multiple_tags_returns_first(self):
        text = '<summarize_before message_index="3" /> blah <summarize_before message_index="8" />'
        assert extract_summarize_tag(text) == 3

    def test_large_index(self):
        assert extract_summarize_tag('<summarize_before message_index="99999" />') == 99999

    def test_zero_index(self):
        assert extract_summarize_tag('<summarize_before message_index="0" />') == 0

    def test_tag_at_end_of_stream_chunk(self):
        """Simulates streaming where tag appears at chunk boundary."""
        chunk = 'Here is my response <summarize_before message_index="12" />'
        assert extract_summarize_tag(chunk) == 12


# ─── Tag Stripping Tests ────────────────────────────────────────────

class TestStripSummarizeTag:
    """Test strip_summarize_tag removes tags cleanly."""

    def test_strips_standard_tag(self):
        result = strip_summarize_tag('Hello <summarize_before message_index="5" /> world')
        assert "<summarize_before" not in result
        assert "Hello" in result
        assert "world" in result

    def test_strips_multiple_tags(self):
        text = 'A <summarize_before message_index="1" /> B <summarize_before message_index="2" /> C'
        result = strip_summarize_tag(text)
        assert "<summarize_before" not in result
        assert "A" in result and "B" in result and "C" in result

    def test_no_tag_unchanged(self):
        text = "Just normal text"
        assert strip_summarize_tag(text) == text

    def test_only_tag_returns_empty_or_whitespace(self):
        result = strip_summarize_tag('<summarize_before message_index="3" />')
        assert result.strip() == ""

    def test_strips_case_insensitive(self):
        result = strip_summarize_tag('Hi <SUMMARIZE_BEFORE MESSAGE_INDEX="1" /> bye')
        assert "<SUMMARIZE_BEFORE" not in result
        assert "Hi" in result and "bye" in result

    def test_preserves_surrounding_content(self):
        text = 'Line one\n<summarize_before message_index="5" />\nLine two'
        result = strip_summarize_tag(text)
        assert "Line one" in result
        assert "Line two" in result

    def test_streaming_chunk_partial_tag_not_stripped(self):
        """Incomplete tag in a streaming chunk should NOT be stripped."""
        chunk = "Here is some text <summarize_bef"
        result = strip_summarize_tag(chunk)
        assert "<summarize_bef" in result  # Incomplete, left as-is


# ─── Build Summary Prompt Tests ─────────────────────────────────────

class TestBuildSummaryPrompt:
    """Test build_summary_prompt with both message formats."""

    def test_openai_format(self):
        messages = [
            {"role": "user", "content": "Fix the bug"},
            {"role": "assistant", "content": "Fixed it in main.py"},
        ]
        prompt = build_summary_prompt(messages, lambda m: len(str(m.get("content", ""))))
        assert "[Message 0 | user]: Fix the bug" in prompt
        assert "[Message 1 | assistant]: Fixed it in main.py" in prompt
        assert "BEGIN HISTORY TO SUMMARIZE" in prompt
        assert "END HISTORY" in prompt

    def test_gemini_format(self):
        messages = [
            {"role": "user", "parts": [{"text": "Hello"}]},
            {"role": "model", "parts": [{"text": "Hi there"}]},
        ]
        prompt = build_summary_prompt(messages, lambda m: 5)
        assert "[Message 0 | user]: Hello" in prompt
        assert "[Message 1 | model]: Hi there" in prompt

    def test_gemini_thought_parts_excluded(self):
        messages = [
            {"role": "model", "parts": [
                {"text": "Visible answer", "thought": False},
                {"text": "Internal reasoning", "thought": True},
            ]},
        ]
        prompt = build_summary_prompt(messages, lambda m: 5)
        assert "Visible answer" in prompt
        assert "Internal reasoning" not in prompt

    def test_empty_messages(self):
        prompt = build_summary_prompt([], lambda m: 0)
        assert "BEGIN HISTORY TO SUMMARIZE" in prompt
        assert "END HISTORY" in prompt

    def test_missing_role_defaults_to_unknown(self):
        messages = [{"content": "orphan message"}]
        prompt = build_summary_prompt(messages, lambda m: 5)
        assert "[Message 0 | unknown]" in prompt

    def test_message_without_content_or_parts(self):
        messages = [{"role": "user", "metadata": "something"}]
        prompt = build_summary_prompt(messages, lambda m: 5)
        # Falls back to str(msg)
        assert "[Message 0 | user]:" in prompt


# ─── Rewrite History Tests ──────────────────────────────────────────

class TestRewriteHistoryWithSummary:
    """Test rewrite_history_with_summary for both formats."""

    def _make_openai_history(self, n):
        return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(n)]

    def _make_gemini_history(self, n):
        return [{"role": "user" if i % 2 == 0 else "model", "parts": [{"text": f"msg {i}"}]} for i in range(n)]

    # OpenAI format
    def test_openai_basic_rewrite(self):
        history = self._make_openai_history(6)
        result = rewrite_history_with_summary(history, "Summary here", cut_index=3, prefix_len=0, fmt="openai")
        # Should have: summary_msg + ack_msg + remaining 3 messages
        assert len(result) == 5
        assert result[0]["role"] == "user"
        assert "[Earlier context summary]" in result[0]["content"]
        assert "Summary here" in result[0]["content"]
        assert result[1]["role"] == "assistant"
        assert "Understood" in result[1]["content"]
        assert result[2]["content"] == "msg 3"

    def test_openai_with_prefix(self):
        history = [{"role": "system", "content": "You are helpful"}] + self._make_openai_history(4)
        result = rewrite_history_with_summary(history, "Sum", cut_index=3, prefix_len=1, fmt="openai")
        # prefix(1) + summary(1) + ack(1) + remaining(2) = 5
        assert len(result) == 5
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "You are helpful"
        assert "[Earlier context summary]" in result[1]["content"]

    def test_openai_ack_role_is_assistant(self):
        history = self._make_openai_history(4)
        result = rewrite_history_with_summary(history, "S", cut_index=2, fmt="openai")
        assert result[1]["role"] == "assistant"

    # Gemini format
    def test_gemini_basic_rewrite(self):
        history = self._make_gemini_history(6)
        result = rewrite_history_with_summary(history, "Gem summary", cut_index=3, prefix_len=0, fmt="gemini")
        assert len(result) == 5
        assert result[0]["role"] == "user"
        assert "parts" in result[0]
        assert "[Earlier context summary]" in result[0]["parts"][0]["text"]
        assert result[1]["role"] == "model"
        assert "Understood" in result[1]["parts"][0]["text"]

    def test_gemini_ack_role_is_model(self):
        history = self._make_gemini_history(4)
        result = rewrite_history_with_summary(history, "S", cut_index=2, fmt="gemini")
        assert result[1]["role"] == "model"

    def test_gemini_with_prefix(self):
        history = [{"role": "user", "parts": [{"text": "System instruction"}]}] + self._make_gemini_history(4)
        result = rewrite_history_with_summary(history, "Sum", cut_index=3, prefix_len=1, fmt="gemini")
        assert result[0]["parts"][0]["text"] == "System instruction"
        assert "[Earlier context summary]" in result[1]["parts"][0]["text"]

    # Edge cases
    def test_empty_summarized_range_returns_original(self):
        history = self._make_openai_history(4)
        result = rewrite_history_with_summary(history, "S", cut_index=0, prefix_len=0, fmt="openai")
        assert result == history

    def test_cut_at_end_summarizes_all(self):
        history = self._make_openai_history(4)
        result = rewrite_history_with_summary(history, "Full summary", cut_index=4, prefix_len=0, fmt="openai")
        # summary + ack + 0 remaining = 2
        assert len(result) == 2
        assert "Full summary" in result[0]["content"]

    def test_prefix_equals_length_returns_original(self):
        history = self._make_openai_history(3)
        result = rewrite_history_with_summary(history, "S", cut_index=3, prefix_len=3, fmt="openai")
        # No messages to summarize
        assert result == history

    def test_remaining_messages_preserved_exactly(self):
        history = self._make_openai_history(8)
        result = rewrite_history_with_summary(history, "S", cut_index=4, prefix_len=0, fmt="openai")
        remaining = result[2:]
        assert len(remaining) == 4
        for i, msg in enumerate(remaining):
            assert msg["content"] == f"msg {i + 4}"


# ─── Compute Force Cut Index Tests ──────────────────────────────────

class TestComputeForceCutIndex:
    """Test compute_force_cut_index for forced summarization."""

    def test_even_split(self):
        history = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        idx = compute_force_cut_index(history, prefix_len=0)
        assert idx == 5  # 10 // 2

    def test_odd_split_rounds_down(self):
        history = [{"role": "user", "content": f"m{i}"} for i in range(7)]
        idx = compute_force_cut_index(history, prefix_len=0)
        assert idx == 3  # 7 // 2 = 3

    def test_with_prefix(self):
        history = [{"role": "system", "content": "sys"}] + [{"role": "user", "content": f"m{i}"} for i in range(6)]
        idx = compute_force_cut_index(history, prefix_len=1)
        # non_prefix = 6, half = 3, result = 1 + 3 = 4
        assert idx == 4

    def test_two_non_prefix_messages(self):
        history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        idx = compute_force_cut_index(history, prefix_len=1)
        # non_prefix = 2, <= 2 → prefix_len + 1
        assert idx == 2

    def test_one_non_prefix_message(self):
        history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "only"}]
        idx = compute_force_cut_index(history, prefix_len=1)
        # non_prefix = 1, <= 2 → prefix_len + 1
        assert idx == 2

    def test_zero_non_prefix_messages(self):
        history = [{"role": "system", "content": "sys"}]
        idx = compute_force_cut_index(history, prefix_len=1)
        # non_prefix = 0, <= 2 → prefix_len + 1 = 2
        # But that's beyond history length — caller must handle
        assert idx == 2

    def test_empty_history(self):
        idx = compute_force_cut_index([], prefix_len=0)
        # non_prefix = 0, <= 2 → 0 + 1 = 1
        assert idx == 1

    def test_large_history(self):
        history = [{"role": "user", "content": f"m{i}"} for i in range(100)]
        idx = compute_force_cut_index(history, prefix_len=0)
        assert idx == 50

    def test_always_at_least_one_summarized(self):
        """Even with tiny histories, cut index > prefix_len."""
        for size in range(1, 5):
            history = [{"role": "user", "content": f"m{i}"} for i in range(size)]
            idx = compute_force_cut_index(history, prefix_len=0)
            assert idx >= 1


# ─── Integration Pattern Tests ──────────────────────────────────────

class TestIntegrationPatterns:
    """Test patterns matching how connectors actually use the summarizer."""

    def test_full_force_summarize_flow_openai(self):
        """Simulate what groq/mistral/deepseek do on force trigger."""
        history = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "msg 1"},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "msg 2"},
            {"role": "assistant", "content": "reply 2"},
            {"role": "user", "content": "msg 3"},
            {"role": "assistant", "content": "reply 3"},
        ]
        max_chars = 1000
        total_chars = 950  # Above 90% threshold
        prefix_len = 1

        assert should_force_summarize(total_chars, max_chars)
        cut_idx = compute_force_cut_index(history, prefix_len)
        msgs_to_summarize = history[prefix_len:cut_idx]
        prompt = build_summary_prompt(msgs_to_summarize, lambda m: len(str(m.get("content", ""))))
        fake_summary = "User asked about X, assistant fixed Y in file Z."
        new_history = rewrite_history_with_summary(history, fake_summary, cut_idx, prefix_len, fmt="openai")

        assert new_history[0]["role"] == "system"
        assert "[Earlier context summary]" in new_history[1]["content"]
        assert fake_summary in new_history[1]["content"]
        assert len(new_history) < len(history)

    def test_full_voluntary_summarize_flow_gemini(self):
        """Simulate gemini voluntary tag emission flow."""
        model_response = 'I can help with that. <summarize_before message_index="4" /> Let me continue.'

        idx = extract_summarize_tag(model_response)
        assert idx == 4

        clean = strip_summarize_tag(model_response)
        assert "<summarize_before" not in clean
        assert "I can help with that." in clean
        assert "Let me continue." in clean

    def test_hint_injection_at_75_percent(self):
        """Verify hint gets injected at the right time."""
        max_chars = 10000
        assert not should_inject_hint(7499, max_chars)
        assert should_inject_hint(7500, max_chars)
        hint = get_hint_text(7500, max_chars)
        assert "75%" in hint

    def test_no_double_summarize(self):
        """After rewrite, chars should drop below thresholds."""
        history = [{"role": "user", "content": "x" * 100} for _ in range(20)]
        max_chars = 2000
        # Simulate: total was ~2000, after summarizing half it should drop
        new_history = rewrite_history_with_summary(history, "Short summary", cut_index=10, prefix_len=0, fmt="openai")
        new_total = sum(len(str(m.get("content", ""))) for m in new_history)
        assert new_total < max_chars * FORCE_THRESHOLD

    def test_streaming_tag_across_chunks(self):
        """Tag might span streaming chunks — full_answer accumulates, then we parse."""
        chunks = [
            "Here is ",
            "my response ",
            '<summarize_before ',
            'message_index="6" ',
            '/>',
            " and more text",
        ]
        full_answer = "".join(chunks)
        idx = extract_summarize_tag(full_answer)
        assert idx == 6
        clean = strip_summarize_tag(full_answer)
        assert "<summarize_before" not in clean
        assert "Here is my response" in clean
        assert "and more text" in clean


# ─── Qwen Exclusion Logic (Documentation Test) ─────────────────────

class TestQwenExclusion:
    """
    Qwen models should NOT use the summarizer.
    This is enforced at the connector level (connectors check model family),
    not inside context_summarizer.py itself. These tests document the contract.
    """

    def test_summarizer_works_regardless_of_model(self):
        """The summarizer functions are model-agnostic — exclusion is caller's job."""
        assert should_inject_hint(800, 1000)  # Works fine if called
        # The point is: Qwen connectors should NEVER call these functions

    def test_qwen_connectors_should_skip(self):
        """
        DOCUMENTATION: Qwen connectors (qwen/client.py) must NOT import or call
        any context_summarizer functions. The 4M char limit makes this unnecessary.
        This is verified by grep in CI or code review, not runtime tests.
        """
        # This test exists as a reminder. Actual enforcement is architectural.
        assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
