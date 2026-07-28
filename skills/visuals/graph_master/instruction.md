# Graph Master: High-Fidelity Technical Plotting

Use this skill to generate precise mathematical plots, physics visualizations, and data graphs using Matplotlib via the `plotter.py` script. Ideal for SHM, wave mechanics, calculus curves, probability distributions, and any data that needs a coordinate system.

***

## Output Format

Use the `<plot_graph>` tag with a `filename` attribute. Inside the tag, provide a JSON configuration object.

### Tag Attributes:
- `filename`: String (**required**). Output filename ending in `.png` or `.jpg` (e.g., `"shm_displacement.png"`, `"damped_wave.png"`).

### JSON Parameters:
- `title`: String. Plot title displayed at the top.
- `xlabel`: String. X-axis label.
- `ylabel`: String. Y-axis label.
- `plots`: Array of plot objects. Each object defines one curve:
  - `equation`: String (**required**). A Python-evaluable expression using `x` as the variable. Supports: `sin`, `cos`, `tan`, `exp`, `log`, `sqrt`, `pi`, `e`, and any custom params. Do **not** use `np.` prefix — the namespace already has NumPy ufuncs injected as bare names.
  - `label`: String. Legend label for this curve.
  - `range`: Array `[min, max]`. X-axis range. See **Range Selection Guide** below for defaults.
  - `params`: Object. Key-value pairs for custom symbols in the equation (e.g., `{"A": 1, "omega": 2}`).
- `options`: Object *(optional)*. Advanced layout flags — see **Complexity Tiers** below.

### The Engine Logic
The `plotter.py` script will:
1. Parse the JSON configuration.
2. For each plot, evaluate the equation over its range using **1000 evenly-spaced points** via `numpy.linspace` (ensures smooth curves — never jagged).
3. The `eval` namespace contains **exactly**: `sin, cos, tan, arcsin, arccos, arctan, exp, log, log10, sqrt, pi, e, abs` — all as NumPy ufuncs. Plus any keys from `params`. Nothing else. Write equations accordingly.
4. Plot all curves on a single Matplotlib figure with grid, legend, and labels.
5. Apply the **mandatory style spec** (see below).
6. Save the image to `<ASSETS_DIR>/{filename}`.

***

## Usage Example

**Goal**: Plot displacement and velocity curves for SHM: $x = A\sin(\omega t + \phi)$.

```xml
<plot_graph filename="shm_x_v.png">
{
  "title": "SHM: Displacement vs Velocity",
  "xlabel": "Time (s)",
  "ylabel": "Amplitude",
  "plots": [
    {
      "equation": "A * sin(omega * x + phi)",
      "label": "x(t) — Displacement",
      "range": [0, 10],
      "params": {"A": 1, "omega": 2, "phi": 0}
    },
    {
      "equation": "A * omega * cos(omega * x + phi)",
      "label": "v(t) — Velocity",
      "range": [0, 10],
      "params": {"A": 1, "omega": 2, "phi": 0}
    }
  ]
}
</plot_graph>
```

***

##  Mandatory Style Spec

Every plot **must** use this visual standard. Default Matplotlib grey is never acceptable.

```python
# Applied automatically by plotter.py — reference this when debugging or extending
plt.style.use('dark_background')
fig.set_size_inches(12, 6)
fig.set_dpi(150)
fig.patch.set_facecolor('#1e1e2e')        # Catppuccin Mocha base
ax.set_facecolor('#181825')               # Slightly darker plot area

# Grid
ax.grid(color='#313244', linestyle='--', linewidth=0.6, alpha=0.7)
ax.tick_params(colors='#a6adc8', labelsize=10)
ax.xaxis.label.set_color('#cdd6f4')
ax.yaxis.label.set_color('#cdd6f4')
ax.title.set_color('#cdd6f4')
ax.title.set_fontsize(14)
ax.title.set_fontweight('bold')

# Spine color
for spine in ax.spines.values():
    spine.set_edgecolor('#313244')

# Curve color cycle (Catppuccin Mocha accents — applied in order)
CURVE_COLORS = ['#cba6f7', '#89b4fa', '#a6e3a1', '#fab387', '#f38ba8', '#94e2d5']

# Legend
ax.legend(facecolor='#313244', edgecolor='#45475a',
          labelcolor='#cdd6f4', fontsize=9)
```

Each curve gets the next color from `CURVE_COLORS` in order. For single-curve plots, always use `#cba6f7`.

***

##  Range Selection Guide

Never blindly default to `[0, 10]`. Choose the range that makes the physics or math **meaningful**.

| Concept | Recommended Range |
|---|---|
| Trig / oscillation (period visible) | `[0, 4π]` ≈ `[0, 12.57]` |
| Probability distributions (Gaussian) | `[-4σ, 4σ]` e.g. `[-4, 4]` |
| Exponential growth/decay | `[0, 5]` (captures the inflection) |
| Orbital / long-period motion | `[0, 2π × T]` |
| Calculus curves (general) | Center around the interesting region |
| Log functions | `[0.01, 10]` (avoid x=0) |

**Rule**: Ask — "What x-range makes this curve tell its full story?" Use that. If the range is physics-derived (e.g., one full period), calculate it from params and set it explicitly.

***

##  Complexity Tiers

Match implementation effort to the concept. Use the `options` field to unlock advanced layouts.

| Tier | Concepts | Layout |
|---|---|---|
| **Simple** | Single curve, basic trig, polynomials | Single axes, no options needed |
| **Medium** | Multi-curve overlay, phase comparisons, superposition | Single axes, 2–4 curves, shared x-range |
| **Complex** | Dual-axis (e.g., displacement + energy), subplots, annotated critical points | Use `options` flags below |

### `options` flags (Complex tier):

```json
"options": {
  "twin_axis": true,          // Dual y-axes for curves with different units
  "subplots": 2,              // Split into N vertically stacked subplots
  "annotations": [            // Mark critical points with labels
    {"x": 3.14, "label": "π"},
    {"x": 0, "label": "Origin"}
  ],
  "log_scale_y": false,       // Logarithmic y-axis
  "log_scale_x": false,       // Logarithmic x-axis
  "fill_under": true          // Shade area under curve (useful for distributions)
}
```

Use `twin_axis` when two curves have fundamentally different y-units (e.g., displacement in meters and energy in joules). Never force them onto the same axis — it destroys readability.

***

## Embedding in Final Response (MANDATORY)

After the engine confirms `SUCCESS`, you **MUST** embed the plot in your response using an Obsidian wikilink:

```markdown
Here's the SHM displacement vs velocity plot:

![Name](relative/file/path.png)
```

> The `![Name](relative/file/path.png)` syntax renders the image inline in Obsidian. Never use raw HTML `<img>` tags — always use markdown embeds.

### If the engine returns `FAILED`:
1. Read the error details returned by the engine.
2. If it's a JSON syntax error → fix and retry immediately.
3. If it's an equation eval error → check for unsupported symbols (remember: no `np.` prefix, no SymPy notation) and fix.
4. If the error is engine-internal (file system, path) → report to Sifat with the exact error message. Do not retry blindly.

***

## Critical Rules

1. **Wait for confirmation**: Do NOT embed the wikilink in the same message as the `<plot_graph>` tag. Wait for the engine to confirm the file was saved.
2. **Valid JSON**: The body must be valid JSON. No trailing commas, no comments.
3. **Use `x` as variable**: The plotter uses `x` as the independent variable in all equations. Map time `t` → `x`, position `r` → `x`, etc.
4. **No `np.` prefix**: The eval namespace has NumPy ufuncs injected as bare names. Write `sin(x)`, not `np.sin(x)`. Writing `np.anything` will throw a NameError.
5. **Physical Accuracy**: Label axes with correct units (e.g., `"Time (s)"`, `"Displacement (m)"`). Use physically correct equations — never approximate silently.
6. **Smart range selection**: Use the Range Selection Guide. Never default lazily to `[0, 10]` for all concepts.
7. **Resolution is fixed at 1000 points**: The engine always samples 1000 points. You do not need to specify this. Do not attempt to override it.
8. **Multi-curve**: Overlay multiple plots by adding entries to the `plots` array. For curves with different units, use `"twin_axis": true` in `options`.
9. **Descriptive Filenames**: Name by content (e.g., `wave_superposition.png`, `gaussian_distribution.png`) — not generic names like `plot1.png`.
10. **Always embed**: After success, include `![Name](relative/file/path.png)` in your final answer so Sifat sees the graph immediately.