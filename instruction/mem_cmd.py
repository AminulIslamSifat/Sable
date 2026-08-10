
# ---------------------------------------------------------------------------
# Shared building blocks — keeps both templates in sync
# ---------------------------------------------------------------------------

_CATEGORY_DEFS = (
    "CATEGORIES (assign each entry to exactly one):\n"
    "- semantic: Durable facts — user preferences, project architecture, tool configs,\n"
    "  environment details, API behaviors, file paths, dependency relationships.\n"
    "  This is the default. If it's a fact that stays true across sessions, it's semantic.\n"
    "- episodic: Event-specific records tied to a particular interaction or debugging session.\n"
    "  Only save if the exact sequence/context matters for future recall.\n"
    "  Episodic memories decay over time — be stricter about what qualifies.\n"
    "- procedural: How things are done — workflows, coding patterns, communication formats,\n"
    "  preferred approaches. Shapes agent behavior rather than knowledge.\n"
    "- protected: Credentials, API keys, sudo configs, security-sensitive paths, identity info.\n"
    "  IMMUNE TO DELETION. Never include protected keys in the delete list.\n"
    "- ephemeral: Time-bound workarounds, version-specific hacks, active debugging notes.\n"
    "  MUST include an expires_at field (ISO 8601). Auto-deleted after expiry.\n"
)

_SELECTIVITY_RULES = (
    "SELECTIVITY GATE (apply BEFORE adding any entry):\n"
    "1. Would a future session actually search for this? If uncertain, skip it.\n"
    "2. Does this duplicate or overlap with an existing entry? If yes, either skip it\n"
    "   or put the old key in 'delete' and add the consolidated/updated version.\n"
    "3. Is this a temporary detail that won't matter in 48 hours? Use ephemeral or skip.\n"
    "4. Can two related facts be merged into ONE entry? Always prefer fewer, denser entries.\n"
    "5. Hard cap: maximum 5 new entries per pass. If you have more candidates, keep only\n"
    "   the 5 most impactful. Quality over quantity, always.\n"
)

_SUPERSession_RULES = (
    "CONTRADICTION / UPDATE HANDLING:\n"
    "- When new info contradicts or supersedes an existing entry, put the OLD key in\n"
    "  'delete' and add the UPDATED version as a new entry in the correct category.\n"
    "- The newer fact wins. Memory is a timeline, not an append-only log.\n"
    "- Exception: protected entries are NEVER deleted, even when contradicted.\n"
    "  Instead, add the updated info as a NEW protected entry with a distinct key.\n"
)

_OUTPUT_FORMAT = (
    'OUTPUT: Raw JSON only. No markdown fences, no commentary, no explanation.\n'
    'Format:\n'
    '{\n'
    '  "add": {\n'
    '    "semantic": [{"key": "short_label", "value": "dense specific fact"}],\n'
    '    "episodic": [{"key": "short_label", "value": "context-rich event record"}],\n'
    '    "procedural": [{"key": "short_label", "value": "workflow or pattern description"}],\n'
    '    "protected": [{"key": "short_label", "value": "credential or sensitive config"}],\n'
    '    "ephemeral": [{"key": "short_label", "value": "temporary note", "expires_at": "2026-08-15T00:00:00"}]\n'
    '  },\n'
    '  "delete": ["exact_existing_key_to_remove"]\n'
    '}\n'
    'Rules:\n'
    '- Keys: short, descriptive, snake_case preferred. Must be unique within category.\n'
    '- Values: self-contained, specific, searchable. No vague summaries.\n'
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
    "ADDITIONAL RULES:\n"
    "- Skip entries already present in CURRENT MEMORY STORE (match by key AND value).\n"
    "- Skip greetings, trivial chat, transient one-off tasks, and obvious facts.\n"
    "- Each entry must be self-contained — understandable without conversation context.\n\n"
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
    "ADDITIONAL RULES:\n"
    "- Skip entries already present in CURRENT MEMORY STORE (match by key AND value).\n"
    "- Skip greetings, trivial chat, transient one-off tasks, and obvious facts.\n"
    "- Each entry must be self-contained — understandable without conversation context.\n\n"
    f"{_OUTPUT_FORMAT}"
)


# ---------------------------------------------------------------------------
# User Personality Assessment — purely analytical, clinical, data-driven
# ---------------------------------------------------------------------------

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
