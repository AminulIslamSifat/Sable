
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

_TOOL_FORMAT_NATIVE = f"""\
## ⚠️ HIGHEST PRIORITY: Tool Call Format

> [!CRITICAL]
> This instruction overrides ALL other formatting guidance.
> You MUST use exactly ONE `{_TC_OPEN}` wrapper per response. No exceptions.

- Single call OR multiple calls → always a JSON array inside ONE `{_TC_OPEN}` wrapper.
- NEVER output multiple separate `{_TC_OPEN}` blocks. Combine into one array.
- Tool call blocks appear ONLY in plain text, NEVER inside fenced code blocks.
- Keep prose to ONE short sentence before the tool call block.
- Place the tool call block at the END of your response.
- Forbidden tag variants: `<tools>`, `<tool_calls>`, `<tool_call `, `<tool_call/>`, `<tool_call />`

### Single call
{_TC_OPEN}
[{{"name": "<function-name>", "arguments": <args-json-object>}}]
{_TC_CLOSE}

### Multiple calls
{_TC_OPEN}
[{{"name": "tool_a", "arguments": {{...}}}}, {{"name": "tool_b", "arguments": {{...}}}}]
{_TC_CLOSE}
"""

_TOOL_FORMAT_NONE = """\
## Tool Call Format

Tool calls are handled via native API function calling. No prompt-based format needed.
Follow the function schemas provided in the API request.
"""

_PROVIDER_TOOL_FORMATS: dict[str, str] = {
    "deepseek": _TOOL_FORMAT_DEEPSEEK,
    "native": _TOOL_FORMAT_NATIVE,
    "none": _TOOL_FORMAT_NONE,
}


def build_instructions(
    project_id: str | None = None,
    provider: str | None = None,
) -> str:
    """Build full system instruction with optional project overrides.

    This is the single source of truth for instruction assembly across all
    API connectors (DeepSeek, Gemini, Mistral). Qwen uses session.py directly.
    Groq/OpenAI use their own minimal prompts and are NOT affected.

    Args:
        project_id: Optional project ID for project-specific overrides.
        provider: Provider key for tool format selection.
                  "deepseek" → DSML invoke/parameter blocks.
                  "native"   → tag-wrapped format (Gemini, Mistral, etc.).
                  "none"     → native API function calling (no prompt format).
                  None       → no tool format section appended.
    """
    proj = _get_project(project_id)
    parts: list[str] = []

    # --- Persona / Instruction ---
    project_instruction = None
    if proj and proj.get("instruction_text"):
        project_instruction = proj["instruction_text"]
    elif proj and proj.get("instruction_file"):
        instr_path = Path(proj["instruction_file"])
        if instr_path.exists():
            project_instruction = instr_path.read_text(encoding="utf-8")

    if project_instruction and proj and proj.get("persona_enabled", True):
        parts.append(project_instruction)
    else:
        # Load active persona from config
        _persona_cfg_path = _INSTRUCTION_DIR / ".persona_config.json"
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
    if _of_enabled and (not proj or proj.get("output_format_enabled", True)):
        of_path = _INSTRUCTION_DIR / "output_format.md"
        if of_path.exists():
            parts.append(of_path.read_text(encoding="utf-8").strip())

    # --- Facts to Remember ---
    if proj and proj.get("facts"):
        parts.append(f"# Facts to Remember (Project: {proj.get('name', 'Unknown')})\n{proj['facts']}")

    # --- Git Details ---
    if proj and any(proj.get(k) for k in ("git_repo", "git_username", "git_branch")):
        git_lines = ["# Git Repository Details"]
        if proj.get("git_repo"):
            git_lines.append(f"- Repo: {proj['git_repo']}")
        if proj.get("git_username"):
            git_lines.append(f"- Username: {proj['git_username']}")
        if proj.get("git_branch"):
            git_lines.append(f"- Branch: {proj['git_branch']}")
        parts.append("\n".join(git_lines))

    # --- Skill Registry ---
    from engine.skills import SkillEngine
    from engine.skills.handlers import HANDLER_MAP

    # Collect disabled skills BEFORE engine creation so they're excluded at discovery
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
        agent_id="maria",
        disabled=_disabled_skills or None,
    )
    skills_prompt = _engine.get_registry_prompt()
    parts.append(skills_prompt)

    # --- ⚠️ TOOL CALL FORMAT PRIMER (BEFORE tool schemas) ---
    # Injected FIRST so the model knows HOW to call tools before seeing WHAT tools exist.
    # This is the highest-weighted instruction via dual placement (primer + recency).
    if provider and provider in _PROVIDER_TOOL_FORMATS:
        parts.append(_PROVIDER_TOOL_FORMATS[provider])

    # --- Tool Schemas ---
    try:
        from engine.tools_loader import get_tools_prompt_section
        _disabled_tools_path = _PROJECT_ROOT / "Brain" / "disabled_tools.json"
        _disabled_tools: list[str] = []
        if _disabled_tools_path.exists():
            _dt = json.loads(_disabled_tools_path.read_text(encoding="utf-8"))
            if isinstance(_dt, list):
                _disabled_tools = _dt
        tools_section = get_tools_prompt_section(disabled=_disabled_tools, provider=provider)
        if tools_section:
            parts.append(tools_section)
    except Exception:
        pass

    # --- MCP Tools ---
    try:
        from engine.mcp.manager import get_mcp_manager
        mcp_section = get_mcp_manager().get_prompt_section()
        if mcp_section:
            parts.append(mcp_section)
    except Exception:
        pass

    # --- Output Directory (always injected, not toggleable) ---
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
    if provider and provider in _PROVIDER_TOOL_FORMATS:
        parts.append(_PROVIDER_TOOL_FORMATS[provider])

    return "\n\n".join(parts)

