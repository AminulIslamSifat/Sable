"""Per-model instruction settings for Cookbook local models.

Stored in system/cookbook_model_settings.json — cookbook-native, no global touch.
Each model gets: use_maria, use_output_format, skills list, distilled flag.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.cookbook")

_SETTINGS_FILE = Path(__file__).resolve().parent.parent.parent / "system" / "cookbook_model_settings.json"

# Default settings for a new model
_DEFAULTS: dict[str, Any] = {
    "use_maria": True,
    "use_output_format": True,
    "use_memory": True,
    "use_utilities": True,
    "skills": [],
    "tools": [],
    "distilled": False,
}


def _load() -> dict[str, dict[str, Any]]:
    if not _SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_model_settings(model_id: str) -> dict[str, Any]:
    """Get settings for a model, merging with defaults for missing keys."""
    all_settings = _load()
    stored = all_settings.get(model_id)
    if stored is None:
        return dict(_DEFAULTS)
    # Merge: defaults first, then stored overrides (handles missing keys like use_memory)
    merged = dict(_DEFAULTS)
    merged.update(stored)
    return merged


def get_all_model_settings() -> dict[str, dict[str, Any]]:
    """Get all model settings."""
    return _load()


def update_model_settings(model_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Update settings for a model. Returns the merged result."""
    all_settings = _load()
    current = all_settings.get(model_id, dict(_DEFAULTS))

    for key in ("use_maria", "use_output_format", "use_memory", "use_utilities", "distilled"):
        if key in updates:
            current[key] = bool(updates[key])
    if "skills" in updates:
        current["skills"] = [s for s in updates["skills"] if isinstance(s, str)]
    if "tools" in updates:
        current["tools"] = [t for t in updates["tools"] if isinstance(t, str)]

    all_settings[model_id] = current
    _save(all_settings)
    logger.info("Updated cookbook model settings for %s: %s", model_id, current)
    return current


def delete_model_settings(model_id: str) -> bool:
    """Remove settings for a model (e.g. when server is stopped permanently)."""
    all_settings = _load()
    if model_id in all_settings:
        del all_settings[model_id]
        _save(all_settings)
        return True
    return False


def build_system_prompt(model_id: str) -> str | None:
    """Build the system prompt for a local model based on its settings.

    Returns None if distilled=False and no instruction files are selected.
    Returns the groq-style minimal prompt if distilled=True.
    """
    settings = get_model_settings(model_id)

    if settings.get("distilled"):
        return _distilled_prompt()

    parts: list[str] = []
    instruction_dir = Path(__file__).resolve().parent.parent.parent / "instruction"

    if settings.get("use_maria"):
        # Load active persona from config (falls back to Maria if no config)
        _pcfg_path = instruction_dir / ".persona_config.json"
        _active = "Maria"
        _disabled: list = []
        if _pcfg_path.exists():
            try:
                import json as _json
                _pc = _json.loads(_pcfg_path.read_text(encoding="utf-8"))
                _active = _pc.get("active") or "Maria"
                _disabled = _pc.get("disabled", [])
            except Exception:
                pass
        if _active not in _disabled:
            persona_path = instruction_dir / f"{_active}.md"
            if persona_path.is_file():
                parts.append(persona_path.read_text(encoding="utf-8").strip())

    if settings.get("use_output_format"):
        fmt_path = instruction_dir / "output_format.md"
        if fmt_path.is_file():
            parts.append(fmt_path.read_text(encoding="utf-8").strip())

    # Load selected skill instructions
    skills_dir = Path(__file__).resolve().parent.parent.parent / "skills"
    for skill_key in settings.get("skills", []):
        instr_path = skills_dir / skill_key / "instruction.md"
        if instr_path.is_file():
            try:
                skill_content = instr_path.read_text(encoding="utf-8").strip()
                skill_content = skill_content.replace("SKILL_DIR", str(skills_dir / skill_key / "scripts"))
                parts.append(f"## Skill: {skill_key}\n{skill_content}")
            except OSError:
                pass

    # Load selected tool schemas as instruction
    tools_dir = Path(__file__).resolve().parent.parent.parent / "tools"
    for tool_key in settings.get("tools", []):
        tool_json_path = tools_dir / tool_key / "tool.json"
        if tool_json_path.is_file():
            try:
                import json as _json
                schema = _json.loads(tool_json_path.read_text(encoding="utf-8"))
                func_names = [f.get("name", "?") for f in schema] if isinstance(schema, list) else []
                parts.append(
                    f"## Tool: {tool_key}\n"
                    f"Available functions: {', '.join(func_names)}\n"
                    f"Use native function calling for these tools."
                )
            except (OSError, Exception):
                pass

    if not parts:
        return None
    return "\n\n***\n\n".join(parts)


def _distilled_prompt() -> str:
    """Minimal agentic prompt — same philosophy as groq connector."""
    base = (
        "CRITICAL RULE: Every response may contain exactly ONE <tool_call> opening tag and ONE </tool_call> closing tag.\n"
        "The extractor only reads what is inside <tool_call>; anything outside is prose.\n\n"
        "Single call: <tool_call>{\"name\": \"grep\", ...}</tool_call>\n"
        "Multiple calls: <tool_call>[{\"name\": \"grep\", ...}, {\"name\": \"view_file\", ...}]</tool_call>\n"
        "NEVER output multiple separate <tool_call> blocks. Always wrap ALL calls in ONE array inside ONE wrapper.\n\n"
        "If you use <tool_call>, keep prose to ONE short sentence before the block. "
        "<tool_call> appears only in plain text, never inside a fenced code block."
    )
    editor = """# File I/O

## Read files
<tool_call>{"name": "get_file", "arguments": {"path": "/abs/path"}}</tool_call> — read any file
<tool_call>{"name": "view_file", "arguments": {"path": "/abs/path", "start": 1, "end": 50}}</tool_call> — read with line numbers

## Write files
<tool_call>{"name": "edit_file", "arguments": {"path": "/abs/path", "old_str": "...", "new_str": "..."}}</tool_call>
<<<<<<< SEARCH
exact old text from view_file
=======
new replacement text
>>>>>>
 — replace text (must match exactly once)

<tool_call>{"name": "create_file", "arguments": {"path": "/abs/path", "content": "..."}}</tool_call>
file content here
 — create new file (fails if exists)

## Rules
- Always <view_file before editing — never build old_str from memory
- Wrap every tag in <tool_call>...</tool_call>
- One short sentence + the <tool_call> block, nothing else"""
    return base + "\n\n***\n\n" + editor
