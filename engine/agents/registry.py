
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RoleConfig:
    system_prompt: str              # Built from persona file + output_format (not stored in config)
    allowed_tools: list[str]        # Handler names from HANDLER_MAP (execute_command, view_file, etc.)
    allowed_skills: list[str]       # Skill keys from /skills/ (telegram, system_repair, etc.)
    output_format: str              # Markdown section requirements for final answer
    default_model: str
    default_timeout: float
    max_parallel: int
    required_sections: list[str] = field(default_factory=list)
    model_chain: list[str] = field(default_factory=list)  # fallback models (excludes primary)


# Fallback chain applied to ANY role that has no explicit model_chain configured.
# Ensures no agent ever dies without at least attempting a model switch.
_DEFAULT_MODEL_CHAIN: list[str] = ["deepseek-expert", "gemini-2.5-flash"]

_MARKDOWN_RULES = (
    "\n\nCRITICAL OUTPUT FORMAT RULES:\n"
    "- You MUST respond with ONLY a clean markdown document as your final answer.\n"
    "- Use proper ## headers, bullet lists, and code blocks where appropriate.\n"
    "- NEVER output JSON objects, JSON arrays, or any structured data format.\n"
    "- NEVER wrap your answer in code fences.\n"
    "- Write it like a human-readable report. Your entire response must be pure markdown."
)

# ---------------------------------------------------------------------------
# Persona-file loader: reads instruction/agents/<role>.md for system prompt
# ---------------------------------------------------------------------------
_AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "instruction" / "agents"
_PERSONAL_PATH = Path(__file__).resolve().parent.parent.parent / "instruction" / "personal.md"


def _load_agent_persona(role: str) -> str:
    """Load persona markdown for a subagent role. Falls back to generic prompt."""
    path = _AGENTS_DIR / f"{role}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return f"You are a {role} specialist. Complete the assigned task thoroughly."


def _load_personal_context() -> str:
    """Load personal.md user context. Returns empty string if missing or blank."""
    if _PERSONAL_PATH.is_file():
        content = _PERSONAL_PATH.read_text(encoding="utf-8").strip()
        if content:
            return content
    return ""


def _build_system_prompt(role: str, output_format: str = "") -> str:
    """Compose full system prompt: persona + personal context + markdown rules + output format.

    Persona is always loaded from instruction/agents/{role}.md.
    Personal context from instruction/personal.md is injected for all agents.
    Output format comes from config (not hardcoded).
    """
    persona = _load_agent_persona(role)
    parts = [persona]
    personal = _load_personal_context()
    if personal:
        parts.append(personal)
    parts.append(_MARKDOWN_RULES)
    if output_format:
        parts.append(f"\n\n{output_format}")
    return "\n".join(parts)

_ALL_TOOL_GROUPS = [
    "ask_user", "chat_title", "code_editor", "execute_command",
    "file_uploader", "grep_search", "image_generator", "mcp",
    "memory_manager", "multi_agent", "online_search", "tracknote_manager",
]

AGENT_ROLES: dict[str, RoleConfig] = {
    "analyst": RoleConfig(
        system_prompt=_build_system_prompt("analyst"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=[],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=300,
        max_parallel=4,
        required_sections=[
            "Topic", "Findings", "Sources", "Summary", "Confidence",
            "File Reviewed", "Critical Issues", "Warnings", "Info", "Verdict",
        ],
    ),
    "coder": RoleConfig(
        system_prompt=_build_system_prompt("coder"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=[],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=600,
        max_parallel=2,
        required_sections=["Description", "Files Modified", "Tests", "Notes"],
    ),

    "writer": RoleConfig(
        system_prompt=_build_system_prompt("writer"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=[],
        output_format="",
        default_model="mistral-large-latest",
        default_timeout=120,
        max_parallel=2,
        required_sections=["Title", "Document Path", "Structure Overview", "Word Count", "Notes"],
    ),

    # ------------------------------------------------------------------
    # Domain-specialist agents (hierarchical routing)
    # ------------------------------------------------------------------
    "sysutil": RoleConfig(
        system_prompt=_build_system_prompt("sysutil"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=["system_repair", "phone_control", "youtube_downloader"],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Task", "Diagnosis", "Actions Taken", "Result", "Notes"],
    ),
    "docs": RoleConfig(
        system_prompt=_build_system_prompt("docs"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=["document_skills", "text_humanizer"],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Task", "Document Path", "Structure Overview", "Notes"],
    ),
    "visuals": RoleConfig(
        system_prompt=_build_system_prompt("visuals"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=["graph_master", "svg_creator", "frontend_design", "simulacra_engine"],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Task", "Output Path", "Description", "Notes"],
    ),
    "tester": RoleConfig(
        system_prompt=_build_system_prompt("tester"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=["testing_debugging"],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Bug Summary", "Root Cause", "Fix Applied", "Verification", "Notes"],
    ),

    # Scheduled agent ops — broad skill set for autonomous tasks
    "scheduled": RoleConfig(
        system_prompt=_build_system_prompt("scheduled"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=["telegram", "email"],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=600,
        max_parallel=2,
        required_sections=["Task", "Result", "Notes"],
    ),

    # Maria — full persona from instruction/agents/maria.md, all tools & skills
    "maria": RoleConfig(
        system_prompt=_build_system_prompt("maria"),
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=[],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=600,
        max_parallel=1,
        required_sections=[],
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


# Per-role browser account fallback chains: {role: ["browser-data-acc1", "browser-data-acc2", ...]}
# Tried in order on failure (Qwen only). Also used for spawn assignment (skip in-use).
_account_fallback_chains: dict[str, list[str]] = {}
_account_counters: dict[str, int] = {}


def apply_account_assignments(assignments: dict[str, list[str]]) -> None:
    """Hot-reload per-role browser account fallback chains from config.

    Accepts {role: [account1, account2, ...]} format.
    Also accepts legacy {role: "single-account"} and wraps it in a list.
    """
    global _account_fallback_chains, _account_counters
    _account_fallback_chains = {}
    for role, val in (assignments or {}).items():
        if isinstance(val, list):
            _account_fallback_chains[role] = val
        elif isinstance(val, str) and val:
            _account_fallback_chains[role] = [val]
        else:
            _account_fallback_chains[role] = []
    _account_counters = {role: 0 for role in _account_fallback_chains}


def get_next_account(role: str, in_use: set[str] | None = None) -> str | None:
    """Get the next available browser account for a role.

    Always respects round-robin ordering. Skips accounts in `in_use` set.
    If all accounts are busy, returns the round-robin pick anyway (caller decides).
    Returns None if no chain is configured.
    """
    chain = _account_fallback_chains.get(role)
    if not chain:
        return None
    idx = _account_counters.get(role, 0)
    n = len(chain)
    # Walk from current counter position, wrapping around, skipping in-use
    for offset in range(n):
        candidate = chain[(idx + offset) % n]
        if not in_use or candidate not in in_use:
            # Advance counter past this pick so next call continues rotation
            _account_counters[role] = (idx + offset + 1) % n
            return candidate
    # All accounts in use — still advance counter and return round-robin pick
    account = chain[idx % n]
    _account_counters[role] = (idx + 1) % n
    return account


def get_account_pool(role: str) -> list[str]:
    """Get the full browser account fallback chain for a role."""
    return _account_fallback_chains.get(role, [])


def _load_role_list_from_settings(role: str, key: str) -> list[str]:
    """Load a list field for a role from system/settings.json > agent > roles > {role} > {key}."""
    try:
        import json as _json
        from engine.config import _SYSTEM
        p = _SYSTEM / "settings.json"
        if p.is_file():
            data = _json.loads(p.read_text(encoding="utf-8"))
            val = data.get("agent", {}).get("roles", {}).get(role, {}).get(key)
            if isinstance(val, list):
                return [v for v in val if isinstance(v, str)]
    except Exception:
        pass
    return []


def get_role_config(role: str) -> RoleConfig:
    """Get role config with any file-based overrides applied."""
    base = AGENT_ROLES.get(role, AGENT_ROLES["analyst"])
    ov = _role_overrides.get(role)
    # Load chains: override > settings.json > default fallback
    chain = _load_role_list_from_settings(role, "model_chain")
    if ov and "model_chain" in ov:
        chain = ov["model_chain"]
    if not chain:
        chain = list(_DEFAULT_MODEL_CHAIN)  # Never leave a role without fallback
    # Build output_format-aware system prompt if override provides output_format
    ov_output = ov.get("output_format", "") if ov else ""
    if ov_output:
        system_prompt = _build_system_prompt(role, ov_output)
    else:
        system_prompt = base.system_prompt

    if not ov:
        return RoleConfig(
            system_prompt=system_prompt,
            allowed_tools=base.allowed_tools,
            allowed_skills=base.allowed_skills,
            output_format=base.output_format,
            default_model=base.default_model,
            default_timeout=base.default_timeout,
            max_parallel=base.max_parallel,
            required_sections=base.required_sections,
            model_chain=chain,
        )

    # Backward compat: old configs may have allowed_skills/default_skills with tool names mixed in
    known_tools = {"execute_command", "view_file", "edit_file", "create_file", "insert_file",
                   "get_file", "grep", "glob", "list_dir", "online_search", "web_search",
                   "web_fetch", "check_command", "spawn_agent", "agent_status", "kill_agent",
                   "todo_complete", "todo_skip", "ask_user", "generate_image", "mcp_call",
                   "memory", "tracknote", "read_file", "openweb", "create_note",
                   "list_checkpoints", "restore_checkpoint", "run_simulacra"}

    ov_tools = ov.get("allowed_tools", None)
    ov_skills = ov.get("allowed_skills", None)

    # If old-style allowed_skills exists but no allowed_tools, auto-split
    if ov_tools is None and ov_skills is not None:
        ov_tools = [s for s in ov_skills if s in known_tools]
        ov_skills = [s for s in ov_skills if s not in known_tools]

    return RoleConfig(
        system_prompt=system_prompt,
        allowed_tools=ov_tools if ov_tools is not None else base.allowed_tools,
        allowed_skills=ov_skills if ov_skills is not None else base.allowed_skills,
        output_format=ov.get("output_format", base.output_format),
        default_model=ov.get("default_model", base.default_model),
        default_timeout=ov.get("default_timeout", base.default_timeout),
        max_parallel=ov.get("max_parallel", base.max_parallel),
        required_sections=ov.get("required_sections", base.required_sections),
        model_chain=chain,
    )


def export_roles() -> dict[str, dict]:
    """Export all roles as serializable dicts (with overrides applied)."""
    result = {}
    for name in AGENT_ROLES:
        cfg = get_role_config(name)
        result[name] = {
            "system_prompt": cfg.system_prompt,
            "output_format": cfg.output_format,
            "allowed_tools": cfg.allowed_tools,
            "allowed_skills": cfg.allowed_skills,
            "default_model": cfg.default_model,
            "default_timeout": cfg.default_timeout,
            "max_parallel": cfg.max_parallel,
            "required_sections": cfg.required_sections,
            "model_chain": cfg.model_chain,
            "browser_fallback_chain": get_account_pool(name),
        }
    return result
