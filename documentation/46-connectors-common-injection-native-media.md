---
tags: [connectors, prompt-injection, native-tools, media]
status: full-read
read: prompt_injection.py (283L), native_tools.py (346L), media.py (335L)
---

# 46 — Injection Toggles, Native Tools, Media

> The last three substantial files in `connectors/common/`. 964 lines. One dead streaming branch, one URL-parsing mistake, and a large amount of clean code.

| File | Lines | Role |
|:--|--:|:--|
| `native_tools.py` | 346 | OpenAI↔Gemini schema conversion, function-call parsing, result formatting |
| `media.py` | 335 | MIME detection, base64 inline files, doc→image conversion |
| `prompt_injection.py` | 283 | Per-provider toggles for every prompt fragment |

***

## 1. `prompt_injection.py` — the design is right, one lookup is wrong

The contract is stated up front and enforced everywhere:

> *Disabling a key means the fragment is NEVER added — the model sees none of it, not even a stub.*

`INJECTION_KEYS` lists 14 keys split into two families — 9 system-prompt keys (consumed by `build_instructions`, doc 42) and 5 message-prefix keys (consumed by `chat.py`). Doc 42 verified all 9 system keys are gated. This file owns the resolution.

### 1.1 Defaults-all-on is the right call and is documented

```python
def _defaults() -> dict[str, bool]:
    return {k: True for k in INJECTION_KEYS}
…
# Unknown providers fall back to all-on so a new connector is never
# silently starved of context.
```

A new provider added tomorrow works without touching this file. That is the correct default for a feature whose failure mode is *silence*.

### 1.2 Custom-endpoint fallback parses the URL as a provider name

```python
if prov.startswith(ENDPOINT_PREFIX):
    base = cfg.get(prov[len(ENDPOINT_PREFIX):].split(":")[0]) or cfg.get("local")
    if base is not None:
        return dict(base)
```

`prov` is `"ep:http://192.168.1.5:8080/v1"`. So:

```
prov[len("ep:"):]        = "http://192.168.1.5:8080/v1"
              .split(":")[0] = "http"
cfg.get("http")          = None        ← always
or cfg.get("local")      = the row    ← always
```

The `split(":")[0]` was clearly meant to extract the *backend* from the endpoint. It extracts `"http"` instead, because the first colon in a URL is in the scheme. The `or cfg.get("local")` then saves it — every custom endpoint has `api_backend="local"`, so `local` is the correct answer.

Correct by accident. The `cfg.get("http")` half can never succeed, and if the fallback key were ever anything other than `local`, an unconfigured endpoint would silently get all-on defaults while its configured siblings did not. Finding #146, 🟡 — latent, but it is a lookup that cannot work.

### 1.3 Endpoint rows are keyed by URL, and the comment says why

```python
# Real user-added endpoints are appended at runtime by ``list_providers()``
# below, keyed by endpoint URL rather than by backend (every custom endpoint
# resolves to api_backend="local", so keying on the backend alone would merge
# them all into one row).
```

That is the right key and the right justification. Two models on one base URL share one row (`list_custom_endpoints` groups by URL and relabels to `"<host> (N models)"`), and the emoji-suffix strip — `label.split(" 🧠")[0].split(" 🔧")[0]` — removes the feature badges the model picker appends.

One consequence worth naming: `label.split(" 🧠")[0]` means a model legitimately named with a brain emoji gets truncated. Acceptable, and the cost of not having a structured field.

***

## 2. `native_tools.py` — the streaming branch cannot be reached

```python
def parse_openai_function_call(delta_or_message: dict) -> list[dict[str, Any]] | None:
    # Non-streaming: message.tool_calls
    tool_calls = delta_or_message.get("tool_calls")
    if tool_calls:
        … return calls if calls else None

    # Streaming delta: delta.tool_calls (incremental)
    delta_tc = delta_or_message.get("tool_calls")   # ← the same key
    if delta_tc:
        … return calls if calls else None
```

Both blocks read `delta_or_message.get("tool_calls")` from the same dict. If it is truthy the first block returns; the second is only reached when it is falsy, in which case `delta_tc` is falsy too.

**The streaming branch is unreachable.** Its output shape — `{"name", "args_partial", "id", "index"}` — is never produced by this function, so any caller expecting incremental arguments gets the non-streaming shape or `None`.

> [!NOTE] Not currently load-bearing
> The only production consumer is `connectors/local/client.py` (`:268`), which calls `native_call_to_tag_event(fc)`. That path uses the non-streaming branch, which is correct. The dead branch is a bug waiting for the first streaming consumer — and the shape it promised (`args_partial`) is exactly what `native_preview_lines` was written to render.

Finding #147, 🟡.

### 2.1 `_clean_gemini_params` has a self-assignment

```python
# Convert type aliases
if p.get("type") == "integer":
    p["type"] = "integer"  # Gemini accepts this
```

Assigns a value to itself. The comment says Gemini accepts `integer`, which is the *reason no conversion is needed* — but the branch was clearly meant to convert something. Harmless, and the comment makes the intent recoverable.

### 2.2 `_partial_string_value` is careful and correct

A hand-written partial-JSON string scanner, because native tool arguments stream as raw fragments and mid-stream the buffer does not parse. It:

- skips whitespace (`chr` set of 32/9/13/10),
- handles `\uXXXX` via `int(hexs, 16)`,
- maps the seven single-char escapes,
- tolerates an unterminated value (loop ends at buffer end).

Everything is built from `chr()` — `q = chr(34)`, `bs = chr(92)` — so the source never fights quoting. Same instinct as `editor_tools.py`'s `chr(60)` and `render-html.js`'s array-join bridge.

One soft spot: an invalid `\u` escape falls through `except ValueError: pass` into `out.append(esc_map.get("u", "u"))`, appending a literal `u`. Degrades rather than raising, which is right for a preview.

### 2.3 `native_preview_lines` mirrors the text-parser card exactly

```python
"""Mirrors SkillParser._xml_preview_lines so a native call renders the same
card the XML/JSON text paths do: path, then command, then the trailing
lines of the body being written."""
```

Path → first non-blank command line → last three body lines → fallback `"streaming… N B"`. The fallback is the important part: a native call with an unparseable buffer still renders *something*, so the card never sits blank.

***

## 3. `media.py` — careful conversion, two orphans, one leak

### 3.1 The MIME table is the useful part

`_EXTRA_MIMES` covers ~35 extensions Python's `mimetypes` misses — HEIC/HEIF/WebP, five video containers, four audio formats, and **twenty source-code extensions** (`text/x-go`, `text/x-rust`, …). The comment names the reason: *"Qwen parses them as text"*. Sending `.rs` as `application/octet-stream` makes a connector treat it as binary and refuse it.

`categorize()` then buckets by prefix, with `application/pdf` and all of `text/*` landing in `document`. Per-category limits are explicit, including the `other` fallback.

### 3.2 The video limit is a payload-size decision, not a media one

```python
"video": 100 * 1024 * 1024,   # 100 MB
```

100 MB becomes ~133 MB of base64 inside a JSON request body. That will fail at the provider long before this limit fires. Not filed — the limit is a ceiling, not a promise, and every caller can pass `max_bytes`.

### 3.3 `_convert_pdf_to_images` never removes its tempdir

```python
out_dir = Path(tempfile.mkdtemp(prefix="sable_doc2img_"))
…
pix.save(str(img_path))
```

`mkdtemp` creates a directory nothing owns. Up to 20 PNGs at 200 dpi (a few MB each) per conversion, never cleaned. `_convert_pptx_to_images` has the same shape plus a second `mkdtemp` for the LibreOffice profile — which *is* deliberate and commented (*"Kept OUTSIDE out_dir so it can't be mistaken for a converted PNG"*). Finding #148, 🔵.

### 3.4 `to_anthropic_source` and `convert_doc_to_images` have no production callers

```
$ grep -rn 'to_anthropic_source' connectors/ | grep -v backups
connectors/common/media.py:172:def to_anthropic_source(...)

$ grep -rn 'convert_doc_to_images' connectors/ | grep -v backups
connectors/common/media.py:194:def convert_doc_to_images(...)
```

One hit each — the definition. `to_anthropic_source` has no Anthropic API connector to serve (Claude is driven through the scraper, doc 42). `convert_doc_to_images` is the fallback for models that accept images but not documents, and nothing calls it.

Both are complete, correct implementations of a capability that was never wired up. Finding #149, 🔵.

### 3.5 The PPTX path is the better one

```python
profile = Path(tempfile.mkdtemp(prefix="sable_lo_profile_"))
subprocess.run([lo_bin, "--headless", "--norestore",
                f"-env:UserInstallation={profile.as_uri()}", …], timeout=120, check=True)
```

Isolated LibreOffice profile so a running desktop instance cannot hold the default lock; `as_uri()` for the Windows-correct `file:///C:/…` form; `timeout=120`; `check=True`. That is the same set of decisions `library_ingest.convert_to_pdf` (doc 36) makes, and both files got them right independently.

It also does a cheap slide-count check via `python-pptx` **before** invoking LibreOffice — and if that import fails it proceeds anyway, with the comment *"LibreOffice will handle it"*. Correct: the pre-check is an optimisation, not a guard.

***

## 4. Findings

**No 🔴, no 🟠.** Two 🟡, both latent; the rest is dead code and a temp-dir leak.

### 🟡 Correctness

**#146 — `injection_enabled_map` parses a URL as a provider name.** `prov[len("ep:"):].split(":")[0]` yields `"http"` for every endpoint URL, so `cfg.get("http")` is always `None` and the `or cfg.get("local")` fallback always decides. Correct today because every custom endpoint is `api_backend="local"`; wrong the moment that assumption changes, and the first half of the expression can never succeed.

**#147 — `parse_openai_function_call`'s streaming branch is unreachable.** Both blocks read `delta_or_message.get("tool_calls")`; the first returns whenever it is truthy. The incremental shape (`args_partial`, `index`) is never produced. No current consumer needs it — `connectors/local/client.py` uses the non-streaming path — but `native_preview_lines` exists specifically to render streaming partials.

### 🔵 Dead code / cosmetic

| Where | What |
|:--|:--|
| `media.to_anthropic_source` | No callers, and no Anthropic API connector exists (**#149**) |
| `media.convert_doc_to_images` | No callers — the doc→image fallback was built and never wired (**#149**) |
| `media._convert_pdf_to_images` / `_convert_pptx_to_images` | `mkdtemp` output never removed — up to 20 PNGs per conversion (**#148**) |
| `native_tools._clean_gemini_params` | `p["type"] = "integer"` when `p["type"] == "integer"` — self-assignment |
| `prompt_injection.list_custom_endpoints` | `label.split(" 🧠")[0]` truncates a model legitimately named with that emoji |
| `media.DEFAULT_LIMITS` | `video: 100 MB` → ~133 MB of base64 in one JSON body; the provider rejects it first |

***

## 5. What's right, and worth copying

1. **Defaults-all-on for a feature whose failure mode is silence.** A new connector works without touching the config.
2. **Custom endpoints keyed by URL, with the reason written down** — every custom backend is `local`, so a backend key would merge them all.
3. **`_partial_string_value` built entirely from `chr()`.** A hand-written partial-JSON scanner that never fights source quoting, and tolerates an unterminated value.
4. **`native_preview_lines` mirroring the text parser's card**, with a `"streaming… N B"` fallback so the card is never blank.
5. **20 source-code MIME mappings, with the failure named** (*"Qwen parses them as text"*).
6. **The LibreOffice invocation: isolated profile + `as_uri()` + timeout + `check=True`.** Independently arrived at twice in this codebase.

***

## 6. What this changes upstream

- **`connectors/common/` is now read in full** — 9 modules, ~4,930 lines: docs 27, 30, 42, 46. Remaining small files: `browser_errors.py` (219), `jitter.py` (169), `context_summarizer.py` (162), `history_budget.py` (89), `error_ctx.py` (84) — 723 lines.
- **#147 pairs with `native_preview_lines`**: the renderer for streaming partials exists and the parser that would feed it is dead.
- Python coverage: **~77,270 → ~78,234 / 137,031 = ~57%**. Application Python: **~64%**. Overall: **~41%**.

***

## Cross-references

- [[42-connectors-common-shared-layer]] — `instruction_builder`, which consumes all 9 system-prompt injection keys
- [[30-markdown-compat-humanizer]] — `openai_compat.py`, the API-side consumer of `native_tools`' schema helpers
- [[36-library-stack]] — `library_ingest.convert_to_pdf`, the other correct LibreOffice invocation
- [[12-connectors]] — the provider contract; no Anthropic API connector exists, which is why #149 is dead
