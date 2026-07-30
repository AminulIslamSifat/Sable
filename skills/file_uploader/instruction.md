# File Uploader

## Usage
Wrap the absolute file path inside the get_file tag in an action block.
System uploads file directly to context interface.

## Workflow
1. If path unknown, use execute_command with find first.
2. Use get_file tag with absolute path.
3. File content appears in next system prompt.

## Rules
- For READING non-text files only. Editing -> Code Editor.
- Text files (.py, .md, .json) -> use view_file instead.
