# Document Skills: Multi-Modal Office Engine
Comprehensive toolkit for creating, editing, and analyzing professional office documents
(DOCX, PDF, PPTX, XLSX). This is a router skill — its sole job is to identify the
document type, load the correct sub-skill manual, and hand off execution to it.
Do not attempt any document operation without first reading the relevant SKILL.md.

---

## Trigger Guard

| Condition | Action |
|---|---|
| User mentions a DOCX, PDF, PPTX, or XLSX file by name or extension | Fire this skill |
| User says "create", "edit", "fill", "merge", "analyze", "format" on any document | Fire this skill |
| Task involves redlining, form filling, slide creation, or financial modeling | Fire this skill |
| File type is ambiguous (e.g. "this document") | Ask for the file extension before routing |
| File type is not DOCX, PDF, PPTX, or XLSX | Report unsupported type. Do not attempt a workaround. |

---

## Sub-Skill Routing

Identify the document type from the file extension or the user's description, then read
the corresponding SKILL.md **before doing anything else** — including planning,
designing, or writing code.

| Type | Primary Use Cases | SKILL.md Path |
|---|---|---|
| **DOCX** | Legal/academic editing, redlining, tracked changes, document creation | `PROJECT_ROOT/skills/document_skills/docx/SKILL.md` |
| **PDF** | Form filling, merging, splitting, text/table extraction | `PROJECT_ROOT/skills/document_skills/pdf/SKILL.md` |
| **PPTX** | Presentation creation (template or scratch), slide reordering, design | `PROJECT_ROOT/skills/document_skills/pptx/SKILL.md` |
| **XLSX** | Financial modeling, data analysis, formula auditing, structured data | `PROJECT_ROOT/skills/document_skills/xlsx/SKILL.md` |

Reading the SKILL.md is not optional and is not skippable for "simple" tasks. Every
sub-skill encodes environment-specific constraints, available libraries, and workflows
that cannot be assumed from general knowledge.

---

## Global Protocol (All Document Types)

### Phase 1 — Discovery (always)

1. **Read the SKILL.md** for the identified document type. Do this before any other action.
2. **Analyze the source file** if editing an existing document:
   - DOCX → extract with Pandoc
   - PPTX → extract with MarkItDown
   - PDF → extract with PDFPlumber
   - XLSX → read with openpyxl or pandas per the sub-skill instructions
3. **State the plan** before writing any code or making any edit:
   - For PPTX/DOCX creation: declare color palette, font choices, and structural hierarchy.
   - For XLSX: declare which values are inputs and which are formula-derived.
   - For PDF: declare which fields are being filled and their source values.
   - For edits: declare what is changing and what must be preserved.

Wait for confirmation on the plan if it involves significant structural changes.
Proceed directly for straightforward edits.

### Phase 2 — Execution

Follow the sub-skill's prescribed workflow exactly. The global defaults are:

| Document type | Execution workflow |
|---|---|
| **DOCX / PPTX** | `unpack → script → repack`. Never edit OOXML directly as raw text. Preserve original styles unless replacement is explicitly requested. |
| **XLSX** | Formulas over hardcoded values, always. Never write a static number where a formula can derive it. |
| **PDF** | Verify fillable fields with `check_fillable_fields.py` before filling. Never assume field names — inspect first. |

### Phase 3 — Validation (always)

Run the appropriate validator before delivering output:

| Document type | Validator | What it checks |
|---|---|---|
| **XLSX** | `recalc.py` | Zero formula errors, no broken references |
| **PPTX** | `thumbnail.py` | Text cutoffs, overflow, layout integrity |
| **PDF** | `check_fillable_fields.py` | All target fields exist and are filled correctly |
| **DOCX** | Open in LibreOffice headless, check exit code | Structural validity, no corrupt XML |

Do not deliver output that has not passed its validator. If a validator is missing or
fails to run, report the issue to the user before delivering the file.

### Phase 4 — Output

- Save all outputs to `<OUTPUT_ROOT>/assets/` unless the user specifies a different path.
- Use meaningful filenames that reflect the document's content and version.
  Good: `q3_financial_model_v2.xlsx`. Bad: `output.xlsx`, `final_final.docx`.
- Report the output path and filename explicitly after saving.

---

## Dependency Reference

These binaries must be present for full functionality. If a command fails due to a
missing dependency, report the exact error and the install command — do not attempt
a workaround or substitute tool.

| Dependency | Used by | Install |
|---|---|---|
| Pandoc | DOCX extraction | `pacman -S pandoc` |
| LibreOffice | DOCX validation, format conversion | `pacman -S libreoffice-still` |
| Poppler | PDF utilities (`pdfinfo`, `pdftoppm`) | `pacman -S poppler` |
| PDFPlumber | PDF text/table extraction (Python) | `pip install pdfplumber` |
| MarkItDown | PPTX extraction | `pip install markitdown` |
| openpyxl | XLSX read/write (Python) | `pip install openpyxl` |

---

## Quality Rules (All Types)

1. **Surgical edits on existing files.** When modifying OOXML (DOCX/PPTX), only touch
   what needs to change. Preserve all original styles, fonts, and structure unless
   replacement is explicitly requested.
2. **No hardcoded values in XLSX.** If a number can be derived by formula, it must be.
   Hardcoding is only acceptable for raw input data.
3. **No generic templates for PPTX.** Presentations must be designed to "CEO Era"
   quality — bold palettes, intentional hierarchy, no stock layouts. Follow the palette
   and design guidance in `pptx/SKILL.md`.
4. **Fidelity on existing documents.** When the user hands over a document, the output
   must be structurally and visually faithful to the original except for the requested
   changes.
5. **Validators are not optional.** A document that hasn't been validated hasn't been
   finished.

---

## Failure Handling

| Failure type | Symptom | Action |
|---|---|---|
| **SKILL.md not found** | Path returns empty or error | Report to the user immediately. Do not proceed with assumed knowledge of the sub-skill. |
| **Missing dependency** | Command returns `not found` | Report the exact error and the install command from the dependency table. Do not substitute a different tool. |
| **Validator failure** | Validator returns errors or non-zero exit | Do not deliver the file. Report what the validator found and fix before re-running. |
| **Ambiguous file type** | No extension, multiple formats mentioned | Ask the user to clarify the target format before routing. |
| **Corrupt source file** | Extraction tool fails on the input file | Report the extraction error verbatim. Do not attempt to reconstruct the file from partial output. |

---

## Global Rules

1. **Read the SKILL.md first.** Always. No exceptions for "simple" tasks.
2. **Plan before executing.** State the approach and what will be preserved vs. changed.
3. **Validate before delivering.** Every output passes its validator or the issue is
   reported before delivery.
4. **Report dependency failures immediately.** Do not attempt workarounds. The correct
   tool is the correct tool.
5. **One document type per turn.** If the user asks for DOCX and PDF output simultaneously,
   handle one at a time and confirm each before proceeding to the next.