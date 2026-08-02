
# Text Humanizer

## Architecture

Two-phase pipeline. **You (the agent) ARE the LLM.** No external API.

1. **Phase 1 — You rewrite the text** using the persona prompt below (internal, in your response)
2. **Phase 2 — Rule script** enforces burstiness, kills residual AI markers, breaks paragraph symmetry

## Protocol

### Step 1: Rewrite (YOU do this — no tool call)

Apply the appropriate persona prompt to the input text. Rewrite it fully in your response, preserving ALL factual content. Then pass the result to Step 2.

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

**Persona: anti** (aggressive detector evasion — use for Turnitin/GPTZero)
```
Never use: delve, leverage, tapestry, furthermore, moreover, notably,
"it's important to note", "in conclusion", "additionally", "crucial",
"landscape", "multifaceted", "nuanced", "comprehensive", "seamless", "robust"
Vary sentence length wildly (3 words to 40+). Include a sentence fragment.
One colloquial contraction per paragraph. No equal-length paragraphs.
Start one sentence with "And" or "But". Remove all hedging. No conclusion.
```

### Step 2: Rule Pass (script)

Pipe your rewritten text through the structural post-processor:

```bash
python3 SKILL_DIR/scripts/humanize.py --text "your rewritten text here"
```

Or for long text, write to a temp file first:
```bash
python3 SKILL_DIR/scripts/humanize.py --file /tmp/humanize_input.txt --output /tmp/humanize_output.txt
```

### Step 3: Deliver

Return the script's output as the final humanized text. Do NOT add commentary, headers, or formatting around it unless the user asks.

## Rules
- SKILL_DIR resolves to the directory containing this instruction.md.
- For texts >1000 words: rewrite in chunks (~500 words each), run script on each chunk, concatenate.
- NEVER skip Phase 1. The rule script alone catches markers but can't fix statistical flatness.
- NEVER skip Phase 2. Your rewrite may still have residual AI patterns the script catches.
- Preserve ALL factual content, numbers, names, and technical terms exactly.
- If user specifies a custom voice/persona, use that instead of the built-in ones.
- Output is the humanized text ONLY — no "here's your rewritten text:" preamble.

## Quick Reference
| User says | Persona | Notes |
|:--|:--|:--|
| "humanize this" / "make it sound human" | peer | Default |
| "for my thesis" / "academic" | grad | Keep jargon |
| "bypass GPTZero/Turnitin" / "undetectable" | anti | Most aggressive |
| "write like [X]" | custom | Use their description as persona prompt |
