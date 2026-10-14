
"""Shared instruction builder for all API connectors.

Builds the system instruction payload with project-aware overrides:
- Project instruction replaces Maria.md when active
- Output format can be disabled per-project
- Facts and git details injected from project config
- Skills filtered by project skills_config
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_INSTRUCTION_DIR = Path(__file__).resolve().parent.parent.parent / "instruction"
_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Tools that must NEVER be available to subagents (agent_role != None).
# Subagents operate autonomously — no interactive user prompts or nested multiagent spawns.
_SUBAGENT_BLOCKED_TOOLS = frozenset({"ask_user", "multi_agent"})

# The study-mode directive. Study mode is a normal chat whose ONLY deltas are
# (1) a different tool list and (2) this directive appended at the end. It is
# defined here — not in the route — so every transport (API, scraper, Qwen)
# assembles study instructions through the SAME builder as a normal chat.
_STUDY_MODE_DIRECTIVE = (
    "## Study Mode\n"
    "You are an expert study assistant embedded in a Sable study workspace. "
    "You are in STUDY MODE and you CONTROL study mode through your tools: "
    "you can highlight passages, take notes, edit notes, read any page of any "
    "library PDF, and inspect the user's study activity.\n"
    "Use the tools when the user asks you to change something or look something "
    "up — do not just describe what you would do. Prefer precise, surgical "
    "tools (append / replace_section) over rewriting a whole note.\n"
    "Answer in clear, well-structured markdown. Never output a chat_title "
    "block — that tool does not exist here."
)

# Cache-busting version counter — incremented on persona/instruction changes
# so all connectors (Gemini, DeepSeek, Mistral/OpenAI-compat) detect stale caches.
_instruction_version: int = 0


def invalidate_cache() -> None:
    """Bump the instruction version to force all connectors to rebuild."""
    global _instruction_version
    _instruction_version += 1


def get_instruction_version() -> int:
    return _instruction_version


def _get_project(project_id: str | None) -> dict[str, Any] | None:
    """Load project config from DB. Returns None if no project or lookup fails."""
    if not project_id:
        return None
    try:
        from server.database import get_project
        return get_project(project_id)
    except Exception:
        return None


def _filter_skills_prompt(skills_prompt: str, skills_config: dict) -> str:
    """Remove disabled skill sections from the registry prompt."""
    disabled = [k for k, v in skills_config.items() if not v]
    if not disabled:
        return skills_prompt
    lines = skills_prompt.split("\n")
    filtered: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip().lower()
        new_skip = False
        for ds in disabled:
            if stripped.startswith("###") and ds.lower().replace("_", " ") in stripped:
                new_skip = True
                break
        if new_skip:
            skip = True
            continue
        if skip and line.strip().startswith("###"):
            skip = False
        if not skip:
            filtered.append(line)
    return "\n".join(filtered)


# ---------------------------------------------------------------------------
# Provider-specific tool call format instructions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HIGH-PRIORITY tool call format instructions.
# These are injected TWICE: once BEFORE tool schemas (primer) and once at the
# very END of the system prompt (recency reinforcement). This dual placement
# ensures the format instruction is the most weighted instruction in the prompt.
# ---------------------------------------------------------------------------

# DeepSeek V4 DSML format — uses ASCII pipe | (U+007C), NOT fullwidth ｜
_DSML_O = "<|DSML|tool_calls>"
_DSML_C = "</|DSML|tool_calls>"
_DSML_I = "<|DSML|invoke"
_DSML_P = "<|DSML|parameter"
_DSML_IC = "</|DSML|invoke>"
_DSML_PC = "</|DSML|parameter>"

_TOOL_FORMAT_DEEPSEEK = f"""\
## ⚠️ HIGHEST PRIORITY: Tool Call Format (DSML)

> [!CRITICAL]
> This instruction overrides ALL other formatting guidance.
> You MUST use DSML format for every tool call. No exceptions.

- ALL tool invocations MUST be wrapped in `{_DSML_O}` blocks with `{_DSML_I}>` / `{_DSML_P}>` tags.
- NEVER output bare JSON arrays, plain JSON objects, or any other format for tool calls.
- If you output anything other than properly formatted DSML blocks for tool calls, the system WILL FAIL.
- Multiple tool calls go inside ONE `{_DSML_O}` block as separate `{_DSML_I}>` elements.
- String parameters: set `string="true"` and pass the raw text value.
- Non-string parameters (numbers, booleans, arrays, objects): set `string="false"` and pass JSON.

### Template
{_DSML_O}
  {_DSML_I} name="$TOOL_NAME">
    {_DSML_P} name="$PARAMETER_NAME" string="true|false">$VALUE{_DSML_PC}
  {_DSML_IC}
{_DSML_C}

### Example
{_DSML_O}
  {_DSML_I} name="execute_command">
    {_DSML_P} name="command" string="true">ls -la{_DSML_PC}
    {_DSML_P} name="timeout" string="false">30{_DSML_PC}
  {_DSML_IC}
{_DSML_C}
"""

_TC_OPEN = "<" + "tool_call" + ">"
_TC_CLOSE = "</" + "tool_call" + ">"

_ACTION_OPEN = "<action>"
_ACTION_CLOSE = "</action>"

_TOOL_FORMAT_QWEN = f"""\
## ⚠️ HIGHEST PRIORITY: Tool Call Format

> [!CRITICAL]
> This instruction overrides ALL other formatting guidance.
> You MUST wrap every tool call in {_ACTION_OPEN}...{_ACTION_CLOSE} tags. No exceptions.

- All tool calls MUST be a JSON array inside {_ACTION_OPEN}...{_ACTION_CLOSE} tags.
- Single call OR multiple calls → always a JSON array inside ONE {_ACTION_OPEN} block.
- NEVER output multiple separate {_ACTION_OPEN} blocks. Combine into one array.
- Tool call blocks appear ONLY in plain text, NEVER inside fenced code blocks.
- Keep prose to ONE short sentence before the tool call block.
- Place the tool call block at the END of your response.


### Single call
{_ACTION_OPEN}[{{"name": "<function-name>", "arguments": <args-json-object>}}]{_ACTION_CLOSE}

### Multiple calls
{_ACTION_OPEN}[{{"name": "tool_a", "arguments": {{...}}}}, {{"name": "tool_b", "arguments": {{...}}}}]{_ACTION_CLOSE}
"""

_TOOL_FORMAT_NATIVE = f"""\
## ⚠️ HIGHEST PRIORITY: Tool Call Format

> [!CRITICAL]
> This instruction overrides ALL other formatting guidance.
> You MUST use exactly ONE [] wrapper per response. No exceptions.

- Single call OR multiple calls → always a JSON array inside [] brackets.
- NEVER output multiple separate [] blocks. Combine into one array.
- Tool call blocks appear ONLY in plain text, NEVER inside fenced code blocks.
- Keep prose to ONE short sentence before the tool call block.
- Place the tool call block at the END of your response.


### Single call
[{{"name": "<function-name>", "arguments": <args-json-object>}}]

### Multiple calls
[{{"name": "tool_a", "arguments": {{...}}}}, {{"name": "tool_b", "arguments": {{...}}}}]
"""

_TOOL_FORMAT_HERMES = f"""\
## ⚠️ HIGHEST PRIORITY: Tool Call Format (Hermes XML)

> [!CRITICAL]
> This instruction overrides ALL other formatting guidance.
> You MUST wrap every tool call in {_ACTION_OPEN}...{_ACTION_CLOSE} tags using Hermes XML structure. No exceptions.

- All tool calls MUST be wrapped inside a single `{_ACTION_OPEN}`...`{_ACTION_CLOSE}` block.
- Each tool invocation uses `<invoke name="TOOL_NAME">` with `<parameter name="KEY">VALUE</parameter>` children.
- Multiple tool calls go inside ONE `{_ACTION_OPEN}` block as separate `<invoke>` elements.
- NEVER output JSON arrays for tool calls. Use ONLY the XML invoke structure above.
- The wrapper is `{_ACTION_OPEN}` ... `{_ACTION_CLOSE}`. Do not substitute any other tag name.
- Tool call blocks appear ONLY in plain text, NEVER inside fenced code blocks.
- Keep prose to ONE short sentence before the tool call block.
- Place the tool call block at the END of your response.

### Single call
{_ACTION_OPEN}
<invoke name="TOOL_NAME">
<parameter name="arg_name">value</parameter>
</invoke>
{_ACTION_CLOSE}

### Multiple calls
{_ACTION_OPEN}
<invoke name="TOOL_A">
<parameter name="arg1">value1</parameter>
</invoke>
<invoke name="TOOL_B">
<parameter name="arg2">value2</parameter>
</invoke>
{_ACTION_CLOSE}
"""

_TOOL_FORMAT_MIMO = f"""\
## ⚠️ HIGHEST PRIORITY: Tool Call Format (XML)

> [!CRITICAL]
> This instruction overrides ALL other formatting guidance.
> You MUST use XML tool call format for every tool call. No exceptions.

- Every tool call MUST be wrapped in `<tool_call>` ... `</tool_call>` tags.
- Inside the wrapper, use `<function=TOOL_NAME>` to specify the tool.
- Inside the function tag, use `<parameter=KEY>VALUE</parameter>` for each argument.
- For parameters that are numbers, booleans, arrays, or objects, pass valid JSON as the value.
- For string parameters, pass the raw text value directly.
- Multiple tool calls: use SEPARATE `<tool_call>` blocks, one per call.
- NEVER output JSON arrays, bare JSON objects, or any other format for tool calls.
- Tool call blocks appear ONLY in plain text, NEVER inside fenced code blocks.
- Keep prose to ONE short sentence before the tool call block.
- Place all tool call blocks at the END of your response.

### Single call
<tool_call><function=execute_command><parameter=command>ls -la</parameter><parameter=timeout>30</parameter></function></tool_call>

### Multiple calls
<tool_call><function=view_file><parameter=path>/home/user/file.txt</parameter></function></tool_call>
<tool_call><function=execute_command><parameter=command>echo hello</parameter></function></tool_call>
"""


_TOOL_CHANNEL_MANDATE = """
## HELPER TOOL:
The listed tool here are helper tool which you can use to extend your capabilities. 
These tools run and parse in user machine (other listed tool run in cloud sandbox), so act and response accordingly.
Use helper tool to work on user machine.

Follow the structure provided in the instruction to call those helper tool.
"""

_TOOL_FORMAT_NONE = """
## Tool Call Format

Tool calls are handled via native API function calling. No prompt-based format needed.
Follow the function schemas provided in the API request.
"""

# Named format registry — selectable from settings UI
_NAMED_TOOL_FORMATS: dict[str, str] = {
    "qwen_json": _TOOL_FORMAT_QWEN,
    "hermes": _TOOL_FORMAT_HERMES,
    "dsml": _TOOL_FORMAT_DEEPSEEK,
    "mimo": _TOOL_FORMAT_MIMO,
    "native": _TOOL_FORMAT_NATIVE,
    "none": _TOOL_FORMAT_NONE,
}

# Default per-provider mapping (used when no settings override is set)
_PROVIDER_TOOL_FORMATS: dict[str, str] = {
    "deepseek": _TOOL_FORMAT_QWEN,  # TEMP: use Qwen <action> format instead of DSML
    "qwen": _TOOL_FORMAT_QWEN,
    "zai": _TOOL_FORMAT_QWEN,
    "mimo": _TOOL_FORMAT_MIMO,
    "kimi": _TOOL_FORMAT_QWEN,
    "chatgpt": _TOOL_FORMAT_QWEN,
    "mistral_web": _TOOL_FORMAT_QWEN,
    # Claude web: the SPA is Anthropic-hosted, but Sable drives it through the
    # browser and parses the reply text itself — so it needs a PROMPT-INJECTED
    # tool format like the other scrapers. Qwen/action style is the shared
    # default for every browser-scraped provider here.
    "claude_scraper": _TOOL_FORMAT_QWEN,
    "claude": _TOOL_FORMAT_QWEN,
    "native": _TOOL_FORMAT_NATIVE,
    "none": _TOOL_FORMAT_NONE,
}


# Raw connector provider strings -> canonical settings/UI key. Mirrors the
# aliasing in ``prompt_injection.canonical_provider`` so a per-provider format
# set against the UI row ("gemini") still applies when the connector passes its
# raw string ("none").
_FORMAT_PROVIDER_ALIASES: dict[str, str] = {
    "none": "gemini",
    "gemini_api": "gemini",
    "openai_compat": "native",
    "cloudflare": "native",
    "url": "native",
    "deepseek_scraper": "deepseek",
    "qwen_scraper": "qwen",
    # Claude web connector -> canonical settings/UI key.
    "claude": "claude_scraper",
    "anthropic": "claude_scraper",
}


def _canonical_format_provider(provider: str | None) -> str:
    """Normalise a raw connector provider string to its settings/UI key."""
    if not provider:
        return ""
    _p = str(provider).strip().lower()
    return _FORMAT_PROVIDER_ALIASES.get(_p, _p)


def _named_format_for(provider: str | None) -> str:
    """Resolve the active tool-format NAME for a provider.

    Priority:
      1. Per-provider override — ``tool_call_format_by_provider[<key>]``
         (set from Settings -> Providers)
      2. Legacy global override — ``tool_call_format`` (pre per-provider UI)
      3. Built-in provider default from ``_PROVIDER_FORMAT_NAMES``

    Returns ``""`` when nothing applies, i.e. "no prompt format section".
    """
    _raw = str(provider).strip().lower() if provider else ""
    _key = _canonical_format_provider(provider)

    _settings: dict[str, Any] = {}
    try:
        _settings_path = Path(__file__).resolve().parent.parent.parent / "system" / "settings.json"
        if _settings_path.is_file():
            import json as _json
            _settings = _json.loads(_settings_path.read_text(encoding="utf-8"))
    except Exception:
        _settings = {}

    # 1. Per-provider override. An explicit "" means "use the provider default".
    _map = _settings.get("tool_call_format_by_provider")
    if isinstance(_map, dict) and _key and _key in _map:
        _val = _map.get(_key, "")
        if _val in _NAMED_TOOL_FORMATS:
            return _val
        return _PROVIDER_FORMAT_NAMES.get(_key) or _PROVIDER_FORMAT_NAMES.get(_raw, "")

    # 2. Legacy global override — applies to every provider.
    _global = _settings.get("tool_call_format", "")
    if _global and _global in _NAMED_TOOL_FORMATS:
        return _global

    # 3. Provider default (canonical key first, then raw as a fallback so a
    #    raw provider like "none" still resolves to the gemini/none default).
    return _PROVIDER_FORMAT_NAMES.get(_key) or _PROVIDER_FORMAT_NAMES.get(_raw, "")


def get_tool_format(provider: str | None = None) -> str:
    """Return the active tool format string for *provider*.

    Resolution priority:
      1. Per-provider override in ``system/settings.json``
         (``tool_call_format_by_provider``) — set from Settings -> Providers.
      2. Legacy global override (``tool_call_format``) — pre per-provider UI.
      3. Built-in provider default from ``_PROVIDER_TOOL_FORMATS``.
      4. Empty string if nothing matches.
    """
    return _NAMED_TOOL_FORMATS.get(_named_format_for(provider), "")


# Human-readable wrapper info per named format: (open_tag, close_tag, example).
# Consumers that render format-correct guidance (e.g. the resilience guard's
# malformed warnings) read this instead of hardcoding "<action>".
_NAMED_FORMAT_DESCRIPTIONS: dict[str, tuple[str, str, str]] = {
    "qwen_json": (
        _ACTION_OPEN,
        _ACTION_CLOSE,
        _ACTION_OPEN + '[{"name": "tool_name", "arguments": {...}}]' + _ACTION_CLOSE,
    ),
    "hermes": (
        _ACTION_OPEN,
        _ACTION_CLOSE,
        _ACTION_OPEN + '<invoke name="tool_name"><parameter name="arg">value</parameter></invoke>' + _ACTION_CLOSE,
    ),
    "dsml": (
        "<\uff5cDSML\uff5ctool_calls>",
        "</\uff5cDSML\uff5ctool_calls>",
        "a DSML tool_calls block",
    ),
    "mimo": (
        _TC_OPEN,
        _TC_CLOSE,
        _TC_OPEN + "<function=tool_name><parameter=arg>value</parameter></function>" + _TC_CLOSE,
    ),
    "native": (
        "[",
        "]",
        '[{"name": "tool_name", "arguments": {...}}]',
    ),
    "none": ("", "", "native API function calling"),
    # Empty name = no override set; fall back to the qwen/action style.
    "": (
        _ACTION_OPEN,
        _ACTION_CLOSE,
        _ACTION_OPEN + '[{"name": "tool_name", "arguments": {...}}]' + _ACTION_CLOSE,
    ),
}

# Reverse-map so provider -> format NAME is derived, never hand-maintained.
_FORMAT_STR_TO_NAME: dict[str, str] = {v: k for k, v in _NAMED_TOOL_FORMATS.items()}
_PROVIDER_FORMAT_NAMES: dict[str, str] = {
    p: _FORMAT_STR_TO_NAME.get(f, "") for p, f in _PROVIDER_TOOL_FORMATS.items()
}


def get_tool_format_name(provider: str | None = None) -> str:
    """Return the NAME of the active tool format (e.g. ``qwen_json``, ``hermes``).

    Same resolution order as :func:`get_tool_format`, but returns the settings
    key so consumers can render format-correct guidance without parsing prose.
    """
    return _named_format_for(provider)


def get_default_tool_format_name(provider: str | None) -> str:
    """Built-in default format NAME for a provider, ignoring user overrides."""
    _raw = str(provider).strip().lower() if provider else ""
    _key = _canonical_format_provider(provider)
    return _PROVIDER_FORMAT_NAMES.get(_key) or _PROVIDER_FORMAT_NAMES.get(_raw, "")


def available_tool_format_names() -> list[str]:
    """All selectable format names, in UI display order."""
    return ["qwen_json", "hermes", "dsml", "mimo", "native", "none"]


def get_tool_format_tags(provider: str | None = None) -> tuple[str, str, str]:
    """Return ``(open_tag, close_tag, example)`` for the active tool format.

    Empty ``open_tag``/``close_tag`` means the format uses no text wrapper
    (bare JSON array for ``native``, or no prompt format at all for ``none``).
    """
    _name = get_tool_format_name(provider)
    return _NAMED_FORMAT_DESCRIPTIONS.get(_name, _NAMED_FORMAT_DESCRIPTIONS[""])


def build_instructions(
    project_id: str | None = None,
    provider: str | None = None,
    agent_role: str | None = None,
    agent_skills: list[str] | None = None,
    layout_mode: str | None = None,
    persona_override: str | None = None,
    custom_instructions: str | None = None,
) -> str:
    """Build full system instruction with optional project overrides.

    This is the single source of truth for instruction assembly across all
    API connectors (DeepSeek, Gemini, Mistral), AND subagent roles.
    Qwen uses session.py directly. Groq/OpenAI use their own minimal prompts.

    Args:
        project_id: Optional project ID for project-specific overrides.
        provider: Provider key for tool format selection.
                  "deepseek" → DSML invoke/parameter blocks.
                  "native"   → tag-wrapped format (Gemini, Mistral, etc.).
                  "none"     → native API function calling (no prompt format).
                  None       → no tool format section appended.
        agent_role: If set, build instructions for a subagent role instead of
                    the main chat persona. Loads instruction/agents/{role}.md
                    as persona, skips project/git/facts sections.

        agent_skills: Skill keys for subagent (filters skill registry).
        layout_mode: "chat" strips all tools/skills/MCP except web search
                     and chat_title. "agent" or None = full tool access.
    """
    proj = None if agent_role else _get_project(project_id)
    parts: list[str] = []
    _active_tool_format = None  # Default init to prevent UnboundLocalError in non-chat/edge cases

    # Per-provider injection toggles (Settings -> Providers).
    # Unknown/None provider -> all-on defaults, so new connectors are never
    # silently starved of context until explicitly toggled off.
    try:
        from connectors.common.prompt_injection import injection_enabled_map
        _inj = injection_enabled_map(provider)
    except Exception:
        _inj = {}

    # Persona config is needed for both main chat and subagent paths (output_format toggle)
    _persona_cfg_path = _INSTRUCTION_DIR / ".persona_config.json"

    # --- Persona / Instruction ---
    if agent_role:
        # Subagent mode: load role-specific persona from instruction/agents/{role}.md
        _agents_dir = _INSTRUCTION_DIR / "agents"
        _role_persona = _agents_dir / f"{agent_role}.md"
        if _role_persona.is_file():
            parts.append(_role_persona.read_text(encoding="utf-8").strip())
        else:
            parts.append(f"You are a {agent_role} specialist. Complete the assigned task thoroughly.")

        # Always load personal.md for subagents too (user context)
        personal_path = _INSTRUCTION_DIR / "personal.md"
        if personal_path.exists():
            parts.append(personal_path.read_text(encoding="utf-8").strip())

        # Teacher escalation awareness — agents know they can get help when stuck
        parts.append(
            "# Stuck Recovery\n"
            "If you find yourself repeatedly failing the same tool call, getting identical "
            "errors, or unable to make progress after 2-3 attempts:\n"
            "1. Stop repeating the same approach immediately.\n"
            "2. Try a fundamentally different strategy or tool.\n"
            "3. If you are still stuck after trying alternatives, the system will automatically "
            "request guidance from the orchestrator. You will receive a [TEACHER GUIDANCE] block "
            "with specific instructions — follow them exactly.\n"
            "4. Do NOT ignore teacher guidance or repeat the same failed approach after receiving it."
        )
    else:
        # Main chat mode: original persona logic
        project_instruction = None
        if proj and proj.get("instruction_text"):
            project_instruction = proj["instruction_text"]
        elif proj and proj.get("instruction_file"):
            instr_path = Path(proj["instruction_file"])
            if instr_path.exists():
                project_instruction = instr_path.read_text(encoding="utf-8")

        if _inj.get("persona", True):
            if persona_override and persona_override.strip():
                # Study mode may name its own persona; it replaces the active one.
                parts.append(persona_override.strip())
            elif project_instruction and proj and proj.get("persona_enabled", True):
                parts.append(project_instruction)
            else:
                # Load active persona from config
                _active_persona = None
                _disabled_personas: list[str] = []
                if _persona_cfg_path.exists():
                    try:
                        _pcfg = json.loads(_persona_cfg_path.read_text(encoding="utf-8"))
                        _active_persona = _pcfg.get("active")
                        _disabled_personas = _pcfg.get("disabled", [])
                    except (json.JSONDecodeError, OSError):
                        pass

                if _active_persona and _active_persona not in _disabled_personas:
                    persona_path = _INSTRUCTION_DIR / f"{_active_persona}.md"
                    if persona_path.exists():
                        parts.append(persona_path.read_text(encoding="utf-8").strip())

            # Always load personal.md (user info, not a persona)
            personal_path = _INSTRUCTION_DIR / "personal.md"
            if personal_path.exists():
                parts.append(personal_path.read_text(encoding="utf-8").strip())

    # --- Output Format ---
    _of_enabled = True
    if _persona_cfg_path.exists():
        try:
            _pcfg2 = json.loads(_persona_cfg_path.read_text(encoding="utf-8"))
            _of_enabled = _pcfg2.get("output_format_enabled", True)
        except (json.JSONDecodeError, OSError):
            pass
    if _inj.get("output_format", True) and _of_enabled and (not proj or proj.get("output_format_enabled", True)):
        of_path = _INSTRUCTION_DIR / "output_format.md"
        if of_path.exists():
            parts.append(of_path.read_text(encoding="utf-8").strip())

    # --- Facts to Remember ---
    if _inj.get("facts", True) and proj and proj.get("facts"):
        parts.append(f"# Facts to Remember (Project: {proj.get('name', 'Unknown')})\n{proj['facts']}")

    # --- Git Details ---
    if _inj.get("git", True) and proj and any(proj.get(k) for k in ("git_repo", "git_username", "git_branch")):
        git_lines = ["# Git Repository Details"]
        if proj.get("git_repo"):
            git_lines.append(f"- Repo: {proj['git_repo']}")
        if proj.get("git_username"):
            git_lines.append(f"- Username: {proj['git_username']}")
        if proj.get("git_branch"):
            git_lines.append(f"- Branch: {proj['git_branch']}")
        parts.append("\n".join(git_lines))

    # --- Layout mode: chat / study / agent ---
    _is_chat_mode = layout_mode == "chat"
    _is_study_mode = layout_mode == "study"

    if _is_study_mode:
        # Study mode = a NORMAL chat with a different tool list and one extra
        # study directive at the end. Persona + personal.md + output format are
        # already assembled above (identical to a normal chat); here we add the
        # tool-call primer, the study tool schemas, then the study directive.
        # No skills registry, no MCP, no chat_title, no agent tooling.
        _active_tool_format = get_tool_format(provider) if _inj.get("tool_format", True) else ""
        if _active_tool_format:
            parts.append(_active_tool_format)
        if _inj.get("tools", True):
            try:
                from engine.tools_loader import get_study_tool_schemas
                _study_disabled_tools: list[str] = []
                _study_dt_path = _PROJECT_ROOT / "Brain" / "disabled_tools.json"
                if _study_dt_path.exists():
                    try:
                        _study_dt = json.loads(_study_dt_path.read_text(encoding="utf-8"))
                        if isinstance(_study_dt, list):
                            _study_disabled_tools = _study_dt
                    except (json.JSONDecodeError, OSError):
                        pass
                _study_schemas = get_study_tool_schemas(_study_disabled_tools)
                if _study_schemas:
                    _study_lines = ["<tools>"]
                    _study_lines.extend(json.dumps(s, ensure_ascii=False) for s in _study_schemas)
                    _study_lines.append("</tools>")
                    parts.append("\n".join(_study_lines))
            except Exception:
                pass
        if custom_instructions and custom_instructions.strip():
            parts.append(custom_instructions.strip())
        parts.append(_STUDY_MODE_DIRECTIVE)
    elif not _is_chat_mode:
        # --- Skill Registry ---
        from engine.skills import SkillEngine
        from engine.skills.handlers import HANDLER_MAP

        # --- Skill Registry ---
        # All chats (main + subagent) get the same full skill set.
        # No skill filtering for subagents — they use the main chat pipeline.
        _disabled_skills: list[str] = []
        _global_disabled_path = _PROJECT_ROOT / "Brain" / "disabled_skills.json"
        if _global_disabled_path.exists():
            try:
                _gd = json.loads(_global_disabled_path.read_text(encoding="utf-8"))
                if isinstance(_gd, list):
                    _disabled_skills.extend(_gd)
            except Exception:
                pass
        if proj and proj.get("skills_config"):
            _disabled_skills.extend([k for k, v in proj["skills_config"].items() if not v])

        _engine = SkillEngine(
            skills_dir=_SKILLS_DIR,
            handlers=HANDLER_MAP,
            agent_id="maria" if not agent_role else f"agent-{agent_role}",
            disabled=_disabled_skills or None,
        )
        skills_prompt = _engine.get_registry_prompt() if _inj.get("skills", True) else ""
        if skills_prompt.strip():
            parts.append(skills_prompt)

        # --- ⚠️ TOOL CALL FORMAT PRIMER (BEFORE tool schemas) ---
        # Injected FIRST so the model knows HOW to call tools before seeing WHAT tools exist.
        # This is the highest-weighted instruction via dual placement (primer + recency).
        # Respects user override from settings UI (tool_call_format dropdown).
        _active_tool_format = get_tool_format(provider) if _inj.get("tool_format", True) else ""
        if _active_tool_format:
            parts.append(_active_tool_format)

        # --- Tool Schemas ---
        if _inj.get("tools", True):
            try:
                from engine.tools_loader import get_tools_prompt_section
                _disabled_tools: list[str] = []
                _disabled_tools_path = _PROJECT_ROOT / "Brain" / "disabled_tools.json"
                if _disabled_tools_path.exists():
                    _dt = json.loads(_disabled_tools_path.read_text(encoding="utf-8"))
                    if isinstance(_dt, list):
                        _disabled_tools = _dt
                # Block interactive/multiagent tools for subagents
                if agent_role:
                    _disabled_tools = list(set(_disabled_tools) | _SUBAGENT_BLOCKED_TOOLS)
                tools_section = get_tools_prompt_section(disabled=_disabled_tools, provider=provider)
                if tools_section:
                    parts.append(tools_section)
            except Exception:
                pass

        # --- MCP Tools ---
        if _inj.get("mcp", True):
            try:
                from engine.mcp.manager import get_mcp_manager
                mcp_section = get_mcp_manager().get_prompt_section()
                if mcp_section:
                    parts.append(mcp_section)
            except Exception:
                pass
    else:
        # Chat mode: only web_search + chat_title, no skills, no MCP, no format primer
        try:
            from engine.tools_loader import get_all_tool_schemas
            _chat_allowed = ["online_search", "chat_title"]
            _chat_schemas = get_all_tool_schemas([], allowed=_chat_allowed, tier=None)
            if _chat_schemas:
                import json as _json
                lines = ["<tools>"]
                for s in _chat_schemas:
                    lines.append(_json.dumps(s, ensure_ascii=False))
                lines.append("</tools>")
                parts.append("\n".join(lines))
        except Exception:
            pass

    # --- Output Directory (toggleable per-provider) ---
    if _inj.get("output_dir", True):
        from engine.config import OUTPUT_ROOT as _OUT
        parts.append(
            f"# Output Directory (MANDATORY)\n"
            f"ALL generated content (notes, research, text files, agent logs, assets, downloads) "
            f"MUST be saved under `{_OUT}/`. NEVER save to CWD or project root unless explicitly instructed.\n"
            f"Subdirs: notes/, research/, agent/, assets/, sessions/, logs/.\n"
            f"When user asks to 'save' anything without specifying a path, default to `{_OUT}/notes/` "
            f"for text/docs, or the appropriate subdirectory otherwise."
        )

    # --- ⚠️ TOOL CALL FORMAT REINFORCEMENT (END of prompt — recency weight) ---
    # Second injection of the same format instruction. Models weight instructions at
    # both the beginning and end of system prompts most heavily. Dual placement ensures
    # this is the MOST WEIGHTED instruction in the entire prompt.
    if _active_tool_format:
        parts.append(_active_tool_format)
        # Channel mandate, dead-last: this deployment has no native tool API.
        # Models with heavy native tool-call pretraining sometimes ignore the
        # prompt format and emit their own syntax (function_call blocks,
        # invoke tags, to=functions.* routing); those calls never reach the
        # host. Naming the failure makes channel exclusivity the final,
        # heaviest instruction in the prompt.
        if _active_tool_format not in (_TOOL_FORMAT_NONE, _TOOL_FORMAT_NATIVE):
            parts.append(_TOOL_CHANNEL_MANDATE)

    return "\n\n".join(parts)

