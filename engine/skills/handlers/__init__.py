
"""Skill handler registry — maps tag names to handler functions."""

from engine.skills.handlers.execute import (
    BG_JOBS,
    handle_check_command,
    handle_execute_command,
)
from engine.skills.handlers.file_ops import (
    handle_create_file,
    handle_edit_file,
    handle_insert_file,
    handle_list_checkpoints,
    handle_restore_checkpoint,
    handle_view_file,
)
from engine.skills.handlers.io import (
    handle_create_note,
    handle_get_file,
)
from engine.skills.handlers.web import (
    handle_online_search,
    handle_openweb,
)
from engine.skills.handlers.agents import (
    handle_agent_status,
    handle_kill_agent,
    handle_spawn_agent,
    handle_todo_complete,
    handle_todo_skip,
)
from engine.skills.handlers.ask_user import handle_ask_user
from engine.skills.handlers.grep_search import (
    handle_grep,
    handle_glob,
    handle_list_dir,
)
from engine.skills.handlers.simulacra import handle_run_simulacra
from engine.skills.handlers.tracknote import handle_tracknote
from engine.skills.handlers.memory_manager import handle_memory
from engine.skills.handlers.image_generator import handle_generate_image
from engine.mcp.handler import handle_mcp_call

HANDLER_MAP: dict[str, object] = {
    "execute_command": handle_execute_command,
    "get_file": handle_get_file,
    "read_file": handle_get_file,

    "check_command": handle_check_command,
    "openweb": handle_openweb,
    "web_search": handle_online_search,
    "web_fetch": handle_online_search,
    "online_search": handle_online_search,
    "create_note": handle_create_note,

    "view_file": handle_view_file,
    "edit_file": handle_edit_file,
    "create_file": handle_create_file,
    "insert_file": handle_insert_file,
    "list_checkpoints": handle_list_checkpoints,
    "restore_checkpoint": handle_restore_checkpoint,
    "spawn_agent": handle_spawn_agent,
    "agent_status": handle_agent_status,
    "kill_agent": handle_kill_agent,
    "todo_complete": handle_todo_complete,
    "todo_skip": handle_todo_skip,
    "ask_user": handle_ask_user,
    "grep": handle_grep,
    "glob": handle_glob,
    "list_dir": handle_list_dir,
    "run_simulacra": handle_run_simulacra,
    "tracknote": handle_tracknote,
    "memory": handle_memory,
    "generate_image": handle_generate_image,
    "mcp_call": handle_mcp_call,
}

__all__ = [
    "BG_JOBS",
    "HANDLER_MAP",
    "handle_check_command",
    "handle_create_file",
    "handle_create_note",
    "handle_edit_file",
    "handle_execute_command",
    "handle_get_file",
    "handle_insert_file",
    "handle_online_search",
    "handle_openweb",


    "handle_view_file",
    "handle_spawn_agent",
    "handle_agent_status",
    "handle_kill_agent",
    "handle_todo_complete",
    "handle_todo_skip",
    "handle_ask_user",
    "handle_grep",
    "handle_glob",
    "handle_list_dir",
    "handle_run_simulacra",
    "handle_tracknote",
    "handle_memory",
    "handle_generate_image",
    "handle_mcp_call",
]
