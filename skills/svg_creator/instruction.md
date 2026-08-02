# SVG Creator: Data Structure & Algorithm Visualizer

Use this skill to generate high-fidelity SVG diagrams for data structures (binary trees, linked lists, stacks, queues, heaps, graphs), algorithm state visualizations, circuit diagrams, and any custom technical illustration that Mermaid can't render well.

***

## Output Format

Use the `<create_svg>` tag with a `filename` attribute. The **body** of the tag is the raw SVG markup.

### Tag Attributes:
- `filename`: String (**required**). Descriptive name ending in `.svg` (e.g., `"avl_tree_insert.svg"`, `"wave_superposition.svg"`).

### The Engine Logic
The dispatcher (`_handle_create_svg`) will:
1. Extract the `filename` attribute.
2. Save the raw SVG content directly to `<ASSETS_DIR>/{filename}`.
3. Return a success/failure message.

The SVG content must be **complete, valid, self-contained XML** — no external dependencies, no linked stylesheets, no `<image>` hrefs.

***

## Usage Example

**Goal**: Visualize a binary search tree with nodes [42, 21, 63].

```xml
<create_svg filename="bst_example.svg">
<svg viewBox="0 0 300 220" width="300" height="220" xmlns="http://www.w3.org/2000/svg">
<defs>
  <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="#585b70"/>
  </marker>
</defs>
<style>
  .node       { fill: #181825; stroke: #cba6f7; stroke-width: 2; }
  .node-label { fill: #cdd6f4; font-family: 'JetBrains Mono', 'Fira Mono', monospace;
                font-size: 13px; text-anchor: middle; dominant-baseline: central; }
  .edge       { stroke: #585b70; stroke-width: 1.8; fill: none; }
  .bg         { fill: #1e1e2e; }
</style>
<!-- Background -->
<rect class="bg" width="300" height="220" rx="10"/>
<!-- Edges -->
<line x1="150" y1="60" x2="80" y2="140" class="edge"/>
<line x1="150" y1="60" x2="220" y2="140" class="edge"/>
<!-- Root: 42 -->
<circle cx="150" cy="60" r="24" class="node"/>
<text x="150" y="60" class="node-label">42</text>
<!-- Left: 21 -->
<circle cx="80" cy="140" r="24" class="node"/>
<text x="80" y="140" class="node-label">21</text>
<!-- Right: 63 -->
<circle cx="220" cy="140" r="24" class="node"/>
<text x="220" y="140" class="node-label">63</text>
</svg>
</create_svg>
```

***

## 🎨 Mandatory Visual Standards

Every SVG **must** meet this baseline. Flat, unpolished diagrams are a failure.

### Color Palette — Catppuccin Mocha (strict)
| Role | Value |
|---|---|
| Background (outer) | `#1e1e2e` |
| Background (canvas) | `#181825` |
| Node fill | `#181825` |
| Node stroke / primary accent | `#cba6f7` |
| Secondary accent | `#89b4fa` |
| Warning / highlight | `#fab387` |
| Success / active | `#a6e3a1` |
| Error / deletion | `#f38ba8` |
| Default text | `#cdd6f4` |
| Muted text / edges | `#585b70` |
| Subtle grid / dividers | `#313244` |

Use accent colors semantically — e.g., `#a6e3a1` for a newly inserted node, `#f38ba8` for a deleted node, `#fab387` for the currently active/visited node during traversal.

### Typography
Always declare this font stack for all text elements:
```
font-family: 'JetBrains Mono', 'Fira Mono', 'Cascadia Code', monospace
```
- Node labels: `font-size: 13px`
- Section headers / titles: `font-size: 15px`, `font-weight: bold`, color `#cdd6f4`
- Annotations / captions: `font-size: 11px`, color `#a6adc8`

### Background & Rounding
- Always add a `<rect class="bg" width="W" height="H" rx="12"/>` as the first element — gives the diagram a card-like appearance in Obsidian.
- Use `rx="10"` on node rectangles (for stack/queue boxes), `rx="50%"` is handled by `<circle>` naturally.

### Spacing & Layout Rhythm
- **Node radius (circles)**: `r="24"` standard, `r="20"` for dense trees.
- **Node box (rectangles)**: minimum `60×36px` per cell.
- **Vertical level spacing (trees)**: minimum `80px` between levels.
- **Horizontal sibling spacing**: minimum `60px` between node centers.
- **Padding**: always leave at least `30px` margin from SVG edges to any element — never clip nodes at borders.

***

## 📐 viewBox & Sizing Rules

Always use **both** `viewBox` and explicit `width`/`height`. This ensures correct scaling in Obsidian and browsers.

```xml
<svg viewBox="0 0 W H" width="W" height="H" xmlns="http://www.w3.org/2000/svg">
```

**Sizing guide by structure type:**

| Structure | Recommended Base Size |
|---|---|
| Small tree (≤7 nodes) | `500 × 320` |
| Medium tree (8–15 nodes) | `700 × 450` |
| Large tree / heap (16+ nodes) | `900 × 550` |
| Linked list (≤8 nodes) | `700 × 160` |
| Stack / Queue | `200 × (80 + 60×n)` |
| Graph (general) | `700 × 500` |
| Multi-step algorithm state | `800 × 500` |

**Rule**: Calculate required dimensions from the actual node count and layout — don't pick a size then squeeze nodes into it. Size to fit the content, then add padding.

***

## 🏹 Arrow & Edge Markers

For any **directed** structure (directed graphs, linked list pointers, tree child edges), always define an arrowhead marker in `<defs>` and reference it on edges:

```xml
<defs>
  <!-- Standard directional arrow -->
  <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="#585b70"/>
  </marker>
  <!-- Accent arrow (for highlighted paths) -->
  <marker id="arrow-accent" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="#cba6f7"/>
  </marker>
</defs>

<!-- Usage -->
<line x1="100" y1="60" x2="180" y2="60" stroke="#585b70" stroke-width="2"
      marker-end="url(#arrow)"/>
<!-- Highlighted path -->
<line x1="100" y1="60" x2="180" y2="60" stroke="#cba6f7" stroke-width="2.5"
      marker-end="url(#arrow-accent)"/>
```

For **undirected graphs**, plain `<line>` or `<path>` with no marker is correct — never add unnecessary arrows.

***

## 📊 Complexity Tiers

| Tier | Concepts | Approach |
|---|---|---|
| **Simple** | Single linked list, small BST (≤7 nodes), stack/queue | Flat layout, manually positioned, no animation |
| **Medium** | AVL/Red-Black trees, heaps, small graphs (≤10 nodes), adjacency lists | Calculated node positions from depth/index, semantic colors for node states |
| **Complex** | Large graphs (11+ nodes), algorithm step-by-step states, multi-structure comparisons | CSS animations for state transitions, multi-panel layout, step labels |

### Complex tier: CSS Animation for Algorithm States

When visualizing an algorithm in progress (e.g., BFS traversal, quicksort partition, Dijkstra's visited set), use CSS `@keyframes` to highlight state changes:

```xml
<style>
  @keyframes pulse {
    0%   { stroke: #cba6f7; stroke-width: 2; }
    50%  { stroke: #fab387; stroke-width: 4; }
    100% { stroke: #cba6f7; stroke-width: 2; }
  }
  .active { animation: pulse 1.2s ease-in-out infinite; }
  @keyframes fadeIn {
    from { opacity: 0; transform: scale(0.8); }
    to   { opacity: 1; transform: scale(1); }
  }
  .new-node { animation: fadeIn 0.5s ease-out forwards; }
</style>
```

Apply `.active` to the currently visited node and `.new-node` to freshly inserted elements. Keep animations subtle — they should guide attention, not distract.

***

## 📏 Layout Math Guide

Never hardcode node positions without calculating them. Use these formulas:

**Binary Tree node positions** (root at top-center):
```
nodeX(level, index) = canvasWidth/2 + (index - nodesInLevel/2 + 0.5) × horizontalSpacing
nodeY(level) = topPadding + level × verticalSpacing

horizontalSpacing = canvasWidth / (2^level + 1)  ← shrinks per level
verticalSpacing   = 80px  (minimum)
```

**Linked List** (horizontal, left to right):
```
nodeX(i) = leftPadding + i × (nodeWidth + arrowGap)
nodeY    = canvasHeight / 2  ← all on same horizontal line
arrowGap = 40px
```

**Circular Layout** (for general graphs):
```
nodeX(i) = centerX + radius × cos(2π × i / n)
nodeY(i) = centerY + radius × sin(2π × i / n)
radius = min(canvasWidth, canvasHeight) / 2 - padding
```

Always compute SVG canvas size **after** calculating all node positions — add `30px` padding on all sides.

***

## Embedding in Final Response (MANDATORY)

After the engine confirms the SVG was saved, you **MUST** embed the file in your response using an Obsidian wikilink so the user can see it inline:

```markdown
Here's the BST visualization:

![Name](relative/file/path.svg)
```

> The `![Name](relative/file/path.svg)` syntax renders the SVG inline in Obsidian. Never use raw HTML `<img>` tags — always use markdown embeds.

### If the engine returns `FAILED`:
1. **Parse error** (malformed XML) → validate tag nesting, check unclosed elements, fix and retry.
2. **File write error** (path/permission) → report the exact error to the user. Do not retry blindly.
3. **Empty output** → ensure the SVG tag is the direct body of `<create_svg>` with no wrapper.

***

## Critical Rules

1. **Wait for confirmation**: Do NOT embed the wikilink in the same message as the `<create_svg>` tag. Wait for the engine to return `Success`.
2. **Self-Contained SVG**: No external fonts, no linked images, no `xlink:href`. Everything inline. Fonts declared via `font-family` stack only.
3. **Always use viewBox + width/height**: Naked `width/height` without `viewBox` breaks scaling. Both are required on every `<svg>` root element.
4. **Dark Theme**: Catppuccin Mocha palette is mandatory. No white backgrounds, no default browser colors.
5. **Always define arrow markers in `<defs>`** for directed structures. Never draw arrowheads manually with triangles floating near line ends.
6. **Calculate positions mathematically**: Use the Layout Math Guide formulas. Hardcoded positions for large structures produce misaligned, unscalable diagrams.
7. **Semantic color usage**: Use accent colors to convey state — active, inserted, deleted, visited. A diagram where every node looks the same teaches nothing.
8. **Descriptive Filenames**: Name files by content and state (e.g., `avl_rotation_left.svg`, `bfs_step3_visited.svg`) — not generic names like `diagram_1.svg`.
9. **Always embed**: After success, include `![Name](relative/file/path.svg)` in your final answer so the user sees the visual immediately.