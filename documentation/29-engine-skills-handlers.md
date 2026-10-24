# 29 — `engine/skills/handlers/`

> [!INFO] Why this file exists
> Priority 1 on the README's coverage list, and the largest single block outside the read set: **18 modules, 5,401 lines**, behind one dispatch table. [[04-engine-skills]] covers `parser.py` and `build_tool_feedback`; it never opened the handlers those tags dispatch to.
>
> Read end to end: `__init__`, `common`, `execute`, `file_ops`, `io`, `grep_search`, `ask_user`, `load_tool`, `browser`, `web`, `agents`, `tracknote`, `memory_manager`, `mcp_list`, `critique`, `image_generator`, `study`, `computer_use`. Not read: `dil_parser.py` (989L — covered via its JS twin in [[25-web-frontend-sse-and-components]]).

***

## 1. The dispatch surface

`__init__.py` is a 40-entry `HANDLER_MAP` from tag name to generator. Aliases matter — four names collapse onto two handlers:

| Tag(s) | Handler |
|:--|:--|
| `get_file`, `read_file` | `handle_get_file` |
| `web_search`, `web_fetch`, `online_search` | `handle_online_search` (dispatches on `urls`) |
| `memory`, `memory_manager` | `handle_memory` |
| `view_file`, `edit_file`, `create_file`, `insert_file` | `file_ops` — native calls into `tools/code_editor` |

Every handler has the same signature and the same shape:

```python
def handle_x(tag_id, name, attrs, content) -> Generator[dict, None, None]:
    started = time.time()
    ...                       # at least one _output_event
    yield _end_event(tag_id, name, ok, started, result, error)
```

**Every one of the 18 is a synchronous generator.** That single fact is the root of §7, and it is the most important structural finding in this document.

***

## 2. `common.py` — shared contract

| Constant | Value | Note |
|:--|--:|:--|
| `DEFAULT_TIMEOUT` / `MAX_TIMEOUT` | 15 / 180 | clamped in `execute` |
| `MAX_TEXT_BYTES` | 2 MB | `get_file` reads fully under this |
| `PREVIEW_BYTES` | 96 KB | over it, head-read only |
| `RESULT_PREVIEW_CHARS` | 75,000 | raised from 20k, per the ledger's "fixed earlier" entry |
| `SUDO_PASSWORD` | env | `SABLE_SUDO_PASSWORD`, empty by default |

`safe_under(base, raw)` resolves both sides and rejects anything not under `base` — the correct `resolve()`-based form, unlike `filesystem.py`'s lexical check (ledger #13).

`parse_editor_command` sniffs a shell command for `editor_tools.py <op> <path>` so `execute_command` can mirror a file mutation into a diff event. It skips `--content-file`, `--json-file`, `--start`, `--end` as value-flags and treats the first bare token as the path. Sound, but it means an editor invocation with an unknown value-flag misparses the flag's argument as the path.

`make_backup` snapshots to `.sable_backups/<stamp>_<uuid>_<name>` — see §3.1 for the double-backup it causes.

***

## 3. `file_ops.py` — native editor calls

A deliberate inversion of the old design: instead of shelling out to `editor_tools.py`, the handler imports it and calls the function directly. The `# ponytail:` comments say so — "direct import instead of subprocess — native call, zero overhead". That is correct and it is why `parse_editor_command`'s shell-sniffing path is now only a fallback for when the model calls the script explicitly.

### 3.1 `edit_file` takes two backups

```python
backup_path = make_backup(path) if not dry_run else None      # handler's own
...
output = _edit_file(path, payload, backup=True, replace_all=..., dry_run=dry_run)
```

`make_backup` copies the file to `.sable_backups/`, then `_edit_file(backup=True)` takes its own. **Every edit produces two backups of the same file.** The handler's copy is the one threaded into `build_file_edit_event(..., backup_path)` for the UI's revert button, so it cannot simply be dropped — but the `backup=True` argument is redundant and doubles I/O on every edit.

`handle_insert_file` does not pass a `backup` argument (`_insert_file` takes none), so insert gets exactly one. The asymmetry is invisible from the call sites.

### 3.2 `create_file` re-reads what it just wrote

For the `create` event, `build_file_edit_event` opens the newly written file and reads up to 64 KB to build the diff preview. `_create_file` had the content in hand moments earlier. Correct, wasteful, bounded — noted, not a bug.

### 3.3 The `view_file` path has no `safe_under`

`handle_view_file` calls `os.path.expandvars(os.path.expanduser(path))` and hands it straight to `_view_file`. There is no containment check here. Whatever policy `tools/code_editor` enforces is the only policy — and `handle_get_file` in `io.py` likewise resolves any path the model names. That is consistent with the tool being a general file reader, but it means the "path escapes target directory" guard in `common.safe_under` is used by exactly one caller (`create_note`).

***

## 4. `io.py`

### 4.1 Image reads leak a copy per call

```python
dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext or '.img'}"
shutil.copy2(path, dest)
```

Every `get_file` on an image copies it into `system/uploads/` under a fresh UUID and never removes it. Ten reads of the same screenshot leave ten copies. The `local_copy` key is genuinely needed (the connector inlines the on-disk file because the model cannot fetch `/system/uploads/`), so the copy is correct — the absence of any cleanup is the finding.

### 4.2 Text results carry no `text` key

The text branch reads the content, yields it as an `_output_event`, and sets only `preview_chars` on `result`. The `_end_event` result therefore has `path`, `size`, `mime`, `kind`, `preview_chars` — **no content**. Anything downstream that reads the structured result rather than the SSE text gets nothing. Probably intentional (the text goes over the event stream), but it is an undocumented asymmetry with the image branch, which does populate `local_copy`.

### 4.3 `create_note` writes non-atomically

```python
path.write_text(note_content, encoding="utf-8")
```

Violates README convention #2. Every other write in this directory either goes through `editor_tools` (atomic) or a tmp-then-`replace`. A crash mid-write leaves a truncated note, and there is no size cap on `note_content`.

***

## 5. `execute.py` — the largest and most carefully reasoned handler

### 5.1 What it blocks

Two command policies, both regex, both pre-spawn:

```python
_SABLE_SERVICE_RE = re.compile(r"systemctl\s+(--user\s+)?(restart|stop)\s+sable\.service")
_GIT_CHECKOUT_RE = re.compile(r"\bgit\s+checkout\b")
```

Restarting `sable.service` from inside a Sable turn would kill the turn; `git checkout` destroys uncommitted work. Both produce a `[BLOCKED]` stderr line and a failed `_end_event`.

> [!NOTE] Both are pattern matches, not parsers
> `systemctl restart sable.service --now` matches. `systemctl --user restart sable.service` matches. `sudo systemctl restart  'sable.service'` (quoted) does not. `git checkout-index` does not match `\bgit\s+checkout\b`. These are guardrails against the model doing the obvious thing, not a security boundary — and they are correctly scoped as such.

### 5.2 `sudo` injection

```python
if use_sudo and "sudo -S" not in cmd:
    cmd = _SUDO_TOKEN_RE.sub("sudo -S -p ''", cmd, count=1)
```

`(?<![\w-])sudo(?![\w-])` — word-boundary safe, so `sudoers` and `my-sudo` are untouched, and `count=1` means only the first. The password is then written to stdin and stdin closed. POSIX-only (`IS_WINDOWS` guard), and it correctly leaves an already-`sudo -S` command alone.

> [!WARNING] The password is only injected for `sudo` at position 0
> `use_sudo = cmd.lstrip().startswith("sudo ")`. A command like `cd /x && sudo systemctl ...` does **not** get stdin wired, so `sudo -S` has nothing to read and the command hangs on the password prompt until the timeout. The regex would have rewritten it to `sudo -S` — the rewrite runs on `cmd` regardless of `use_sudo` — so the result is a `-S` invocation with a closed stdin. See ledger #46.

### 5.3 The process-group capture is the key fix in the file

```python
_pgid = os.getpgid(proc.pid)   # captured AT SPAWN
```

Both the comment and the code are right about why: the group outlives its leader, and once `proc.wait()` reaps the shell, `/proc/<pid>` is gone and `getpgid(pid)` can never resolve the group again. Every kill path — the timer, the in-loop deadline, and the `finally` — therefore uses `_pgid`, not `proc.pid`.

The `finally` comment records the actual bug this replaced:

> *"Previously this block only cancelled the timer, i.e. it DISARMED the one thing that would ever have killed the child, then walked away. Any command still running at Stop time was orphaned deliberately."*

And the second half:

> *"The old `if proc.poll() is None:` guard was wrong in both directions: Normal completion — the shell has already exited and been reaped by proc.wait(), so poll() is not None -> the kill was SKIPPED entirely. Every `nohup x &` inside a foreground command leaked its child."*

So the unconditional `_kill_group(SIGKILL)` in `finally` is deliberate: **a foreground `execute_command` never leaves descendants behind, even ones the command intended to background.** That is a policy decision with a real cost — `nohup ./server &` from `execute_command` dies at the end of the command. `bg=true` is the supported path for that, and §5.5 shows it has its own gaps.

### 5.4 The in-loop deadline kills the wrong handle

```python
if time.time() - started > timeout:
    kill_process_tree(proc.pid, sig=signal.SIGKILL)   # <- pid, not _pgid
    break
```

Three kill mechanisms now exist and they disagree:

| Mechanism | Targets |
|:--|:--|
| `threading.Timer._enforce_timeout` | `_kill_group` → `killpg(_pgid)` |
| in-loop deadline | `kill_process_tree(proc.pid)` — **pid tree, not group** |
| `finally` | `_kill_group` → `killpg(_pgid)` |

The in-loop one is the odd one out. It is also the one that fires when the child keeps printing past the deadline — exactly the case where a forked grandchild is most likely. `finally` catches it afterwards, so the end state is correct; the immediate kill is narrower than intended.

### 5.5 Background jobs: three gaps

`handle_execute_background_command` is a much simpler path, and it drops most of what the foreground handler guarantees:

| Foreground | Background |
|:--|:--|
| `cwd=` attr honoured | **always `home_dir()`** |
| `env=` attr merged | **ignored** |
| sudo stdin injected | **not supported** |
| timeout enforced | none — runs until killed |
| `_SABLE_SERVICE_RE` / `_GIT_CHECKOUT_RE` blocks | **not applied** |

The policy blocks not applying is the notable one: `execute_command(bg=true)` with `systemctl restart sable.service` is not blocked, because the block lives in the foreground branch and `handle_execute_command` returns to the background handler *before* either regex runs.

`BG_JOBS` is a module-level `dict[int, dict]` that is only ever removed by `kill_command`. A background job that exits on its own stays in the map forever, and nothing prunes it. `check_command` re-reports dead jobs as `exited` on every call rather than evicting them.

### 5.6 A bad `pid` silently lists everything

```python
pid_raw = attrs.get("pid") or content.strip()
try:
    pid = int(pid_raw)
except Exception:
    pid_raw = ""            # <- falls through to the list-all branch
```

`check_command(pid="abc")` does not error; it clears `pid_raw` and drops into the "list every tracked job" path. The user asked about one job and got a directory listing.

***

## 6. `ask_user.py` — the escape hatch can be truncated away

`_parse_options` is a four-strategy ladder (JSON → `ast.literal_eval` → comma-split → quoted-substring regex), each documented with the exact mangling it exists for. That is good defensive parsing.

The bug is three lines later:

```python
_OTHER = "Other (type manually)"
if not any(o.strip().lower().startswith("other") for o in options):
    options.append(_OTHER)          # now len == 9 when the model gave 8
options = options[:8]               # <- Other is the 9th element and is DROPPED
```

The comment above it states the invariant: *"Models drop it constantly; the UI needs a manual-entry path regardless."* The slice then violates it for any model that supplies 8 options — which is exactly the maximum the schema allows, so a compliant model always loses it.

Worse, `default` is bounds-checked **after** the slice, so a `default=8` that pointed at `Other` is silently discarded rather than clamped.

> [!CAUTION] Fix is a reorder, not a rewrite
> Slice first, then append: `options = options[:7] + [_OTHER]` when `Other` is absent, or `options = options[:8]` then append if still under 8. The current order is the only thing wrong.

Strategy 3 also splits on `,` unquoted, so an option containing a comma becomes two options. Lower severity — strategies 1 and 2 run first and only fail on genuinely malformed input.

***

## 7. Blocking calls inside synchronous generators

The README's convention #4 says *"`asyncio.to_thread` for every blocking call"*. Six handlers call `subprocess.run` or a blocking HTTP client directly, inside a `def`-generator:

| Handler | Blocking call | Worst-case block |
|:--|:--|--:|
| `image_generator` | `subprocess.run` × 5 providers | **180 s** (`_try_dreamforge`, `_try_advanced_sdxl`) |
| `computer_use` | `subprocess.run` | 30 s default, uncapped by attr |
| `tracknote` | `subprocess.run` | 15 s |
| `memory_manager` | `subprocess.run` | 15 s |
| `web.handle_fetch_docs` / `handle_openweb` | `httpx.get` | 20 s / 15 s |
| `image_generator._try_pollinations` | `urllib.request.urlopen` | 60 s |

A synchronous generator's body runs on whatever thread drives it. If the skill loop drives these from the event loop, a single image generation with the DreamForge fallback stalls **every** concurrent request for up to three minutes. `image_generator`'s `auto` chain can chain several of these in one call.

This is not a subtle bug — it is a convention the directory does not follow, and the largest offender is the one with a 180-second timeout.

***

## 8. `agents.py`

Subagents reuse the main chat pipeline: `spawn_agent` persists an `Agent` row, pushes `agent_start` + `agent_trigger` onto the agent-events SSE queue, and the frontend fires a normal `POST /api/chat`. No second LLM loop — that is a good design and the docstring explains it.

Three defects:

1. **Four `logger.warning("[AGENT_DEBUG] ...")` calls** left in the spawn path. They fire on every spawn at WARNING level.
2. **`from pathlib import PurePosixPath` inside the agent loop** — re-imported per running agent on every spawn.
3. **Private access**: `runtime._agents`, `runtime._max_agents` reached from a handler rather than through `list_agents` / a capacity method.

`_sync_agent_context` constructs a whole `ChatService` per agent and closes it — correct, but it means spawning a Qwen agent creates and tears down a service object before the frontend's own chat turn creates another.

***

## 9. `browser.py`

40+ action verbs in one `if/elif` ladder, all routed to a persistent in-process Playwright session. Structurally the cleanest large handler: every branch assigns `result`, the tail normalises it (`if not isinstance(result, dict): result = {"result": result}`), and the screenshot is popped and re-emitted as `screenshot_path` / `screenshot_url` before the JSON preview.

> [!WARNING] `_launch_opts` raises outside the try block
> ```python
> elif attrs.get("width") or attrs.get("height"):
>     opts["viewport"] = {"width": int(attrs.get("width") or 1440), ...}
> ```
> `handle_browser` computes `launched_now` and calls `_launch_opts(attrs)` **before** the main `try:` that wraps every action. A non-numeric `width="abc"` raises `ValueError` out of the generator entirely, which is a different failure mode from every other bad-attribute case (which yields a `skill_end` with `error=`). Same for `int()` on `nth`, `timeout_ms`, `amount`, `index` inside the try — those are caught.

Two deliberate omissions worth recording: `click` drops the `selector` when taking a post-click screenshot (the element is usually gone, and screenshotting it stalls 30 s waiting on a detached handle); `snapshot` returns only the count, not the refs, because the text already carries them.

***

## 10. `web.py`

`handle_online_search` dispatches on the presence of `urls` rather than on the tag name, so `web_search`, `web_fetch` and `online_search` all land here. `max_chars` is clamped to `[1000, 100_000]` with an explicit comment about negative slicing — good catch, since `text[:-5]` on a negative would silently drop the tail instead of erroring.

Every result is wrapped with `wrap_untrusted(..., source=...)` before reaching the model. That is the right default for scraped content and it is applied consistently across search, fetch, openweb and context7.

`_pick_library` carries an unusually candid docstring:

> *"Do NOT re-sort by trust. Context7's `score` is already relevance-ranked and correct… An earlier version sorted trust-first and demoted it to 5th, behind random verified packages — over-engineering that made resolution strictly worse."*

One real leak: `handle_openweb`'s fetch path calls `strip_html(res.text)` with **no size limit** — the full body is materialised and stripped before `[:RESULT_PREVIEW_CHARS]` is applied. `handle_fetch_docs` has the same shape for `res.text`.

***

## 11. `critique.py`

Parses `content` as JSON and **ignores `attrs` entirely**. If a model emits `<critique context="..." criteria="..."/>` with attributes and no body, `json.loads("")` raises and the handler yields `Invalid JSON arguments` — even though every parameter was supplied correctly in the form the parser also accepts. Every other handler in this directory reads `attrs` first and falls back to `content`.

The persona-override block in the message is a notable fix, documented in a `# ponytail:` comment: without it the model receives Maria's system prompt and a clinical evaluation request, gets confused, outputs one sentence and stops.

***

## 12. `image_generator.py`

A seven-provider fallback chain: cloudflare → pollinations → perchance → dreamforge → advanced-sdxl, plus puter, selected by `provider=` with `auto` trying cloudflare first.

The model-ID namespace guard is the good part:

```python
_PROVIDER_MODEL_PREFIX = {"cloudflare": "@cf/", "puter": "openai/", ...}
```

A Cloudflare model ID sent to Puter fails deep in the provider with a cryptic error; this catches it up front and substitutes the provider default. Correct instinct — the failure it prevents is exactly the kind that costs an hour.

Two gaps:

- **`auto` with `count > 1` falls back to Perchance, not Pollinations**, even though the single-image path tries Pollinations. The two branches disagree about the chain order.
- **`_STYLE_PREFIXES` is applied for every provider except Perchance** (`_apply_style` is only called when `style != "no_style"`, and `_try_perchance` receives the raw `prompt`). That is intentional — Perchance has its own 36-preset style system — but it means the same `style=` produces visibly different art depending on which provider answered, which is invisible to the caller.

***

## 13. `study.py`

Four tools (`study_document`, `study_note`, `study_pdf`, `study_diagnostics`), 40+ actions between them, all speaking to `engine.study.store` rather than the filesystem so a highlight written by the model appears in the reader immediately.

The ambient-state pattern is worth naming: `_resolve_doc_id` falls back through **explicit attr → `get_study_context().doc_id` → JSON content blob**, and `_current_page` does the same for the page. That is what lets the model say "highlight this" without repeating the document ID on every turn.

`_parse_pages` handles `"3"`, `"3-12"`, `"1,4,9"`, de-duplicates while preserving order, and caps — correctly ordered before the cap, so `"1-100"` with `cap=10` gives pages 1–10 rather than failing.

The page caps are asymmetric and intentional: `_MAX_IMAGE_PAGES = 10`, `_MAX_TEXT_PAGES = 30`. Ten page images is already ~10 MB of base64 in one SSE message.

No findings here. It is the cleanest handler in the directory.

***

## 14. Findings

| # | Finding | Severity | Status |
|--:|:--|:--|:--|
| 44 | `ask_user`: `Other` is appended then sliced away by `options[:8]` — a model supplying 8 options always loses the manual-entry path the code exists to guarantee. | 🔴 high | **not fixed** |
| 45 | `execute`: `sudo -S` is rewritten into `cmd` whenever the command *starts* with `sudo`, so `cd x && sudo …` gets `-S` with no stdin wired and hangs to timeout. | 🟠 medium | **not fixed** |
| 46 | `execute`: in-loop deadline calls `kill_process_tree(proc.pid)` while every other kill path uses `killpg(_pgid)`. | 🟡 low | **not fixed** |
| 47 | `execute`: `execute_command(bg=true)` bypasses both `_SABLE_SERVICE_RE` and `_GIT_CHECKOUT_RE`, ignores `cwd=`/`env=`, and has no timeout. | 🟠 medium | **not fixed** |
| 48 | `execute`: `BG_JOBS` is never pruned; `check_command(pid="abc")` silently lists all jobs instead of erroring. | 🟡 low | **not fixed** |
| 49 | `file_ops`: `edit_file` takes two backups — `make_backup()` in the handler plus `_edit_file(backup=True)`. | 🟡 low | **not fixed** |
| 50 | `io`: every `get_file` on an image leaves a permanent copy in `system/uploads/`. | 🟡 low | **not fixed** |
| 51 | `io`: `create_note` writes with a bare `write_text` — violates convention #2, no size cap. | 🟡 low | **not fixed** |
| 52 | Six handlers make blocking `subprocess.run`/`httpx.get` calls inside synchronous generators; worst case 180 s in `image_generator`. | 🟠 medium | **not fixed** |
| 53 | `browser`: `_launch_opts` runs before the handler's `try`, so `width="abc"` raises out of the generator instead of yielding a failed `skill_end`. | 🟡 low | **not fixed** |
| 54 | `critique` ignores `attrs` entirely — an attribute-only invocation always fails with "Invalid JSON arguments". | 🟡 low | **not fixed** |
| 55 | `agents`: four `logger.warning("[AGENT_DEBUG] …")` calls on the spawn path, plus `PurePosixPath` imported inside the loop and private `runtime._agents` access. | 🔵 cosmetic | **not fixed** |
| 56 | `web`: `handle_openweb` and `handle_fetch_docs` materialise the full response body before applying the preview cap. | 🟡 low | **not fixed** |

One 🔴 in 5,401 lines — and it is a three-line ordering mistake in the one handler whose entire job is to not lose a user's answer.

***

## 15. What this changes upstream

- `documentation/README.md`: coverage tables, ledger #44–#56, priority list.
- [[04-engine-skills]]: cross-reference added — it documents the parser, not the handlers.

**Coverage after this batch:** Python 51,500 → 56,900 / 122,907 = **46.3%**. Overall ~57,800 → ~63,200 / 200,884 = **31.5%**.

**Next on the list:** `web/js/markdown.js` (809), `connectors/common/openai_compat.py` (989), `skills/text_humanizer/scripts/humanize.py` (1,052).
