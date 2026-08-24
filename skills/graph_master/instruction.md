# Graph Master: High-Fidelity Technical Plotting

Use this skill to generate precise mathematical plots, physics visualizations, and data graphs using Matplotlib. Ideal for SHM, wave mechanics, calculus curves, probability distributions, and any data that needs a coordinate system.

***

## Output Method

Write a **self-contained Python script** and execute it via the `execute_command` tool. The script must:

1. Import matplotlib and numpy.
2. Apply the mandatory style spec (see below).
3. Plot all curves.
4. Save to `~/sable_output/assets/{filename}`.
5. Print a JSON status on completion.

After successful execution, embed the plot in your response:

```markdown
![Description](~/sable_output/assets/filename.png)
```

### Critical Rules for Output
1. Always use `execute_command` to run the script — never use `<plot_graph>` tags (deprecated).
2. Save output to `~/sable_output/assets/` with a descriptive `.png` filename.
3. The script must be fully self-contained — no external config files or imports from the Sable project.
4. Always print `{"status": "SUCCESS", "path": "..."}` on success or `{"status": "FAILED", "message": "..."}` on error.
5. After saving, always embed the result so the user sees it inline.

***

## Usage Example

**Goal**: Plot displacement and velocity curves for SHM.

Call `execute_command` with:

```python
import matplotlib.pyplot as plt
import numpy as np
import json, os

# === Style Spec (MANDATORY) ===
plt.style.use('dark_background')
fig, ax = plt.subplots(figsize=(12, 6), dpi=150)
fig.patch.set_facecolor('#1e1e2e')
ax.set_facecolor('#181825')
ax.grid(color='#313244', linestyle='--', linewidth=0.6, alpha=0.7)
ax.tick_params(colors='#a6adc8', labelsize=10)
for spine in ax.spines.values():
    spine.set_edgecolor('#313244')
CURVE_COLORS = ['#cba6f7', '#89b4fa', '#a6e3a1', '#fab387', '#f38ba8', '#94e2d5']

# === Plot Data ===
x = np.linspace(0, 10, 1000)
A, omega, phi = 1, 2, 0

y1 = A * np.sin(omega * x + phi)
y2 = A * omega * np.cos(omega * x + phi)

ax.plot(x, y1, label='x(t) — Displacement', color=CURVE_COLORS[0])
ax.plot(x, y2, label='v(t) — Velocity', color=CURVE_COLORS[1])

ax.set_title('SHM: Displacement vs Velocity', color='#cdd6f4', fontsize=14, fontweight='bold')
ax.set_xlabel('Time (s)', color='#cdd6f4')
ax.set_ylabel('Amplitude', color='#cdd6f4')
ax.legend(facecolor='#313244', edgecolor='#45475a', labelcolor='#cdd6f4', fontsize=9)

# === Save ===
out_dir = '~/sable_output/assets'
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, 'shm_x_v.png')
plt.tight_layout()
plt.savefig(out_path)
plt.close()
print(json.dumps({"status": "SUCCESS", "path": out_path}))
```

Then in your response:
```markdown
![SHM Plot](~/sable_output/assets/shm_x_v.png)
```

***

## 🎨 Mandatory Style Spec

Every plot **must** use this visual standard. Default Matplotlib grey is never acceptable.

```python
# Apply this EXACTLY at the start of every script
plt.style.use('dark_background')
fig, ax = plt.subplots(figsize=(12, 6), dpi=150)
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

# Curve color cycle (Catppuccin Mocha accents — apply in order)
CURVE_COLORS = ['#cba6f7', '#89b4fa', '#a6e3a1', '#fab387', '#f38ba8', '#94e2d5']

# Legend
ax.legend(facecolor='#313244', edgecolor='#45475a',
          labelcolor='#cdd6f4', fontsize=9)
```

Each curve gets the next color from `CURVE_COLORS` in order. For single-curve plots, always use `#cba6f7`.

***

## 📐 Range Selection Guide

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

## 📊 Complexity Tiers

Match implementation effort to the concept.

| Tier | Concepts | Layout |
|---|---|---|
| **Simple** | Single curve, basic trig, polynomials | Single axes, straightforward script |
| **Medium** | Multi-curve overlay, phase comparisons, superposition | Single axes, 2–4 curves, shared x-range |
| **Complex** | Dual-axis, subplots, annotated critical points | Use `twinx()`, `subplots()`, `annotate()` |

### Complex tier techniques:

- **Dual y-axes**: Use `ax2 = ax.twinx()` when two curves have fundamentally different y-units (e.g., displacement in meters and energy in joules). Apply the same style spec to `ax2`.
- **Subplots**: Use `fig, axes = plt.subplots(n, 1, ...)` for vertically stacked comparisons. Apply style to each subplot.
- **Annotations**: Use `ax.annotate(label, xy=(x, y), ...)` to mark critical points (maxima, zeros, inflection points).
- **Fill under curve**: Use `ax.fill_between(x, y, alpha=0.15, color=color)` for probability distributions or area-under-curve emphasis.
- **Log scales**: Use `ax.set_yscale('log')` or `ax.set_xscale('log')` for exponential/power-law data.

***

## Error Handling

If the script fails:
1. **Import error** → ensure matplotlib and numpy are available. Report to user.
2. **Eval/math error** → check equation syntax, domain issues (log(0), division by zero). Fix and retry.
3. **File write error** → check path exists, permissions. Report exact error.
4. **Style not applied** → verify the mandatory style block is present at the top of the script.

***

## Critical Rules

1. **Self-contained script**: No imports from Sable project, no external config files. Only `matplotlib`, `numpy`, `json`, `os`.
2. **Always apply style spec**: Copy the mandatory style block verbatim into every script. Never skip it.
3. **Use numpy for math**: Use `np.sin`, `np.cos`, `np.exp`, etc. — not Python's `math` module (doesn't work with arrays).
4. **1000 sample points**: Always use `np.linspace(start, end, 1000)` for smooth curves.
5. **Physical accuracy**: Label axes with correct units (e.g., `"Time (s)"`, `"Displacement (m)"`). Use physically correct equations.
6. **Smart range selection**: Use the Range Selection Guide. Never default lazily to `[0, 10]`.
7. **Descriptive filenames**: Name by content (e.g., `wave_superposition.png`, `gaussian_distribution.png`) — not generic names like `plot1.png`.
8. **Save to output directory**: Always use `~/sable_output/assets/` as the target directory.
9. **Embed after saving**: Always include a markdown image link in your response after successful execution.
10. **Print JSON status**: Script must print `{"status": "SUCCESS", "path": "..."}` or `{"status": "FAILED", "message": "..."}` as the last line.
