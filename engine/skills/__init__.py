
"""Skill discovery, parsing, and execution engine."""

from engine.skills.bg_jobs import BackgroundJobManager
from engine.skills.engine import SkillEngine
from engine.skills.events import build_tool_feedback, end_event, output_event, start_event
from engine.skills.middleware import (
    ExecutionMiddleware,
    LoggingMiddleware,
    MiddlewarePipeline,
    PermissionMiddleware,
    TagContext,
    ValidationMiddleware,
)
from engine.skills.parser import SkillParser, parse_attrs
from engine.skills.registry import (
    SkillMeta,
    build_tag_ownership,
    discover_skills,
    validate_registry,
)

__all__ = [
    "BackgroundJobManager",
    "SkillEngine",
    "SkillMeta",
    "SkillParser",
    "TagContext",
    "MiddlewarePipeline",
    "ValidationMiddleware",
    "PermissionMiddleware",
    "ExecutionMiddleware",
    "LoggingMiddleware",
    "build_tag_ownership",
    "build_tool_feedback",
    "discover_skills",
    "end_event",
    "output_event",
    "parse_attrs",
    "start_event",
    "validate_registry",
]


from pathlib import Path as _Path
from engine.skills.handlers.common import BACKUP_DIR

# --- Shared SkillEngine singleton (used by agents + any non-route caller) ---

_shared_engine: SkillEngine | None = None


def get_skill_engine() -> SkillEngine:
    """Get or create the shared SkillEngine singleton."""
    global _shared_engine
    if _shared_engine is None:
        from engine.skills.handlers import HANDLER_MAP
        _shared_engine = SkillEngine(
            skills_dir=_Path(__file__).resolve().parent.parent.parent / "skills",
            handlers=HANDLER_MAP,
            agent_id="maria",
        )
    return _shared_engine

# --- Backward-compatible API shims (used by server/api/routes/misc.py) ---

def list_skills() -> list[dict]:
    """Return skill summaries for the /api/skills endpoint."""
    from engine.skills.registry import discover_skills
    skills = discover_skills(_Path(__file__).resolve().parent.parent.parent / 'skills')
    return [
        {'key': s.key, 'name': s.name, 'category': s.category, 'trigger': s.trigger, 'priority': s.priority}
        for s in skills
    ]


def browse_skills() -> list[dict]:
    """Return detailed skill info for the /api/skills/browse endpoint."""
    from engine.skills.registry import discover_skills
    skills_dir = _Path(__file__).resolve().parent.parent.parent / 'skills'
    skills = discover_skills(skills_dir)
    result = []
    for s in skills:
        entry = {
            'key': s.key,
            'name': s.name,
            'category': s.category,
            'description': s.description,
            'trigger': s.trigger,
            'not_this_if': s.not_this_if,
            'tags': s.tags,
            'default': s.default,
            'inline': s.inline,
            'priority': s.priority,
            'path': str(s.dir_path),
            'scripts': s.scripts,
        }
        if s.instruction_path and s.instruction_path.exists():
            entry['instruction_content'] = s.instruction_path.read_text(errors='replace')
        result.append(entry)
    return result
