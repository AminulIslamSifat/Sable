
"""SkillEngine — central orchestrator for skill discovery, parsing, and execution.

Ties together: registry (discovery) + parser (tag extraction) +
middleware pipeline (validation/execution) + background jobs.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Generator

from engine.skills.bg_jobs import BackgroundJobManager
from engine.skills.events import build_tool_feedback
from engine.skills.middleware import (
    ExecutionMiddleware,
    LoggingMiddleware,
    MiddlewarePipeline,
    TagContext,
    ValidationMiddleware,
)
from engine.skills.parser import KNOWN_TAGS, SkillParser
from engine.skills.registry import SkillMeta, build_tag_ownership, discover_skills, validate_registry

logger = logging.getLogger(__name__)

# Type alias for handler functions
HandlerFn = Callable[[str, str, dict[str, str], str], Generator[dict[str, Any], None, None]]


class SkillEngine:
    """Central skill execution engine.

    Usage:
        engine = SkillEngine(skills_dir=Path("skills"), handlers=HANDLERS)
        # During streaming:
        parser = engine.create_parser()
        for event in parser.feed(chunk):
            if event["type"] == "tag_found":
                yield from engine.process_tag(event["name"], event["attrs"], event["content"])
            else:
                yield event  # text, tool_pending, tool_progress
    """

    def __init__(
        self,
        skills_dir: Path,
        handlers: dict[str, HandlerFn],
        agent_id: str | None = None,
        disabled: list[str] | None = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._handlers = handlers
        self._agent_id = agent_id

        # Discovery
        self._skills = discover_skills(skills_dir, agent_id=agent_id, disabled=disabled)
        self._tag_ownership = build_tag_ownership(self._skills)

        # Validation warnings (logged at init)
        warnings = validate_registry(self._skills)
        if warnings:
            logger.warning("Registry has %d warnings", len(warnings))

        # Background jobs
        self.bg_jobs = BackgroundJobManager()

        # Middleware pipeline (lazy import to avoid circular dep)
        from engine.security.middleware import SecurityMiddleware

        self._pipeline = MiddlewarePipeline([
            ValidationMiddleware(self._tag_ownership, set(handlers.keys())),
            SecurityMiddleware(),
            ExecutionMiddleware(handlers),
            LoggingMiddleware(),
        ])

        logger.info(
            "SkillEngine initialized: %d skills, %d tags, agent=%s",
            len(self._skills), len(self._tag_ownership), agent_id or "*",
        )

    @property
    def skills(self) -> list[SkillMeta]:
        return self._skills

    @property
    def tag_ownership(self) -> dict[str, SkillMeta]:
        return self._tag_ownership

    def create_parser(self) -> SkillParser:
        """Create a parser instance with tags from the registry + handler tags."""
        skill_tags = set(self._tag_ownership.keys())
        handler_tags = set(self._handlers.keys())
        all_tags = tuple(skill_tags | handler_tags)
        return SkillParser(known_tags=all_tags if all_tags else None)

    def process_tag(
        self,
        name: str,
        attrs: dict[str, str],
        content: str,
        namespace: str = "default",
        chat_id: str | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Run a single tag through the middleware pipeline.

        Yields SSE events (skill_start, skill_output, skill_end).
        """
        ctx = TagContext(
            tag_id=uuid.uuid4().hex[:12],
            name=name,
            attrs=attrs,
            content=content,
            namespace=namespace,
            chat_id=chat_id,
        )
        yield from self._pipeline.execute(ctx)

    def get_known_tags(self) -> tuple[str, ...]:
        """Return all registered tag names (for parser initialization)."""
        skill_tags = set(self._tag_ownership.keys())
        handler_tags = set(self._handlers.keys())
        all_tags = tuple(skill_tags | handler_tags)
        return all_tags or KNOWN_TAGS

    def get_registry_prompt(self, include_defaults: bool = True) -> str:
        """Generate the skills section for the LLM system prompt.

        Args:
            include_defaults: If True, default skills get full instruction.md injected.
                              If False, ALL skills (including defaults) get compact
                              trigger/path listing only — for models that can't handle
                              large system prompts.

        Non-default skills always get a compact trigger/path listing.
        Includes routing protocol and action wrapper rules.
        """
        lines: list[str] = []
        lines.append("# GhostChat Skills Registry & Routing Protocol")
        lines.append("")
        lines.append("> [!CAUTION]")
        lines.append("> ## ROUTING PROTOCOL — NON-NEGOTIABLE")
        lines.append("> - Always read the skill instruction file before working with that skill.")
        lines.append(">")
        lines.append("> ### Routing Discipline")
        lines.append("> - Never put a tool_call block inside a fenced code block.")
        lines.append(">")
        lines.append("> ### Tool Call Wrapper (mandatory)")
        lines.append("> All calls must be inside a single tool_call tag pair as a JSON array, placed at response end.")
        lines.append("***")
        lines.append("")

        # Default skills
        default_skills = [s for s in self._skills if s.default]
        if include_defaults:
            # Full instruction.md injection for capable models
            for skill in default_skills:
                lines.append(f"## {skill.name} (DEFAULT — always active)")
                lines.append("")
                if skill.instruction_path and skill.instruction_path.exists():
                    content = skill.instruction_path.read_text(encoding="utf-8", errors="replace")
                    lines.append(content.strip())
                else:
                    lines.append(f"*Trigger: {skill.trigger}*")
                lines.append("")
                lines.append("***")
                lines.append("")
        else:
            # Compact listing only — treat defaults same as non-defaults
            pass  # They'll be included in the unified listing below

        # Non-default skills (or ALL skills when include_defaults=False): compact listing grouped by category
        non_default = self._skills if not include_defaults else [s for s in self._skills if not s.default]
        if non_default:
            # Separate reference-only skills (one-liner) from full compact listing
            reference_skills = [s for s in non_default if s.context == "reference"]
            full_skills = [s for s in non_default if s.context != "reference"]

            # Reference-only skills: single line each
            if reference_skills:
                lines.append("## Subagent Skills (reference only — spawn matching agent)")
                lines.append("")
                for skill in reference_skills:
                    lines.append(f"- `{skill.key}`: `{skill.dir_path}/instruction.md`")
                lines.append("")

            # Full compact listing grouped by category
            categories: dict[str, list[SkillMeta]] = {}
            for skill in full_skills:
                categories.setdefault(skill.category, []).append(skill)

            for category in ("core", "data", "visuals", "study", "system"):
                skills_in_cat = categories.get(category, [])
                if not skills_in_cat:
                    continue
                lines.append(f"## {category.title()} Skills")
                lines.append("")
                for skill in skills_in_cat:
                    lines.append(f"### {skill.name}")
                    lines.append(f"* **Trigger:** {skill.trigger}")
                    if skill.not_this_if:
                        lines.append(f"* **Not this if:** {skill.not_this_if}")
                    lines.append(f"* **Instruction:** `{skill.dir_path}/instruction.md`")
                    lines.append("")

            # Remaining categories
            for category, skills_in_cat in categories.items():
                if category in ("core", "data", "visuals", "study", "system"):
                    continue
                lines.append(f"## {category.title()} Skills")
                lines.append("")
                for skill in skills_in_cat:
                    lines.append(f"### {skill.name}")
                    lines.append(f"* **Trigger:** {skill.trigger}")
                    lines.append(f"* **Instruction:** `{skill.dir_path}/instruction.md`")
                    lines.append("")

        return "\n".join(lines)

    def get_skill_instruction(self, skill_key: str) -> str | None:
        """Load instruction.md content for a skill by key."""
        for skill in self._skills:
            if skill.key == skill_key and skill.instruction_path:
                try:
                    return skill.instruction_path.read_text(encoding="utf-8")
                except OSError:
                    return None
        return None

    def reload(self) -> None:
        """Re-discover skills from disk (hot reload)."""
        self._skills = discover_skills(self._skills_dir, agent_id=self._agent_id)
        self._tag_ownership = build_tag_ownership(self._skills)
        # Update validation middleware's ownership reference
        self._pipeline._middlewares[0] = ValidationMiddleware(self._tag_ownership, set(self._handlers.keys()))
        logger.info("SkillEngine reloaded: %d skills", len(self._skills))
