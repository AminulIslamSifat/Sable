
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
    required_sections: list[str] = field(default_factory=list)


_MARKDOWN_INSTRUCTION = (
    "\n\nCRITICAL OUTPUT FORMAT RULES:\n"
    "- You MUST respond with ONLY a clean markdown document as your final answer.\n"
    "- Use proper ## headers, bullet lists, and code blocks where appropriate.\n"
    "- NEVER output JSON objects, JSON arrays, or any structured data format.\n"
    "- NEVER wrap your answer in code fences.\n"
    "- Write it like a human-readable report. Your entire response must be pure markdown."
)

# Universal skill available to ALL agents (not listed per-role)
UNIVERSAL_SKILLS: list[str] = ["execute_command"]

AGENT_ROLES: dict[str, RoleConfig] = {
    "researcher": RoleConfig(
        system_prompt=(
            "You are a research specialist. Search the web, read pages, and produce "
            "concise factual summaries. Always cite sources."
            + _MARKDOWN_INSTRUCTION
            + "\n\nYour final answer MUST include these sections:\n"
            "## Topic\n## Findings\n## Sources\n## Summary\n## Confidence (high/medium/low)"
        ),
        allowed_skills=["execute_command", "online_search", "file_uploader"],
        default_skills=["online_search"],
        default_model="deepseek-instant",
        default_timeout=90,
        max_parallel=4,
        required_sections=["Topic", "Findings", "Summary"],
    ),
    "coder": RoleConfig(
        system_prompt=(
            "You are a code implementation specialist. Write, edit, and test code. "
            "Use early returns, explicit types, clean error handling. No bloated OOP."
            + _MARKDOWN_INSTRUCTION
            + "\n\nYour final answer MUST include these sections:\n"
            "## Description\n## Files Modified (list each file with path and what changed)\n"
            "## Tests (pass/fail/skipped)\n## Notes"
        ),
        allowed_skills=["execute_command", "code_editor", "background_command", "online_search"],
        default_skills=["code_editor", "background_command"],
        default_model="qwen3.7-max",
        default_timeout=180,
        max_parallel=1,
        required_sections=["Description", "Files Modified"],
    ),
    "reviewer": RoleConfig(
        system_prompt=(
            "You are a code review specialist. Read code, identify bugs, suggest fixes. "
            "Do NOT modify files."
            + _MARKDOWN_INSTRUCTION
            + "\n\nYour final answer MUST include these sections:\n"
            "## File Reviewed\n## Critical Issues (with location and explanation)\n"
            "## Warnings\n## Info\n## Verdict (approve/request_changes/needs_discussion)"
        ),
        allowed_skills=["execute_command", "code_editor", "online_search"],
        default_skills=["code_editor"],
        default_model="deepseek-instant",
        default_timeout=60,
        max_parallel=3,
        required_sections=["File Reviewed", "Verdict"],
    ),
    "writer": RoleConfig(
        system_prompt=(
            "You are a documentation and writing specialist. Create clear, structured documents."
            + _MARKDOWN_INSTRUCTION
            + "\n\nYour final answer MUST include these sections:\n"
            "## Title\n## Document Path\n## Structure Overview\n## Word Count\n## Notes"
        ),
        allowed_skills=["execute_command", "code_editor", "online_search"],
        default_skills=["code_editor"],
        default_model="qwen3.7-plus",
        default_timeout=120,
        max_parallel=2,
        required_sections=["Title", "Structure Overview"],
    ),
    "utility": RoleConfig(
        system_prompt=(
            "You are a general-purpose assistant. Handle any miscellaneous task: "
            "file operations, data formatting, quick lookups, renaming, organizing, "
            "conversions, simple scripting — whatever needs doing. Be fast, be precise, "
            "don't overthink it."
            + _MARKDOWN_INSTRUCTION
            + "\n\nYour final answer MUST include these sections:\n"
            "## Task\n## Actions Taken\n## Result\n## Notes"
        ),
        allowed_skills=["execute_command", "code_editor", "background_command", "online_search", "file_uploader"],
        default_skills=["code_editor", "background_command"],
        default_model="deepseek-instant",
        default_timeout=120,
        max_parallel=3,
        required_sections=["Task", "Result"],
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


# Per-role account pools: {role: ["browser-data-acc1", "browser-data-acc2", ...]}
# Agents spawned with the same role cycle through the list incrementally.
_account_pools: dict[str, list[str]] = {}
_account_counters: dict[str, int] = {}


def apply_account_assignments(assignments: dict[str, list[str]]) -> None:
    """Hot-reload per-role account pools from config.

    Accepts {role: [account1, account2, ...]} format.
    Also accepts legacy {role: "single-account"} and wraps it in a list.
    """
    global _account_pools, _account_counters
    _account_pools = {}
    for role, val in (assignments or {}).items():
        if isinstance(val, list):
            _account_pools[role] = val
        elif isinstance(val, str) and val:
            _account_pools[role] = [val]
        else:
            _account_pools[role] = []
    # Reset counters only for roles whose pool changed
    _account_counters = {role: 0 for role in _account_pools}


def get_next_account(role: str) -> str | None:
    """Get the next browser account for a role using round-robin.

    Returns None if no pool is configured (falls back to active/default).
    """
    pool = _account_pools.get(role)
    if not pool:
        return None
    idx = _account_counters.get(role, 0)
    account = pool[idx % len(pool)]
    _account_counters[role] = idx + 1
    return account


def get_account_pool(role: str) -> list[str]:
    """Get the full account pool for a role (for API/config export)."""
    return _account_pools.get(role, [])


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
        required_sections=ov.get("required_sections", ov.get("required_json_keys", base.required_sections)),
    )


def export_roles() -> dict[str, dict]:
    """Export all roles as serializable dicts (with overrides applied)."""
    result = {}
    for name in AGENT_ROLES:
        cfg = get_role_config(name)
        base = AGENT_ROLES[name]
        # Extract the required sections from system prompt
        schema = ""
        if "Your final answer MUST include these sections:" in cfg.system_prompt:
            schema = cfg.system_prompt.split("Your final answer MUST include these sections:\n", 1)[-1].strip()
        result[name] = {
            "system_prompt": cfg.system_prompt,
            "base_prompt": base.system_prompt,
            "output_format": schema,
            "allowed_skills": cfg.allowed_skills,
            "default_skills": cfg.default_skills,
            "default_model": cfg.default_model,
            "default_timeout": cfg.default_timeout,
            "max_parallel": cfg.max_parallel,
            "required_sections": cfg.required_sections,
            "account_pool": get_account_pool(name),
        }
    return result
