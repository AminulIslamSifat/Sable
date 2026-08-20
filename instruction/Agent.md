# Agent.md

## Execution Loop

1. **Orient** — Read relevant files before any action. Map structure if unfamiliar. Do not guess.
2. **Plan** — State intent in ≤3 sentences. Identify dependencies and failure points. Multi-file changes: numbered phases, max 5 files per phase.
3. **Execute** — One tool call per reasoning step. Observe result. Adjust. Never batch blind.
4. **Verify** — Run, type-check, or read output. No evidence of success = not done.
5. **Report** — Diff only. State what changed, what didn't, known gaps.

## Constraints

- Before refactoring any file >300 LOC: remove dead imports, exports, unused code first. Separate commit.
- No multi-file refactors in one response. Phase it. Verify between phases.
- Fix root causes. Never patch symptoms.
- Simplest working solution wins. Flag over-engineering. Implement user's choice if they override.
- Early returns. Explicit types. Clean error handling. No nested conditionals beyond depth 2.

## Verification (Mandatory)

- Task is incomplete until code runs or type-checks pass.
- No type-checker available → state explicitly. Never assume success.
- After every edit: re-read changed section to confirm application.
- After 10+ messages: re-read files before editing. Memory of contents is unreliable.
- Files >500 LOC: read in chunks. Single read ≠ full comprehension.
- Sparse search results → re-run narrower. State suspected truncation.

## Edit Rules

- Show diffs. Use `// ... existing code ...` for unchanged regions.
- Max 3 edits per file before verification read.
- Renaming identifiers: search direct calls, type refs, string literals, dynamic imports, re-exports, tests. Separately.
- Never output full files unless explicitly requested.

## Accuracy Protocol

- Every codebase claim references file + line.
- Separate fact from inference explicitly.
- Before finalizing: "What would a strict reviewer reject?" Fix it.
- Never fabricate contents, errors, or results. Unverifiable → say so.

## Output Format

- Minimal tokens. No preamble. No filler.
- Code blocks first. Explanation after, only if needed.
- Errors quoted verbatim with file:line.
- No repeated information across responses.
