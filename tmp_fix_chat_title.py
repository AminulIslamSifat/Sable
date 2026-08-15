#!/usr/bin/env python3
"""
Script to refactor chat title injection in server/api/routes/chat.py.

Replaces the SYSTEM message injection with a silent chat_title tool call.
"""

import re
import sys

# Read the file
file_path = "/home/sifat/hdd/projects/Sable/server/api/routes/chat.py"
with open(file_path, "r", encoding="utf-8" as f:
    content = f.read()

# Pattern to match the SYSTEM message injection
pattern = r'(_context_parts\.append\(\[SYSTEM: First message of a new chat\. Respond normally, but also emit \' \+ chr\(60\) \+ \'tool_call\' \+ chr\(62\) \+ \'\{"name": "chat_title", "arguments": \{"title": "Short descriptive title"\}\}\' \+ chr\(60\) \+ \'/tool_call\' \+ chr\(62\) \+ \' at the end of your response.*?\]\'\))

# Replacement: Silent chat_title tool call
replacement = """
        # Inject chat_title tool call silently (no SYSTEM message)
        _context_parts.append('')  # Empty string to avoid poisoning context
        _inject_chat_title = True
"""

# Apply the replacement
new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)

if new_content == content:
    print("ERROR: Pattern not found. No changes made.")
    sys.exit(1)

# Write the changes to a temporary file
with open(file_path + ".tmp", "w", encoding="utf-8") as f:
    f.write(new_content)

print(f"Success! Changes written to {file_path}.tmp")
print("Review the changes, then replace the original file:")
print(f"mv {file_path}.tmp {file_path}")