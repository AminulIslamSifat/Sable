# Ask User

Structured tag for quick, unambiguous input from the user during agent execution.

## Tag Reference

| Attribute | Required | Description |
|:--|:--|:--|
| `question` | ✅ | Clear, concise question text |
| `options` | ✅ | JSON array of 2–8 string choices. **Last option must always be a manual-input escape hatch** (e.g. `"Other (type manually)"`) |
| `multi` | ❌ | `"true"` allows multiple selections; default is single-select |
| `default` | ❌ | Pre-selected option index (0-based); highlights the recommended choice |

## Format

```xml
<ask_user question="Your question here?" options='["Option A", "Option B", "Other (type manually)"]' default="0" multi="true"/>
```

> [!IMPORTANT]
> Manual escape hatch must **always** be the last option.
