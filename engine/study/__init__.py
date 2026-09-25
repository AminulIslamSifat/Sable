"""Study-mode domain layer.

Pure data-access helpers shared by the study API routes and the study-mode
skill handlers. Kept inside ``engine`` (not ``server``) so handlers never have
to import a route module — that would invert the normal dependency direction
and risk circular imports.
"""

from engine.study.store import (
    add_highlight,
    append_note_body,
    create_note,
    delete_highlight,
    delete_note,
    find_note,
    get_note,
    list_docs,
    list_highlights,
    list_notes,
    page_text_for,
    read_meta,
    read_note_body,
    replace_note_section,
    set_bookmark,
    set_progress,
    study_overview,
    update_highlight,
    update_note,
    update_note_refs,
)

__all__ = [
    "add_highlight",
    "append_note_body",
    "create_note",
    "delete_highlight",
    "delete_note",
    "find_note",
    "get_note",
    "list_docs",
    "list_highlights",
    "list_notes",
    "page_text_for",
    "read_meta",
    "read_note_body",
    "replace_note_section",
    "set_bookmark",
    "set_progress",
    "study_overview",
    "update_highlight",
    "update_note",
    "update_note_refs",
]
