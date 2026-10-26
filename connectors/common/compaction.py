"""Central context compaction for Sable.

Replaces the per-connector, invisible summarisers with one route-layer pass
that:

  * triggers off the same max_session_chars already configured per model,
  * uses the SAME model the chat is already using (no extra provider),
  * persists the summary to the chats table so it survives refresh,
  * persists the cut-off message id so outbound history is sliced, not
    re-sent in full,
  * emits SSE events so the frontend can render a card and reset the ring.

Design notes
------------
The DB always keeps the FULL transcript. Only what we hand the model is
sliced:

    [{"role": "user", "content": "[Earlier context summary]\\n" + summary}]
    + every message with id > compacted_until_msg_id

That is the whole trick. Without the slice the summary is pointless: the
model would keep receiving the entire history and pay the full price again.

The summary prompt is adapted from opencode's structured handoff template.
We deliberately do NOT port its 930-line Effect-TS machinery, its native
(provider-side encrypted) compaction path, or its measured-token anchoring.
The template and the atomic-exchange rule are the two things worth having.
"""

from __future__ import annotations

from typing import Any

# Fire when the outbound request reaches this share of max_session_chars.
TRIGGER_RATIO = 0.90

# Keep this share of max_session_chars verbatim beside the summary.
# Mirrors opencode's default 15k-token tail, expressed as a ratio so it
# scales down for a tiny test budget.
KEEP_RATIO = 0.15

# Hard floor on the verbatim tail, so a 4000-char test budget still keeps
# at least the most recent message instead of summarising everything.
MIN_KEEP_CHARS = 400

# Never summarise fewer than this many messages - a two-message conversation
# has nothing worth compressing.
MIN_MESSAGES_TO_SUMMARIZE = 2

# The history must occupy at least this share of the budget before compaction
# is worth doing. Below it the summary would be roughly as large as the
# transcript it replaces, so every compaction would GROW the context.
MIN_HISTORY_SHARE = 0.20

SUMMARY_PREFIX = "[Earlier context summary]\n"

# --------------------------------------------------------------------------
# Prompt - adapted from opencode's buildPrompt()
# --------------------------------------------------------------------------

SUMMARY_TEMPLATE = """## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Requirements
- [constraints, preferences, requirements, and scope boundaries stated by the user, or "(none)"]

## Decisions
- [decisions already made and why, or "(none)"]

## Work State
### Completed
- [goals that have been completed; otherwise "(none)"]

### Active
- [goals currently being worked on; otherwise "(none)"]

### Blocked
- [anything blocking progress, and why; otherwise "(none)"]

## Next Move
1. [ordered list of next actions, or "(none)"]

## Relevant Files
- `[file or directory path]`: [brief reason it matters]

## Important Context
- [facts needed to continue that cannot be easily recovered; or "(none)"]"""

SUMMARY_RULES = """Rules:
- Keep each section concise. Use terse, single-line bullets, not prose paragraphs or nested lists.
- Prefer short references over detailed restatement. It is fine to leave out information that can be recovered from the code or the files listed above.
- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers.
- Carry forward only user questions or requests that remain unanswered or require further action. Do not repeat ones that newer history has already answered.
- Preserve consequential workflow state, including whether changes are uncommitted, committed, pushed, under review, or merged.
- Do not mention the summary process or that context was compacted."""


def build_summary_prompt(history: list[dict[str, Any]]) -> str:
    """Build the summarisation prompt from the messages being replaced."""
    lines: list[str] = []
    for i, msg in enumerate(history):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = [
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            ]
            content = " ".join(parts)
        lines.append(f"[Message {i} | {role}]: {content}")

    return (
        "Summarize the conversation above into a structured summary that will "
        "be given to another agent to resume the work.\n\n"
        f"{SUMMARY_TEMPLATE}\n\n"
        f"{SUMMARY_RULES}\n\n"
        "Do not continue the task or call tools. "
        "Return only the structured summary. Do not include a preamble, "
        "explanation, or other commentary.\n\n"
        "--- BEGIN HISTORY TO SUMMARIZE ---\n"
        + "\n".join(lines)
        + "\n--- END HISTORY ---"
    )


# --------------------------------------------------------------------------
# Size estimation - deliberately matches token_counter's char-based maths
# --------------------------------------------------------------------------

def _content_chars(msg: dict[str, Any]) -> int:
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for p in content:
            if isinstance(p, dict):
                total += len(p.get("text", ""))
            elif isinstance(p, str):
                total += len(p)
        return total
    return 0


def history_chars(history: list[dict[str, Any]]) -> int:
    """Total characters across a history list."""
    return sum(_content_chars(m) for m in history)


def last_message_id(history: list[dict[str, Any]]) -> int | None:
    """Row id of the newest message, when the caller carried ids along.

    Compaction needs an EXACT cutoff. Deriving it by matching content prefixes
    is unsafe: repeated tool output and restated paragraphs are common, and the
    scan then lands on the wrong row and truncates the very turns it was asked
    to preserve. Returns None when ids are absent, so callers can fall back.
    """
    for m in reversed(history):
        _id = m.get("_id")
        if isinstance(_id, int):
            return _id
    return None


def should_compact(current_chars: int, max_chars: int | None) -> bool:
    """True when the outbound request has crossed the trigger line."""
    if not max_chars or max_chars <= 0:
        return False
    return current_chars >= max_chars * TRIGGER_RATIO


def can_compact(
    history: list[dict[str, Any]],
    max_chars: int | None,
    overhead_chars: int = 0,
) -> bool:
    """True when a compaction would actually help.

    Three independent reasons to decline, all of them learned the hard way:

    1. No budget configured.

    2. FIXED OVERHEAD ALONE BUSTS THE BUDGET. The trigger is measured on the
       whole prompt (system instruction + tool schemas + history), but only the
       history is reducible. If the persona + tool docs already cross the line,
       compaction cannot bring the request back under it - it would fire on
       every single turn, summarising the previous summary forever, each one
       bigger than the last. That is not a theory; it happened.

    3. The history is too small to be worth rewriting. A 650-char summary
       re-summarised into a 900-char summary is pure loss.
    """
    if not max_chars or max_chars <= 0:
        return False
    # (2) Irreducible overhead already at/over the trigger line.
    if overhead_chars >= max_chars * TRIGGER_RATIO:
        return False
    # (3) Not enough history to reclaim anything meaningful.
    if history_chars(history) < max_chars * MIN_HISTORY_SHARE:
        return False
    return split_for_summary(history, max_chars) is not None


# --------------------------------------------------------------------------
# Splitting - atomic on user boundaries
# --------------------------------------------------------------------------

def split_for_summary(
    history: list[dict[str, Any]],
    max_chars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Split history into (to_summarize, to_keep).

    Walks backwards from the newest message accumulating characters until
    the keep budget is spent, then snaps the cut point to the nearest user
    message so a tool call never gets separated from its result.

    Returns None when there is nothing worth summarising (too few messages,
    or the cut lands at the very start).
    """
    if len(history) < MIN_MESSAGES_TO_SUMMARIZE:
        return None

    keep_budget = max(MIN_KEEP_CHARS, int(max_chars * KEEP_RATIO))

    # Walk backwards, accumulating the tail we intend to keep verbatim.
    used = 0
    cut = len(history)
    for i in range(len(history) - 1, -1, -1):
        used += _content_chars(history[i])
        if used > keep_budget:
            cut = i + 1
            break
    else:
        # The whole history fits the keep budget - nothing to compress.
        return None

    # Snap forward to the next user message so we never cut mid-exchange.
    while cut < len(history) and history[cut].get("role") != "user":
        cut += 1

    if cut >= len(history):
        # No user message after the cut point; snap backwards instead.
        cut = len(history) - 1
        while cut > 0 and history[cut].get("role") != "user":
            cut -= 1

    if cut <= 0:
        return None

    to_summarize = history[:cut]
    to_keep = history[cut:]

    if len(to_summarize) < MIN_MESSAGES_TO_SUMMARIZE:
        return None

    return to_summarize, to_keep


# --------------------------------------------------------------------------
# Slicing - what actually gets sent to the model
# --------------------------------------------------------------------------

def clean_summary(text: str) -> str:
    """Strip a wrapping markdown fence from a model-produced summary.

    Smaller models (ministral especially) like to wrap the whole reply in a
    ``` fence, which then gets stored, re-sent as context, and rendered as a
    code block in the transcript. The template is markdown headings, not code.
    """
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    # Drop the opening fence line (``` or ```markdown).
    nl = t.find("\n")
    if nl < 0:
        return t
    t = t[nl + 1:]
    # Drop a trailing fence if present.
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


def build_sliced_history(
    summary: str | None,
    messages_after_cutoff: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prepend the summary, then append everything after the cutoff.

    The summary rides as a single user message so every provider accepts it
    without a new role. The assistant ack keeps the turn alternation valid
    for providers that are strict about it.
    """
    def _clean(m: dict[str, Any]) -> dict[str, Any]:
        # `_id` is Sable-internal bookkeeping for the compaction cutoff. It must
        # never reach a provider: strict OpenAI-compatible endpoints reject
        # unknown message keys, and it would also be sent back as context.
        return {"role": m.get("role", "user"), "content": m.get("content", "")}

    tail = [_clean(m) for m in messages_after_cutoff]
    if not summary:
        return tail
    return [
        {"role": "user", "content": SUMMARY_PREFIX + summary},
        {"role": "assistant", "content": "Understood, I have the earlier context."},
        *tail,
    ]