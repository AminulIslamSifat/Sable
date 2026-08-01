# Online Search (Two-Phase)

## Protocol

### Phase 1 — Search
```bash
python3 SKILL_DIR/scripts/web_search_batch.py --json --search-only "your query here"
```
Add `--max-results 20` to control result count (default 10).

### Phase 2 — Fetch
```bash
python3 SKILL_DIR/scripts/web_search_batch.py --json --fetch-urls url1 url2 url3
```
Add `--max-chars 20000` for larger page context (default 10000).

## Rules
- Always review Phase 1 results before fetching — never fetch blindly.
- SKILL_DIR resolves to the directory containing this instruction.md.
- Settings live in SKILL_DIR/settings.json.
