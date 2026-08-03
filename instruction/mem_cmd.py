
_CONSOLIDATE_PROMPT_TEMPLATE_HISTORY = (
    "[SYSTEM: Memory consolidation pass. You already have the full conversation above. Do NOT re-summarize it.]\n\n"
    "CURRENT MEMORY STORE:\n<<CURRENT_MEMORY>>\n\n"
    "TASK: Scan the conversation above. Extract facts worth remembering for future sessions.\n"
    "Memory is now semantic-search backed, so granular entries are fine — prefer many specific entries over few broad ones.\n\n"
    "WHAT TO CAPTURE:\n"
    "- Architecture decisions, design patterns, project structure\n"
    "- User preferences, workflows, tool configs, environment details\n"
    "- Bugs found, fixes applied, workarounds discovered\n"
    "- API behaviors, quirks, gotchas, version-specific notes\n"
    "- File paths, command patterns, dependency relationships\n"
    "- Anything that would save time or prevent confusion in a future session\n\n"
    "AUTO-CLASSIFICATION:\n"
    "- PROTECTED: Credentials, passwords, API keys, sudo configs, security-sensitive paths,\n"
    "  identity info, anything that MUST NEVER be forgotten or accidentally deleted.\n"
    "  Protected entries are immune to deletion — never include protected keys in the delete list.\n"
    "- EPHEMERAL: Temporary workarounds, time-bound tasks, version-specific hacks,\n"
    "  debugging notes for active issues, anything with a natural expiration.\n"
    "  Ephemeral entries require an expires_at field (ISO 8601 datetime string).\n"
    "- SEMANTIC/EPISODIC/PROCEDURAL: Everything else — classify normally.\n\n"
    "RULES:\n"
    "- Skip entries already present in the current memory store (check keys and values)\n"
    "- Each entry should be self-contained and searchable by its key\n"
    "- Keys should be short descriptive labels; values should be dense and specific\n"
    "- Delete entries that are now outdated, superseded, or contradicted by new info\n"
    "- NEVER delete protected entries — they are permanent regardless of staleness\n"
    "- No maximum limit — capture everything genuinely useful\n"
    "- Still skip pure greetings, trivial chat, and transient one-off tasks\n\n"
    'OUTPUT: Raw JSON only, no markdown fences, no explanation.\n'
    'Format: {\n'
    '  "add": {\n'
    '    "semantic": [{"key": "...", "value": "..."}],\n'
    '    "episodic": [{"key": "...", "value": "..."}],\n'
    '    "procedural": [{"key": "...", "value": "..."}],\n'
    '    "protected": [{"key": "...", "value": "..."}],\n'
    '    "ephemeral": [{"key": "...", "value": "...", "expires_at": "2026-08-15T00:00:00"}]\n'
    '  },\n'
    '  "delete": ["exact_key_string"]\n'
    '}\n'
    'Delete list must NEVER contain keys from the protected category.\n'
    'If nothing qualifies, return exactly: {"add": {"semantic": [], "episodic": [], "procedural": [], "protected": [], "ephemeral": []}, "delete": []}\n\n'
    'SKILL CREATION (optional):\n'
    'If the user explicitly asks to remember a repeatable workflow or says "create a skill",\n'
    'include a "create_skill" field in the JSON:\n'
    '  "create_skill": {"name": "skill-name", "description": "...", "trigger": "when to use it", "prompt": "full instruction text"}\n'
    'Only include this when the user explicitly requests it — never auto-create skills from normal conversation.'
)

_CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE = (
    "[SYSTEM: Memory consolidation pass. Use this chat's message thread for context — do NOT ask for more context.]\n\n"
    "CURRENT MEMORY STORE:\n<<CURRENT_MEMORY>>\n\n"
    "CONTEXT:\n<<CONVERSATION_SUMMARY>>\n\n"
    "TASK: Based on the conversation in this thread, extract facts worth remembering for future sessions.\n"
    "Memory is now semantic-search backed, so granular entries are fine — prefer many specific entries over few broad ones.\n\n"
    "WHAT TO CAPTURE:\n"
    "- Architecture decisions, design patterns, project structure\n"
    "- User preferences, workflows, tool configs, environment details\n"
    "- Bugs found, fixes applied, workarounds discovered\n"
    "- API behaviors, quirks, gotchas, version-specific notes\n"
    "- File paths, command patterns, dependency relationships\n"
    "- Anything that would save time or prevent confusion in a future session\n\n"
    "AUTO-CLASSIFICATION:\n"
    "- PROTECTED: Credentials, passwords, API keys, sudo configs, security-sensitive paths,\n"
    "  identity info, anything that MUST NEVER be forgotten or accidentally deleted.\n"
    "  Protected entries are immune to deletion — never include protected keys in the delete list.\n"
    "- EPHEMERAL: Temporary workarounds, time-bound tasks, version-specific hacks,\n"
    "  debugging notes for active issues, anything with a natural expiration.\n"
    "  Ephemeral entries require an expires_at field (ISO 8601 datetime string).\n"
    "- SEMANTIC/EPISODIC/PROCEDURAL: Everything else — classify normally.\n\n"
    "RULES:\n"
    "- Skip entries already present in the current memory store (check keys and values)\n"
    "- Each entry should be self-contained and searchable by its key\n"
    "- Keys should be short descriptive labels; values should be dense and specific\n"
    "- Delete entries that are now outdated, superseded, or contradicted by new info\n"
    "- NEVER delete protected entries — they are permanent regardless of staleness\n"
    "- No maximum limit — capture everything genuinely useful\n"
    "- Still skip pure greetings, trivial chat, and transient one-off tasks\n\n"
    'OUTPUT: Raw JSON only, no markdown fences, no explanation.\n'
    'Format: {\n'
    '  "add": {\n'
    '    "semantic": [{"key": "...", "value": "..."}],\n'
    '    "episodic": [{"key": "...", "value": "..."}],\n'
    '    "procedural": [{"key": "...", "value": "..."}],\n'
    '    "protected": [{"key": "...", "value": "..."}],\n'
    '    "ephemeral": [{"key": "...", "value": "...", "expires_at": "2026-08-15T00:00:00"}]\n'
    '  },\n'
    '  "delete": ["exact_key_string"]\n'
    '}\n'
    'Delete list must NEVER contain keys from the protected category.\n'
    'If nothing qualifies, return exactly: {"add": {"semantic": [], "episodic": [], "procedural": [], "protected": [], "ephemeral": []}, "delete": []}\n\n'
    'SKILL CREATION (optional):\n'
    'If the user explicitly asks to remember a repeatable workflow or says "create a skill",\n'
    'include a "create_skill" field in the JSON:\n'
    '  "create_skill": {"name": "skill-name", "description": "...", "trigger": "when to use it", "prompt": "full instruction text"}\n'
    'Only include this when the user explicitly requests it — never auto-create skills from normal conversation.'
)
