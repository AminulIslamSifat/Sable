#!/usr/bin/env python3
"""
Text Humanizer — structural post-processing pass (v2).

This script is the RULE pass only. The LLM rewrite is done by the caller
(the agent, or /api/study/humanize). Run this after the rewrite.

What it targets (from the 2025-2026 detector literature):
  * perplexity   — predictable word choice; fixed by high-surprisal swaps
  * burstiness   — sentence-length variation; fixed by variance engineering
  * n-gram reps  — overused phrases ("in order to", "the fact that")
  * discourse    — furthermore/moreover/in conclusion and friends
  * AI tells     — em-dash overuse, tricolons, "not only ... but also"

Usage:
  echo "text" | python3 humanize.py
  python3 humanize.py --file input.txt --output output.txt
  python3 humanize.py --file input.txt --stats
  python3 humanize.py --file in.txt --seed 42 --strength 0.8
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
from pathlib import Path

# ─── AI Discourse Markers → Human Replacements ──────────────────────────────
# Order matters: longer phrases first so "in order to" isn't half-matched.

DISCOURSE_REPLACEMENTS: list[tuple[str, list[str]]] = [
    (r"\bIt is important to note that\b", ["Worth noting:", "Thing is,", "Keep in mind:"]),
    (r"\bIt's important to note that\b", ["Worth noting:", "Thing is,", "Keep in mind:"]),
    (r"\bIt should be noted that\b", ["Note:", "FYI,", "Heads up:"]),
    (r"\bAs previously mentioned\b", ["Like I said", "As noted", "Earlier"]),
    (r"\bDue to the fact that\b", ["Because", "Since", "As"]),
    (r"\bIn the event that\b", ["If", "When", "Should"]),
    (r"\bAt this point in time\b", ["Now", "Right now", "Currently"]),
    (r"\bIn order to\b", ["To", "For"]),
    (r"\bA plethora of\b", ["Tons of", "A bunch of", "Lots of"]),
    (r"\bA myriad of\b", ["Tons of", "A bunch of", "Lots of"]),
    (r"\bWith regard to\b", ["About", "Regarding", "On"]),
    (r"\bWith respect to\b", ["About", "On", "For"]),
    (r"\bIn conclusion\b", ["So", "Bottom line", "Look"]),
    (r"\bIn summary\b", ["So basically", "TL;DR", "Long story short"]),
    (r"\bTo summarize\b", ["So basically", "In short", "Short version:"]),
    (r"\bFurthermore\b", ["Also", "And", "On top of that", "Plus"]),
    (r"\bMoreover\b", ["And", "Besides", "What's more"]),
    (r"\bAdditionally\b", ["Also", "And", "Oh, and"]),
    (r"\bNotably\b", ["Interestingly", "Worth mentioning", "Here's the thing"]),
    (r"\bConsequently\b", ["So", "Which means", "Because of that"]),
    (r"\bNevertheless\b", ["Still", "Even so", "But"]),
    (r"\bNonetheless\b", ["Still", "Even so", "But"]),
    (r"\bSubsequently\b", ["Then", "After that", "Next"]),
]

# ─── Perplexity substitutions: predictable → surprising ─────────────────────
# These raise the model's "surprise" at each token. Chosen to stay natural.

PERPLEXITY_SWAPS: list[tuple[str, list[str]]] = [
    (r"\bDelve\w* into\b", ["Dig into", "Poke at", "Get into"]),
    (r"\bDelve\w*\b", ["Dig", "Look"]),
    (r"\bLeverag\w*\b", ["Use", "Tap into", "Work with"]),
    (r"\bFacilitat\w*\b", ["Help with", "Enable", "Smooth the way for"]),
    (r"\bUtiliz\w*\b", ["Use", "Put to work"]),
    (r"\bCommenc\w*\b", ["Start", "Begin", "Kick off"]),
    (r"\bTerminat\w*\b", ["End", "Stop", "Wrap up"]),
    (r"\bAscertain\w*\b", ["Figure out", "Pin down", "Find out"]),
    (r"\bComprehensive\b", ["Thorough", "Full", "Complete"]),
    (r"\bMultifaceted\b", ["Complex", "Layered", "Many-sided"]),
    (r"\bNuanced\b", ["Subtle", "Tricky", "Not black-and-white"]),
    (r"\bCrucial\b", ["Key", "Vital", "Big"]),
    (r"\bLandscape\b", ["Space", "World", "Scene"]),
    (r"\bSeamless\w*\b", ["Smooth", "Clean", "Frictionless"]),
    (r"\bRobust\b", ["Solid", "Sturdy", "Reliable"]),
    (r"\bScalable\b", ["Expandable", "Growable"]),
    (r"\bCutting-edge\b", ["Modern", "New", "Current"]),
    (r"\bState-of-the-art\b", ["Top-tier", "Best available", "Leading"]),
    (r"\bTapestry\b", ["Mix", "Weave", "Blend"]),
    (r"\bTestament to\b", ["Proof of", "Sign of", "Shows"]),
    (r"\bIn the realm of\b", ["In", "Within"]),
    (r"\bIt is worth noting that\b", ["Note that", "Worth saying:"]),
]

# ─── n-gram repetition: collapse wordy stock phrases ────────────────────────

NGRAM_COLLAPSE: list[tuple[str, str]] = [
    # "<marker>, the fact that X" → "<marker> X". The marker set includes the
    # discourse words that get substituted INTO conjunctions (Moreover,
    # Furthermore, ...) because this rule may run on either side of that pass.
    (r"\b(And|But|So|Or|Also|Plus|Moreover|Furthermore|Additionally|Besides),\s+the fact that\s+", r"\1 "),
    # "And, that X" (a stranded collapse from an earlier pass) → "And X".
    (r"\b(And|But|So|Or|Also|Plus),\s+that\s+", r"\1 "),
    # "<prep> the fact that X" → "<prep> that X". Only collapse when the phrase
    # is in a complement position (after a preposition) — a sentence-initial
    # "The fact that X is Y" MUST keep its subject, or stripping it leaves
    # "X is Y" with a dangling verb ("tools scale is crucial").
    # Keep "that": "because of that the tests passed" is still wrong, but
    # "because the tests passed" is what we want — so drop BOTH preposition
    # and phrase, keeping the bare clause.
    (r"\b(?:because of|due to|in light of)\s+the fact that\s+", "because "),
    (r"\b(?:despite|in spite of)\s+the fact that\s+", "although "),
    # Bare "the fact that" mid-sentence, NOT followed by a verb-less clause
    # ending in is/was/were. Handled conservatively: only drop when NOT the
    # subject of a following be-verb.
    (r"\bthe fact that\s+(?![^.?!]*\b(?:is|was|were|are)\b)", ""),
    (r"\bin spite of the fact that\b", "although"),
    (r"\bdespite the fact that\b", "although"),
    (r"\bin the process of\b", ""),
    (r"\bfor the purpose of\b", "to"),
    (r"\bin the near future\b", "soon"),
    (r"\ba large number of\b", "many"),
    (r"\ba small number of\b", "a few"),
    (r"\bthe majority of\b", "most"),
    (r"\bin the majority of cases\b", "usually"),
    (r"\btake into consideration\b", "consider"),
    (r"\bmake a decision\b", "decide"),
    (r"\bhas the ability to\b", "can"),
    # Tense-preserving: flattening was/were → can silently rewrites the
    # meaning ("they were able to finish" is past, "they can finish" is not).
    (r"\b(?:is|are) able to\b", "can"),
    (r"\b(?:was|were) able to\b", "could"),
    (r"\bmeans that teams are able to\b", "lets teams"),
    (r"\bprior to\b", "before"),
    (r"\bsubsequent to\b", "after"),
]

# ─── Sentence Splitting ─────────────────────────────────────────────────────

_ABBREVS = ["e.g.", "i.e.", "etc.", "Dr.", "Mr.", "Mrs.", "Ms.", "vs.", "approx.", "Fig.", "No."]


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, protecting common abbreviations."""
    protected = text
    placeholders: dict[str, str] = {}
    for i, abbr in enumerate(_ABBREVS):
        ph = f"\x00A{i}\x00"
        placeholders[ph] = abbr
        protected = protected.replace(abbr, ph)

    parts = re.split(r"(?<=[.!?])\s+", protected)
    result: list[str] = []
    for part in parts:
        for ph, abbr in placeholders.items():
            part = part.replace(ph, abbr)
        if part.strip():
            result.append(part.strip())
    return result


def _word_count(s: str) -> int:
    return len(s.split())


# ─── Burstiness Engineering ─────────────────────────────────────────────────


def burstiness_score(sentences: list[str]) -> float:
    """Coefficient of variation of sentence lengths (stdev / mean).

    Human prose typically scores > 0.5; flat model output sits near 0.2-0.3.
    """
    lengths = [_word_count(s) for s in sentences if _word_count(s) > 0]
    if len(lengths) < 2:
        return 0.0
    mean = statistics.fmean(lengths)
    if mean <= 0:
        return 0.0
    return statistics.pstdev(lengths) / mean


def _find_split_point(sentence: str) -> int:
    """Find a natural split point (semicolon, comma, conjunction)."""
    idx = sentence.find(";")
    if 10 < idx < len(sentence) - 10:
        return idx + 1

    third = max(len(sentence) // 3, 8)
    for m in re.finditer(r",\s", sentence):
        if third < m.start() < len(sentence) - third:
            return m.start()

    # Conjunctions are risky: "tools are sturdy and expandable" would split into
    # the ungrammatical "tools are sturdy. and expandable...". Only split when
    # the conjunction is followed by a fresh subject (pronoun/determiner), which
    # is what makes a real clause boundary.
    for conj in (" but ", " and "):
        for m in re.finditer(re.escape(conj), sentence):
            idx = m.start()
            if not (third < idx < len(sentence) - third):
                continue
            after = sentence[idx + len(conj):]
            if re.match(r"(?:I|you|he|she|it|we|they|this|that|these|those|the|a|an)\b", after, re.IGNORECASE):
                return idx
    return -1


def _break_runons(sentences: list[str], max_words: int = 35) -> list[str]:
    """Split any sentence longer than ``max_words`` at a natural comma.

    This runs INDEPENDENTLY of burstiness: the score can be fooled by a short
    fragment next to a huge run-on, so length is checked on its own. Repeated
    until every resulting sentence is under the limit.
    """
    out: list[str] = []
    for s in sentences:
        out.extend(_split_one_runon(s, max_words))
    return out


def _split_one_runon(sentence: str, max_words: int) -> list[str]:
    """Recursively split ``sentence`` at commas until it fits under max_words."""
    if _word_count(sentence) <= max_words:
        return [sentence]

    # Find a comma near the middle to break on.
    commas = [m.start() for m in re.finditer(r",\s", sentence)]
    if not commas:
        return [sentence]  # nothing to split on — leave it
    mid = len(sentence) / 2
    split_at = min(commas, key=lambda c: abs(c - mid))

    head = sentence[:split_at].strip().rstrip(",")
    tail = sentence[split_at:].strip().lstrip(", ")
    if not head or not tail:
        return [sentence]
    head = head[0].upper() + head[1:]
    if len(tail) > 1 and tail[1:2].islower():
        tail = tail[0].lower() + tail[1:]

    return _split_one_runon(head + ".", max_words) + _split_one_runon(tail, max_words)


def enforce_burstiness(
    sentences: list[str],
    rng: random.Random,
    strength: float = 0.6,
    target: float = 0.55,
) -> list[str]:
    """Split long sentences until variance clears the target.

    Unlike a fixed 3-1-5 pattern, this measures the actual spread and only
    intervenes while the text is still too uniform. ``strength`` scales how
    aggressively it splits (0 = off, 1 = until target met or nothing splits).
    """
    if len(sentences) < 3 or strength <= 0:
        return sentences

    result = list(sentences)

    # Hard length guard FIRST: any sentence over ~35 words is a run-on no
    # human writes, and it must be split even if the burstiness score looks
    # "good". A 1-word fragment next to a 60-word monster yields a high
    # coefficient of variation while reading as clearly machine-broken.
    result = _break_runons(result, max_words=35)

    # Only touch text that is actually flat. If it already clears the target,
    # any surgery is more likely to flatten it than help (splitting a long
    # sentence into two mediums LOWERS the coefficient of variation).
    if burstiness_score(result) >= target:
        return result

    # Try every mutation that could raise variance — split long sentences AND
    # merge adjacent short ones — and keep only the ones that actually help.
    # The guard is what makes this safe: an operation that lowers the score is
    # discarded, so the pass can never make the text worse than it started.
    for _ in range(max(2, int(round(strength * 8)))):
        if burstiness_score(result) >= target:
            break
        before_score = burstiness_score(result)
        best = None
        best_score = before_score

        split = list(result)
        if _split_longest(split, strength):
            s = burstiness_score(split)
            if s > best_score:
                best, best_score = split, s

        merge = list(result)
        if _merge_shortest_adjacent(merge, strength):
            s = burstiness_score(merge)
            if s > best_score:
                best, best_score = merge, s

        if best is None:
            break  # no operation helps
        result = best

    return result


def _split_longest(result: list[str], strength: float) -> bool:
    """Split the longest splittable sentence. Mutates ``result``. Returns True
    if a split happened."""
    lengths = [_word_count(s) for s in result]
    for i in sorted(range(len(result)), key=lambda k: lengths[k], reverse=True):
        if _word_count(result[i]) < 11:
            continue
        # Never split a markdown structure line (list item, heading, table row,
        # blockquote) -- that breaks the document.
        if re.match(r"^\s*(?:[-*+]|#|>|\d+\.)\s", result[i]) or "|" in result[i]:
            continue
        sp = _find_split_point(result[i])
        if sp <= 0:
            continue
        head = result[i][:sp].strip().rstrip(",;")
        tail = result[i][sp:].strip().lstrip(",; ")
        if _word_count(head) >= 3 and _word_count(tail) >= 3:
            head = head[0].upper() + head[1:] if head else head
            # Preserve an acronym/proper-noun capital at the tail too.
            if len(tail) > 1 and tail[1].islower():
                tail = tail[0].lower() + tail[1:]
            result[i:i + 1] = [head + ".", tail]
            return True
    return False


def _merge_shortest_adjacent(result: list[str], strength: float) -> bool:
    """Merge the two shortest adjacent sentences, if both are short. Returns
    True if a merge happened."""
    if len(result) < 3:
        return False
    pairs = [
        (i, _word_count(result[i]) + _word_count(result[i + 1]))
        for i in range(len(result) - 1)
    ]
    # Only merge when BOTH sentences are short — merging a short into a long
    # just produces another medium sentence and flattens the distribution.
    # Skip any pair touching markdown structure (list items, headings, tables,
    # blockquotes, bold) — joining those corrupts the document layout.
    _md = re.compile(r"(^\s*(?:[-*+]|#|>|\d+\.)\s)|(\|)|(\*\*)|(\[\[)")
    pairs = [p for p in pairs
             if _word_count(result[p[0]]) <= 12 and _word_count(result[p[0] + 1]) <= 12
             and not _md.search(result[p[0]]) and not _md.search(result[p[0] + 1])]
    if not pairs:
        return False
    i, _ = min(pairs, key=lambda p: p[1])
    a = result[i].rstrip(".!?")
    b = result[i + 1]
    # Don't inject a conjunction if the second sentence already opens with one
    # (or with a discourse marker) — that produced "and look, and organizations".
    starts_with_conn = bool(re.match(
        r"(?:and|but|so|or|yet|also|plus|look|note|still|then)\b", b, re.IGNORECASE
    ))
    conn = "" if starts_with_conn else "and "
    # Only lowercase when the second char is also lowercase, so "API calls"
    # doesn't become "and aPI calls".
    b_lc = b[0].lower() + b[1:] if len(b) > 1 and b[1].islower() else b
    merged = f"{a}, {conn}{b_lc}"
    merged = merged.replace(",  ", ", ").replace(", ,", ",")
    if not merged.endswith((".", "!", "?")):
        merged += "."
    result[i:i + 2] = [merged]
    return True


def inject_short_bursts(sentences: list[str], rng: random.Random, rate: float = 0.12) -> list[str]:
    """Occasionally demote a clause to its own very short sentence.

    This is the classic human rhythm: a long explanation, then three words.
    """
    out: list[str] = []
    for s in sentences:
        words = _word_count(s)
        if words >= 9 and rng.random() < rate:
            for m in re.finditer(r"\s(but|and|so|yet|because|while|which)\s", s, re.IGNORECASE):
                head = s[:m.start()].strip().rstrip(",;")
                tail = s[m.end():].strip()
                # Same safety rule as _find_split_point: the tail must start a
                # new clause, not continue the current subject.
                if _word_count(head) < 4 or _word_count(tail) < 3:
                    continue
                if not re.match(
                    r"(?:I|you|he|she|it|we|they|this|that|these|those|the|a|an)\b",
                    tail, re.IGNORECASE,
                ):
                    continue
                conn = m.group(1)
                out.append(head + ".")
                out.append(conn.capitalize() + " " + tail)
                break
            else:
                out.append(s)
            continue
        out.append(s)
    return out


# ─── Paragraph Symmetry Breaking ────────────────────────────────────────────


def vary_paragraph_lengths(text: str) -> str:
    """Break suspiciously uniform paragraphs."""
    paragraphs = text.split("\n\n")
    if len(paragraphs) < 3:
        return text

    lengths = [_word_count(p) for p in paragraphs if p.strip()]
    if not lengths:
        return text

    avg = statistics.fmean(lengths)
    if avg > 0 and all(abs(l - avg) / avg < 0.2 for l in lengths):
        longest_idx = max(range(len(paragraphs)), key=lambda i: _word_count(paragraphs[i]))
        longest = paragraphs[longest_idx]
        sentences = split_sentences(longest)
        if len(sentences) > 4:
            mid = len(sentences) // 2
            paragraphs[longest_idx] = " ".join(sentences[:mid])
            paragraphs.insert(longest_idx + 1, " ".join(sentences[mid:]))

    return "\n\n".join(paragraphs)


# ─── AI Tells ───────────────────────────────────────────────────────────────


def kill_ai_tells(text: str, rng: random.Random, strength: float = 0.6) -> str:
    """Remove/soften structural AI fingerprints.

      * "not only X but also Y"  →  "X, and Y too"
      * em-dash overuse          →  commas
    """
    text = re.sub(
        r"\bnot only\s+(.+?)\s+but also\s+(.+?)(?=[.,;!?]|$)",
        lambda m: f"{m.group(1)}, and {m.group(2)} too",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if strength > 0:
        paras = text.split("\n\n")
        rebuilt: list[str] = []
        for p in paras:
            if p.count("—") > 1:
                seen = 0

                def _swap(m: re.Match) -> str:
                    nonlocal seen
                    seen += 1
                    # Keep the first em-dash (a single one reads human);
                    # replace the rest with varied punctuation so the
                    # paragraph doesn't turn into a comma dump. The match
                    # includes any surrounding spaces (" — ") so the
                    # replacement doesn't leave a space before the comma.
                    if seen == 1:
                        return m.group(0)
                    # Punctuation with a single trailing space; the leading
                    # space was consumed by the match, so we don't emit one.
                    return rng.choice([",", ";", " -"]).strip() + " "

                # Consume the " — " span (with its spaces) as a unit, then
                # normalize any doubled spaces the replacement introduced.
                p = re.sub(r"\s*—\s*", _swap, p)
                p = re.sub(r" {2,}", " ", p)
            rebuilt.append(p)
        text = "\n\n".join(rebuilt)

    return text


# ─── Controlled Imperfections ───────────────────────────────────────────────


def add_controlled_imperfections(text: str, rng: random.Random) -> str:
    """Add subtle human touches without changing meaning."""
    sentences = split_sentences(text)
    if len(sentences) < 4:
        return text

    candidates = [i for i in range(1, len(sentences)) if _word_count(sentences[i]) > 8]
    if candidates and rng.random() < 0.6:
        idx = rng.choice(candidates)
        s = sentences[idx]
        if not s.startswith(("And ", "But ")) and re.match(r"^(This|That|The|It|These|Those)\b", s):
            prefix = rng.choice(["And ", "But "])
            # Only lowercase when the second char is also lowercase — otherwise
            # "API calls…" would become "And aPI calls…". Acronyms and proper
            # nouns keep their capital.
            if len(s) > 1 and s[1].islower():
                s = s[0].lower() + s[1:]
            sentences[idx] = prefix + s

    return " ".join(sentences)


# ─── Human Typos (the "chaos" pass) ─────────────────────────────────────────
#
# Real humans make small, inconsistent mistakes. LLMs almost never do — the
# error rate is one of the strongest signals a detector uses. This pass injects
# a small, seeded number of REALISTIC typos (not keyboard-mash): dropped
# apostrophes, doubled letters, transposed letters in common words, and the
# occasional doubled word. It is deliberately conservative — one or two per
# piece, never enough to look careless.

# Common words where a transposition reads as a natural slip ("teh", "adn").
# Keyed lowercase; the first letter's case is preserved on output.
_TYPO_TRANSPOSE: dict[str, str] = {
    "the": "teh", "and": "adn", "that": "taht", "with": "wiht",
    "from": "form", "this": "tihs", "have": "hvae", "they": "tehy",
    "which": "whcih", "there": "ther", "their": "thier", "about": "abotu",
    "would": "woudl", "could": "cuold", "should": "shuold",
    "people": "poeple", "because": "becuase", "between": "bewteen",
    "another": "anohter", "different": "diffrent", "important": "importnat",
    "does": "deos", "just": "jsut", "like": "liek", "make": "mkae",
}

# Contractions that are commonly typed without the apostrophe.
_TYPO_APOSTROPHE: dict[str, str] = {
    "don't": "dont", "can't": "cant", "won't": "wont", "isn't": "isnt",
    "didn't": "didnt", "doesn't": "doesnt", "it's": "its", "that's": "thats",
    "there's": "theres", "they're": "theyre", "you're": "youre",
    "we're": "were", "i'm": "im", "let's": "lets", "wasn't": "wasnt",
    "aren't": "arent", "couldn't": "couldnt", "wouldn't": "wouldnt",
}


def add_human_typos(text: str, rng: random.Random, rate: float = 1.0) -> str:
    """Inject a small number of realistic, human-looking typos.

    ``rate`` (0..1) scales how many typos are introduced relative to the text
    length. The count is derived from the word count so a long piece gets a
    couple more than a short one — always proportionate, never a mess. A
    single typo per several hundred words is the human norm; we aim low.

    Typo kinds (chosen at random per hit):
      * dropped apostrophe  — don't → dont
      * letter transposition — the → teh
      * doubled letter       — and → annd
      * doubled word         — the the
    """
    if rate <= 0:
        return text

    words = text.split()
    if len(words) < 8:
        return text

    # Roughly one typo per ~50 words at full rate. The ceiling scales with the
    # text instead of being a flat number: at most one typo per 2.5 sentences,
    # so a long piece gets more slips but never turns into a minefield.
    sentences = max(1, len(split_sentences(text)))
    cap = max(1, int(sentences / 2.5))
    budget = max(1, min(cap, round(len(words) / 50 * rate)))

    # Work on the raw text via regex so punctuation/casing are preserved.
    def _drop_apostrophe(m: re.Match) -> str:
        return m.group(0).replace("'", "", 1) if "'" in m.group(0) else m.group(0)

    def _transpose(m: re.Match) -> str:
        w = m.group(0)
        low = w.lower()
        repl = _TYPO_TRANSPOSE.get(low)
        if not repl:
            return w
        return repl[0].upper() + repl[1:] if w[0].isupper() else repl

    def _double_letter(m: re.Match) -> str:
        w = m.group(0)
        # Double a letter in the middle of the word, never the first.
        if len(w) < 4:
            return w
        i = len(w) // 2
        return w[:i] + w[i - 1] + w[i:]

    def _double_word(m: re.Match) -> str:
        return f"{m.group(0)} {m.group(0)}"

    apos_pat = r"\b(?:%s)\b" % "|".join(re.escape(k) for k in _TYPO_APOSTROPHE)
    trans_pat = r"\b(?:%s)\b" % "|".join(re.escape(k) for k in _TYPO_TRANSPOSE)
    long_word_pat = r"\b[A-Za-z]{6,12}\b"
    common_word_pat = r"\b(the|and|to|of|a|in|is|it|that|this)\b"

    def _attempt(kind: str) -> str | None:
        """Try one typo of ``kind``; return the new text or None if no hit."""
        nonlocal text
        if kind == "apostrophe":
            new, n = re.subn(apos_pat, _drop_apostrophe, text, count=1, flags=re.IGNORECASE)
            return new if n else None
        if kind == "transpose":
            hits = list(re.finditer(trans_pat, text, re.IGNORECASE))
            if not hits:
                return None
            m = rng.choice(hits)
            return text[:m.start()] + _transpose(m) + text[m.end():]
        if kind == "double_letter":
            # Only long-ish words, so the doubling is subtle. Skip words that
            # already have a doubled letter (else "really" -> "reallly").
            hits = [m for m in re.finditer(long_word_pat, text)
                    if not re.search(r"(.)\1", m.group(0))]
            if not hits:
                return None
            m = rng.choice(hits)
            return text[:m.start()] + _double_letter(m) + text[m.end():]
        # double_word — rare, since it needs a natural repeat point.
        m = re.search(common_word_pat, text, re.IGNORECASE)
        if not m:
            return None
        # Randomly pick a later common word if there are several.
        all_hits = list(re.finditer(common_word_pat, text, re.IGNORECASE))
        if all_hits:
            m = rng.choice(all_hits)
        return text[:m.end()] + " " + text[m.start():m.end()] + text[m.end():]

    kinds = ["apostrophe", "transpose", "double_letter", "double_word"]
    for _ in range(budget):
        # Shuffle kinds so a piece with no contractions doesn't waste every
        # attempt on the apostrophe rule — retry until one actually lands.
        rng.shuffle(kinds)
        for kind in kinds:
            new = _attempt(kind)
            if new is not None and new != text:
                text = new
                break

    # Collapse any accidental triple spaces.
    text = re.sub(r" {3,}", "  ", text)
    return text


# ─── Grammar Chaos ──────────────────────────────────────────────────────────
#
# Real human writing is not just typo-free — it is GRAMMATICALLY loose. A
# detector's strongest "human" signal is grammatical imperfection: a real
# person drops articles, slips subject-verb agreement, splices commas, and
# forgets possessive apostrophes. LLM output almost never does any of this.
# This pass injects those mistakes, seeded and scaled by ``rate``.

# Irregular subject-verb disagreements that read as a natural slip. Only
# noun/verb pairs where the mismatch is plausible, never nonsense.
_SV_SLIPS: list[tuple[str, str]] = [
    (r"\b(they|we|you) are\b", r"\1 is"),
    (r"\b(they|we|you) were\b", r"\1 was"),
    (r"\b(they|we|you) have\b", r"\1 has"),
    (r"\b(they|we|you) do\b", r"\1 does"),
    (r"\b(they|we|you) don't\b", r"\1 doesn't"),
    (r"\b(they|we|you) go\b", r"\1 goes"),
    (r"\b(they|we|you) make\b", r"\1 makes"),
    (r"\b(they|we|you) need\b", r"\1 needs"),
    (r"\bthere are\b", "there is"),
    (r"\bthere were\b", "there was"),
    (r"\b(it|he|she) are\b", r"\1 is"),
    (r"\b(it|he|she) were\b", r"\1 was"),
]

# Singular nouns that commonly lose their article in fast typing. Matched only
# when preceded by "a"/"an"/"the" and a determiner is safe to drop.
_ARTICLE_DROP = re.compile(r"\b(?:a|an|the)\s+(?=[a-z])", re.IGNORECASE)

# Possessive words whose apostrophe is often skipped.
_POSSESSIVES = re.compile(r"\b([A-Za-z]{3,})'s\b")

# Contractions to EXPAND into their sloppier spoken form. Used sparingly —
# a formal-register chaos rewrite should NOT read as chatty.
_CONTRACT_SLOP: list[tuple[str, str]] = [
    (r"\bgoing to\b", "gonna"),
    (r"\bwant to\b", "wanna"),
    (r"\bbecause\b", "cause"),
]

# --- Formal-register mistakes from the gold-standard sample -----------------
# These are the slips a knowledgeable expert makes when they don't proofread.
# They read as human WITHOUT dropping the register into casual slop.

# Plural nouns that lose their -s ("input cost" where "costs" was meant).
# Only nouns that are commonly pluralised in this kind of writing.
_PLURAL_NOUNS = re.compile(
    r"\b(services|costs|providers|devices|towers|stations|generators|prices|"
    r"budgets|markets|carriers|factors|expenses|requirements|components)\b"
)

# "An" before a consonant sound is a classic hurried slip ("An shocking").
_WRONG_ARTICLE = re.compile(r"\bAn\s+(?=[bcdfgjklmnpqrstvwxyzBCDFGJKLMNPQRSTVWXYZ])")

# A conjunction/discourse word capitalised mid-sentence ("electricity, Which").
_CAP_MIDSENTENCE = re.compile(
    r"([a-z,;]\s+)(Which|That|This|It|Thus|But|And|Which|Where|When|However)\b"
)

# Possessive nouns that keep the -s but lose the apostrophe.
# (handled by _POSSESSIVES above)


def add_grammar_chaos(text: str, rng: random.Random, rate: float = 1.0) -> str:
    """Inject grammatical imperfections a hurried human would make.

    ``rate`` (0..1) scales how many slips are attempted, derived from the
    sentence count so longer pieces get proportionally more. The pass is
    deliberately gentle: a couple of slips read as human, ten read as broken.

    Kinds:
      * subject-verb disagreement — "they are" -> "they is"
      * dropped article          — "a massive grid" -> "massive grid"
      * missing possessive        — "executives head" (no apostrophe)
      * sloppy contraction        — "going to" -> "gonna"
      * lowercase sentence start  — "And that matters"
      * comma splice              — join two sentences with a comma
    """
    if rate <= 0:
        return text

    words = text.split()
    if len(words) < 12:
        return text

    sentences = split_sentences(text)
    cap = max(1, int(len(sentences) / 3))
    budget = max(1, min(cap, round(len(sentences) / 3 * rate)))

    def _apply_sv(text: str, rng: random.Random) -> str | None:
        """Apply one subject-verb slip using the pattern's OWN groups."""
        pairs = list(_SV_SLIPS)
        rng.shuffle(pairs)
        for pat, repl in pairs:
            hits = list(re.finditer(pat, text, re.IGNORECASE))
            if not hits:
                continue
            m = rng.choice(hits)
            # Expand against the pattern that actually matched (which owns \1).
            out = m.expand(repl)
            if out != m.group(0):
                return text[:m.start()] + out + text[m.end():]
        return None

    # Strong signals first (grammar), then weaker stylistic ones. Shuffled
    # within each tier so variety stays but strong mistakes get picked often.
    strong = ["sv", "article", "wrong_article", "plural", "possessive"]
    weak = ["cap_mid", "slop", "splice"]

    for _ in range(budget):
        rng.shuffle(strong)
        rng.shuffle(weak)
        for kind in strong + weak:
            before = text
            if kind == "sv":
                new = _apply_sv(text, rng)
                if new is not None:
                    text = new
            elif kind == "article":
                hits = list(_ARTICLE_DROP.finditer(text))
                # Skip when the next word is a proper noun / all-caps acronym.
                hits = [m for m in hits
                        if text[m.end():m.end() + 1].islower()]
                if hits:
                    m = rng.choice(hits)
                    text = text[:m.start()] + text[m.end():]
            elif kind == "wrong_article":
                # "An" before a consonant sound — drop or swap to "A".
                m = _WRONG_ARTICLE.search(text)
                if m:
                    text = text[:m.start()] + "A " + text[m.end():]
            elif kind == "plural":
                # Drop the trailing -s from a plural noun.
                hits = list(_PLURAL_NOUNS.finditer(text))
                if hits:
                    m = rng.choice(hits)
                    word = m.group(1)
                    text = text[:m.start()] + word[:-1] + text[m.end():]
            elif kind == "possessive":
                hits = list(_POSSESSIVES.finditer(text))
                if hits:
                    m = rng.choice(hits)
                    text = text[:m.start()] + m.group(1) + text[m.end():]
            elif kind == "cap_mid":
                # Capitalise a discourse word after a comma ("...oil, Which").
                m = _CAP_MIDSENTENCE.search(text)
                if m:
                    text = text[:m.start(2)] + m.group(2) + text[m.end(2):]
            elif kind == "slop":
                hits = []
                for pat, _ in _CONTRACT_SLOP:
                    hits += list(re.finditer(pat, text, re.IGNORECASE))
                if hits:
                    m = rng.choice(hits)
                    low = m.group(0).lower()
                    for pat, repl in _CONTRACT_SLOP:
                        if low == pat.replace(r"\b", ""):
                            text = text[:m.start()] + repl + text[m.end():]
                            break
            elif kind == "lower":
                # Lowercase the first letter of a mid-text sentence that
                # starts with a common word (not a proper noun).
                sents = split_sentences(text)
                cands = [i for i, s in enumerate(sents)
                         if i > 0 and s[:1].isupper()
                         and s[1:2].islower()
                         and not s.startswith("I ")]
                if cands:
                    i = rng.choice(cands)
                    s = sents[i]
                    sents[i] = s[0].lower() + s[1:]
                    text = " ".join(sents)
            else:  # splice
                sents = split_sentences(text)
                cands = [i for i in range(len(sents) - 1)
                         if sents[i].rstrip().endswith(".")
                         and len(sents[i].split()) >= 3
                         and len(sents[i + 1].split()) >= 3]
                if cands:
                    i = rng.choice(cands)
                    a = sents[i].rstrip().rstrip(".")
                    b = sents[i + 1]
                    b = b[0].lower() + b[1:] if len(b) > 1 and b[1:2].islower() else b
                    sents[i:i + 2] = [a + ", " + b]
                    text = " ".join(sents)
            if text != before:
                break

    # Tidy up spacing the article drop may have left.
    text = re.sub(r" {2,}", " ", text)
    return text


# ─── Marker passes ──────────────────────────────────────────────────────────


def _apply_swaps(text: str, table: list[tuple[str, list[str]]], rng: random.Random) -> tuple[str, int]:
    """Replace EVERY occurrence of each pattern (not just the first)."""
    count = 0
    lookup = {pat: reps for pat, reps in table}

    def _sub(m: re.Match) -> str:
        nonlocal count
        original = m.group(0)
        replacement = rng.choice(lookup[m.re.pattern])
        if original[0].islower():
            replacement = replacement[0].lower() + replacement[1:]
        if original[-1] in ".,;:!?" and replacement and replacement[-1] not in ".,;:!?":
            replacement += original[-1]
        count += 1
        return replacement

    for pattern, _ in table:
        text = re.sub(pattern, _sub, text, flags=re.IGNORECASE)
    return text, count


def replace_discourse_markers(text: str, rng: random.Random) -> tuple[str, int]:
    return _apply_swaps(text, DISCOURSE_REPLACEMENTS, rng)


def raise_perplexity(text: str, rng: random.Random) -> tuple[str, int]:
    return _apply_swaps(text, PERPLEXITY_SWAPS, rng)


def collapse_ngrams(text: str) -> tuple[str, int]:
    """Collapse wordy stock phrases into tighter equivalents.

    Replacement strings may contain backreferences (``\1``) — those are
    expanded by ``re.sub`` via the ``m.expand`` call. A plain callable
    replacement would return the raw ``\1`` literal, which is how a stray
    backslash once leaked into the output.
    """
    count = 0

    def _make(pattern: str, repl: str):
        def _r(m: re.Match) -> str:
            nonlocal count
            count += 1
            out = m.expand(repl) if (chr(92) in repl) else repl
            if out and m.group(0)[0].isupper() and out[0:1].islower():
                out = out[0].upper() + out[1:]
            return out
        return _r

    for pattern, repl in NGRAM_COLLAPSE:
        text = re.sub(pattern, _make(pattern, repl), text, flags=re.IGNORECASE)
    return text, count


# ─── Stats ──────────────────────────────────────────────────────────────────


def analyze(text: str) -> dict:
    """Report the signals a detector actually measures.

    ``ai_markers`` counts INDEPENDENT tells (discourse words that survived the
    swap table, plus structural fingerprints the table does not target) so the
    "after" number is a real residual, not a structurally-guaranteed zero.
    """
    sentences = split_sentences(text)
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    lengths = [_word_count(s) for s in sentences]

    # Independent tell list. These are NOT the swap-table patterns, so a swap
    # that succeeded removes the phrase but leaves this honest if a different
    # variant survives (e.g. "notably" wasn't in the input but "delve" was).
    _TELLS = [
        r"\bdelve\w*\b", r"\bleverag\w*\b", r"\bcomprehensive\b",
        r"\bmultifaceted\b", r"\bnuanced\b", r"\bcrucial\b",
        r"\bseamless\w*\b", r"\brobust\b", r"\btapestry\b",
        r"\bfurthermore\b", r"\bmoreover\b", r"\badditionally\b",
        r"\bin conclusion\b", r"\bit is important to note\b",
        r"\bnot only\b.*?\bbut also\b", r"—",
    ]
    markers = 0
    for pattern in _TELLS:
        markers += len(re.findall(pattern, text, re.IGNORECASE))
    # Uniform sentence lengths are themselves an AI signal; count each run of
    # 4+ near-equal-length sentences as one additional marker.
    run = 1
    for a, b in zip(lengths, lengths[1:]):
        if a and b and abs(a - b) <= 1:
            run += 1
            if run == 4:
                markers += 1
        else:
            run = 1

    return {
        "words": len(text.split()),
        "sentences": len(sentences),
        "paragraphs": len(paragraphs),
        "avg_sentence_len": round(statistics.fmean(lengths), 2) if lengths else 0.0,
        "sentence_len_stdev": round(statistics.pstdev(lengths), 2) if len(lengths) > 1 else 0.0,
        "burstiness": round(burstiness_score(sentences), 3),
        "avg_paragraph_len": round(statistics.fmean([_word_count(p) for p in paragraphs]), 2) if paragraphs else 0.0,
        "ai_markers": markers,
        "em_dashes": text.count("—"),
    }


# ─── Main Pipeline ──────────────────────────────────────────────────────────


def humanize(
    text: str,
    *,
    seed: int | None = None,
    strength: float = 0.6,
    typos: float = 0.0,
) -> str:
    """Full rule-based humanization pipeline.

    ``strength`` (0..1) scales burstiness + tell-removal aggressiveness.
    ``typos`` (0..1) scales how many realistic human typos are injected —
    0 = none (default), 1 = the full conservative budget (see
    :func:`add_human_typos`).
    """
    rng = random.Random(seed)

    # 1. Kill AI discourse markers + raise perplexity FIRST. These substitute
    #    the marker words (Moreover → And) that the n-gram rules below match
    #    against, so collapsing first would miss every one of them.
    text, _ = replace_discourse_markers(text, rng)
    text, _ = raise_perplexity(text, rng)

    # 2. Collapse wordy n-grams (now sees the substituted markers).
    text, _ = collapse_ngrams(text)

    # 3. Structural AI tells (not-only, em-dashes).
    text = kill_ai_tells(text, rng, strength)

    # 4. Burstiness per paragraph.
    processed: list[str] = []
    for para in text.split("\n\n"):
        if not para.strip():
            processed.append(para)
            continue
        sentences = split_sentences(para)
        sentences = enforce_burstiness(sentences, rng, strength)
        # inject_short_bursts is only applied when the paragraph is STILL flat
        # after splitting — otherwise it adds fragments to text that already
        # has good variance and drags the score down.
        if burstiness_score(sentences) < 0.45:
            sentences = inject_short_bursts(sentences, rng, rate=0.15 * strength)
        processed.append(" ".join(sentences))
    text = "\n\n".join(processed)

    # 5. Break paragraph symmetry.
    text = vary_paragraph_lengths(text)

    # 6. Controlled imperfections.
    if strength >= 0.4:
        text = add_controlled_imperfections(text, rng)

    # 7. Final sweep (catch anything the split passes reintroduced).
    text, _ = replace_discourse_markers(text, rng)
    text, _ = collapse_ngrams(text)

    # 8. Chaos: grammar slips then typos, LAST — nothing after this may
    #    "fix" them back. Both share the same slider (``typos``).
    if typos > 0:
        text = add_grammar_chaos(text, rng, rate=typos)
        text = add_human_typos(text, rng, rate=typos)

    return text


# ─── CLI ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rule-based AI text humanizer (structural post-processing v2)"
    )
    parser.add_argument("--text", type=str, help="Input text")
    parser.add_argument("--file", type=str, help="Read from file")
    parser.add_argument("--output", type=str, help="Write to file (default: stdout)")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible output")
    parser.add_argument("--strength", type=float, default=0.6, help="0..1 aggressiveness (default 0.6)")
    parser.add_argument("--typos", type=float, default=0.0, help="0..1 human-typo injection (default 0)")
    parser.add_argument("--stats", action="store_true", help="Print signal stats (JSON) to stderr")
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

    before = analyze(text)
    result = humanize(
        text,
        seed=args.seed,
        strength=max(0.0, min(1.0, args.strength)),
        typos=max(0.0, min(1.0, args.typos)),
    )
    after = analyze(result)

    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")

    # Stats go to stderr LAST and ALONE, so a caller can json.loads(stderr)
    # without tripping over a trailing human-readable line.
    if args.stats:
        print(json.dumps({"before": before, "after": after}, indent=2), file=sys.stderr)

    if not args.output:
        print(result)


if __name__ == "__main__":
    main()
