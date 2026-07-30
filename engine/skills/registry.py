
"""Skill discovery, validation, and tag ownership resolution.

Scans a skills directory for skill.json manifests, validates them,
and provides priority-based tag ownership for the execution engine.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Required fields in every skill.json
_REQUIRED_FIELDS = ("name", "key", "version", "category", "description", "trigger", "tags", "default", "priority", "scope")


@dataclass(slots=True)
class SkillMeta:
    """Parsed and validated skill manifest."""

    name: str
    key: str
    version: str
    category: str
    description: str
    trigger: str
    tags: list[str]
    default: bool
    priority: int
    scope: list[str]
    # Optional fields
    not_this_if: str | None = None
    inline: bool = False
    config: dict = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    # Computed at discovery
    dir_path: Path = field(default_factory=Path)
    instruction_path: Path | None = None
    scripts: list[str] = field(default_factory=list)


def discover_skills(
    skills_dir: Path,
    agent_id: str | None = None,
) -> list[SkillMeta]:
    """Scan skills_dir for skill.json files, parse, filter by scope, sort by priority desc.

    Args:
        skills_dir: Root directory containing skill folders.
        agent_id: If provided, only return skills whose scope includes this agent or '*'.

    Returns:
        List of SkillMeta sorted by priority (highest first).
    """
    if not skills_dir.is_dir():
        logger.error("Skills directory does not exist: %s", skills_dir)
        return []

    skills: list[SkillMeta] = []

    for manifest_path in sorted(skills_dir.glob("*/skill.json")):
        skill = _parse_manifest(manifest_path)
        if skill is None:
            continue
        skills.append(skill)

    # Filter by agent scope
    if agent_id:
        skills = [s for s in skills if "*" in s.scope or agent_id in s.scope]

    # Sort by priority descending (highest priority first)
    skills.sort(key=lambda s: s.priority, reverse=True)

    logger.info("Discovered %d skills (agent=%s)", len(skills), agent_id or "*")
    return skills


def validate_registry(skills: list[SkillMeta]) -> list[str]:
    """Validate a list of discovered skills. Returns a list of warning strings.

    Checks:
    - Duplicate keys
    - Tag conflicts (same tag owned by multiple skills)
    - Missing instruction.md for default skills
    - Unresolved dependencies
    """
    warnings: list[str] = []

    # Duplicate keys
    seen_keys: dict[str, int] = {}
    for s in skills:
        seen_keys[s.key] = seen_keys.get(s.key, 0) + 1
    for key, count in seen_keys.items():
        if count > 1:
            warnings.append(f"Duplicate skill key '{key}' appears {count} times")

    # Tag conflicts
    tag_owners: dict[str, list[str]] = {}
    for s in skills:
        for tag in s.tags:
            tag_owners.setdefault(tag, []).append(s.key)
    for tag, owners in tag_owners.items():
        if len(owners) > 1:
            warnings.append(f"Tag '{tag}' claimed by multiple skills: {owners}")

    # Missing instruction.md for default skills
    for s in skills:
        if s.default and s.instruction_path is None:
            warnings.append(f"Default skill '{s.key}' has no instruction.md")

    # Unresolved dependencies
    all_keys = {s.key for s in skills}
    for s in skills:
        for dep in s.dependencies:
            if dep not in all_keys:
                warnings.append(f"Skill '{s.key}' depends on missing skill '{dep}'")

    if warnings:
        for w in warnings:
            logger.warning("Registry validation: %s", w)

    return warnings


def build_tag_ownership(skills: list[SkillMeta]) -> dict[str, SkillMeta]:
    """Map each tag to its owning skill. On conflict, highest priority wins.

    Args:
        skills: List of SkillMeta (should already be sorted by priority desc).

    Returns:
        Dict mapping tag name -> owning SkillMeta.
    """
    ownership: dict[str, SkillMeta] = {}

    for skill in skills:
        for tag in skill.tags:
            if tag in ownership:
                existing = ownership[tag]
                logger.warning(
                    "Tag conflict: '%s' claimed by '%s' (pri %d) and '%s' (pri %d). Keeping '%s'.",
                    tag, existing.key, existing.priority, skill.key, skill.priority, existing.key,
                )
                # Since skills are sorted by priority desc, first one wins
                continue
            ownership[tag] = skill

    return ownership


def _parse_manifest(manifest_path: Path) -> SkillMeta | None:
    """Parse a single skill.json into SkillMeta. Returns None on failure."""
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read %s: %s", manifest_path, e)
        return None

    # Check required fields
    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        logger.error("skill.json at %s missing required fields: %s", manifest_path, missing)
        return None

    skill_dir = manifest_path.parent

    # Discover instruction.md
    instruction_path = skill_dir / "instruction.md"
    if not instruction_path.exists():
        instruction_path = None

    # Discover scripts
    scripts_dir = skill_dir / "scripts"
    scripts: list[str] = []
    if scripts_dir.is_dir():
        scripts = [f.name for f in scripts_dir.iterdir() if f.is_file()]

    return SkillMeta(
        name=raw["name"],
        key=raw["key"],
        version=raw["version"],
        category=raw["category"],
        description=raw["description"],
        trigger=raw["trigger"],
        tags=raw["tags"],
        default=raw["default"],
        priority=raw["priority"],
        scope=raw["scope"],
        not_this_if=raw.get("not_this_if"),
        inline=raw.get("inline", False),
        config=raw.get("config", {}),
        dependencies=raw.get("dependencies", []),
        dir_path=skill_dir,
        instruction_path=instruction_path,
        scripts=scripts,
    )
