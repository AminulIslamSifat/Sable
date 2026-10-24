# 53 — Document Skills: DOCX Editing & PPTX Inventory

`skills/document_skills/` is 8,926 lines. Three files carry the substance — `docx/scripts/document.py` (1,276), `pptx/scripts/inventory.py` (1,020), `docx/scripts/utilities.py` (374) — and the two `ooxml/` trees are a byte-identical copy of each other.

| Area | Lines | What |
|:--|--:|:--|
| `docx/scripts/document.py` | 1,276 | `Document` + `DocxXMLEditor`: comments, tracked changes, validation |
| `pptx/scripts/inventory.py` | 1,020 | Text extraction, absolute group positions, overflow/overlap detection |
| `docx/scripts/utilities.py` | 374 | `XMLEditor`: line-tracking parser, filterable node lookup |
| `ooxml/` × 2 | ~1,979 each | Schema/redlining validators, pack/unpack — **identical in both trees** |

***

## 1. `utilities.XMLEditor` — the foundation

### 1.1 Line numbers by monkey-patched SAX

OOXML is minified enough that line numbers in the raw file are the only practical anchor for a model reading the XML. `_create_line_tracking_parser` wraps the SAX `startElementNS` handler and stamps each DOM element with `parse_position = (line, column)` from expat.

The patch is surgical and the comment explains why: minidom's own parser does not expose positions, so the SAX handler is intercepted, the original callback preserved, and the position attached to `dom_handler.elementStack[-1]` — the element the DOM builder just created.

### 1.2 `get_node` demands exactly one match

Four filters — `tag`, `attrs`, `line_number` (int or `range`), `contains` — all ANDed. Zero matches raises with a filter-specific hint (`"Text may be split across elements"` vs `"Line numbers may have changed"`). More than one match raises and asks for more filters.

`contains` normalises through `html.unescape`, so `&#8220;` in the source matches a caller passing either the entity or the literal `\u201c`. `_get_element_text` skips whitespace-only text nodes, which is what makes a match possible in pretty-printed XML where every element is separated by newlines and indentation.

### 1.3 `_parse_fragment` namespaces the wrapper from the root

```python
ns_decl = " ".join(namespaces)          # every xmlns* attr on documentElement
wrapper = f"<root {ns_decl}>{xml_content}</root>"
```

Without this a fragment like `<w:r><w:t>x</w:t></w:r>` fails to parse — the prefixes are undeclared in the wrapper document. Copying the root's namespace attributes is the correct and non-obvious fix.

> [!WARNING] The fragment guard is an `assert`
> ```python
> assert elements, "Fragment must contain at least one element"
> ```
> Under `python -O` this vanishes. `replace_node` would then remove the target element and insert nothing — silent content loss with no error. Same class as #167's dead loop: correct in the default runtime, absent in the other.

***

## 2. `document.py` — comments and tracked changes

### 2.1 Three-phase construction

`Document.__init__` is deliberately ordered:

1. Copy the unpacked dir to `tempfile.mkdtemp(prefix="docx_")`.
2. Pack the **original** into `original.docx` — the validation baseline.
3. Load existing comments and compute `next_comment_id` **before** setup modifies any file.
4. `_setup_tracking` → people.xml, content-types, rels, settings.xml.
5. `_add_author_to_people`.

Step 3's ordering is load-bearing: `_setup_tracking` may create `people.xml` and mutate `[Content_Types].xml`, and `_load_existing_comments` returns `{}` early when `comments.xml` is absent — but it has to run before anything writes a template into that path, or every fresh document would report comment id 0 as taken.

### 2.2 `DocxXMLEditor` injects OOXML bookkeeping automatically

Every insert/replace/append routes through `_inject_attributes_to_nodes`, which walks the new subtree and fills in:

| Element | Attributes |
|:--|:--|
| `w:p` | `w:rsidR`, `w:rsidRDefault`, `w:rsidP`, `w14:paraId`, `w14:textId` |
| `w:r` | `w:rsidR`, or `w:rsidDel` when inside a `w:del` |
| `w:t` | `xml:space="preserve"` when text has edge whitespace |
| `w:ins` / `w:del` | `w:id`, `w:author`, `w:date`, `w16du:dateUtc` |
| `w:comment` | `w:author`, `w:date`, `w:initials` |
| `w16cex:commentExtensible` | `w16cex:dateUtc` |

Every setter is guarded with `if not elem.hasAttribute(...)`, so the pass is idempotent — which is why `node.getElementsByTagName(tag)` returning descendants after the node itself was already processed causes no double-write.

The namespaces (`w14`, `w16du`, `w16cex`) are declared lazily on the root only when an element that needs them is created. Three near-identical `_ensure_*_namespace` methods; a single parametrised helper would do, but each is four lines and self-documenting.

### 2.3 `revert_insertion` / `revert_deletion` — a two-step rejection

`revert_insertion` does **not** delete the insertion. It wraps every run inside `w:ins` in a new `w:del`, converts `w:t` → `w:delText`, and rewrites `w:rsidR` → `w:rsidDel`. The result is `<w:ins><w:del>…</w:del></w:ins>`.

`revert_deletion` clones the deleted runs, converts `w:delText` → `w:t`, and inserts a new `w:ins` after the `w:del`. The deletion is left in place.

Both are semantically sound — accepting all changes afterwards produces the intended result — but neither matches what Word does, which is to remove the wrapper outright. A document round-tripped through this tool and then opened in Word will show both the original deletion *and* a compensating insertion where a human would see one operation.

> [!NOTE] `revert_insertion` raises on an empty match
> Both methods `raise ValueError` when the element contains no `w:ins`/`w:del`, with a message naming the element type. That is better than silently returning, and it is the only validation either method performs.

### 2.4 Comment id and change id are separate counters

`Document.next_comment_id` increments per comment. `DocxXMLEditor._get_next_change_id` rescans `w:ins`/`w:del` for max `w:id` + 1. Both start at 0. In OOXML these are indeed independent ID spaces, so a `w:ins w:id="0"` alongside a `w:comment w:id="0"` is valid. Nothing in the code says so — a reader has to know the spec to trust it.

### 2.5 `_generate_hex_id` has a birthday problem

```python
def _generate_hex_id() -> str:
    return f"{random.randint(1, 0x7FFFFFFE):08X}"
```

31 bits of entropy, no collision check. `w14:paraId` must be unique per paragraph within a document. For a document with 10,000 paragraphs the collision probability is ≈ 2.3% (birthday bound, $n^2/2N$). A collision makes a comment attach to the wrong paragraph — silently, with no validation failure, because `docx.py`'s schema validator checks structure, not `paraId` uniqueness.

For a 200-paragraph document it is 0.001% and irrelevant. For a generated report with thousands of paragraphs it is a live risk.

### 2.6 `save()` merges, it does not replace

```python
shutil.copytree(self.unpacked_path, target_path, dirs_exist_ok=True)
```

`unpacked_path` begins as a full copy of the original, so saving back to the original directory is a no-op merge that cannot lose files. Saving to a *third* directory that already holds a different unpacked docx would mix the two trees. Nothing deletes files, so the practical risk is low — but `dirs_exist_ok=True` is the permissive option, chosen without a comment explaining why a merge is safe.

### 2.7 `save()` validates by default

`DOCXSchemaValidator` and `RedliningValidator` both run against the packed original. A manual (non-tracked) edit fails redlining validation and `save()` raises. That is the intent — the tool exists to produce tracked changes — but it means any bug in the editor that produces malformed XML surfaces as a validation error at save time rather than at insert time.

***

## 3. `inventory.py` — PPTX text extraction

### 3.1 Group shapes need accumulated absolute positions

`collect_shapes_with_absolute_positions` recurses through `GroupShape.shapes`, adding each group's `left`/`top` to the running offset. A shape nested two groups deep reports its true slide position. This is the whole reason the function exists, and the docstring says so.

### 3.2 Overflow is estimated with PIL, not measured

`_estimate_frame_overflow` builds a 1×1 image, loads the paragraph's font, wraps each line with `draw.textlength`, and accumulates line heights. It is an estimate by construction:

- Font size falls back to `_get_default_font_size()`, which reads theme `bodyStyle`/`titleStyle` and **defaults to 14 pt** — "conservative" per the comment. PowerPoint's usual body default is 18 pt, so heights are systematically underestimated and real overflow is missed.
- Only `paragraph.runs[0]` is read for font name/size/bold/italic. A paragraph with mixed formatting is measured entirely with its first run's font.
- Line spacing defaults to `font_size × 96/72` when unset, which is PowerPoint's single-spacing approximation.

The 0.05" threshold (`if overflow_inches > 0.05`) discards near-misses, which compensates in the other direction.

### 3.3 `get_font_path` has no Windows branch

```python
if system == "Darwin":
    font_dirs = ["/System/Library/Fonts/", ...]
else:  # Linux
    font_dirs = ["/usr/share/fonts/truetype/", ...]
```

On Windows, `platform.system()` returns `"Windows"`, which falls into the `else` and searches Linux font paths. Every lookup returns `None`, every paragraph measures with `ImageFont.load_default()`, and overflow estimation becomes decorative. This is the *"correct on the machine it was written on"* pattern for the fourth time in this pass (#143, #155, #157, #181).

### 3.4 Overlap detection is pairwise with a tolerance

`calculate_overlap` returns true only when **both** dimensions exceed `tolerance` (default 0.05"), and uses strict `>` — a 0.05" overlap is not reported. `detect_overlaps` is O(n²) per slide and mutates both shapes' `overlapping_shapes` dicts with the area in square inches, keyed by the stable `shape-N` id assigned after sorting.

### 3.5 The second `assert`

```python
assert shape1.shape_id, f"Shape at index {i} has no shape_id"
```

`extract_text_inventory` always assigns ids before calling `detect_overlaps`, so this never fires today. Under `-O` it disappears — which is fine here, since the assertion guards an internal ordering contract rather than user input.

***

## 4. The duplicated `ooxml/` tree

`docx/ooxml/` and `pptx/ooxml/` are **byte-identical**:

```
6143bb27c1039ff3d7486d1e526641dd  docx/ooxml/scripts/validation/base.py
6143bb27c1039ff3d7486d1e526641dd  pptx/ooxml/scripts/validation/base.py
… all five files match
```

Each tree contains **both** validators — `docx.py` (DOCX schema) and `pptx.py` (PPTX schema) — plus `redlining.py`, `pack.py`, `validate.py`. So the PPTX skill ships a DOCX validator and vice versa.

That is ~1,979 lines duplicated, ~3,958 total where half is redundant. Any fix to `base.py`, `pack.py` or `redlining.py` must be applied in two places with nothing checking they stay in sync. This is the largest duplicated block in the tree.

> [!IMPORTANT] It is byte-identical *today*
> The `.editor_tools_backups/` dirs differ (`docx.py.20260825-175143.bak` vs `-175148.bak`) — a backup of one was taken 5 seconds later, so an edit was made in both trees. They have not drifted yet. Nothing prevents it.

***

## 5. Findings

| # | Where | What |
|--:|:--|:--|
| 186 | `docx/ooxml/` vs `pptx/ooxml/` | The whole package is byte-identical in both trees, each containing both validators. ~1,979 lines duplicated; no sync check; every fix must be applied twice. |
| 187 | `inventory.get_font_path` | No `Windows` branch — `platform.system()` falls into the Linux `else`, so every font lookup returns `None` and overflow estimation degrades to `load_default()`. |
| 188 | `utilities._parse_fragment` | `assert elements, …` is the only guard against an element-less fragment. Under `python -O` it vanishes and `replace_node` deletes the target while inserting nothing. |
| 189 | `document._generate_hex_id` | 31 bits, no collision check, used for `w14:paraId` which must be unique per paragraph. ≈2.3% collision probability at 10,000 paragraphs; schema validation does not catch it. |
| 190 | `inventory._get_default_font_size` | Defaults to 14 pt where PowerPoint body default is typically 18 pt, systematically underestimating text height and therefore missing overflow. |
| 191 | `inventory.ParagraphData` | Reads font properties from `paragraph.runs[0]` only; mixed-format paragraphs are measured entirely with the first run's font. Undocumented. |
| 192 | `document.revert_insertion` / `revert_deletion` | Leave the original `w:ins`/`w:del` wrapper in place alongside the compensating element, unlike Word, which removes the wrapper. A round-tripped document shows two operations where a human made one. |
| 193 | `document.save` | `shutil.copytree(..., dirs_exist_ok=True)` merges into the destination. Safe when the destination is the original (which it starts as a copy of); mixes trees when pointed anywhere else. |

***

## 6. What this block shows

- **The OOXML bookkeeping is the hard part, and it is done well.** RSID injection, namespace lazy-declaration, `xml:space="preserve"`, entity-preserving `w:t`↔`w:delText` conversion — every one of those is a detail that breaks Word if missed, and all are handled.
- **Two `assert`s in a codebase that ships under an unknown interpreter flag.** Neither is a live bug today; both are guards that stop guarding when the flag changes.
- **The duplication convention reaches its largest instance.** `#152/#57`, `#156`, `#172` were two implementations with one flaw. This is two identical trees — a whole package, copy-pasted, with no mechanism keeping it honest.
- **Provenance comments are absent here.** `document.py` and `inventory.py` describe *what* the code does in careful docstrings, but — unlike `connectors/` — almost never record *which bug* a given line prevents. The one place a reproduced failure is named (`_get_default_font_size`'s "conservative default") is a choice, not a fix.

Python is now **~92,400 / 137,031 ≈ 67%**; overall **~48%**. `skills/document_skills/` leaves ~6,300 lines unread (thumbnails, replace, rearrange, PDF form scripts, xlsx recalc).
