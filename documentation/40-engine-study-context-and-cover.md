---
tags: [engine, study, contextvars, html-templates]
status: full-read
read: study/context.py (51L), study/cover_html.py (231L), study/cover_html_elegant.py (227L), study/__init__.py (53L)
---

# 40 — Study Context, and the Cover Templates

> The last four files in `engine/study/`. Two of them are the best-commented code in the repository; one is a ContextVar with an asymmetric reset discipline.

| File | Lines | Role |
|:--|--:|:--|
| `study/context.py` | 51 | Per-turn ambient study state via `ContextVar` |
| `study/cover_html.py` | 231 | Classic cover template + shared helpers + dispatcher |
| `study/cover_html_elegant.py` | 227 | Elegant cover template |
| `study/__init__.py` | 53 | Re-exports from `store.py` |

***

## 1. `context.py` — the reset discipline is not uniform

Skill handlers receive only `(tag_id, name, attrs, content)`. There is no argument for "which document is open", so a `ContextVar` carries it down from the chat route to the handlers.

```python
def set_study_context(ctx): return _study_ctx.set(ctx)   # returns a token
def reset_study_context(token): …                        # resets to the token
def get_study_context(): return _study_ctx.get()
```

`reset_study_context` swallows `ValueError` / `LookupError` / `TypeError` from `_study_ctx.reset(token)` — a token reset from a different context would otherwise raise. Defensive, and correct.

### 1.1 Three call sites, two disciplines

| Site | Sets | Resets |
|:--|:--|:--|
| `study_ai.py:1360` | ✅ | ✅ `1375` in `finally` |
| `study_ai.py:1604` | ✅ | ✅ `1617` in `finally` |
| **`chat.py:1755`** | ✅ | ❌ **none** |

`chat.py` carries this comment:

```python
# Study tools (highlight / note / pdf read) read their document via a
# ContextVar. Async generators run in their own context, so setting it
# here is isolated to this turn and discarded when the generator ends.
```

The conclusion is probably right and the stated reason is not. PEP 568 — giving generators their own context — was **not** accepted. An async generator body runs in the context of whoever drives it. What actually bounds the leak is that Starlette runs each request in its own `anyio` task, and the task's context dies with it.

So: no leak today, but the safety comes from the framework's task model, not from the generator. The two `study_ai.py` sites reset with a token because they are *not* relying on that. Finding #124 is the asymmetry plus the inaccurate comment — not a live bug.

> [!NOTE] Why this is worth recording anyway
> The comment tells the next reader that generators have their own context. They do not. If anyone moves this code into a shared task, a background worker, or a generator consumed outside a request, the value persists — and the comment says it cannot.

***

## 2. `cover_html.py` — shared helpers and the classic template

### 2.1 The CSS is a plain string, deliberately

```python
'''… 
Kept apart from the route module so the (large, brace-heavy) CSS never has to
survive an f-string. ``build_cover_html`` is the only public entry point.
'''
_CSS = ''' … '''
```

A ~100-line CSS block with `{` in every rule. As a plain module-level string it needs no escaping; inside an f-string every brace would need doubling. The parts list is then joined with `''.join(parts)` — so even the HTML body avoids f-strings. That is the correct call for template code and it is stated in the docstring.

### 2.2 Every interpolated value goes through `_esc`

```python
def _esc(value): return _html.escape(clean(value), quote=True)
```

`clean` collapses whitespace and clamps to 160 chars; `_html.escape(..., quote=True)` escapes `& < > " '`. Every user-controlled field — university name, course title, student name, teacher name — passes through it. There is no raw interpolation anywhere in either template. The `&mdash;` fallbacks are literal HTML entities appended *after* escaping, so they render rather than being escaped.

This is the one place in the study stack that gets output encoding right. Compare doc 36's finding #106 — `routes/library.library_import_pdf` interpolates `title` and `filename` into YAML frontmatter unsanitised. Same codebase, opposite discipline.

### 2.3 The dispatcher falls back rather than raising

```python
def build_cover_html(data, teacher_name='', design='classic'):
    if (design or '').strip().lower() == 'elegant':
        from engine.study.cover_html_elegant import build_elegant_html
        return build_elegant_html(data, teacher_name)
    return build_classic_html(data, teacher_name)
```

The lazy import is because `cover_html_elegant` imports `DEFAULT_MOTTO`, `_esc` and `clean` from `cover_html` — a top-level import would be circular. The comment says so.

An unknown `design` value falls back to classic. The docstring justifies it: *"a bad value in the store must not break rendering."* Correct — a cover page that 500s because someone typed `"Elegant "` with a trailing space would be worse than a cover page in the wrong style.

***

## 3. `cover_html_elegant.py` — measured, not eyeballed

The module docstring is the most valuable thing in this batch:

```
    motto centre      ~23mm        ASSIGNMENT banner   71.9 - 90.3mm
    diamond           ~29.6mm      topic label         ~101mm
    university l1     ~39mm        topic (2 lines)     ~110 - 119mm
    …
Every vertical position below was MEASURED off the reference PDF (rendered at
110dpi, band-detected on ink coverage), not eyeballed.
```

And then, at the individual rules, the measurement that produced the value:

```css
/* Measured: the reference's masthead diamond is 13px tall at 150dpi = 2.2mm
   bounding box, so a ~1.6mm side. Mine was 2.6mm and visibly chunkier. */
.mast-dia{width:1.6mm;height:1.6mm;…}

/* Font size is MEASURED, not chosen: the reference's university line has a
   3.6mm cap height, which for Cinzel (cap ratio ~0.72) is a 14.5pt em. At 21pt
   the masthead dominated the page and pushed everything below it down. */
.uni{…font-size:14.5pt;…}
```

That is a designer's notebook embedded in the stylesheet. It is the only place in this codebase where a magic number is fully explained — and it makes the numbers *checkable*, which is the whole point.

### 3.1 Two load-bearing comments

```css
/* overflow:hidden is LOAD-BEARING. The decorative arcs below are positioned to
   bleed past the page edge; without clipping, Chromium widens the document to
   fit them and then scales the entire cover down to fit A4 (measured: a 60mm
   overhang shrank every element to 78%). Do not remove. */
```

```
Two rules that must not be broken:
  1. NO linear-gradients. poppler (Evince/Okular) rasterises those as flat gray.
  2. The topic block is width-capped (max-width) so a long title wraps to two
     lines, which is what gives the middle of the page its intended proportion.
```

Both are platform findings — Chromium's shrink-to-fit and poppler's gradient handling — recorded at the line where removing the guard would break them. A future maintainer tidying the CSS will read these first.

### 3.2 The asymmetric course grid is also justified

```css
/* Asymmetric on purpose: the reference puts its divider at ~41% of the content
   width, not 50%, so the course title gets the room it needs to stay on one
   line. An even split wraps it to two and pushes the whole page down. */
.course{display:grid;grid-template-columns:0.66fr 0.6pt 1fr;…}
```

`0.66fr 0.6pt 1fr` looks like a typo until you read the comment. It is not.

***

## 4. `__init__.py` — `get_outline` is reachable but not re-exported

`store.get_outline` is defined and used by the skill handlers:

```
engine/skills/handlers/study.py:174:  outline = store.get_outline(doc_id)
engine/skills/handlers/study.py:478:  outline = store.get_outline(doc_id)
```

Both access it through the module (`from engine.study import store`), so the missing `__all__` entry is harmless. It is a consistency nit only — every other public function in `store.py` is re-exported. Finding #125, 🔵.

***

## 5. Findings

**No 🔴, no 🟠, no 🟡 of consequence.** This is the cleanest 562 lines in the repository.

### 🟡 Correctness (low severity)

**#124 — `chat.py:1755` sets the study ContextVar with no reset; `study_ai.py` resets at both sites.** The comment claims *"async generators run in their own context"* — they do not (PEP 568 was rejected). Safety comes from Starlette's per-request task, not the generator. No leak today; the documented mechanism is wrong, and the two disciplines disagree.

### 🔵 Cosmetic

| Where | What |
|:--|:--|
| `study/__init__.py` | `get_outline` defined and used, but not in the re-export list (**#125**) |
| `cover_html.py` | `today()` is the fallback for both `dates.given` and `dates.due` — a cover with no dates shows the same date twice |
| `cover_html_elegant.py` | `number != '01'` suppresses the banner number for 01 but the classic template always shows it — the two designs disagree on what "01" means |
| `cover_html_elegant.py` | `style="margin-top:4mm"` inline on the session row — the only inline style in either template |

***

## 6. What's right, and worth copying

1. **Measured values with the measurement in the comment.** `.mast-dia` and `.uni` are the two best comments in the codebase — they make a magic number verifiable instead of arbitrary.
2. **Load-bearing comments on CSS.** `overflow:hidden`, no `linear-gradient`, `max-width` on the topic — each names the renderer and the failure.
3. **Every interpolated value escaped, no exceptions.** `_esc` on all ten fields, in both templates, with HTML entities appended after escaping.
4. **Fallback, not raise, on an unknown template name.** A bad stored value degrades the design; it does not 500 the page.
5. **Lazy import with the circularity stated.** `build_cover_html` imports the elegant module inside the branch and says why.
6. **`reset_study_context` swallows the three context errors it can raise.** A token reset from the wrong context is not a crash.

***

## 7. What this changes upstream

- `engine/study/` is now **read in full**: `store.py` (619, doc 38), `pdf.py` (151, earlier), `context.py` + `cover_html.py` + `cover_html_elegant.py` + `__init__.py` (562, this doc).
- **#124 is the counterpart to `study_ai.py`'s discipline**, which doc 22 covered at route level without examining the ContextVar lifecycle.
- Python coverage: **~70,800 → ~71,400 / 137,031 = ~52%**. Application Python: **~58%**. Overall: **~38%**.

***

## Cross-references

- [[38-research-manager-study-store-memory-searcher]] — `store.py`, the data layer these templates render
- [[22-server-study-routes]] — `study_ai.py`, which sets and resets the context correctly at two of three sites
- [[36-library-stack]] — finding #106, the unsanitised frontmatter this file's `_esc` discipline is the opposite of
- [[17-server-chat-route]] — `chat.py`, the site that sets without resetting (#124)
