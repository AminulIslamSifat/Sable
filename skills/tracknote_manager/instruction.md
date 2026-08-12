# TrackNote Manager

Unified CRUD for notes/todos, schedules, and agent_ops in sable.db.
Uses the `tracknote` tag inside an action block.

## Tag Reference

| Action | Attributes | Description |
|:--|:--|:--|
| `list_notes` | `all`, `type` (note/checklist) | List notes/todos |
| `list_schedules` | `all` | List schedules |
| `list_ops` | `all` | List agent ops |
| `add_note` | `title`, `content`, `type` (note/checklist) | Create a note |
| `add_todo` | `title`, `items` (JSON array of {text, done}) | Create a checklist todo |
| `add_schedule` | `title`, `type`, `time`, `day_of_week`, `start_date`, `description` | Add schedule entry |
| `toggle_item` | `note_id`, `index` | Toggle a checklist item |
| `delete` | `kind` (notes/schedules/ops), `id` | Delete an entry |

All tags are self-closing: `<tracknote action="..." attr="val" />`

## Rules

- IDs are prefix-matchable (first 4–6 chars enough)
- Schedule is injected into first message of every new chat (next 10 days)
- Notes and todos are unified — a note becomes a todo when it has checklist items
