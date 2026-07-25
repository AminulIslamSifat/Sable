# File Organizer: Workspace Master
Clean up messy directories, find duplicates, restructure Sifat's workspace, and enforce
logical folder hierarchies. This is a command-based skill — no dedicated script. The
agent executes standard shell commands directly via `<execute_command>`.

---

## Trigger Guard

| Condition | Action |
|---|---|
| Sifat says "clean up", "organize", "sort", "restructure" a directory | Fire this skill |
| Sifat asks to find duplicates or free up space | Fire this skill |
| Target is `~/hdd` | **Ask for explicit confirmation before doing anything.** This is the 500GB Toshiba vault. Never reorganize it on assumption. |
| Directory has 1000+ files | Do not run a full scan. Suggest a targeted cleanup first (e.g., "just PDFs from last month"). |
| Sifat asks to delete files | Move to `_trash/` staging only. Never hard-delete without explicit instruction in the same turn. |

---

## Protocol

### Phase 1 — Analyze Current State (always first)

Run both commands. Read both outputs before proposing anything.

Extension breakdown:
```xml
<execute_command>find ~/Downloads -type f | sed 's/.*\.//' | sort | uniq -c | sort -rn</execute_command>
```

Size breakdown (top 20 by size):
```xml
<execute_command>du -sh ~/Downloads/* | sort -rh | head -20</execute_command>
```

Adapt the target path to whatever directory Sifat specified. Do not default to
`~/Downloads` if a different path was given.

---

### Phase 2 — Find Duplicates (when requested)

Size-based pre-filter (fast, approximate):
```xml
<execute_command>find ~/Downloads -type f -printf '%s %p\n' | sort -n | uniq -D -w 10</execute_command>
```

Hash-based exact duplicates (slower, definitive):
```xml
<execute_command>find ~/Downloads -type f -exec md5sum {} \; | sort | uniq -D -w 32</execute_command>
```

Use size-based first to narrow scope. Only run the hash scan on directories with
fewer than 500 files or when Sifat explicitly asks for exact duplicate detection.
Report duplicates grouped by file, not as a raw list.

---

### Phase 3 — Propose a Plan (mandatory before any move)

For any operation touching more than 5 files, present a structured plan and wait for
approval. Do not move anything before Sifat confirms.

Plan format:
```
## Organization Plan for [target directory]

### Current State
- [N] files across [N] folders
- [X] GB total
- Breakdown: [N] PDFs, [N] images, [N] archives, [N] misc

### Proposed Structure
[target]/
├── Documents/   (PDF, DOCX, TXT)
├── Images/      (PNG, JPG, SVG, WEBP)
├── Archives/    (ZIP, TAR, 7Z, GZ)
├── Code/        (PY, JS, GO, SH, etc.)
└── Misc/        (everything else)

### What will NOT be moved
[list anything being excluded and why]

### Log file
All moves will be logged to /tmp/ghost_organize_log.txt for undo.

Ready to proceed?
```

For operations touching 5 or fewer files, proceed directly with a one-line summary
of what's being done.

---

### Phase 4 — Execute (after approval only)

Create directories first:
```xml
<execute_command>mkdir -p ~/Downloads/{Documents,Images,Archives,Code,Misc}</execute_command>
```

Move by extension (example — adapt to the approved plan):
```xml
<execute_command>find ~/Downloads -maxdepth 1 -name "*.pdf" -exec mv {} ~/Downloads/Documents/ \;</execute_command>
```

Log every move before executing it:
```xml
<execute_command>find ~/Downloads -maxdepth 1 -name "*.pdf" -printf "mv %p ~/Downloads/Documents/%f\n" >> /tmp/ghost_organize_log.txt</execute_command>
```

Always use `mv` — never copy+delete. `mv` preserves original modification timestamps.
Never use `cp` followed by `rm` as a substitute.

---

### Phase 5 — Verify

After execution, confirm the result matches the plan:
```xml
<execute_command>du -sh ~/Downloads/* | sort -rh</execute_command>
```

Report:
- How many files were moved
- Final directory structure
- Path to the log file for undo reference

---

## Grouping Strategies

| Strategy | When to Use | Example Structure |
|---|---|---|
| **By extension** | Downloads folder, general cleanup | `Documents/`, `Images/`, `Archives/`, `Code/` |
| **By date** | Photo libraries, old project archives | `2024-01/`, `2024-02/` |
| **By project** | Code directories, work folders | `project-name/src`, `project-name/assets` |
| **By purpose** | Active vs archive, work vs personal | `Active/`, `Archive/`, `Personal/` |

When the directory contains mixed content with no clear dominant type, default to
**by extension**. When Sifat describes a specific workflow or project, prefer
**by purpose** or **by project**.

---

## Staging Trash (Safe Deletion)

Never hard-delete. Always stage to `_trash/` first:
```xml
<execute_command>mkdir -p ~/Downloads/_trash && mv [file] ~/Downloads/_trash/</execute_command>
```

After staging, tell Sifat:
- What is in `_trash/`
- How to permanently delete it if confirmed: `rm -rf ~/Downloads/_trash/`
- Never run that command without Sifat's explicit go-ahead in the same turn.

---

## Log Format

For any operation moving more than 5 files, log every move to
`/tmp/ghost_organize_log.txt` before executing:

```bash
# Log entry format
echo "$(date '+%Y-%m-%d %H:%M:%S') mv [source] [destination]" >> /tmp/ghost_organize_log.txt
```

To undo a logged operation, the log is human-readable and reversible — each line
shows the exact `mv` that was run. Tell Sifat the log path at the end of every
large operation.

---

## Failure Handling

| Failure type | Symptom | Action |
|---|---|---|
| **Permission denied** | `mv` or `mkdir` returns permission error | Report the exact path and error. Do not use `sudo` for file moves without Sifat's confirmation — ownership changes can cause problems. |
| **Target already exists** | `mv` would overwrite an existing file | Do not overwrite silently. Rename the incoming file with a suffix (e.g., `_1`, `_conflict`) and report the collision. |
| **Partial execution** | Command runs on some files but fails on others | Report which files moved and which failed. Do not retry failed moves without diagnosing the cause. |
| **HDD targeted without confirmation** | Sifat's message implies `~/hdd` | Stop. Ask for explicit confirmation before touching the HDD. |
| **Directory too large** | 1000+ files | Stop full scan. Propose a targeted cleanup and wait for direction. |

---

## Critical Rules

1. **Analyze before proposing.** Phase 1 runs first, always. Never propose a structure
   without reading the actual directory state.
2. **Plan before moving.** Any operation touching more than 5 files requires an
   approved plan. No exceptions.
3. **No hard deletes.** Files go to `_trash/` staging. Sifat confirms permanent
   deletion explicitly, in the same turn, before `rm` runs.
4. **HDD is sacred.** `~/hdd` is the 500GB Toshiba vault. Never reorganize
   it without explicit instructions. When in doubt, ask.
5. **`mv` only.** Never copy+delete. `mv` preserves modification timestamps. `cp`+`rm`
   does not.
6. **Log large operations.** Every move in a bulk operation is logged to
   `/tmp/ghost_organize_log.txt` before it runs.
7. **Scope large directories.** 1000+ files means targeted cleanup, not a full scan.
   Suggest a scoped approach and let Sifat decide the scope.
8. **Report the log path.** At the end of every large operation, tell Sifat where the
   log is and that it can be used to undo the moves.