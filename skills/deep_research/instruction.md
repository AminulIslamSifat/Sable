# Deep Research Skill — Instruction

## Purpose
Multi-round, multi-source research for questions that no single page answers:
comparisons across several items, "comprehensive report on X", questions with
many sub-parts (pricing + reviews + benchmarks + release dates), or explicit
"deep research" / "deep dive" requests.

**Not this if:** a single fact or a quick lookup answers it → use **Online
Search (Two-Phase)** instead. Deep Research is strictly heavier — it costs
more calls and more turns, so don't reach for it on simple queries.

This skill does not introduce a new search backend. It reuses
`web_search_batch.py` (the same script Online Search uses) called multiple
times, in rounds, with a planning/reflection step wrapped around it, plus the
script's `--research-log` state tracking so rounds don't re-fetch the same
URL or lose track of which sub-topic is still open.

---

## Workflow

### Phase 0 — Plan (no tool calls)
Before touching any tool, write out 3–8 sub-questions that together cover the
user's request. Keep the count proportional to scope:
- narrow comparison (2–3 items) → 3–4 sub-questions
- broad report → up to 8

Each sub-question should be answerable from a handful of sources — if one
sub-question is really two questions, split it.

Then initialize the state file, one call, topic names = your sub-questions:

```
python3 PROJECT_ROOT/skills/search_online/scripts/web_search_batch.py \
  --research-log /tmp/ghost_research_<session_id>.json \
  --research-init "sub-question 1" "sub-question 2" "sub-question 3"
```

### Phase 1 — Round 1 search (per sub-topic)
For each sub-topic, run search-only, tagging it with `--topic` so it logs
into the state file:

```
python3 .../web_search_batch.py --json --search-only "query for sub-topic 1" \
  --research-log /tmp/ghost_research_<session_id>.json --topic "sub-question 1"
```

Review the returned titles/URLs/snippets before fetching anything — never
fetch blindly (same rule as Online Search).

### Phase 2 — Fetch (per sub-topic)
Pick the 2–3 most promising URLs per sub-topic and fetch full content, same
`--topic` tag:

```
python3 .../web_search_batch.py --json --fetch-urls url1 url2 url3 \
  --research-log /tmp/ghost_research_<session_id>.json --topic "sub-question 1"
```

If a URL was already fetched for that topic in an earlier round, the script
skips it automatically and tells you — you don't need to track this
yourself.

### Phase 3 — Reflect (mandatory, visible checkpoint)
After every sub-topic has a first-pass result, explicitly state — in the
response, not silently — for each sub-topic:
- **answered**: sources sufficiently cover it
- **thin**: found something but coverage is weak/one-sided
- **conflicting**: sources disagree

Record this by calling `--research-mark` for each:

```
python3 .../web_search_batch.py --research-log /tmp/ghost_research_<session_id>.json \
  --research-mark "sub-question 1" answered
```

You can check the whole state at any point with:

```
python3 .../web_search_batch.py --research-log /tmp/ghost_research_<session_id>.json --research-status
```

### Phase 4 — Round 2 (conditional, bounded)
Only for sub-topics marked `thin` or `conflicting`: write 1–2 refined
follow-up queries (narrower, or targeting a different source type) and
repeat Phase 1–2 for just those topics.

**Hard cap: 2 rounds total.** If a sub-topic is still thin/conflicting after
round 2, say so plainly in the final report rather than looping again.

### Phase 5 — Synthesize
Produce the final report:
- one section per sub-question (section-aware synthesis — merge, don't
  free-write)
- inline citation of the source next to each claim
- a "Sources" list at the end with all URLs actually used
- if any sub-topic ended thin/conflicting, note that explicitly in its
  section instead of papering over it

### Phase 6 — Delivery
- If the user wants it saved as a file, hand the finished markdown to the
  **Code Editor** skill (`<create_file>`) — per the registry's mutation lock,
  Deep Research itself never writes files.
- Otherwise return the report inline, inside the global markdown wrapper.

---

## Rules
- Plan before searching — never start firing queries without the Phase 0 list.
- One `--topic` tag per sub-question, always, so state tracking stays useful.
- Reflect out loud between rounds — this is the step that prevents compounding
  errors from a bad round-1 result carrying through to the final report
  unchecked.
- Max 2 rounds. If still incomplete, report the gap — don't silently loop or
  silently drop the sub-question.
- Never skip straight to synthesis without the reflect checkpoint.
- Clean up: state files are scratch, not deliverables — do not present
  `/tmp/ghost_research_*.json` to the user as output.