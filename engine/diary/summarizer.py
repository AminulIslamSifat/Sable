#!/usr/bin/env python3
"""Summarize one GhostChat session markdown log for diary synthesis (stdout)."""

from __future__ import annotations

import argparse
import os
import sys

from gemini_helpers import DIARY_MODEL, generate_with_key_rotation, load_gemini_config

MAX_CHARS = 120_000


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize a GhostChat session .md file via Gemini API.")
    parser.add_argument("session_path", help="Path to a session .md (e.g. name-hh-mm-ss.md)")
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Richer summary (used for the chronologically last session in a batch)",
    )
    args = parser.parse_args()

    path = os.path.abspath(os.path.expanduser(args.session_path))
    if not os.path.isfile(path):
        print(f"Not a file: {path}", file=sys.stderr)
        return 1

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError as e:
        print(str(e), file=sys.stderr)
        return 1

    if len(raw) > MAX_CHARS:
        raw = "[... earlier log truncated ...]\n\n" + raw[-MAX_CHARS:]

    if args.detailed:
        instructions = (
            "Produce a detailed narrative summary of this GhostChat session log. "
            "Cover: main topics, technical work (code, files, commands), decisions, "
            "open questions or todos, and tone. Write in clear Markdown with short sections."
        )
        temp = 0.55
    else:
        instructions = (
            "Produce a compact bullet-point summary of this GhostChat session log. "
            "Focus on topics, concrete technical actions, and any explicit todos or follow-ups."
        )
        temp = 0.35

    prompt = f"{instructions}\n\n--- SESSION FILE: {os.path.basename(path)} ---\n\n{raw}"
    config = load_gemini_config()
    try:
        out = generate_with_key_rotation(config, prompt, temperature=temp, model=DIARY_MODEL)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
