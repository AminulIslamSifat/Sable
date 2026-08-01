
# Ask User

Structured question tag for getting quick, unambiguous input from Sifat during agent execution.

## Tag Format

```xml
<ask_user question="Your question here?" options='["Option A", "Option B", "Other (type manually)"]' />
```

### Attributes

-   **question** (required): Clear, concise question text.
-   **options** (required): JSON array of 2–8 string choices. Last option should always be a manual-input escape hatch like `"Other (type manually)"`.
-   **multi** (optional): Set `"true"` to allow multiple selections. Default is single-select.
-   **default** (optional): Pre-selected option index (0-based). Highlights the recommended choice.

## Rules

1.  Keep questions short and specific — this is a decision gate, not a survey.
2.  Always include a manual fallback as the last option.
3.  Options must be mutually exclusive (no overlapping meanings).
4.  Don't ask if the answer is already in context or safely inferable.
5.  The selected value is persisted to DB automatically — no need to re-ask on follow-up turns.
6.  Use for: tool/skill selection, config choices, confirmation prompts, disambiguation, priority decisions.
7.  Don't use for: open-ended creative tasks where structured options would constrain the answer.

## Examples

Tool routing:
```xml
<ask_user question="Which approach for scraping this site?" options='["Browser Control (Playwright)", "HTTP Client (direct API)", "Other (type manually)"]' default="0" />
```

Confirmation:
```xml
<ask_user question="Delete all chats older than 30 days?" options='["Yes", "No", "Custom range (type manually)"]' />
```

Multi-select:
```xml
<ask_user question="Which skills to enable for this agent?" options='["code_editor", "browser_control", "search_online", "Other (type manually)"]' multi="true" />
```
