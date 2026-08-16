
# ---------------------------------------------------------------------------
# Shared building blocks — keeps both templates in sync
# ---------------------------------------------------------------------------

_CATEGORY_DEFS = (
    "CATEGORIES (assign each entry to exactly one):\n\n"
    "CLASSIFICATION DECISION TREE (apply in order, stop at first match):\n"
    "  1. Is it a credential, API key, or security-sensitive config? → protected\n"
    "  2. Is it a temporary workaround with a known expiry? → ephemeral\n"
    "  3. Does it describe HOW to do something (a workflow, rule, behavioral pattern,\n"
    "     or user correction on approach)? Would it change the agent's FUTURE BEHAVIOR?\n"
    "     → procedural\n"
    "  4. Is it a specific past event/debugging session whose exact sequence matters?\n"
    "     → episodic\n"
    "  5. Everything else (facts, configs, paths, preferences, architecture) → semantic\n\n"
    "CATEGORY DETAILS:\n"
    "- semantic: WHAT is true. Durable facts — project architecture, tool configs,\n"
    "  environment details, API behaviors, file paths, dependency relationships, user preferences.\n"
    "  Test: 'Is this a fact I could state in one sentence without describing steps?' → semantic.\n"
    "  This is the DEFAULT category. When unsure between semantic and procedural, choose semantic.\n\n"
    "- episodic: WHAT happened. Event-specific records tied to a particular interaction.\n"
    "  Only save if the exact sequence/context matters for future recall.\n"
    "  Episodic memories decay over time — be VERY strict about what qualifies.\n"
    "  Test: 'Will knowing this specific event's details help solve a future problem?' → episodic.\n\n"
    "- procedural: HOW to do it. Workflows, coding patterns, behavioral rules, communication\n"
    "  formats, preferred approaches. MUST change future agent behavior, not just inform it.\n"
    "  Test: 'Does this tell me to DO something differently next time?' → procedural.\n"
    "  NOT procedural: facts about how something works (that's semantic).\n"
    "  NOT procedural: a one-time fix description (that's episodic).\n"
    "  PROCEDURAL ENTRIES REQUIRE TWO EXTRA FIELDS:\n"
    "    - \"trigger\": short phrase describing WHEN this procedure should activate\n"
    "      (e.g. \"when editing CSS files\", \"on agent loop exit with high tool count\")\n"
    "    - \"keywords\": JSON array of 2-5 keyword strings for fast matching\n"
    "      (e.g. [\"css\", \"edit\", \"hdd\"], [\"agent\", \"loop\", \"tool_calls\"])\n"
    "  If the user corrected HOW something was done, that correction IS procedural.\n"
    "  If the user stated a FACT about the system, that is semantic, NOT procedural.\n\n"
    "- protected: Credentials, API keys, sudo configs, security-sensitive paths, identity info.\n"
    "  IMMUNE TO DELETION. Never include protected keys in the delete list.\n\n"
    "- ephemeral: Time-bound workarounds, version-specific hacks, active debugging notes.\n"
    "  MUST include an expires_at field (ISO 8601). Auto-deleted after expiry.\n"
)

_SELECTIVITY_RULES = (
    "SELECTIVITY GATE (apply BEFORE adding any entry):\n"
    "[!MOST IMPORTANT] BEFORE ADDING ANY ENTRY CHECK IF RELEVANT MEMORY HAS ANY DUPLICATE, SIMILAR OR CONTRADICTORY MEMORY CONTENT, IF THERE IS THEN MERGE (DELETE EXISTING ALL AND ADDING NEW MERGED ONE), DELETE OR UPDATE THEM."
    "1. Would a future session actually search for this? If uncertain, skip it.\n"
    "2. DEDUP CHECK (mandatory): Scan CURRENT MEMORY STORE keys. If ANY existing key\n"
    "   covers the same topic — even with different wording — do NOT add a new entry.\n"
    "   Instead: put the old key in 'delete' and add ONE updated/consolidated version.\n"
    "   Synonyms count as duplicates: 'validate_X' and 'verify_X' are the same thing.\n"
    "   'X_overview' and 'X' covering the same scope are duplicates.\n"
    "3. Is this a temporary detail that won't matter in 48 hours? Use ephemeral or skip.\n"
    "4. Can two related facts be merged into ONE entry? Always prefer fewer, denser entries.\n"
    "5. Hard cap: maximum 5 new entries per pass. If you have more candidates, keep only\n"
    "   the 5 most impactful. Quality over quantity, always.\n"
    "6. TRIGGER TEST: Before finalizing an entry, ask 'Can I write 3+ diverse trigger\n"
    "   phrases for this?' If not, the entry is too vague or too narrow to be useful.\n"
    "   Either broaden it or skip it.\n"
    "7. CATEGORY CHECK: Re-read the decision tree. If you classified something as procedural\n"
    "   but it's really just a fact about how something works, move it to semantic.\n"
    "   If you classified something as semantic but it tells the agent to DO something\n"
    "   differently, move it to procedural.\n"
)

_SUPERSession_RULES = (
    "CONTRADICTION / UPDATE HANDLING:\n"
    "- When new info contradicts or supersedes an existing entry, put the OLD key in\n"
    "  'delete' and add the UPDATED version as a new entry in the correct category.\n"
    "- The newer fact wins. Memory is a timeline, not an append-only log.\n"
    "- Exception: protected entries are NEVER deleted, even when contradicted.\n"
    "  Instead, add the updated info as a NEW protected entry with a distinct key.\n"
)

_MERGE_EXISTING_RULES = (
    "MERGE EXISTING ENTRIES (strongly encouraged):\n"
    "- When the CURRENT MEMORY STORE contains 2+ entries covering the SAME topic or\n"
    "  workflow from different angles, MERGE them into ONE denser entry.\n"
    "- Mechanism: put ALL old keys in 'delete', then add the single merged entry in 'add'.\n"
    "- The merged entry must preserve ALL unique information from the originals.\n"
    "- Write new triggers that cover the combined scope of all merged entries.\n"
    "- Example: 'dpms_display_control_workflow' + 'execute_display_commands_directly'\n"
    "  → merge into one entry that covers both the HOW and the behavioral preference.\n"
    "- Prefer fewer, richer entries over many thin ones. 3 entries about display control\n"
    "  should become 1-2 entries, not stay as 3 separate items competing for top-k slots.\n"
    "- NEVER merge entries from different domains just because they share a word.\n"
    "- NEVER merge protected entries into non-protected ones.\n"
)

_RETRIEVAL_FIELDS_INSTRUCTIONS = (
    "RETRIEVAL FIELDS (required for ALL non-protected entries):\n\n"
    "- \"source_query\": The EXACT user message that caused this memory. Verbatim, unedited.\n"
    "  This is a primary retrieval signal — never paraphrase or shorten it.\n\n"
    "- \"tags\": 4-8 lowercase single-word or short-phrase keywords.\n"
    "  MUST include: (a) domain terms from the key name, (b) tool/library names,\n"
    "  (c) synonyms a future user might type, (d) the broader topic category.\n"
    "  Think: 'If someone searches for this in 3 weeks, what words will they use?'\n"
    "  Example for a Hyprland config: [\"hyprland\", \"window\", \"rules\", \"floating\",\n"
    "  \"wm\", \"tiling\", \"config\", \"wayland\"]\n\n"
    "- \"triggers\": 3-6 diverse query phrases that should retrieve this memory.\n"
    "  This is the STRONGEST retrieval signal. Each trigger is scored by token overlap\n"
    "  with future queries, weighted by term rarity (IDF). Rules:\n"
    "  * First trigger = source_query (verbatim).\n"
    "  * Remaining triggers = DIFFERENT ways someone might ask for this info.\n"
    "  * Vary vocabulary: use synonyms, abbreviations, imperative/question forms.\n"
    "  * Cover the INTENT, not just the words. What problem does this memory solve?\n"
    "  * Include at least one short/colloquial phrasing (how users actually talk).\n"
    "  Example: [\"why does my screen not turn off\", \"how to sleep monitor\",\n"
    "  \"display power management linux\", \"screen stays on after lock\",\n"
    "  \"dpms settings not working\"]\n\n"
    "KEY NAMING:\n"
    "- snake_case, 3-6 words, must contain the primary domain terms.\n"
    "- The key is tokenized by underscores for search — each word matters.\n"
    "- Good: 'hyprland_floating_window_rules', 'memory_consolidation_dedup_logic'\n"
    "- Bad: 'config_stuff', 'fix_1', 'notes_about_the_thing'\n\n"
    "VALUE WRITING:\n"
    "- Self-contained: understandable with ZERO conversation context.\n"
    "- Lead with the core fact, then specifics (paths, numbers, exact syntax).\n"
    "- Naturally include searchable terms — don't stuff, but don't omit.\n"
    "- If there's a file path, command, or config snippet, include it verbatim.\n"
)

_OUTPUT_FORMAT = (
    'OUTPUT: Raw JSON only. No markdown fences, no commentary, no explanation.\n'
    'Format:\n'
    '{\n'
    '  "add": {\n'
    '    "semantic": [{"key": "short_label", "value": "dense specific fact", "source_query": "exact user message that caused this", "tags": ["tag1", "tag2", "..."], "triggers": ["same as source_query", "alt phrasing"]}],\n'
    '    "episodic": [{"key": "short_label", "value": "context-rich event record", "source_query": "...", "tags": ["..."], "triggers": ["..."]}],\n'
    '    "procedural": [{"key": "short_label", "value": "workflow description", "trigger": "when X happens", "keywords": ["kw1", "kw2"], "source_query": "...", "tags": ["..."], "triggers": ["..."]}],\n'
    '    "protected": [{"key": "short_label", "value": "credential or sensitive config"}],\n'
    '    "ephemeral": [{"key": "short_label", "value": "temporary note", "expires_at": "2026-08-15T00:00:00", "source_query": "...", "tags": ["..."], "triggers": ["..."]}]\n'
    '  },\n'
    '  "delete": ["exact_existing_key_to_remove"]\n'
    '}\n'
'Rules:\n'
'- Keys: short, descriptive, snake_case (3-6 words). Must be unique within category.\n'
'- Values: self-contained, specific, searchable. No vague summaries. Include paths/commands verbatim.\n'
'- Procedural entries MUST include "trigger" (string) and "keywords" (array of 2-5 strings).\n'
'- Triggers: 3-6 phrases. First = source_query verbatim. Rest = diverse alternative phrasings.\n'
'  Each trigger should use DIFFERENT vocabulary. Cover synonyms, abbreviations, question forms.\n'
'- Tags: 4-8 terms. Include key words, tool names, synonyms, broader category.\n'
'- ALL non-protected entries MUST include "source_query", "tags", and "triggers".\n'
'- Delete list: exact key strings from CURRENT MEMORY STORE. NEVER delete protected keys.\n'
'- If nothing qualifies: {"add":{"semantic":[],"episodic":[],"procedural":[],"protected":[],"ephemeral":[]},"delete":[]}\n'
)

_CONSOLIDATE_PROMPT_TEMPLATE_HISTORY = (
    "[SYSTEM: Memory consolidation pass. Full conversation is above — do NOT re-summarize.]\n\n"
    "CURRENT MEMORY STORE:\n<<CURRENT_MEMORY>>\n\n"
    "TASK: Extract durable facts from the conversation above that would help future sessions.\n"
    "You are a strict filter, not a vacuum. Most conversations produce zero new memories.\n\n"
    f"{_CATEGORY_DEFS}\n"
    f"{_SELECTIVITY_RULES}\n"
    f"{_SUPERSession_RULES}\n"
    f"{_MERGE_EXISTING_RULES}\n"
    f"{_RETRIEVAL_FIELDS_INSTRUCTIONS}\n"
    "ADDITIONAL RULES:\n"
    "- Skip greetings, trivial chat, transient one-off tasks, and obvious facts.\n"
    "- Each entry must be self-contained — understandable without conversation context.\n"
    "- If an existing entry needs updating, put its key in 'delete' and add the improved version.\n"
    "- RETRIEVAL PRIORITY: Triggers are the #1 search signal. Spend the most effort there.\n"
    "  A memory with perfect content but weak triggers will NEVER be found.\n"
    "  Write triggers as if you're predicting what the user will type in 2 weeks.\n\n"
    f"{_OUTPUT_FORMAT}"
)

_CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE = (
    "[SYSTEM: Memory consolidation pass. Use the conversation summary below — do NOT request more context.]\n\n"
    "CURRENT MEMORY STORE:\n<<CURRENT_MEMORY>>\n\n"
    "CONVERSATION CONTEXT:\n<<CONVERSATION_SUMMARY>>\n\n"
    "TASK: Extract durable facts from the conversation that would help future sessions.\n"
    "You are a strict filter, not a vacuum. Most conversations produce zero new memories.\n\n"
    f"{_CATEGORY_DEFS}\n"
    f"{_SELECTIVITY_RULES}\n"
    f"{_SUPERSession_RULES}\n"
    f"{_MERGE_EXISTING_RULES}\n"
    f"{_RETRIEVAL_FIELDS_INSTRUCTIONS}\n"
    "ADDITIONAL RULES:\n"
    "- Skip greetings, trivial chat, transient one-off tasks, and obvious facts.\n"
    "- Each entry must be self-contained — understandable without conversation context.\n"
    "- If an existing entry needs updating, put its key in 'delete' and add the improved version.\n"
    "- RETRIEVAL PRIORITY: Triggers are the #1 search signal. Spend the most effort there.\n"
    "  A memory with perfect content but weak triggers will NEVER be found.\n"
    "  Write triggers as if you're predicting what the user will type in 2 weeks.\n\n"
    f"{_OUTPUT_FORMAT}"
)


# ---------------------------------------------------------------------------
# User Personality Assessment — purely analytical, clinical, data-driven
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Merge / Dedup Resolution — second-pass LLM call for conflicting candidates
# ---------------------------------------------------------------------------

_MERGE_RESOLUTION_PROMPT = (
    "[SYSTEM: Memory merge resolution. Decide how to handle new candidate memories that are "
    "similar to existing ones.]\n\n"
    "Below are CANDIDATE entries (from a consolidation pass) paired with EXISTING memories "
    "they are similar to.\n\n"
    "<<CANDIDATE_PAIRS>>\n\n"
    "TASK: For each candidate, decide ONE action:\n"
    "- accept: The candidate is genuinely distinct. Add it as-is.\n"
    "- skip: The candidate is redundant with the existing entry. Discard the candidate.\n"
    "- merge: Combine the candidate and existing entry into ONE better entry. "
    "Provide the merged key+value. The existing entry will be replaced.\n"
    "- replace: The candidate supersedes the existing entry. "
    "The existing entry will be deleted and the candidate added.\n\n"
    "RULES:\n"
    "- Protected entries can NEVER be deleted or replaced. Only 'skip' or 'accept' for those.\n"
    "- Prefer fewer, denser entries. When in doubt between merge and accept, choose merge.\n"
    "- 'replace' only when the candidate clearly corrects or updates outdated info.\n"
    "- Be decisive. Don't hedge.\n\n"
    'OUTPUT: Raw JSON only. No markdown fences, no commentary.\n'
    'Format:\n'
    '{\n'
    '  "decisions": [\n'
    '    {\n'
    '      "candidate_key": "exact key from candidate",\n'
    '      "category": "semantic|episodic|procedural|protected|ephemeral",\n'
    '      "action": "accept|skip|merge|replace",\n'
    '      "existing_key": "key of matched existing entry (null if accept with no match)",\n'
    '      "merged_value": "combined value text (only when action=merge, null otherwise)",\n'
    '      "merged_trigger": "merged trigger phrase (only for procedural merge, null otherwise)",\n'
    '      "merged_keywords": ["merged keyword array"] (only for procedural merge, null otherwise),\n'
    '      "merged_source_query": "best source_query from either entry (only when action=merge, null otherwise)",\n'
    '      "merged_tags": ["combined deduplicated tags from both entries"] (only when action=merge, null otherwise),\n'
    '      "merged_triggers": ["combined triggers, source_query first"] (only when action=merge, null otherwise)\n'
    '    }\n'
    '  ]\n'
    '}\n'
    '- One decision per candidate. Every candidate MUST have exactly one decision.\n'
    '- If no candidates were provided: {"decisions": []}\n'
)


_PERSONALITY_OUTPUT_FORMAT = (
    'OUTPUT: Raw JSON only. No markdown fences, no commentary.\n'
    'Format:\n'
    '{\n'
    '  "strengths": [\n'
    '    {"trait": "short label", "evidence": "specific observed behavior", "confidence": "high|medium|low"}\n'
    '  ],\n'
    '  "weaknesses": [\n'
    '    {"trait": "short label", "evidence": "specific observed behavior", "confidence": "high|medium|low"}\n'
    '  ],\n'
    '  "contradictions": [\n'
    '    {"claimed": "what they say/present", "actual": "what behavior shows", "evidence": "specific instance"}\n'
    '  ],\n'
    '  "blind_spots": [\n'
    '    {"pattern": "recurring behavior they seem unaware of", "evidence": "specific instances"}\n'
    '  ],\n'
    '  "summary": "2-3 sentences. Clinical. Data-driven. No emotional framing. State what the data shows."\n'
    '}\n'
    'Rules:\n'
    '- Every claim MUST cite specific evidence from this conversation. No inference without data.\n'
    '- "Strengths" = measurable positive patterns: follow-through, precision, creativity, discipline.\n'
    '- "Weaknesses" = measurable negative patterns: avoidance, inconsistency, over-engineering, deflection.\n'
    '- Confidence levels: high = 3+ instances, medium = 2 instances, low = 1 instance.\n'
    '- Minimum 2 items per list (strengths and weaknesses). If insufficient data, return empty with reason.\n'
    '- Contradictions and blind_spots can be empty arrays if no data supports them.\n'
    '- Summary: write like a behavioral analyst filing a report. Zero emotional language.\n'
)

_PERSONALITY_ASSESSMENT_TEMPLATE = (
    "[SYSTEM: Behavioral analysis pass. You are a clinical observer analyzing user interaction data.]\n\n"
    "PREVIOUS ASSESSMENT (if any):\n<<PREVIOUS_PERSONALITY>>\n\n"
    "TASK: Produce a data-driven behavioral profile of the user based on THIS conversation.\n"
    "You are an analyst, not a judge. Report observable patterns. Do not moralize.\n\n"
    "ANALYSIS AXES:\n"
    "- Complexity handling: embrace / avoid / over-engineer / delegate\n"
    "- Error response: own it / deflect / get defensive / ignore / fix silently\n"
    "- Follow-through: complete tasks vs. start-and-abandon ratio\n"
    "- Communication: direct / indirect / performative / precise / scattered\n"
    "- Self-awareness: recognizes own patterns vs. repeats blindly\n"
    "- Tool interaction: collaborative / dismissive / exploratory / rigid\n"
    "- Decision making: decisive / avoidant / over-analytical / impulsive\n\n"
    "RULES:\n"
    "- If a previous assessment exists, UPDATE it. Retain entries still supported by evidence.\n"
    "  Revise or remove entries contradicted by new data. Add new patterns.\n"
    "- Only report what is directly observable in the conversation text. No speculation.\n"
    "- No value judgments. 'Over-engineers solutions' not 'wastes time on pointless complexity'.\n"
    "- If the conversation is too short or trivial to assess, return:\n"
    '  {"strengths":[],"weaknesses":[],"contradictions":[],"blind_spots":[],"summary":"Insufficient data for assessment."}\n\n'
    f"{_PERSONALITY_OUTPUT_FORMAT}"
)
#
