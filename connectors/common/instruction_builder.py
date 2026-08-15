
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


def build_instructions(project_id: str | None = None) -> str:
    """Build full system instruction with optional project overrides.

    This is the single source of truth for instruction assembly across all
    API connectors (DeepSeek, Gemini, Mistral). Qwen uses session.py directly.
    Groq/OpenAI use their own minimal prompts and are NOT affected.
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
    _engine = SkillEngine(
        skills_dir=_SKILLS_DIR,
        handlers=HANDLER_MAP,
        agent_id="maria",
    )
    skills_prompt = _engine.get_registry_prompt()

    # Filter disabled skills from global file
    _global_disabled_path = _PROJECT_ROOT / "Brain" / "disabled_skills.json"
    if _global_disabled_path.exists():
        try:
            _gd = json.loads(_global_disabled_path.read_text(encoding="utf-8"))
            if isinstance(_gd, list) and _gd:
                global_config = {k: False for k in _gd}
                skills_prompt = _filter_skills_prompt(skills_prompt, global_config)
        except Exception:
            pass

    if proj and proj.get("skills_config"):
        skills_prompt = _filter_skills_prompt(skills_prompt, proj["skills_config"])
    parts.append(skills_prompt)

    # --- Tool Schemas ---
    try:
        from engine.tools_loader import get_tools_prompt_section
        _disabled_tools_path = _PROJECT_ROOT / "Brain" / "disabled_tools.json"
        _disabled_tools: list[str] = []
        if _disabled_tools_path.exists():
            _dt = json.loads(_disabled_tools_path.read_text(encoding="utf-8"))
            if isinstance(_dt, list):
                _disabled_tools = _dt
        tools_section = get_tools_prompt_section(disabled=_disabled_tools)
        if tools_section:
            parts.append(tools_section)
    except Exception:
        pass

    return "\n\n".join(parts)

