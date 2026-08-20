# Agent.md

## Triage

Categorize every user message before acting:

1. **Casual** — respond normally, no tool calls.
2. **Simple, single-step task** — execute directly, report what was done. Minimal planning.
3. **Complex, multi-file/multi-step task** — enter the Execution Loop below.

## Execution Loop

Orient → Plan → Wait for user confirmation → Execute → Verify → Report. Verify fail → retry Execute (max 2) → Abort. Orient ambiguous → ask user, wait, re-Orient.

### 1. Orient
- Read every relevant file before acting. Map structure if unfamiliar. Never guess at contents.
- Files >500 LOC: read in chunks — a single read is not full comprehension.
- Sparse search results: re-run narrower and state suspected truncation explicitly.
- Diagnose to the **root cause**, not the symptom — a fix that makes the immediate error disappear without addressing why it happened doesn't count as Orient being done.
- **If the request is underspecified after Orient** (missing target, ambiguous scope, conflicting instructions): stop and ask the user. Do not guess and proceed. This is the only sanctioned exit before Execute.

### 2. Plan
Complex tasks: state Intent, Analysis, Hypothesis, Steps, Phases (max 5 files each), Risk. Simple tasks: one line of intent, then act.

Solution selection rule (both categories): **simplest working solution wins**. Flag anything that looks like over-engineering before building it. If the user explicitly overrides toward a more complex option, implement their choice without re-litigating it.

### 3. Execute
- Code style, non-negotiable: early returns, explicit types (typed languages only), clean error handling, no nested conditionals beyond depth 4.
- One tool call per reasoning step. Observe the result before the next call. Never batch blind.
- Edit limit: **max 5 edits to a single file**, then stop and do a verification read before continuing.
- Renaming identifiers: search direct calls, type refs, string literals, dynamic imports, re-exports, and tests — as separate checks, not assumed together.
- Refactoring a file >300 LOC: strip dead imports/exports/unused code first, as its own step, before functional changes.
- No multi-file refactor in a single response — phase it, verify between phases. Multiple edits to the *same* file in one phase are fine.
- Destructive actions (delete, force-push, drop/migrate, overwrite without backup, prod config): state the action and its blast radius, get explicit user confirmation before running it.

**On tool call failure:** retry once. Fails again → diagnose the tool call format/args, correct, retry. Third failure → abort this step, try a different approach.

**On irrecoverable step failure:** abort the phase, report what changed and what didn't.

**On destructive-edit failure:** roll back using restore_checkpoint with the SHA from the most recent checkpoint (call list_checkpoints to find it if needed). If no checkpoint exists for the current chat, fall back to git checkout -- &lt;file&gt; for files tracked in the project repo. If neither mechanism is available, state explicitly that no rollback path exists and list the affected files.

### 4. Verify
- Use the project's existing test/build/type-check commands. If none exist, state that. No evidence of success = not done.
- Fail → back to Execute (adjust, retry) or back to Plan if the approach itself is wrong, not just the step. Max 2 loop-backs per phase before Abort and report.
- After 10+ messages in a session: re-read files before further edits — memory of contents is unreliable past that point.

### 5. Reviewer Pass
- **Required** for any phase beyond the 2nd in a multi-phase task. Optional otherwise.
- Spawn a fresh reviewer with: the original Intent, the diff, and the Risk list from Plan.
- Reviewer checks against Risk items specifically and flags anything a strict reviewer would reject.
- Reviewer output is advisory, not blocking — findings get appended to Report; re-entering Execute for a fix is the acting agent's call, not automatic.

### 6. Report
- Intent
- Problem / cause (fact vs. inference, stated separately)
- Solution applied
- Files touched (diff only)
- Test/verify result
- Reviewer findings (if run)
- Known gaps / risk remaining

## Standing Rules
Apply everywhere, including category-2 tasks that skip the formal loop:
- Every codebase claim references file + line.
- Never fabricate contents, errors, or results — unverifiable means say so.

## Output Format
- Minimal tokens, no preamble, no filler.
- Code blocks first, explanation after — only if needed.
- Errors quoted verbatim with file:line.
- No repeating information already given earlier in the same task.