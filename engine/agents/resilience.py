
from __future__ import annotations

import time
from collections import defaultdict


class CircuitBreaker:
    """Per-backend circuit breaker. States: closed → open → half-open → closed."""

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 60):
        self.failures = 0
        self.threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.last_failure: float = 0
        self.state = "closed"

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure > self.reset_timeout:
                # Transition to half-open: THIS call is the single probe
                self.state = "half-open"
                return True
            return False
        # half-open: probe already in flight, block additional calls
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure = time.time()
        if self.state == "half-open":
            # Probe failed — go back to open
            self.state = "open"
        elif self.failures >= self.threshold:
            self.state = "open"


class LoopDetector:
    """Detects repeated identical tool calls. Mirrors MainChatGuard's
    warning-based approach — never hard-kills, only returns warnings.

    Uses signature-based detection (tool_name + args) so that legitimate
    repeated use of the same tool with different arguments (e.g. viewing
    different sections of a large file) is NOT flagged.
    """

    CONSECUTIVE_THRESHOLD = 5  # Match MainChatGuard
    STRUCTURE_THRESHOLD = 5

    # Read-only tools that are legitimately called many times with different args.
    # Excluded from structural loop detection (still tracked for consecutive-identical).
    _READ_ONLY_TOOLS = frozenset({
        "view_file", "get_file", "grep", "glob", "list_dir",
        "read_file", "search", "web_search", "fetch_url",
    })

    STUCK_WARNING = (
        "[LOOP WARNING] You have run the same command 5 times in a row. "
        "This approach is not working. Try a different strategy, search online, "
        "or summarize what you have so far."
    )

    STRUCTURE_WARNING = (
        "[LOOP WARNING] The same command structure has been repeated 5+ times. "
        "This approach is not working. Stop repeating and either:\n"
        "1. Try a completely different strategy\n"
        "2. Summarize what you have and provide your final answer\n"
        "Do NOT repeat the same command again."
    )

    def __init__(self, max_consecutive: int = 5, max_total: int = 50):
        self.history: list[str] = []
        self.per_tool_counts: dict[str, int] = defaultdict(int)
        self.structure_history: list[str] = []  # tag names only (no args)
        self.max_consecutive = max_consecutive
        self.max_total = max_total  # Very high — only catches true runaway loops
        self._stuck_warned: bool = False
        self._structure_warned: bool = False

    def check(self, tool_name: str, tool_args: str) -> bool:
        """Record a tool call. Always returns True (never hard-kills).
        
        Use get_warning() after check() to retrieve any warning message.
        """
        sig = f"{tool_name}:{tool_args}"
        self.history.append(sig)
        # Only track non-read-only tools for structural loop detection
        if tool_name not in self._READ_ONLY_TOOLS:
            self.structure_history.append(tool_name)
        self.per_tool_counts[tool_name] += 1
        return True  # Never block — warnings only

    def get_warning(self) -> str | None:
        """Return a warning message if a loop pattern is detected, else None.
        
        Each warning fires only once per detector lifetime to avoid
        self-referential loops.
        """
        # Consecutive identical calls (exact same tool + args)
        if not self._stuck_warned and len(self.history) >= self.CONSECUTIVE_THRESHOLD:
            recent = self.history[-self.CONSECUTIVE_THRESHOLD:]
            if len(set(recent)) == 1:
                self._stuck_warned = True
                return self.STUCK_WARNING

        # Structural looping (same tool names in same order)
        if not self._structure_warned and self.is_structure_looping():
            self._structure_warned = True
            return self.STRUCTURE_WARNING

        return None

    def is_structure_looping(self, threshold: int | None = None) -> bool:
        """Check if the same sequence of tool names repeats N+ times.
        
        Detects patterns like [grep, view_file, grep, view_file, grep, view_file]
        where the agent keeps using the same tools in the same order without progress.
        """
        t = threshold or self.STRUCTURE_THRESHOLD
        if len(self.structure_history) < t:
            return False
        
        # Try pattern lengths from 1 to t
        for pat_len in range(1, min(t + 1, len(self.structure_history) // 2 + 1)):
            pattern = self.structure_history[-pat_len:]
            match_count = 0
            pos = len(self.structure_history) - pat_len
            while pos >= 0:
                segment = self.structure_history[pos:pos + pat_len]
                if segment == pattern:
                    match_count += 1
                    pos -= pat_len
                else:
                    break
            if match_count >= t:
                return True
        
        return False

class MainChatGuard:
    """Guards for the main chat loop — mirrors subagent LoopDetector but
    with warnings injected as feedback instead of hard stops.

    Tracks:
    - Repeated identical commands (5+ consecutive -> loop warning)
    - Consecutive tool failures (5+ -> rethink/search warning)
    - Malformed action blocks (tags outside action wrap, orphan close tags)
    """

    LOOP_THRESHOLD = 5
    FAILURE_THRESHOLD = 5

    LOOP_WARNING = (
        "[LOOP WARNING] You have run the same command 5 times in a row. "
        "This approach is not working. Try a different strategy, search online, "
        "or summarize what you have so far."
    )

    FAILURE_WARNING = (
        "[FAILURE WARNING] 5 consecutive tool failures detected. "
        "Stop retrying the same approach. Either:\n"
        "1. Search online for the correct solution\n"
        "2. Rethink your strategy entirely\n"
        "3. Explain what is failing and ask for guidance"
    )

    MALFORMED_NO_OPEN = (
        f"[FORMAT WARNING] Found a closing </action> tag without a matching "
        f"<action> opening tag. Wrap your tool calls like this:\n"
        f"<action>\n<your_tag>...</your_tag>\n</action>"
    )

    MALFORMED_TAG_OUTSIDE = (
        f"[FORMAT WARNING] A tool tag was found outside an <action> block. "
        f"All tool tags MUST be wrapped:\n"
        f"<action>\n<your_tag>...</your_tag>\n</action>"
    )

    def __init__(self):
        self._command_history: list[str] = []
        self._consecutive_failures: int = 0
        self._loop_warned: bool = False
        self._failure_warned: bool = False
        self._malformed_warned: bool = False
        self._incomplete_warned: bool = False

    def record_command(self, tag_name: str, tag_content: str) -> None:
        """Record a command execution for loop detection."""
        sig = f"{tag_name}:{tag_content.strip()[:200]}"
        self._command_history.append(sig)

    def record_result(self, ok: bool) -> None:
        """Track consecutive failures from skill_end events."""
        if ok:
            self._consecutive_failures = 0
            self._failure_warned = False
        else:
            self._consecutive_failures += 1

    def check_loop(self) -> str | None:
        """Return loop warning message if threshold hit, else None."""
        if self._loop_warned:
            return None
        if len(self._command_history) < self.LOOP_THRESHOLD:
            return None
        recent = self._command_history[-self.LOOP_THRESHOLD:]
        if len(set(recent)) == 1:
            self._loop_warned = True
            return self.LOOP_WARNING
        return None

    def check_failures(self) -> str | None:
        """Return failure warning if consecutive failures hit threshold."""
        if self._failure_warned:
            return None
        if self._consecutive_failures >= self.FAILURE_THRESHOLD:
            self._failure_warned = True
            return self.FAILURE_WARNING
        return None

    def check_malformed_action(self, raw_text: str) -> str | None:
        """Check for malformed action blocks in raw LLM output.

        Returns a warning string if issues found, else None.
        Detects: orphan close tags, tool tags outside action blocks.
        Only fires once per guard lifetime to avoid self-referential loops
        (warning text contains example action tags that would re-trigger).
        """
        if self._malformed_warned:
            return None
        import re
        open_pat = r"<\s*action\s*>"
        close_pat = r"<\s*/\s*action\s*>"
        has_open = bool(re.search(open_pat, raw_text, re.I))
        has_close = bool(re.search(close_pat, raw_text, re.I))

        # Orphan close without open
        if has_close and not has_open:
            self._malformed_warned = True
            return MainChatGuard.MALFORMED_NO_OPEN

        # Check for known tool tags outside action blocks
        from engine.skills.parser import KNOWN_TAGS
        tag_pattern = "|".join(re.escape(t) for t in KNOWN_TAGS)
        tag_re = re.compile(r"<\s*(?:" + tag_pattern + r")\b", re.I)

        if not has_open and not has_close:
            # No action block at all but tool tags present
            if tag_re.search(raw_text):
                self._malformed_warned = True
                return MainChatGuard.MALFORMED_TAG_OUTSIDE
        elif has_open:
            # Strip all action block contents, check for leftover tags
            stripped = re.sub(r"<\s*action\s*>.*?<\s*/\s*action\s*>", "", raw_text, flags=re.S | re.I)
            if tag_re.search(stripped):
                self._malformed_warned = True
                return MainChatGuard.MALFORMED_TAG_OUTSIDE

        return None


    def check_incomplete_action(self, raw_text: str, tools_executed: bool) -> str | None:
        """Check if response contains action/tool markers but nothing was executed.

        Catches truncated responses where the model started an action block
        but the stream ended before any tool was actually dispatched.
        Returns a warning string if incomplete, else None.
        Only fires once per guard lifetime to avoid duplication.
        """
        if self._incomplete_warned:
            return None
        if tools_executed:
            return None
        if not raw_text or not raw_text.strip():
            return None

        import re
        # Check for action block markers
        # Strip code fences and inline backticks before checking
        _stripped = re.sub(r"`{3}.*?`{3}", "", raw_text, flags=re.S)
        _stripped = re.sub(r"`[^`]+`", "", _stripped)
        has_action_markers = bool(re.search(
            r"</?\s*action\s*>", _stripped, re.I
        ))
        # Check for known tool tags
        from engine.skills.parser import KNOWN_TAGS
        tag_pattern = "|".join(re.escape(t) for t in KNOWN_TAGS)
        has_tool_tags = bool(re.search(
            r"<\s*(?:" + tag_pattern + r")\b", _stripped, re.I
        ))

        if has_action_markers or has_tool_tags:
            self._incomplete_warned = True
            return (
                "[INCOMPLETE RESPONSE WARNING] Your last response contained "
                f"tool tags or <action> blocks but no tools were actually executed. "
                "The response may have been truncated. Please retry the last command "
                "or continue from where you left off."
            )
        return None

    def reset(self) -> None:
        """Reset all guard state (e.g., on new user message)."""
        self._command_history.clear()
        self._consecutive_failures = 0
        self._loop_warned = False
        self._failure_warned = False
        self._malformed_warned = False
        self._incomplete_warned = False
