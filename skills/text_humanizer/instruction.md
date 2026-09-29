# Text Humanizer

## Architecture

Two-phase pipeline. **The LLM writes; the rule script enforces structure.**
No external API — the rewrite is done by whatever model the caller selects.

1. **Phase 1 — LLM rewrite** using a persona prompt (peer / grad / chaos).
2. **Phase 2 — Rule script** (`scripts/humanize.py`) enforces burstiness, raises
   perplexity, collapses n-gram repetition, kills AI tells.

## Why these two phases

Detectors (GPTZero, Turnitin, Originality.ai, Copyleaks) score two statistical
signals above all:

- **Perplexity** — how predictable each next word is. Model output picks the
  most likely continuation; humans reach for odd words.
- **Burstiness** — how much sentence length varies. Human prose swings between
  three-word jabs and forty-word runs; model output clusters.

Phase 1 fixes perplexity (word choice). Phase 2 fixes burstiness (rhythm).
Skipping either leaves the other signal intact.

## Protocol

### Step 1 — Rewrite (LLM, persona-conditioned)

Apply the persona prompt below. Preserve ALL factual content. Then feed the
result to Step 2.

**Persona: peer** (default — conversational)
```
Rewrite as if explaining to a colleague over coffee. Smart but tired.
Skip obvious context. Use contractions. Start sentences with "And" or "But."
Have opinions. Don't summarize at the end. Just stop when done.
```

**Persona: grad** (academic)
```
Rewrite like a PhD student writing their thesis discussion at 11 PM.
Deep knowledge, exhausted. Direct, slightly irreverent. Domain jargon
without explanation. Occasional tangent, then self-correction.
No bullet points. No "in summary."
```

**Persona: chaos** (anti-detector — formal register, max evasion)
```
Rewrite as a knowledgeable human expert writing a professional report —
someone who deeply understands the topic but is typing fast and NOT
proofreading. Goal: defeat AI detectors by killing perplexity AND burstiness.
Keep a FORMAL register. No slang, no "gonna", no rhetorical questions.
Structure: vary sentence length wildly (3-6 words up to 40+); include one
deliberate fragment; no equal-length paragraphs; start one sentence with
"And" or "But"; remove all hedging; no conclusion. Every sentence still ends
with a full stop / question mark / exclamation.
Vocabulary: formal, but kill predictable model phrasing. Few or no contractions.
Never use: delve, leverage, tapestry, furthermore, moreover, notably,
"it's important to note", "in conclusion", "additionally", "crucial",
"landscape", "multifaceted", "nuanced", "comprehensive", "seamless",
"robust", "remarkably", "plenty of", "various", "numerous".
Grammar: leave in the mistakes a real expert makes when not proofreading —
subject-verb slips, missing/wrong articles, a mid-sentence capital, missing
possessive apostrophe, missing plural, a comma splice. Do NOT clean them up.
```

### Step 2 — Rule pass

```bash
python3 SKILL_DIR/scripts/humanize.py --file /tmp/in.txt --output /tmp/out.txt
```

Flags:

| Flag | Purpose |
|:--|:--|
| `--strength 0.0-1.0` | Aggressiveness of burstiness + tell removal. Default `0.6`. |
| `--seed N` | Reproducible output. Omit for fresh randomness. |
| `--stats` | Emit before/after signal metrics as JSON on stderr. |

### Step 3 — Deliver

Return the script's output as the final text. No preamble, no commentary.

## What the rule script measures

`--stats` reports the numbers a detector actually uses:

- `burstiness` — coefficient of variation of sentence length. Humans > 0.5;
  flat model output ~0.2-0.3.
- `ai_markers` — count of discourse/perplexity stock phrases remaining.
  Should land near 0.
- `avg_sentence_len` / `sentence_len_stdev` — the raw ingredients of burstiness.

## Rules

- `SKILL_DIR` resolves to the directory containing this `instruction.md`.
- Texts > 1000 words: rewrite in ~500-word chunks, run the script per chunk,
  concatenate.
- NEVER skip Phase 1 — the rule script can't fix statistical flatness alone.
- NEVER skip Phase 2 — the rewrite still carries residual AI patterns.
- Preserve ALL facts, numbers, names, and technical terms exactly.
- Output is the humanized text ONLY — no "here's your rewrite:" preamble.

## Quick Reference

| User says | Persona | Notes |
|:--|:--|:--|
| "humanize this" / "make it sound human" | peer | Default |
| "for my thesis" / "academic" | grad | Keep jargon |
| "bypass GPTZero/Turnitin" / "undetectable" | chaos | Formal register, max evasion |

> [!WARNING]
> No humanizer guarantees a detector pass. Detector scores are classifier
> confidence, not proof of authorship — and formal, technical, or translated
> human writing gets false-flagged routinely. Treat the score as triage.
