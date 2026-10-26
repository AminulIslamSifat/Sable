
from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class GuardrailDecision:
    """Result of a guardrail check with optional recovery support.

    Actions:
        allow   — proceed normally
        warn    — proceed but inject warning message
        recover — reset session, inject recovery prompt, give one more attempt
        block   — hard block, do not execute
    """
    action: Literal["allow", "warn", "recover", "block"]
    message: str | None = None
    recovery_key: str | None = None
    tool_name: str | None = None


def build_recovery_prompt(
    tool_name: str,
    args_preview: str,
    error_summary: str,
    attempt_count: int,
    original_task: str = "",
) -> str:
    """Build a recovery prompt for when a guardrail triggers recovery.

    This replaces the need for an external summarizer — the prompt itself
    carries the error context and instructs the model to try differently.
    """
    task_section = ""
    if original_task:
        task_section = f"\n\nOriginal task:\n{original_task[:1000]}"

    return (
        f"[GUARDRAIL RECOVERY — ATTEMPT {attempt_count}]\n\n"
        f"A tool-loop guardrail was triggered. The previous attempts got stuck "
        f"repeating a failing or no-progress action.\n\n"
        f"Failure details:\n"
        f"- Tool: {tool_name}\n"
        f"- Args preview: {args_preview[:200]}\n"
        f"- Error/result summary: {error_summary[:500]}\n"
        f"- Total attempts before recovery: {attempt_count}\n\n"
        f"Your task:\n"
        f"1. Briefly explain what went wrong.\n"
        f"2. Identify why repeating the same action will NOT help.\n"
        f"3. Choose a DIFFERENT tool or strategy to make progress.\n"
        f"4. Continue the original task, but do NOT repeat `{tool_name}` "
        f"with the same arguments unless you have a clear new reason."
        f"{task_section}"
    )


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

    Three independent detectors:
    1. Exact failure: same tool + args + error → warn@2, hard-stop@5
    2. Same-tool failure: same tool, any failure → warn@3, halt@8
    3. Idempotent no-progress: read-only tool, same args, same result → warn@2, block@5

    Plus structural loop detection (repeating tool-name patterns).

    Per-turn caps enforced via TurnCapTracker (separate class).
    """

    # --- Thresholds ---
    EXACT_FAIL_WARN = 2       # Warn at 2 exact failures (tool+args+error)
    EXACT_FAIL_STOP = 5       # Hard-stop at 5
    SAME_TOOL_FAIL_WARN = 3   # Warn at 3 failures on same tool (any error)
    SAME_TOOL_FAIL_HALT = 8   # Halt at 8
    NO_PROGRESS_WARN = 2      # Warn at 2 identical read-only results
    NO_PROGRESS_BLOCK = 5     # Block at 5
    CONSECUTIVE_THRESHOLD = 5 # Consecutive identical calls (same tool + args)

    # Read-only tools eligible for no-progress detection.
    _READ_ONLY_TOOLS = frozenset({
        "view_file", "get_file", "grep", "glob", "list_dir",
        "read_file", "search", "web_search", "fetch_url",
        "list_checkpoints", "agent_status",
    })

    # Tools exempt from ALL stall/no-progress guards (legitimate polling).
    _POLLING_EXEMPT_SUFFIXES = ("_get_result", "_poll")
    _POLLING_EXEMPT_EXACT = frozenset({"process", "bfl_flux3_get_result"})

    _GUARD_PFX = "[GUARD]"

    STUCK_WARNING = (
        _GUARD_PFX + "[LOOP WARNING] You have run the same command 5 times in a row. "
        "This approach is not working. Try a different strategy, search online, "
        "or summarize what you have so far."
    )

    EXACT_FAIL_WARNING = (
        _GUARD_PFX + "[EXACT FAILURE WARNING] The same tool call with identical arguments "
        "has failed {count} times with the same error. This will not succeed "
        "by retrying. Change your approach entirely."
    )

    EXACT_FAIL_STOP_MSG = (
        "[HARD STOP] Tool call '{tool}' with these arguments has failed "
        "{count} times with the same error. Execution blocked. "
        "You MUST use a different tool or strategy."
    )

    SAME_TOOL_FAIL_WARNING = (
        "[TOOL FAILURE WARNING] '{tool}' has failed {count} times total. "
        "Consider using an alternative tool or approach."
    )

    NO_PROGRESS_WARNING = (
        "[NO PROGRESS WARNING] Read-only tool '{tool}' returned identical "
        "results {count} times. The data hasn't changed — stop re-reading "
        "and use what you already have."
    )

    NO_PROGRESS_BLOCK_MSG = (
        "[BLOCKED] Read-only tool '{tool}' has returned identical results "
        "{count} times. Further identical calls are suppressed. "
        "Use the data you already have or try a different approach."
    )

    def __init__(self):
        self.history: list[str] = []
        self.per_tool_counts: dict[str, int] = defaultdict(int)
        self._stuck_warned: bool = False
        self._last_tool_name: str = ""  # for consecutive warning message

        # --- Detector 1: Exact failure tracking ---
        # Key: hash(tool_name:args:error_msg) → count
        self._exact_fail_counts: dict[str, int] = defaultdict(int)
        self._exact_fail_tool_names: dict[str, str] = {}  # err_sig → tool_name (for messages)
        self._exact_fail_warned: set[str] = set()  # signatures that already warned
        self._exact_fail_stopped: set[str] = set()  # signatures hard-stopped

        # --- Detector 2: Same-tool failure tracking ---
        # Key: tool_name → consecutive failure count
        self._tool_fail_counts: dict[str, int] = defaultdict(int)
        self._tool_fail_warned: set[str] = set()

        # --- Detector 3: No-progress tracking ---
        # Key: hash(tool_name:args:result_hash) → consecutive count
        self._no_progress_counts: dict[str, int] = defaultdict(int)
        self._no_progress_tool_names: dict[str, str] = {}  # np_key → tool_name
        self._no_progress_warned: set[str] = set()
        self._no_progress_blocked: set[str] = set()
        # Last result hash per (tool, args) for comparison
        self._last_result_hashes: dict[str, str] = {}

        # --- Result stubbing state ---
        self._last_results: dict[str, str] = {}  # sig → last full result text
        self._stub_count: dict[str, int] = defaultdict(int)  # sig → consecutive stubs

        # --- Recovery session tracking ---
        # Key: err_sig or np_key → number of recovery attempts used
        self._recovery_attempts: dict[str, int] = defaultdict(int)
        # Last error info for building recovery prompts
        self._last_error_info: dict[str, dict] = {}  # key → {tool, args, error}

    @classmethod
    def _is_polling_exempt(cls, tool_name: str) -> bool:
        """Check if tool is exempt from stall/no-progress guards."""
        if tool_name in cls._POLLING_EXEMPT_EXACT:
            return True
        return tool_name.endswith(cls._POLLING_EXEMPT_SUFFIXES)

    @staticmethod
    def _hash_str(s: str) -> str:
        """Fast short hash for signature keys."""
        return hashlib.md5(s.encode(), usedforsecurity=False).hexdigest()[:16]

    def check(self, tool_name: str, tool_args: str, error_msg: str = "") -> bool:
        """Record a tool call. Returns False if hard-stopped (caller must respect).

        Args:
            tool_name: Name of the tool being called.
            tool_args: Serialized arguments string.
            error_msg: Error message from previous call (empty = success).

        Use get_warning() after check() to retrieve any warning message.
        """
        sig = f"{tool_name}:{tool_args}"
        self.history.append(sig)
        self.per_tool_counts[tool_name] += 1

        self._last_tool_name = tool_name

        # --- Detector 1: Exact failure ---
        if error_msg:
            err_sig = self._hash_str(f"{sig}:{error_msg}")
            self._exact_fail_counts[err_sig] += 1
            self._exact_fail_tool_names[err_sig] = tool_name
            # Track per-tool failures too
            self._tool_fail_counts[tool_name] += 1
        else:
            # Success resets per-tool consecutive failure counter
            self._tool_fail_counts[tool_name] = 0

        # Check hard-stop for exact failures
        if error_msg:
            err_sig = self._hash_str(f"{sig}:{error_msg}")
            if err_sig in self._exact_fail_stopped:
                return False  # Already hard-stopped
            if self._exact_fail_counts[err_sig] >= self.EXACT_FAIL_STOP:
                self._exact_fail_stopped.add(err_sig)
                return False  # Signal hard-stop

        return True

    def check_decision(
        self, tool_name: str, tool_args: str, error_msg: str = "",
    ) -> GuardrailDecision:
        """Like check() but returns a GuardrailDecision with recovery support.

        On first hard-stop threshold → returns 'recover' (caller should reset
        session and inject recovery prompt). On second hard-stop after recovery
        → returns 'block'. Warnings return 'warn'. Normal operation returns 'allow'.
        """
        allowed = self.check(tool_name, tool_args, error_msg=error_msg)
        warning = self.get_warning()

        if not allowed:
            # Hard-stop triggered — check if recovery is available
            # Find the signature that caused the stop
            sig = f"{tool_name}:{tool_args}"
            recovery_key = None

            # Check exact failure signatures
            if error_msg:
                err_sig = self._hash_str(f"{sig}:{error_msg}")
                if err_sig in self._exact_fail_stopped:
                    recovery_key = err_sig

            # Check no-progress blocks
            if not recovery_key:
                for np_key in self._no_progress_blocked:
                    np_tn = self._no_progress_tool_names.get(np_key, "")
                    if np_tn == tool_name:
                        recovery_key = np_key
                        break

            if recovery_key:
                attempts = self._recovery_attempts.get(recovery_key, 0)
                if attempts == 0:
                    # First hard-stop → offer recovery
                    self._recovery_attempts[recovery_key] = 1
                    self._last_error_info[recovery_key] = {
                        "tool": tool_name,
                        "args": tool_args,
                        "error": error_msg or "no-progress (identical results)",
                        "count": self._exact_fail_counts.get(
                            recovery_key,
                            self._no_progress_counts.get(recovery_key, 0),
                        ),
                    }
                    # Undo the hard-stop so the tool can execute once more after recovery
                    self._exact_fail_stopped.discard(recovery_key)
                    self._no_progress_blocked.discard(recovery_key)
                    return GuardrailDecision(
                        action="recover",
                        message=warning or f"Recovery triggered for '{tool_name}'",
                        recovery_key=recovery_key,
                        tool_name=tool_name,
                    )
                # Already recovered once → true hard block
                return GuardrailDecision(
                    action="block",
                    message=warning or f"[HARD STOP] '{tool_name}' blocked after failed recovery.",
                    recovery_key=recovery_key,
                    tool_name=tool_name,
                )

            # No recovery key found (shouldn't happen) → block
            return GuardrailDecision(
                action="block",
                message=warning or f"[HARD STOP] '{tool_name}' blocked by loop guard.",
                tool_name=tool_name,
            )

        # Check for no-progress blocks (these don't make check() return False,
        # but they should trigger recovery/block in check_decision)
        sig = f"{tool_name}:{tool_args}"
        for np_key in self._no_progress_blocked:
            np_tn = self._no_progress_tool_names.get(np_key, "")
            if np_tn == tool_name:
                attempts = self._recovery_attempts.get(np_key, 0)
                if attempts == 0:
                    self._recovery_attempts[np_key] = 1
                    self._last_error_info[np_key] = {
                        "tool": tool_name,
                        "args": tool_args,
                        "error": "no-progress (identical results)",
                        "count": self._no_progress_counts.get(np_key, 0),
                    }
                    self._no_progress_blocked.discard(np_key)
                    return GuardrailDecision(
                        action="recover",
                        message=warning or f"Recovery triggered for '{tool_name}' (no-progress)",
                        recovery_key=np_key,
                        tool_name=tool_name,
                    )
                return GuardrailDecision(
                    action="block",
                    message=warning or f"[HARD STOP] '{tool_name}' blocked after failed recovery.",
                    recovery_key=np_key,
                    tool_name=tool_name,
                )

        if warning:
            return GuardrailDecision(
                action="warn",
                message=warning,
                tool_name=tool_name,
            )

        return GuardrailDecision(action="allow", tool_name=tool_name)

    def get_recovery_prompt(self, recovery_key: str, original_task: str = "") -> str:
        """Build a recovery prompt for the given recovery key."""
        info = self._last_error_info.get(recovery_key, {})
        tool_name = info.get("tool", "unknown")
        args_preview = info.get("args", "")
        error_summary = info.get("error", "unknown")
        attempt_count = info.get("count", 0)
        return build_recovery_prompt(
            tool_name=tool_name,
            args_preview=args_preview,
            error_summary=error_summary,
            attempt_count=attempt_count,
            original_task=original_task,
        )

    def record_result(self, tool_name: str, tool_args: str, result: str) -> str:
        """Record a tool result for no-progress detection and stubbing.

        Returns the result text, possibly replaced with a stub if duplicate.
        """
        if self._is_polling_exempt(tool_name):
            return result

        sig = f"{tool_name}:{tool_args}"
        result_hash = self._hash_str(result) if result else ""

        # --- Detector 3: No-progress detection (read-only tools only) ---
        if tool_name in self._READ_ONLY_TOOLS and result_hash:
            prev_hash = self._last_result_hashes.get(sig)
            if prev_hash == result_hash:
                np_key = self._hash_str(f"{sig}:{result_hash}")
                self._no_progress_counts[np_key] += 1
                self._no_progress_tool_names[np_key] = tool_name
            else:
                # Different result — reset counter for this sig
                for k in list(self._no_progress_counts):
                    if k.startswith(self._hash_str(sig)):
                        self._no_progress_counts[k] = 0
            self._last_result_hashes[sig] = result_hash

        # --- Result stubbing (all tools, >512 chars, byte-identical) ---
        if len(result) > 512:
            prev = self._last_results.get(sig)
            if prev == result:
                self._stub_count[sig] += 1
                if self._stub_count[sig] >= 2:  # From 2nd consecutive identical
                    args_preview = tool_args[:120]
                    stub = f"[STUB] Same result as previous call ({len(result)} chars). Args: {args_preview}"
                    self._last_results[sig] = result  # Keep tracking
                    return stub
            else:
                self._stub_count[sig] = 0
            self._last_results[sig] = result
        else:
            self._stub_count[sig] = 0

        return result

    def get_warning(self) -> str | None:
        """Return a warning message if a loop pattern is detected, else None.

        Priority: exact-fail-stop > exact-fail-warn > same-tool-warn >
                  no-progress-block > no-progress-warn > stuck > structure.
        Each warning fires only once per unique signature to avoid spam.
        """
        # --- Exact failure warnings/stops ---
        for err_sig, count in self._exact_fail_counts.items():
            if err_sig in self._exact_fail_stopped:
                _tn = self._exact_fail_tool_names.get(err_sig, "unknown")
                return self.EXACT_FAIL_STOP_MSG.format(
                    tool=_tn, count=count
                )
            if count >= self.EXACT_FAIL_WARN and err_sig not in self._exact_fail_warned:
                self._exact_fail_warned.add(err_sig)
                return self.EXACT_FAIL_WARNING.format(count=count)

        # --- Same-tool failure warnings ---
        for tool, count in self._tool_fail_counts.items():
            if count >= self.SAME_TOOL_FAIL_WARN and tool not in self._tool_fail_warned:
                self._tool_fail_warned.add(tool)
                return self.SAME_TOOL_FAIL_WARNING.format(tool=tool, count=count)

        # --- No-progress warnings/blocks ---
        for np_key, count in self._no_progress_counts.items():
            _np_tn = self._no_progress_tool_names.get(np_key, "unknown")
            if count >= self.NO_PROGRESS_BLOCK and np_key not in self._no_progress_blocked:
                self._no_progress_blocked.add(np_key)
                return self.NO_PROGRESS_BLOCK_MSG.format(
                    tool=_np_tn, count=count
                )
            if count >= self.NO_PROGRESS_WARN and np_key not in self._no_progress_warned:
                self._no_progress_warned.add(np_key)
                return self.NO_PROGRESS_WARNING.format(
                    tool=_np_tn, count=count
                )

        # --- Consecutive identical calls (exact same tool + args) ---
        if not self._stuck_warned and len(self.history) >= self.CONSECUTIVE_THRESHOLD:
            recent = self.history[-self.CONSECUTIVE_THRESHOLD:]
            if len(set(recent)) == 1:
                self._stuck_warned = True
                return self._GUARD_PFX + f"[LOOP WARNING] You have run '{self._last_tool_name}' {self.CONSECUTIVE_THRESHOLD} times in a row with identical arguments. This approach is not working. Try a different strategy, search online, or summarize what you have so far."

        return None


class TurnCapTracker:
    """Per-turn caps on expensive operations. Resets at start of each turn.

    Inspired by Claude Code v2.1.212 per-turn limits. Hard ceiling regardless
    of hard_stop_enabled flag — prevents runaway context bloat and API costs.
    """

    # Per-turn caps
    MAX_WEB_SEARCHES = 50
    MAX_SUBAGENT_SPAWNS = 50

    # Tool names that count toward each cap
    _WEB_SEARCH_TOOLS = frozenset({"web_search", "web_fetch", "search"})
    _SPAWN_TOOLS = frozenset({"spawn_agent"})

    CAP_WARNING = (
        "[PER-TURN CAP] You have reached the maximum of {cap} {operation} "
        "calls this turn. No more {operation} calls are allowed until the "
        "next user message. Summarize what you have so far."
    )

    def __init__(self):
        self.web_search_count: int = 0
        self.spawn_count: int = 0

    def reset(self) -> None:
        """Reset all counters. Call at start of each run_conversation / turn."""
        self.web_search_count = 0
        self.spawn_count = 0

    def check_and_record(self, tool_name: str) -> str | None:
        """Check if tool is within caps. Records the call if allowed.

        Returns a warning string if cap exceeded (caller should inject as
        feedback and skip execution), or None if allowed.
        """
        if tool_name in self._WEB_SEARCH_TOOLS:
            self.web_search_count += 1
            if self.web_search_count > self.MAX_WEB_SEARCHES:
                return self.CAP_WARNING.format(
                    cap=self.MAX_WEB_SEARCHES, operation="web search"
                )
        elif tool_name in self._SPAWN_TOOLS:
            self.spawn_count += 1
            if self.spawn_count > self.MAX_SUBAGENT_SPAWNS:
                return self.CAP_WARNING.format(
                    cap=self.MAX_SUBAGENT_SPAWNS, operation="subagent spawn"
                )
        return None


def _tool_param_map() -> dict[str, frozenset[str]]:
    """Map tool name -> its declared parameter names (lowercased).

    Reads the same live discovery cache the parser and the prompt use, so a
    tool added at runtime is covered the moment ``reload_tools()`` runs. Built
    per call rather than cached — ``discover_tools()`` is already cached, so
    this is a cheap iteration over a small list, and it can never go stale.
    """
    out: dict[str, frozenset[str]] = {}
    try:
        from engine.tools_loader import discover_tools

        for group in discover_tools():
            for fn in group.functions:
                props = (fn.get("parameters") or {}).get("properties") or {}
                if isinstance(props, dict) and props:
                    out[fn["name"].lower()] = frozenset(str(p).lower() for p in props)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("_tool_param_map unavailable: %s", exc)
    return out


class MainChatGuard:
    """Guards for the main chat loop — mirrors subagent LoopDetector but
    with warnings injected as feedback instead of hard stops.

    Tracks:
    - Repeated identical commands (5+ consecutive -> loop warning)
    - Consecutive tool failures (5+ -> rethink/search warning)
    - Malformed tool_call blocks (JSON outside tool_call wrap, orphan close tags)
    """

    _GUARD_PFX = "[GUARD]"
    LOOP_THRESHOLD = 5
    FAILURE_THRESHOLD = 5

    LOOP_WARNING = (
        _GUARD_PFX + "[LOOP WARNING] You have run the same command 5 times in a row. "
        "This approach is not working. Try a different strategy, search online, "
        "or summarize what you have so far."
    )

    FAILURE_WARNING = (
        _GUARD_PFX + "[FAILURE WARNING] 5 consecutive tool failures detected. "
        "Stop retrying the same approach. Either:\n"
        "1. Search online for the correct solution\n"
        "2. Rethink your strategy entirely\n"
        "3. Explain what is failing and ask for guidance"
    )

    def __init__(self, provider: str | None = None):
        self._provider = provider
        self._command_history: list[str] = []
        self._consecutive_failures: int = 0
        self._loop_warned: bool = False
        self._failure_warned: bool = False
        self._malformed_warned: bool = False
        self._incomplete_warned: bool = False
        self._scattered_warned: bool = False

    def _format_tags(self) -> tuple[str, str, str]:
        """Resolve (open_tag, close_tag, example) for the ACTIVE tool format.

        Reads the SAME resolver the system prompt uses, so the guard's guidance
        always matches whatever wrapper the model was told to emit — including a
        user override from the settings UI. Falls back to the <action> style.
        """
        try:
            from connectors.common.instruction_builder import get_tool_format_tags
            return get_tool_format_tags(self._provider)
        except Exception:
            _ao, _ac = "<action>", "</action>"
            return (_ao, _ac, _ao + '[{"name": "tool_name", "arguments": {...}}]' + _ac)

    def _wrapper_phrase(self) -> str:
        """Human phrase for the active wrapper (or bare-array wording if tagless)."""
        open_tag, close_tag, _ = self._format_tags()
        if open_tag and close_tag:
            return f"{open_tag}...{close_tag}"
        return "a bare JSON array (no wrapper tags)"

    def _get_malformed_warning(self, kind: str) -> str:
        """Return a format-correct malformed warning for the active tool format.

        Dynamically reflects the model's instructed wrapper (settings override or
        per-provider default) instead of hardcoding <action>.
        """
        open_tag, close_tag, example = self._format_tags()
        wrapper = self._wrapper_phrase()

        if kind == "no_open":
            return (
                self._GUARD_PFX + "[FORMAT WARNING] Found a closing tag without a matching opening tag. "
                f"Wrap tool calls in {wrapper}."
            )
        if kind == "no_close":
            _close = close_tag or "its matching close tag"
            _open = open_tag or "tool call"
            return (
                self._GUARD_PFX + "[FORMAT WARNING] Found an opening tag without a closing tag. "
                f"Every {_open} block must be properly closed with {_close}."
            )
        if kind == "json_outside":
            return (
                self._GUARD_PFX + "[FORMAT WARNING] Tool call JSON was found outside the required wrapper. "
                f"All tool calls MUST be wrapped as {wrapper}. Example: {example}"
            )
        # invalid_json (and any unknown kind)
        return (
            self._GUARD_PFX + "[FORMAT WARNING] Tool call contains invalid JSON. "
            f"Fix the JSON syntax inside {wrapper} and try again."
        )

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
        """Check for malformed tool_call blocks in raw LLM output (Hermes format).

        Returns a warning string if issues found, else None.
        Detects: orphan close tags, unclosed open tags, JSON outside tool_call blocks,
        invalid JSON inside tool_call blocks.
        Only fires once per guard lifetime to avoid self-referential loops.
        """
        if self._malformed_warned:
            return None
        # Native function-calling / no-prompt-format modes emit bare JSON (or no
        # text tool calls at all). There is no text wrapper to validate, so skip —
        # otherwise we'd flag the model's CORRECT output as malformed.
        try:
            from connectors.common.instruction_builder import get_tool_format_name
            if get_tool_format_name(self._provider) in ("native", "none"):
                return None
        except Exception:
            pass
        import re as _re
        # Support both <action> and <tool_call wrappers
        open_pat = r"<\s*(?:action|tool_call)\s*>"
        close_pat = r"<\s*/\s*(?:action|tool_call)\s*>"
        has_open = bool(_re.search(open_pat, raw_text, _re.I))
        has_close = bool(_re.search(close_pat, raw_text, _re.I))

        # Orphan close without open — only warn if surrounding content looks like JSON attempt
        if has_close and not has_open:
            _close_match = _re.search(close_pat, raw_text, _re.I)
            if _close_match:
                _before_close = raw_text[:_close_match.start()].rstrip()
                _looks_like_json = _before_close.endswith('}') or _before_close.endswith(']')
                if _looks_like_json:
                    logger.warning("[MALFORMED_GUARD] Orphan close tag with JSON content. raw_len=%d preview=%r", len(raw_text), raw_text[:300])
                    self._malformed_warned = True
                    return self._get_malformed_warning("no_open")

        # Open without close — only warn if content after open tag looks like JSON attempt
        if has_open and not has_close:
            _open_match = _re.search(open_pat, raw_text, _re.I)
            if _open_match:
                _after_open = raw_text[_open_match.end():].lstrip()
                _looks_like_json = _after_open.startswith('{') or _after_open.startswith('[')
                if _looks_like_json:
                    logger.warning("[MALFORMED_GUARD] Open tag without close (JSON content). raw_len=%d preview=%r", len(raw_text), raw_text[:300])
                    self._malformed_warned = True
                    return self._get_malformed_warning("no_close")

        # Check for JSON tool calls outside tool_call blocks
        from engine.skills.parser import get_known_tags
        tool_json_pat = _re.compile(
            r'"name"\s*:\s*"(' + '|'.join(_re.escape(t) for t in get_known_tags()) + r')"',
            _re.I
        )

        if not has_open and not has_close:
            if tool_json_pat.search(raw_text):
                self._malformed_warned = True
                return self._get_malformed_warning("json_outside")
        elif has_open and has_close:
            stripped = _re.sub(r"<\s*(?:action|tool_calls?)\s*>.*?<\s*/\s*(?:action|tool_calls?)\s*>", "", raw_text, flags=_re.S | _re.I)
            if tool_json_pat.search(stripped):
                self._malformed_warned = True
                return self._get_malformed_warning("json_outside")

        # Check for invalid JSON inside tool_call blocks
        if has_open and has_close:
            from engine.skills.parser import _parse_action_payload, _diagnose_json_failure, sanitize_transport
            action_contents = _re.findall(r"<\s*(?:action|tool_call)\s*>(.*?)<\s*/\s*(?:action|tool_call)\s*>", raw_text, flags=_re.S | _re.I)
            for block in action_contents:
                block = block.strip()
                if not block:
                    continue
                # Skip JSON validation for XML invoke/function format — it's valid, just not JSON.
                # Models may emit <invoke name="..."> or <function=name> inside <tool_call> wrappers.
                if _re.search(r'<\s*(?:invoke\b|function\s*=)', block, _re.I):
                    continue
                # Pre-validate: try sanitizing before declaring failure
                sanitized_block = sanitize_transport(block)
                if not _parse_action_payload(sanitized_block) and not _parse_action_payload(block):
                    logger.warning(
                        "[MALFORMED_GUARD] Invalid JSON in tool_call block. "
                        "raw_len=%d block_preview=%r raw_text_preview=%r",
                        len(raw_text), block[:200], raw_text[:300],
                    )
                    self._malformed_warned = True
                    wrapper = self._wrapper_phrase()
                    _example = self._format_tags()[2]
                    return (
                        self._GUARD_PFX + f"[FORMAT WARNING] Invalid JSON in tool call ({wrapper}).\n"
                        + _diagnose_json_failure(block) + "\n\n"
                        + f"Fix the JSON syntax inside {wrapper} and try again. Example: {_example}"
                    )

        return None


    def check_incomplete_action(self, raw_text: str, tools_executed: bool) -> str | None:
        """Check if response contains tool_call markers but nothing was executed.

        Catches truncated responses where the model started a tool_call block
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

        import re as _re
        # Strip code fences and inline backticks before checking
        _stripped = _re.sub(r"`{3}.*?`{3}", "", raw_text, flags=_re.S)
        _stripped = _re.sub(r"`[^`]+`", "", _stripped)
        has_action_markers = bool(_re.search(
            r"<\s*/?\s*tool_call\s*>", _stripped, _re.I
        ))
        # Check for JSON tool call patterns with known tool names
        from engine.skills.parser import get_known_tags
        _live_tags = get_known_tags()
        tool_json_pat = _re.compile(
            r'"name"\s*:\s*"(' + '|'.join(_re.escape(t) for t in _live_tags) + r')"',
            _re.I
        )
        has_tool_json = bool(tool_json_pat.search(_stripped))
        # Check for MiMo-style XML function calls: <function=tool_name>
        has_mimo_xml = bool(_re.search(
            r'<\s*function\s*=\s*(?:' + '|'.join(_re.escape(t) for t in _live_tags) + r')\b',
            _stripped, _re.I
        ))

        if has_action_markers or has_tool_json or has_mimo_xml:
            self._incomplete_warned = True
            return (
                "[INCOMPLETE RESPONSE WARNING] Your last response contained "
                "tool calls or <tool_call> blocks but no tools were actually executed. "
                "The response may have been truncated. Please retry the last command "
                "or continue from where you left off."
            )
        return None

    def check_scattered_tool_call(self, raw_text: str, tools_executed: bool) -> str | None:
        """Catch a tool call NARRATED in prose that never actually executed.

        The failure mode: the model writes something like
            "Let me run execute_command with command='ls -la' and cwd='/tmp'"
        in plain text, with no wrapper. Nothing errors, nothing dispatches, the
        turn just ends and the user sees a promise instead of an action.

        False positives are the whole risk, so the match is deliberately narrow.
        ALL of these must hold:
          * a known tool name, AND
          * one of that tool's REAL parameters, AND
          * an assignment (``param=…`` / ``param: "…"`` / ``param("…")``),
          * all inside ONE small window (a little before the name, ~160 chars
            after it) — a long message that mentions a tool near the top and
            "the command" twenty lines down never trips it,
          * not inside a fenced block or inline code (that is an example, not
            intent),
          * with no wrapper tag present (broken-JSON is the other guards' job),
          * and zero real tools executed this round.
        Fires at most once per guard lifetime.
        """
        if self._scattered_warned:
            return None
        if tools_executed:
            return None
        if not raw_text or not raw_text.strip():
            return None
        # Native / no-prompt-format providers have no text wrapper to validate.
        try:
            from connectors.common.instruction_builder import get_tool_format_name

            if get_tool_format_name(self._provider) in ("native", "none"):
                return None
        except Exception:
            pass

        import re as _re

        # Code fences / inline code: an example call here is explanation.
        _stripped = _re.sub(r"`{3}.*?`{3}", "", raw_text, flags=_re.S)
        _stripped = _re.sub(r"`[^`\n]+`", "", _stripped)

        # A wrapper exists -> broken JSON inside it is check_malformed_action's job.
        if _re.search(r"<\s*/?\s*(?:action|tool_call|tool_calls)\b", _stripped, _re.I):
            return None

        param_map = _tool_param_map()
        if not param_map:
            return None

        low = _stripped.lower()
        # Longest name first so web_search beats a hypothetical search.
        for tname in sorted(param_map, key=len, reverse=True):
            params = param_map[tname]
            if not params:
                continue
            for nm in _re.finditer(
                r"(?<![A-Za-z0-9_])" + _re.escape(tname) + r"(?![A-Za-z0-9_])", low
            ):
                start = max(0, nm.start() - 40)
                end = min(len(low), nm.end() + 160)
                window = low[start:end]
                for p in params:
                    pat = (
                        r"(?<![A-Za-z0-9_])" + _re.escape(p) + r"(?![A-Za-z0-9_])"
                        r"\s*(?:=\s*[^\s,;]|:\s*[\"'\[\{]|\s*\(\s*[\"'])"
                    )
                    if _re.search(pat, window):
                        logger.warning(
                            "[SCATTERED_TOOL_GUARD] prose tool call not executed: "
                            "tool=%s param=%s window=%r",
                            tname, p, window[:200],
                        )
                        self._scattered_warned = True
                        wrapper = self._wrapper_phrase()
                        example = self._format_tags()[2]
                        return (
                            self._GUARD_PFX
                            + "[FORMAT WARNING] Your message described a tool call in plain "
                            f"text (`{tname}` with `{p}=…`) but no tool was executed. "
                            "Text is not a tool call. If you meant to run it, emit it "
                            f"properly wrapped in {wrapper} — for example: {example}\n"
                            "If you were only explaining, ignore this."
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
        self._scattered_warned = False
