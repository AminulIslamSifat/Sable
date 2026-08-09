# TrackNote Manager

Unified CRUD for notes/todos, schedules, and agent_ops in sable.db.
Uses the `tracknote` tag inside an action block.

## Tag Format

<action>
<tracknote action="add_todo" title="Example" items='[{"text":"task","done":false}]' />
</action>

## Actions

| Action | Attributes | Description |
|:--|:--|:--|
| `add_note` | title, content, type (note/checklist) | Create a note |
| `add_todo` | title, items (JSON array of text/done) | Create a checklist todo |
| `add_schedule` | title, type, time, day_of_week, start_date, description | Add schedule entry |
| `toggle_item` | note_id, index | Toggle a checklist item |
| `delete` | kind (notes/schedules), id | Delete an entry |

## Examples

```xml
<!-- Add a todo -->
<tracknote action="add_todo" title="Hydration Reminder" items='[{"text": "Drink water", "done": false}]' />

<!-- Add a schedule -->
<tracknote action="add_schedule" title="Standup" type="daily" time="09:00" description="Team sync" />

<!-- Toggle item 0 in a note -->
<tracknote action="toggle_item" note_id="abc123" index="0" />

<!-- Delete a schedule -->
<tracknote action="delete" kind="schedules" id="def456" />
```

## Rules
- IDs are prefix-matchable (first 4-6 chars enough)
- Schedule is injected into first message of every new chat (next 10 days)
- Notes and todos are unified — a note becomes a todo when it has checklist items
