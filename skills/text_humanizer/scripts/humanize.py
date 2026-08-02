
#!/usr/bin/env python3
"""
Text Humanizer — Rule-based post-processing pass.
Kills AI discourse markers, enforces burstiness, breaks paragraph symmetry.

This script is the STRUCTURAL pass only. The LLM rewrite is done by the
agent itself (no external API). Run this after the agent rewrites text.

Usage:
  echo "text" | python3 humanize.py
  python3 humanize.py --file input.txt --output output.txt
"""

import argparse
import random
import re
import sys
from pathlib import Path

# ─── AI Discourse Markers → Human Replacements ──────────────────────────────

DISCOURSE_REPLACEMENTS: list[tuple[str, list[str]]] = [
    (r"\bFurthermore\b", ["Also", "And", "On top of that", "Plus"]),
    (r"\bMoreover\b", ["And", "Besides", "What's more"]),
    (r"\bAdditionally\b", ["Also", "And", "Oh, and"]),
    (r"\bNotably\b", ["Interestingly", "Worth mentioning", "Here's the thing"]),
    (r"\bIn conclusion\b", ["So", "Bottom line", "Look"]),
    (r"\bIt is important to note that\b", ["Worth noting:", "Thing is,"]),
    (r"\bIt's important to note that\b", ["Worth noting:", "Thing is,"]),
    (r"\bConsequently\b", ["So", "Which means", "Because of that"]),
    (r"\bNevertheless\b", ["Still", "Even so", "But"]),
    (r"\bNonetheless\b", ["Still", "Even so", "But"]),
    (r"\bSubsequently\b", ["Then", "After that", "Next"]),
    (r"\bIn summary\b", ["So basically", "TL;DR", "Long story short"]),
    (r"\bTo summarize\b", ["So basically", "In short"]),
    (r"\bIt should be noted that\b", ["Note:", "FYI,"]),
    (r"\bAs previously mentioned\b", ["Like I said", "As noted"]),
    (r"\bIn order to\b", ["To", "For"]),
    (r"\bDue to the fact that\b", ["Because", "Since"]),
    (r"\bAt this point in time\b", ["Now", "Right now"]),
    (r"\bIn the event that\b", ["If", "When"]),
    (r"\bWith regard to\b", ["About", "Regarding", "On"]),
    (r"\bWith respect to\b", ["About", "On"]),
    (r"\bA plethora of\b", ["Tons of", "A bunch of", "Lots of"]),
    (r"\bA myriad of\b", ["Tons of", "A bunch of", "Lots of"]),
    (r"\bDelve\w* into\b", ["Dig into", "Look at", "Get into"]),
    (r"\bLeverag\w*\b", ["Use", "Tap into", "Work with"]),
    (r"\bFacilitat\w*\b", ["Help", "Enable", "Make easier"]),
    (r"\bUtiliz\w*\b", ["Use"]),
    (r"\bCommenc\w*\b", ["Start", "Begin", "Kick off"]),
    (r"\bTerminat\w*\b", ["End", "Stop", "Wrap up"]),
    (r"\bAscertain\w*\b", ["Figure out", "Determine", "Find out"]),
    (r"\bComprehensive\b", ["Thorough", "Full", "Complete"]),
    (r"\bMultifaceted\b", ["Complex", "Layered", "Multi-part"]),
    (r"\bNuanced\b", ["Subtle", "Tricky", "Not black-and-white"]),
    (r"\bCrucial\b", ["Key", "Critical", "Big"]),
    (r"\bLandscape\b", ["Space", "World", "Scene"]),
    (r"\bSeamless\b", ["Smooth", "Clean", "Frictionless"]),
    (r"\bRobust\b", ["Solid", "Sturdy", "Reliable"]),
    (r"\bScalable\b", ["Expandable", "Growable"]),
    (r"\bCutting-edge\b", ["Modern", "New", "Current"]),
    (r"\bState-of-the-art\b", ["Top-tier", "Best available", "Leading"]),
]

# ─── Sentence Splitting ─────────────────────────────────────────────────────


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, preserving abbreviations."""
    protected = text
    abbrevs = ["e.g.", "i.e.", "etc.", "Dr.", "Mr.", "Mrs.", "Ms.", "vs.", "approx."]
    placeholders: dict[str, str] = {}
    for i, abbr in enumerate(abbrevs):
        ph = f"__ABBR{i}__"
        placeholders[ph] = abbr
        protected = protected.replace(abbr, ph)

    parts = re.split(r'(?<=[.!?])\s+', protected)

    result = []
    for part in parts:
        for ph, abbr in placeholders.items():
            part = part.replace(ph, abbr)
        if part.strip():
            result.append(part.strip())
    return result


# ─── Burstiness Engineering ─────────────────────────────────────────────────


def _find_split_point(sentence: str) -> int:
    """Find a natural split point (semicolon, comma, conjunction)."""
    idx = sentence.find(";")
    if 10 < idx < len(sentence) - 10:
        return idx + 1

    third = len(sentence) // 3
    for match in re.finditer(r",\s", sentence):
        if third < match.start() < len(sentence) - third:
            return match.start()

    for conj in [" and ", " but ", " which ", " because ", " while "]:
        idx = sentence.find(conj, third)
        if 0 < idx < len(sentence) - third:
            return idx

    return -1


def enforce_burstiness(sentences: list[str]) -> list[str]:
    """
    3-1-5 pattern: after 3 regular sentences, insert one short burst,
    then continue. Splits long sentences at natural break points.
    """
    if len(sentences) < 5:
        return sentences

    result: list[str] = []
    i = 0

    while i < len(sentences):
        batch = sentences[i:i + 3]
        result.extend(batch)
        i += 3

        if i >= len(sentences):
            break

        candidate = sentences[i]
        words = candidate.split()
        if len(words) > 12:
            split_point = _find_split_point(candidate)
            if split_point > 0:
                short_part = candidate[:split_point].strip().rstrip(",;")
                long_part = candidate[split_point:].strip().lstrip(",; ")
                if len(short_part.split()) >= 3:
                    result.append(short_part + ".")
                    if long_part:
                        result.append(long_part)
                    i += 1
                    continue
        result.append(candidate)
        i += 1

    return result


# ─── Paragraph Symmetry Breaking ────────────────────────────────────────────


def vary_paragraph_lengths(text: str) -> str:
    """Break suspiciously uniform paragraphs."""
    paragraphs = text.split("\n\n")
    if len(paragraphs) < 3:
        return text

    lengths = [len(p.split()) for p in paragraphs if p.strip()]
    if not lengths:
        return text

    avg = sum(lengths) / len(lengths)
    if all(abs(l - avg) / max(avg, 1) < 0.2 for l in lengths):
        longest_idx = max(range(len(paragraphs)), key=lambda i: len(paragraphs[i].split()))
        longest = paragraphs[longest_idx]
        sentences = split_sentences(longest)
        if len(sentences) > 4:
            mid = len(sentences) // 2
            paragraphs[longest_idx] = " ".join(sentences[:mid])
            paragraphs.insert(longest_idx + 1, " ".join(sentences[mid:]))

    return "\n\n".join(paragraphs)


# ─── Controlled Imperfections ───────────────────────────────────────────────


def add_controlled_imperfections(text: str) -> str:
    """Add subtle human touches without changing meaning."""
    sentences = split_sentences(text)
    if len(sentences) < 4:
        return text

    candidates = [i for i in range(1, len(sentences)) if len(sentences[i].split()) > 8]
    if candidates and random.random() < 0.6:
        idx = random.choice(candidates)
        s = sentences[idx]
        if not s.startswith(("And ", "But ")):
            if re.match(r"^(This|That|The|It|These|Those)\b", s):
                prefix = random.choice(["And ", "But "])
                sentences[idx] = prefix + s[0].lower() + s[1:]

    return " ".join(sentences)


# ─── Main Pipeline ──────────────────────────────────────────────────────────


def replace_discourse_markers(text: str) -> str:
    """Replace AI-typical discourse markers with human alternatives."""
    for pattern, replacements in DISCOURSE_REPLACEMENTS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            replacement = random.choice(replacements)
            original = match.group(0)
            if original[0].islower():
                replacement = replacement[0].lower() + replacement[1:]
            text = text[:match.start()] + replacement + text[match.end():]
    return text


def humanize(text: str) -> str:
    """Full rule-based humanization pipeline."""
    # Pass 1: Kill AI discourse markers
    text = replace_discourse_markers(text)

    # Pass 2: Enforce burstiness per paragraph
    paragraphs = text.split("\n\n")
    processed = []
    for para in paragraphs:
        if not para.strip():
            processed.append(para)
            continue
        sentences = split_sentences(para)
        sentences = enforce_burstiness(sentences)
        processed.append(" ".join(sentences))
    text = "\n\n".join(processed)

    # Pass 3: Break paragraph symmetry
    text = vary_paragraph_lengths(text)

    # Pass 4: Controlled imperfections
    text = add_controlled_imperfections(text)

    # Pass 5: Final sweep (catch anything introduced by earlier passes)
    text = replace_discourse_markers(text)

    return text


# ─── CLI ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rule-based AI text humanizer (structural post-processing)"
    )
    parser.add_argument("--text", type=str, help="Input text")
    parser.add_argument("--file", type=str, help="Read from file")
    parser.add_argument("--output", type=str, help="Write to file (default: stdout)")
    args = parser.parse_args()

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"ERROR: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        text = path.read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)

    if not text.strip():
        print("ERROR: Empty input.", file=sys.stderr)
        sys.exit(1)

    result = humanize(text)

    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
        print(f"✓ Written to {args.output}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()
