
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoleConfig:
    system_prompt: str
    allowed_skills: list[str]       # all skills the agent CAN use
    default_skills: list[str]       # subset auto-loaded with instruction.md
    default_model: str
    default_timeout: float
    max_parallel: int
    required_json_keys: list[str] = field(default_factory=list)


_JSON_INSTRUCTION = (
    "\n\nYou MUST respond with a single valid JSON object as your final answer. "
    "No markdown, no prose outside the JSON. The JSON schema is specified per task."
)

# Universal skill available to ALL agents (not listed per-role)
UNIVERSAL_SKILLS: list[str] = ["execute_command"]

AGENT_ROLES: dict[str, RoleConfig] = {
    "researcher": RoleConfig(
        system_prompt=(
            "You are a research specialist. Search the web, read pages, and produce "
            "concise factual summaries. Always cite sources."
            + _JSON_INSTRUCTION
            + "\nFinal answer schema:\n"
            '{"topic": str, "sources": [str], "findings": [str], "summary": str, "confidence": "high|medium|low"}'
        ),
        allowed_skills=["execute_command", "online_search", "file_uploader"],
        default_skills=["online_search"],
        default_model="deepseek-instant",
        default_timeout=90,
        max_parallel=4,
        required_json_keys=["topic", "findings", "summary"],
    ),
    "coder": RoleConfig(
        system_prompt=(
            "You are a code implementation specialist. Write, edit, and test code. "
            "Use early returns, explicit types, clean error handling. No bloated OOP."
            + _JSON_INSTRUCTION
            + "\nFinal answer schema:\n"
            '{"description": str, "files_modified": [{"path": str, "lines": str, "change": str}], '
            '"tests": "pass|fail|skipped", "notes": str}'
        ),
        allowed_skills=["execute_command", "code_editor", "background_command", "online_search"],
        default_skills=["code_editor", "background_command"],
        default_model="qwen3.7-max",
        default_timeout=180,
        max_parallel=1,
        required_json_keys=["description", "files_modified"],
    ),
    "reviewer": RoleConfig(
        system_prompt=(
            "You are a code review specialist. Read code, identify bugs, suggest fixes. "
            "Do NOT modify files."
            + _JSON_INSTRUCTION
            + "\nFinal answer schema:\n"
            '{"file": str, "critical": [{"issue": str, "location": str, "explanation": str}], '
            '"warnings": [{"issue": str, "location": str}], "info": [str], '
            '"verdict": "approve|request_changes|needs_discussion"}'
        ),
        allowed_skills=["execute_command", "code_editor", "online_search"],
        default_skills=["code_editor"],
        default_model="deepseek-instant",
        default_timeout=60,
        max_parallel=3,
        required_json_keys=["file", "verdict"],
    ),
    "writer": RoleConfig(
        system_prompt=(
            "You are a documentation and writing specialist. Create clear, structured documents."
            + _JSON_INSTRUCTION
            + "\nFinal answer schema:\n"
            '{"title": str, "path": str, "structure": [str], "word_count": int, "notes": str}'
        ),
        allowed_skills=["execute_command", "code_editor", "online_search"],
        default_skills=["code_editor"],
        default_model="deepseek-expert",
        default_timeout=120,
        max_parallel=2,
        required_json_keys=["title", "structure"],
    ),
    "utility": RoleConfig(
        system_prompt=(
            "You are a general-purpose assistant. Handle any miscellaneous task: "
            "file operations, data formatting, quick lookups, renaming, organizing, "
            "conversions, simple scripting — whatever needs doing. Be fast, be precise, "
            "don't overthink it."
            + _JSON_INSTRUCTION
            + "\nFinal answer schema:\n"
            '{"task": str, "actions_taken": [str], "result": str, "notes": str}'
        ),
        allowed_skills=["execute_command", "code_editor", "background_command", "online_search", "file_uploader"],
        default_skills=["code_editor", "background_command"],
        default_model="deepseek-instant",
        default_timeout=120,
        max_parallel=3,
        required_json_keys=["task", "result"],
    ),
}


# --------------------------------------------------------------------------
# Runtime overrides (loaded from agent_config.json "roles" section)
# --------------------------------------------------------------------------

_role_overrides: dict[str, dict] = {}
_universal_overrides: list[str] | None = None


def apply_role_overrides(overrides: dict[str, dict], universal: list[str] | None = None) -> None:
    """Hot-reload role overrides from config. Called on PUT /api/agents/config."""
    global _role_overrides, _universal_overrides
    _role_overrides = overrides or {}
    _universal_overrides = universal


def get_universal_skills() -> list[str]:
    """Get universal skills (config override or hardcoded default)."""
    if _universal_overrides is not None:
        return _universal_overrides
    return UNIVERSAL_SKILLS


def get_role_config(role: str) -> RoleConfig:
    """Get role config with any file-based overrides applied."""
    base = AGENT_ROLES.get(role, AGENT_ROLES["researcher"])
    ov = _role_overrides.get(role)
    if not ov:
        return base
    # Merge: override fields replace base fields
    return RoleConfig(
        system_prompt=ov.get("system_prompt", base.system_prompt),
        allowed_skills=ov.get("allowed_skills", base.allowed_skills),
        default_skills=ov.get("default_skills", base.default_skills),
        default_model=ov.get("default_model", base.default_model),
        default_timeout=ov.get("default_timeout", base.default_timeout),
        max_parallel=ov.get("max_parallel", base.max_parallel),
        required_json_keys=ov.get("required_json_keys", base.required_json_keys),
    )


def export_roles() -> dict[str, dict]:
    """Export all roles as serializable dicts (with overrides applied)."""
    result = {}
    for name in AGENT_ROLES:
        cfg = get_role_config(name)
        base = AGENT_ROLES[name]
        # Extract the output format (JSON schema line) from system prompt
        schema = ""
        if "Final answer schema:" in cfg.system_prompt:
            schema = cfg.system_prompt.split("Final answer schema:\n", 1)[-1].strip()
        result[name] = {
            "system_prompt": cfg.system_prompt,
            "base_prompt": base.system_prompt,
            "output_format": schema,
            "allowed_skills": cfg.allowed_skills,
            "default_skills": cfg.default_skills,
            "default_model": cfg.default_model,
            "default_timeout": cfg.default_timeout,
            "max_parallel": cfg.max_parallel,
            "required_json_keys": cfg.required_json_keys,
        }
    return result
