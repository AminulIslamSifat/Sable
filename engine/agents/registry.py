from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoleConfig:
    system_prompt: str              # Built lazily via shared instruction builder
    allowed_tools: list[str]        # Tool group keys (execute_command, grep_search, etc.)
    allowed_skills: list[str]       # Skill keys from /skills/ (telegram, system_repair, etc.)
    output_format: str              # Markdown section requirements for final answer
    default_model: str
    default_timeout: float
    max_parallel: int
    required_sections: list[str] = field(default_factory=list)
    model_chain: list[str] = field(default_factory=list)  # fallback models (excludes primary)


# Fallback chain applied to ANY role that has no explicit model_chain configured.
_DEFAULT_MODEL_CHAIN: list[str] = ["deepseek-expert", "gemini-2.5-flash"]

_ALL_TOOL_GROUPS = [
    "ask_user", "chat_title", "code_editor", "execute_command",
    "file_uploader", "grep_search", "image_generator", "mcp",
    "memory_manager", "multi_agent", "online_search", "tracknote_manager",
]

AGENT_ROLES: dict[str, RoleConfig] = {
    "analyst": RoleConfig(
        system_prompt="",
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
        system_prompt="",
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=[],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=600,
        max_parallel=2,
        required_sections=["Description", "Files Modified", "Tests", "Notes"],
    ),

    "writer": RoleConfig(
        system_prompt="",
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
        system_prompt="",
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=["system_repair", "phone_control", "youtube_downloader"],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Task", "Diagnosis", "Actions Taken", "Result", "Notes"],
    ),
    "docs": RoleConfig(
        system_prompt="",
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=["document_skills", "text_humanizer"],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Task", "Document Path", "Structure Overview", "Notes"],
    ),
    "visuals": RoleConfig(
        system_prompt="",
        allowed_tools=list(_ALL_TOOL_GROUPS),
        allowed_skills=["graph_master", "svg_creator", "frontend_design", "simulacra_engine"],
        output_format="",
        default_model="qwen3.8-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Task", "Output Path", "Description", "Notes"],
    ),
    "tester": RoleConfig(
        system_prompt="",
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
        system_prompt="",
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
        system_prompt="",
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

    Searches from the back (highest number first) to avoid competing with
    main chat auto-switch which searches forward. Falls back to the global
    reverse pool if the role-specific chain is empty or exhausted.
    Skips accounts that are in-use, rate-limit exhausted, or captcha-blocked.
    Returns None if nothing is available.
    """
    from engine.config import is_account_exhausted, is_account_captcha_blocked
    from pathlib import PurePosixPath

    # Normalize in_use to bare account names (callers may pass full paths)
    _in_use_names: set[str] = set()
    if in_use:
        for entry in in_use:
            _in_use_names.add(PurePosixPath(entry).name if "/" in str(entry) else entry)

    chain = _account_fallback_chains.get(role)
    if chain:
        # Search role-specific chain in reverse order, skipping in-use/exhausted/blocked
        for candidate in reversed(chain):
            if candidate in _in_use_names:
                continue
            if is_account_exhausted(candidate):
                continue
            if is_account_captcha_blocked(candidate):
                continue
            return candidate

    # Fall back to global reverse pool (highest number first)
    from engine.config import get_available_accounts_reverse
    for acc_name in get_available_accounts_reverse(exclude=_in_use_names, limit=5):
        return acc_name

    return None


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
    """Get role config with any file-based overrides applied.

    System prompt is built lazily via the shared instruction builder
    (connectors/common/instruction_builder.py) so subagents use the same
    pipeline as main chat — persona + personal.md + output_format + skills + tools.
    """
    base = AGENT_ROLES.get(role, AGENT_ROLES["analyst"])
    ov = _role_overrides.get(role)
    # Load chains: override > settings.json > default fallback
    chain = _load_role_list_from_settings(role, "model_chain")
    if ov and "model_chain" in ov:
        chain = ov["model_chain"]
    if not chain:
        chain = list(_DEFAULT_MODEL_CHAIN)  # Never leave a role without fallback

    # Resolve effective tools/skills (override > base)
    known_tools = {"execute_command", "view_file", "edit_file", "create_file", "insert_file",
                   "get_file", "grep", "glob", "list_dir", "online_search", "web_search",
                   "web_fetch", "check_command", "spawn_agent", "agent_status", "kill_agent",
                   "todo_complete", "todo_skip", "ask_user", "generate_image", "mcp_call",
                   "memory", "tracknote", "read_file", "openweb", "create_note",
                   "list_checkpoints", "restore_checkpoint", "run_simulacra"}

    ov_tools = ov.get("allowed_tools", None) if ov else None
    ov_skills = ov.get("allowed_skills", None) if ov else None

    # If old-style allowed_skills exists but no allowed_tools, auto-split
    if ov_tools is None and ov_skills is not None:
        ov_tools = [s for s in ov_skills if s in known_tools]
        ov_skills = [s for s in ov_skills if s not in known_tools]

    eff_tools = ov_tools if ov_tools is not None else base.allowed_tools
    eff_skills = ov_skills if ov_skills is not None else base.allowed_skills
    eff_output = ov.get("output_format", base.output_format) if ov else base.output_format

    # Build system prompt via shared instruction builder
    from connectors.common.instruction_builder import build_instructions
    system_prompt = build_instructions(
        agent_role=role,
        agent_tools=eff_tools,
        agent_skills=eff_skills,
    )
    # Append role-specific output format if configured
    if eff_output:
        system_prompt += f"\n\n{eff_output}"

    return RoleConfig(
        system_prompt=system_prompt,
        allowed_tools=eff_tools,
        allowed_skills=eff_skills,
        output_format=eff_output,
        default_model=ov.get("default_model", base.default_model) if ov else base.default_model,
        default_timeout=ov.get("default_timeout", base.default_timeout) if ov else base.default_timeout,
        max_parallel=ov.get("max_parallel", base.max_parallel) if ov else base.max_parallel,
        required_sections=ov.get("required_sections", base.required_sections) if ov else base.required_sections,
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
