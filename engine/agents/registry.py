
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
    model_chain: list[str] = field(default_factory=list)  # fallback models (excludes primary)


# Fallback chain applied to ANY role that has no explicit model_chain configured.
# Ensures no agent ever dies without at least attempting a model switch.
_DEFAULT_MODEL_CHAIN: list[str] = ["deepseek-expert", "gemini-2.5-flash"]

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
    "analyst": RoleConfig(
        system_prompt=(
            "You are an analysis specialist. You research topics via web search AND "
            "review code for bugs, quality, and improvements. When researching, always "
            "cite sources. When reviewing, be specific about locations and fixes. "
            "Adapt your output format to the task type."
            + _MARKDOWN_INSTRUCTION
            + "\n\nOUTPUT FORMAT FOR RESEARCH TASKS:\n"
            "## Topic\n## Findings\n## Sources\n## Summary\n## Confidence (high/medium/low)\n\n"
            "OUTPUT FORMAT FOR CODE REVIEW TASKS:\n"
            "## File Reviewed\n## Critical Issues (with location and explanation)\n"
            "## Warnings\n## Info\n## Verdict (approve/request_changes/needs_discussion)\n\n"
            "Do NOT use these headers in intermediate responses. "
            "Intermediate response = one brief sentence + tool call. Nothing else."
        ),
        allowed_skills=["execute_command", "online_search", "code_editor", "file_uploader"],
        default_skills=["online_search", "code_editor"],
        default_model="qwen3.7-max",
        default_timeout=300,
        max_parallel=4,
        required_sections=[
            "Topic", "Findings", "Sources", "Summary", "Confidence",
            "File Reviewed", "Critical Issues", "Warnings", "Info", "Verdict",
        ],
    ),
    "coder": RoleConfig(
        system_prompt=(
            "You are a code implementation specialist. Write, edit, and test code. "
            "Use early returns, explicit types, clean error handling. No bloated OOP."
            + _MARKDOWN_INSTRUCTION
            + "\n\nOUTPUT FORMAT (applies ONLY to your very last response, after all tool work is complete):\n"
            "Your final answer MUST include these sections:\n"
            "## Description\n## Files Modified (list each file with path and what changed)\n"
            "## Tests (pass/fail/skipped)\n## Notes\n\n"
            "Do NOT use these headers in intermediate responses. "
            "Intermediate response = one brief sentence + tool call. Nothing else."
        ),
        allowed_skills=["execute_command", "code_editor", "online_search"],
        default_skills=["code_editor"],
        default_model="qwen3.7-max",
        default_timeout=300,
        max_parallel=1,
        required_sections=["Description", "Files Modified", "Tests", "Notes"],
    ),

    "writer": RoleConfig(
        system_prompt=(
            "You are a documentation and writing specialist. Create clear, structured documents."
            + _MARKDOWN_INSTRUCTION
            + "\n\nOUTPUT FORMAT (applies ONLY to your very last response, after all tool work is complete):\n"
            "Your final answer MUST include these sections:\n"
            "## Title\n## Document Path\n## Structure Overview\n## Word Count\n## Notes\n\n"
            "Do NOT use these headers in intermediate responses. "
            "Intermediate response = one brief sentence + tool call. Nothing else."
        ),
        allowed_skills=["execute_command", "code_editor", "online_search"],
        default_skills=["code_editor"],
        default_model="qwen3.7-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Title", "Document Path", "Structure Overview", "Word Count", "Notes"],
    ),

    # ------------------------------------------------------------------
    # Domain-specialist agents (hierarchical routing)
    # ------------------------------------------------------------------
    "sysutil": RoleConfig(
        system_prompt=(
            "You are a system, media & general utility specialist. You handle OS-level repairs "
            "(Hyprland, pacman, systemd, Wayland, display issues), Android phone "
            "automation via ADB, video/audio downloads from any platform, "
            "long-running background processes, AND miscellaneous tasks like file operations, "
            "data formatting, conversions, renaming, organizing, and simple scripting. "
            "Diagnose first, fix second. Always check logs before guessing. "
            "Be fast, be precise, don't overthink it."
            + _MARKDOWN_INSTRUCTION
            + "\n\nOUTPUT FORMAT (applies ONLY to your very last response, after all tool work is complete):\n"
            "Your final answer MUST include these sections:\n"
            "## Task\n## Diagnosis\n## Actions Taken\n## Result\n## Notes\n\n"
            "Do NOT use these headers in intermediate responses. "
            "Intermediate response = one brief sentence + tool call. Nothing else."
        ),
        allowed_skills=["execute_command", "system_repair", "phone_control", "youtube_downloader", "grep_search", "code_editor", "online_search", "file_uploader"],
        default_skills=["system_repair", "youtube_downloader", "code_editor"],
        default_model="qwen3.7-max",
        default_timeout=300,
        max_parallel=3,
        required_sections=["Task", "Diagnosis", "Actions Taken", "Result", "Notes"],
    ),
    "docs": RoleConfig(
        system_prompt=(
            "You are a document specialist. You create, edit, read, and transform "
            "professional documents: DOCX, PDF, PPTX, XLSX. You can also read "
            "non-text files (images, PDFs, Office docs) into context and rewrite "
            "AI-generated text to sound human. Always preserve formatting and "
            "structure. Ask for clarification if the output format is ambiguous."
            + _MARKDOWN_INSTRUCTION
            + "\n\nOUTPUT FORMAT (applies ONLY to your very last response, after all tool work is complete):\n"
            "Your final answer MUST include these sections:\n"
            "## Task\n## Document Path\n## Structure Overview\n## Notes\n\n"
            "Do NOT use these headers in intermediate responses. "
            "Intermediate response = one brief sentence + tool call. Nothing else."
        ),
        allowed_skills=["execute_command", "document_skills", "file_uploader", "text_humanizer", "code_editor"],
        default_skills=["document_skills", "file_uploader"],
        default_model="qwen3.7-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Task", "Document Path", "Structure Overview", "Notes"],
    ),
    "visuals": RoleConfig(
        system_prompt=(
            "You are a visualization & UI specialist. You create mathematical plots "
            "(Cartesian, polar, parametric), node/edge diagrams (trees, flowcharts, "
            "state machines), production-grade web UI components, and animated/interactive "
            "physics simulations. Choose the right tool: static plots for data, SVG for "
            "structure, HTML/CSS for UI, canvas/WebGL for animation. Always label axes "
            "and use clean typography."
            + _MARKDOWN_INSTRUCTION
            + "\n\nOUTPUT FORMAT (applies ONLY to your very last response, after all tool work is complete):\n"
            "Your final answer MUST include these sections:\n"
            "## Task\n## Output Path\n## Description\n## Notes\n\n"
            "Do NOT use these headers in intermediate responses. "
            "Intermediate response = one brief sentence + tool call. Nothing else."
        ),
        allowed_skills=["execute_command", "graph_master", "svg_creator", "frontend_design", "simulacra_engine", "code_editor"],
        default_skills=["graph_master", "svg_creator"],
        default_model="qwen3.7-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Task", "Output Path", "Description", "Notes"],
    ),
    "tester": RoleConfig(
        system_prompt=(
            "You are a testing & debugging specialist. You investigate bugs, errors, "
            "crashes, and unexpected behavior. Reproduce first, diagnose second, fix "
            "third. Always read error messages and tracebacks carefully. Check logs, "
            "run the failing command, and verify your fix actually resolves the issue. "
            "Never claim a fix works without running the test."
            + _MARKDOWN_INSTRUCTION
            + "\n\nOUTPUT FORMAT (applies ONLY to your very last response, after all tool work is complete):\n"
            "Your final answer MUST include these sections:\n"
            "## Bug Summary\n## Root Cause\n## Fix Applied\n## Verification\n## Notes\n\n"
            "Do NOT use these headers in intermediate responses. "
            "Intermediate response = one brief sentence + tool call. Nothing else."
        ),
        allowed_skills=["execute_command", "testing_debugging", "code_editor", "grep_search"],
        default_skills=["testing_debugging", "code_editor"],
        default_model="qwen3.7-max",
        default_timeout=300,
        max_parallel=2,
        required_sections=["Bug Summary", "Root Cause", "Fix Applied", "Verification", "Notes"],
    ),

    # Scheduled agent ops — broad skill set for autonomous tasks
    "scheduled": RoleConfig(
        system_prompt=(
            "You are an autonomous scheduled agent. You execute recurring tasks "
            "independently. Be thorough, produce clear markdown results, and handle "
            "errors gracefully. You have access to code editing, web search, file "
            "operations, and communication tools (Telegram, email).\n\n"
            "IMPORTANT: For reminders and notifications, you MUST send a Telegram message "
            "as the primary delivery. Read the telegram skill instruction before first use. "
            "Only fall back to markdown-only output if Telegram is unavailable."
            + _MARKDOWN_INSTRUCTION
            + "\n\nOUTPUT FORMAT (applies ONLY to your very last response, after all tool work is complete):\n"
            "## Task\n## Result\n## Notes\n\n"
            "Do NOT use these headers in intermediate responses. "
            "Intermediate response = one brief sentence + tool call. Nothing else."
        ),
        allowed_skills=[
            "execute_command",
            "code_editor",
            "online_search",
            "file_uploader",
            "telegram",
            "email",
            "grep_search",
        ],
        default_skills=[
            "code_editor",
            "online_search",
            "telegram",
            "grep_search",
        ],
        default_model="qwen3.7-max",
        default_timeout=600,
        max_parallel=2,
        required_sections=["Task", "Result", "Notes"],
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

    Skips accounts already in `in_use` set. Falls back to round-robin if all are busy.
    Returns None if no chain is configured.
    """
    chain = _account_fallback_chains.get(role)
    if not chain:
        return None
    if in_use:
        for acc in chain:
            if acc not in in_use:
                return acc
    idx = _account_counters.get(role, 0)
    account = chain[idx % len(chain)]
    _account_counters[role] = idx + 1
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
    if not ov:
        return RoleConfig(
            system_prompt=base.system_prompt,
            allowed_skills=base.allowed_skills,
            default_skills=base.default_skills,
            default_model=base.default_model,
            default_timeout=base.default_timeout,
            max_parallel=base.max_parallel,
            required_sections=base.required_sections,
            model_chain=chain,
        )
    # Merge: override fields replace base fields
    return RoleConfig(
        system_prompt=ov.get("system_prompt", base.system_prompt),
        allowed_skills=ov.get("allowed_skills", base.allowed_skills),
        default_skills=ov.get("default_skills", base.default_skills),
        default_model=ov.get("default_model", base.default_model),
        default_timeout=ov.get("default_timeout", base.default_timeout),
        max_parallel=ov.get("max_parallel", base.max_parallel),
        required_sections=ov.get("required_sections", ov.get("required_json_keys", base.required_sections)),
        model_chain=chain,
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
            "model_chain": cfg.model_chain,
            "browser_fallback_chain": get_account_pool(name),
        }
    return result
