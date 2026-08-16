"""server.database — split into core/chats/agents/notes submodules.

All public names are re-exported here so existing imports like
`from server.database import ensure_chat` continue to work unchanged.
"""

from .core import get_db, init_db, migrate_skill_events_to_table
from .chats import (
    ensure_chat,
    rename_chat,
    get_chat_mode,
    get_chat_provider,
    get_upstream_session_id,
    set_upstream_session_id,
    set_title_if_default,
    update_chat_title,
    get_injected_memory_keys,
    save_injected_memory_keys,
    touch_chat,
    save_chat_url,
    get_chat_url,
    get_chat_project_id,
    list_chats,
    delete_chat,
    delete_all_chats,
    get_chat_tail_id,
    get_parent_id,
    add_message,
    update_message,
    append_skill_event,
    get_messages,
    get_skill_events_for_message,
    search_messages,
    list_projects,
    create_project,
    get_project,
    update_project,
    delete_project,
    save_checkpoint,
    get_checkpoints_for_chat,
    get_checkpoint_by_sha,
    get_latest_checkpoint_for_message,
)
from .agents import (
    recover_stale_agents,
    insert_agent_run,
    update_agent_status,
    add_agent_message,
    get_agent_runs,
    get_agent_messages,
    list_agent_ops,
    get_agent_op,
    create_agent_op,
    update_agent_op,
    delete_agent_op,
    get_due_agent_ops,
)
from .notes import (
    list_notes,
    get_note,
    create_note,
    update_note,
    delete_note,
    toggle_note_item,
    list_schedules,
    get_upcoming_schedules,
    create_schedule,
    update_schedule,
    delete_schedule,
)

__all__ = [
    # core
    "get_db", "init_db", "migrate_skill_events_to_table",
    # chats
    "ensure_chat", "rename_chat", "get_chat_mode", "get_chat_provider",
    "get_upstream_session_id", "set_upstream_session_id", "set_title_if_default",
    "update_chat_title", "get_injected_memory_keys", "save_injected_memory_keys",
    "touch_chat", "save_chat_url", "get_chat_url", "get_chat_project_id",
    "list_chats", "delete_chat", "delete_all_chats", "get_chat_tail_id",
    "get_parent_id", "add_message", "update_message", "append_skill_event",
    "get_messages", "get_skill_events_for_message", "search_messages",
    "list_projects", "create_project", "get_project", "update_project",
    "delete_project", "save_checkpoint", "get_checkpoints_for_chat",
    "get_checkpoint_by_sha", "get_latest_checkpoint_for_message",
    # agents
    "recover_stale_agents", "insert_agent_run", "update_agent_status",
    "add_agent_message", "get_agent_runs", "get_agent_messages",
    "list_agent_ops", "get_agent_op", "create_agent_op", "update_agent_op",
    "delete_agent_op", "get_due_agent_ops",
    # notes
    "list_notes", "get_note", "create_note", "update_note", "delete_note",
    "toggle_note_item", "list_schedules", "get_upcoming_schedules",
    "create_schedule", "update_schedule", "delete_schedule",
]
#
