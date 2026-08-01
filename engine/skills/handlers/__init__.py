
"""Skill handler registry — maps tag names to handler functions."""

from engine.skills.handlers.execute import (
    BG_JOBS,
    handle_check_command,
    handle_execute_background_command,
    handle_execute_command,
)
from engine.skills.handlers.file_ops import (
    handle_create_file,
    handle_edit_file,
    handle_insert_file,
    handle_view_file,
)
from engine.skills.handlers.io import (
    handle_create_note,
    handle_get_file,
    handle_save_svg,
)
from engine.skills.handlers.web import (
    handle_openweb,
)
from engine.skills.handlers.agents import (
    handle_agent_status,
    handle_kill_agent,
    handle_spawn_agent,
)
from engine.skills.handlers.ask_user import handle_ask_user

HANDLER_MAP: dict[str, object] = {
    "execute_command": handle_execute_command,
    "execute_background_command": handle_execute_background_command,
    "get_file": handle_get_file,
    "read_file": handle_get_file,

    "check_command": handle_check_command,
    "openweb": handle_openweb,
    "create_note": handle_create_note,
    "save_svg": handle_save_svg,
    "view_file": handle_view_file,
    "edit_file": handle_edit_file,
    "create_file": handle_create_file,
    "insert_file": handle_insert_file,
    "spawn_agent": handle_spawn_agent,
    "agent_status": handle_agent_status,
    "kill_agent": handle_kill_agent,
    "ask_user": handle_ask_user,
}

__all__ = [
    "BG_JOBS",
    "HANDLER_MAP",
    "handle_check_command",
    "handle_create_file",
    "handle_create_note",
    "handle_edit_file",
    "handle_execute_background_command",
    "handle_execute_command",
    "handle_get_file",
    "handle_insert_file",
    "handle_openweb",
    "handle_save_svg",

    "handle_view_file",
    "handle_spawn_agent",
    "handle_agent_status",
    "handle_kill_agent",
    "handle_ask_user",
]
