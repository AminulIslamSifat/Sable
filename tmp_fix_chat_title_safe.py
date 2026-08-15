#!/usr/bin/env python3
"""
Safe script to refactor chat title injection in server/api/routes/chat.py.

Uses string concatenation to avoid JSON corruption in tool calls.
"""

import re
import sys

# Read the file
file_path = "/home/sifat/hdd/projects/Sable/server/api/routes/chat.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Pattern to match the SYSTEM message injection
# Split into parts to avoid regex escaping hell
system_msg_start = r'_context_parts.append\(\[SYSTEM: First message of a new chat\. '
system_msg_mid = r'Respond normally, but also emit \' \+ chr\(60\) \+ \'tool_call\' \+ chr\(62\) \+ \'
system_msg_end = r'\' \+ chr\(60\) \+ \'/tool_call\' \+ chr\(62\) \+ \' at the end of your response.*?\]\)'

pattern = system_msg_start + system_msg_mid + r'.*?' + system_msg_end

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