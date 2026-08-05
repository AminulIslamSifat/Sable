# Ask User

Structured tag for quick, unambiguous input from the user during agent execution.

## Format

<ask_user question="Your question here?" options='["Option A", "Option B", "Other (type manually)"]' default="0" multi="true"/>

### Attributes
- question (required): clear, concise question text
- options (required): JSON array of 2–8 string choices; last option should always be a manual-input escape hatch like "Other (type manually)"
- multi (optional): "true" allows multiple selections; default is single-select
- default (optional): pre-selected option index (0-based), highlights the recommended choice

### Examples

<ask_user question="Which approach for scraping this site?" options='["Browser Control (Playwright)", "HTTP Client (direct API)", "Other (type manually)"]' default="0" />

<ask_user question="Delete all chats older than 30 days?" options='["Yes", "No", "Custom range (type manually)"]' />

<ask_user question="Which skills to enable for this agent?" options='["code_editor", "browser_control", "online_search", "Other (type manually)"]' multi="true" />

[!IMPORTANT] MANUAL SHOULD ALWAYS BE THE LAST OPTION.