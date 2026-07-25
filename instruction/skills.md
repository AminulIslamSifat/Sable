# GhostChat Skills Registry & Routing Protocol

> [!CAUTION]
> ## 🚨 MANDATORY ROUTING PROTOCOL — NON-NEGOTIABLE, READ BEFORE EVERY SKILL CALL 🚨
>
> This protocol is **binding**. It is not a suggestion, not a fallback, and not something to weigh against convenience. If a request matches a skill's trigger conditions below, **that skill MUST be used** — the model may not answer from general capability, improvise an ad-hoc solution, or use a generic/native tool as a substitute, when a matching skill exists.
>
> 1. **Match first.** Match the request to a skill using its **trigger conditions** — not the skill's name, not a surface-level keyword guess.
> 2. **Load before acting.** Use `<get_file>` to open that skill's `instruction.md` exactly as listed. Do not proceed on assumption or memory of what the instruction file probably says.
> 3. **Follow exactly.** Follow the loaded protocol precisely — **never guess parameters, formats, or shortcuts.**
> 4. **Precedence rule.** When a matched skill's required method conflicts with any other available tool, capability, or default behavior, **the matched skill wins.** Do not silently fall back to a non-registry tool because it's faster, more familiar, or "close enough."
>
> ### Routing Discipline
> - **Never guess.** Instruction paths are the only source of truth — not training knowledge, not prior turns.
> - **No memory reliance.** Always treat local `.md` instructions as potentially updated since last read.
> - **Square one rule.** On failure, return to discovery commands (`--help`, `openweb [site]`) rather than improvising.
> - **One-liner rule.** During tool gathering: one sentence + tag only. No walls of text.
> - **One skill per response.** Never stack skills. On genuine ambiguity (two skills equally valid), default to the one whose output format best matches the request — diagram over trace, trace over math.
> - **Mutation lock.** File writes/edits go through the Code Editor skill ONLY. No exceptions, no matter how small the change.
> - **Global wrapper (critical).** Wrap the entire final response in ONE ` ```markdown ` block.

***
# MOST IMPORTANT SKILL (MUST PRIORITIZE USING THIS WHILE CODING)

# Code Editor Skill

Only skill that mutates disk. All file I/O via:
`python3 $PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py <op> <path> [flags]`
Content/JSON always on **stdin** (or `--content-file` / `--json-file`). Bash *runs* tools only — **never** author files via `cat >`, `>`, `>>`, `python3 -c`, or `open().write()`. Zero size exceptions.

## Quoting (critical)
- Default = quoted heredoc: `cat << 'EOF' | … <op> path` (quoted delimiter blocks `$` / backtick / brace expansion).
- `echo "…"` only for one-liners with **no** special chars (no nested quotes, `{}`, `$`, backticks, f-strings).
- Payload contains the delimiter? pick a rare one (`'JSON_7F3A'`).
- Quoting-failure signs: f-string `SyntaxError`, JSON parse error, truncation at a quote → re-run with a quoted heredoc; never hack logic with `chr()`.

## Ops
**view** `[--start N --end M | --full]` — path may be a dir (→ tree). Lines prefixed `NUM\t` are display-only: **strip the prefix** before using text as `old_str`. Large files show top+bottom only unless `--start/--end/--full`.

**create** — stdin = content. Fails if file exists; add `--overwrite` for a full rewrite. `--content-file PATH` to stage.

**edit** — stdin = JSON (one `{old_str,new_str}` *or* an array; or `--json-file`). `old_str` must match **exactly once** (0 or 2+ → fail, add context). Atomic: all-or-nothing against original state. Match = exact, then normalized (smart quotes / dashes / whitespace). Preserves LF/CRLF. Backs up to `.editor_tools_backups/` (cap 20), returns unified diff. **Copy `old_str` from a fresh `view`; re-view before chaining edits.** Delete lines = `new_str:""` (include trailing `\n` or you leave a blank line).

~~~bash
cat << 'JSON' | python3 $PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py edit path/file.py
{"old_str": "def foo():\n    return 1", "new_str": "def foo():\n    return 2"}
JSON
~~~

**insert** — stdin = JSON `{content, at_line | after_str}` (**exactly one** of the two). `at_line` = 1-indexed insert-before; `after_str` = unique anchor. Same backup/diff as edit.

## Big rewrites
`view --full` (or a range) → copy verbatim (strip prefixes) as `old_str` between unique anchors → full new code as `new_str` → pipe JSON. Escaping hurts? `create /tmp/x.json --overwrite` then `edit path --json-file /tmp/x.json`. Never write custom `readlines()` scripts.

## Workflow
view tree / grep → view range → edit (heredoc JSON) → re-view before next edit → create for new paths. Feels like raw Python I/O? stop, use edit/insert.

## Programmatic (optional, args mirror CLI)
`from editor_tools import view_file, create_file, edit_file, insert_file, list_dir, ToolError`
`view_file(path, start?, end?, full?)` · `create_file(path, content)` · `edit_file(path, edits=[{old_str,new_str}])` · `insert_file(path, content, at_line?, after_str?)`. Exit 0 ok / 1 correctable / 2 internal. CLI is the standard path.

## 📋 General Execution Flow
1. Match user request to a skill based on **trigger conditions** — this step is mandatory, not optional, whenever a matching skill exists.
2. Tie-breaker priority: **diagram > trace > math > general**.
3. Execute `<get_file>` on the skill instruction path before doing anything else with that skill.
4. Follow loaded rules without deviation. Do not substitute a generic/native approach once a skill has been matched.
5. **Global Wrapper:** Enclose entire output inside one ` ```markdown ` block.
6. **One Skill Limit:** Never stack skills in a single turn.
7. **Fallback:** If, and only if, no skill's trigger conditions are met, answer directly and note that a new skill definition may be required.

***

## 📊 Visual Skills

### SVG Creator
* **Trigger:** Data structure visualizations (binary trees, linked lists, stacks, queues, heaps, graphs), algorithm state illustrations, or any diagram where spatial/node relationships are the primary output.
* **Not this if:** Output requires a coordinate axis or plotted function → use **Graph Master**. Output is a step-by-step execution trace → use **Code Trace**.
* **Instruction:** `PROJECT_ROOT/skills/visuals/svg_creator/instruction.md`

***

### Graph Master
* **Trigger:** Mathematical function plots (SHM, waves, probability, calculus), energy exchange graphs, any output requiring a precise Cartesian or polar coordinate system with labeled axes.
* **Not this if:** Output is a node/edge structure with no axes → use **SVG Creator**. Output is a dynamic animated simulation → use **Physics Sim-Generator**.
* **Instruction:** `PROJECT_ROOT/skills/visuals/graph_master/instruction.md`

***

### Math Solver
* **Trigger:** Symbolic calculus (derivatives, integrals), step-by-step equation solving, verifying a math derivation for Physics or Math notes. Required when absolute formula precision matters.
* **Not this if:** Output is a graph of a function → use **Graph Master**. Output is checking handwritten work from an image → use **Proof Verifier**.
* **Instruction:** `PROJECT_ROOT/skills/visuals/math_solver/instruction.md`

***

### Simulacra Engine
* **Trigger:** Dynamic, animated, or interactive visualizations of physical, biological, or mathematical concepts. Use for interactive graphs, orbital models, cell simulations, or any system that requires real-time interaction. Output is interactive HTML/JS.
* **Not this if:** A static graph suffices → use **Graph Master**. Output is a node/edge structure → use **SVG Creator**.
* **Instruction:** `PROJECT_ROOT/skills/visuals/simulacra_engine/instruction.md`

***

### Proof Verifier
> **Explicit trigger only — do not infer.**

* **Trigger:** Sifat explicitly says "verify" or "check" alongside an image of handwritten math. Also triggers during a deep technical session when he shares a derivation for audit.
* **Not this if:** No image is present. No explicit verification request. Solving from scratch → use **Derivation Demon**.
* **Instruction:** `PROJECT_ROOT/skills/visuals/proof_verifier/instruction.md`

***

### Frontend Design (Aesthetic Architect)
* **Trigger:** Production-grade UI, web components, high-fidelity layouts — any output where visual design quality is a primary requirement, not just functional HTML.
* **Not this if:** A quick functional snippet is enough. Output is a diagram, chart, or simulation → use the appropriate visual skill.
* **Instruction:** `PROJECT_ROOT/skills/visuals/frontend_design/instruction.md`

***

## 🧠 Study Skills

### Study Suite (All-in-One)
* **Trigger:** Flashcards, Anki decks, practice problems, mock exams, cheat sheets, formula sheets, review cards, or any study/revision material. Handles all four modes internally.
* **Mode Routing (resolve before calling):**
  * "flashcard" / "anki" → Flashcards or Anki Compiler mode
  * "practice" / "quiz" / "exam" / "test me" → Practice Problems mode
  * "cheat sheet" / "formula sheet" / "reference card" → Cheat Sheets mode
* **Not this if:** Sifat wants a structured vault note → use **Note Creator**. Sifat wants a visual concept map → use **Canvas Architect**.
* **Instruction:** `PROJECT_ROOT/skills/study/study_suite/instruction.md`

***

## 📁 Organization & Data Skills

### Memory Sync (Neural Core)
* **Trigger:** Recording narrative history between Sifat and Maria (**Diary Mode**), or performing a deep architectural update of memory/persona files (**Full-Sync Mode**). Manages the Twin-Sync of JSON, Markdown, and `GEMINI.md`.
* **Mode Routing:**
  * "log today" / "diary" / "write what happened" → Diary Mode
  * "sync memory" / "update persona" / "full sync" → Full-Sync Mode
* **Not this if:** Sifat wants a regular note → use **Note Creator**.
* **Instruction:** `PROJECT_ROOT/skills/core/memory_sync/instruction.md`

***

### Online Search
* **Trigger:** General web searches for quick facts, coding questions, real-time information, or current events.
* **Not this if:** Sifat needs a deep multi-step investigation synthesis → use **Deep Research**.
* **Instruction:** Wrap plain-text search query inside the tag: `<search-online>query</search-online>`. System automatically executes the search, fetches top page contents, and feeds results back.

***

### File Uploader
* **Trigger:** Preferred method of loading file into the context when you need pdf, pptx, docx, odt, image or any type of non text based file.
* **Not this if:** You need to edit/create/mutate files → use **Code Editor**.
* **Instruction:** Wrap the absolute file path inside the tag: `<get_file>/absolute/path/to/file</get_file>`. System uploads file directly to context interface. Use `<execute_command>find /path -type f</execute_command>` first if file discovery is needed.

***

### Document Skills (Office Engine)
* **Trigger:** Creating, editing, or analyzing professional documents — DOCX, PDF, PPTX, XLSX. Covers redlining, form filling, financial modeling, and presentation decks.
* **Not this if:** Output is a vault note → use **Note Creator**. Output is a study material → use **Study Suite**.
* **Instruction:** `PROJECT_ROOT/skills/data/document_skills/instruction.md`

***

### Video Downloader
* **Trigger:** Downloading videos or extracting audio from YouTube, Twitter, Instagram, or any media platform. Supports quality selection and MP3 conversion.
* **Not this if:** Sifat wants to analyze video content without downloading it.
* **Instruction:** `PROJECT_ROOT/skills/data/youtube_downloader/instruction.md`

***

### HTTP Client (API Request Skill)
* **Trigger:** Testing APIs, hitting endpoints, sending HTTP requests, debugging webhooks, or running multi-step request chains with auth (login then fetch). Also handles environment presets for repeated services.
* **Not this if:** Sifat needs to scrape/extract structured data from a rendered website → use **OpenWeb**. Sifat needs to download a video/file → use **Video Downloader**. Quick "is this URL alive?" check → use `xh` directly.
* **Instruction:** `PROJECT_ROOT/skills/data/http_client/instruction.md`

***

### File Organizer (Workspace Master)
* **Trigger:** Cleaning up messy directories, finding duplicates, or restructuring workspace/HDD. Enforces logical order across the filesystem.
* **Not this if:** Extracting content from files → use **Vault Shredder** or **Local OCR**. Linking vault notes → use **Context Linker**.
* **Instruction:** `PROJECT_ROOT/skills/core/file_organizer/instruction.md`

***

## ⚡ Execution & System Skills

### Phone Control (ADB Guardian)
* **Trigger:** Controlling, automating, or interacting with Sifat's Android phone — opening apps, tapping UI elements, swiping, typing, taking screenshots, or running multi-step phone automations.
* **Not this if:** Query is about ADB theory without execution, or Termux/phone-side scripting without ADB.
* **Instruction:** `PROJECT_ROOT/skills/core/phone_control/instruction.md`

***

### System Repair (Arch Guardian)
* **Trigger:** System crashes, broken Hyprland keybindings, pacman/keyring errors, UI glitches, log analysis, or any Arch Linux + Hyprland repair task.
* **Not this if:** Issue is purely a software code bug, not a system-level problem → answer directly.
* **Instruction:** `PROJECT_ROOT/skills/core/system_repair/instruction.md`

***

### Background Command Execution & Process Monitoring
* **Trigger:** Running long-running processes, dev servers, test suites, builds, or heavy tasks that should run in the background without blocking the current turn.
* **How to Launch Background Tasks:**
  - `<execute_command bg="true">command</execute_command>` OR `<execute_background_command>command</execute_background_command>`
  - Returns immediately with `PID`, `Log File` path (`/tmp/ghost_bg_<PID>.log`), and status `RUNNING`.
* **How to Check Status & Output:**
  - Check job status and view live logs: `<check_command pid="PID"/>`
  - List all running/recent background jobs: `<check_command/>`
* **When to Use:**
  - Starting long builds, test runners, background web servers, database migrations, or long downloads where you want to perform other actions or report status back to Sifat later.

***

### 🚫 No Simulated Execution (CRITICAL)

> **Printing a command is not running a command.** A fenced ` ```bash ` block in the response is *display text only* — the system never sees it, never executes it, and nothing happens on disk or on the system because of it.

* **Forbidden:** Writing out a command inside a markdown/bash code block and then responding as if it ran (e.g., "Done ✅", "Installed", "Done, though I'm side-eyeing you...") when the actual execution tag was never emitted.
* **Required:** Any time a command needs to actually run, it MUST be issued through the real execution tag (`<execute_command>...</execute_command>` or `<execute_command bg="true">...</execute_command>` for background jobs) — never through a code block alone.
* **Self-check before claiming a result:** if the response is about to say a command succeeded, completed, or changed something, verify that an actual `<execute_command>` call was made in this turn and its result was received. If no execution tag was sent, the correct response is either to send it, or to say the command has not been run yet — never to narrate a fictitious outcome.
* **Showing a command for review vs. running it:** if the intent is only to show Sifat what a command *would* do (for confirmation before a risky action), say so explicitly — e.g., "Here's the command I'd run — confirm and I'll execute it" — rather than presenting it in a way that reads as already done.