#!/usr/bin/env python3
"""Merge per-session summaries (stdin) into one diary Markdown entry (stdout)."""

from __future__ import annotations

import sys

from gemini_helpers import DIARY_MODEL, generate_with_key_rotation, load_gemini_config

# Synthesizer input can grow large when many sessions exist.
MAX_INPUT_CHARS = 900_000


def main() -> int:
    try:
        blob = sys.stdin.read()
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    blob = blob.strip()
    if not blob:
        print("No input on stdin.", file=sys.stderr)
        return 1

    if len(blob) > MAX_INPUT_CHARS:
        blob = (
            "[... earliest session summaries truncated to fit context ...]\n\n"
            + blob[-MAX_INPUT_CHARS:]
        )

    prompt = (
        "You are writing one diary entry for the assistant persona (Maria) reflecting on time spent "
        "with the user in GhostChat.\n\n"
        "Below are summaries of individual session logs in chronological order. They may span "
        "multiple calendar days.\n\n"
        "Merge them into a single cohesive Markdown document suitable for a personal diary vault. "
        "Use this structure:\n"
        "## Arc & snapshot\n"
        "## Highlights\n"
        "## Technical / work\n"
        "## Threads & next steps\n"
        "## Closing note\n\n"
        "Be specific where the summaries are specific; do not invent facts not supported by the text.\n\n"
        "--- SESSION SUMMARIES (chronological) ---\n\n"
        f"{blob}"
    )

    config = load_gemini_config()
    try:
        out = generate_with_key_rotation(config, prompt, temperature=0.65, model=DIARY_MODEL)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
