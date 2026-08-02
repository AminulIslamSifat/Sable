# Simulacra Engine: Universal Dynamic Visualization

Use this skill to generate dynamic, interactive simulations, models, and high-fidelity interactive visualizations — covering physics (SHM, fields), biology (population models), mathematics (fractals, chaos), or any concept that benefits from visual motion and real-time interaction. Outputs self-contained HTML/JS files that the user can open directly in a browser.

**When NOT to use this skill:**
- Output is a static diagram, graph, or chart with no animation → use **Graph Master** or **SVG Creator**
- Output is a mathematical derivation or equation solving → use **Math Solver**
- the user just wants a code snippet to run himself, not a browser-openable file
- The concept is fully explainable with a still image — motion adds nothing

---

## Output Format

Use the `<run_simulacra>` tag with a `filename` attribute. The **body** of the tag is raw Python code that generates the simulation file.

### Tag Attributes
- `filename`: String (**required**). Output filename, always `.html` (e.g., `"shm_pendulum.html"`, `"logistic_map_chaos.html"`). Name by concept — never generic names like `"sim.html"`.

### Engine Logic
The dispatcher passes the tag to `sim_engine.py`, which:
1. Extracts the `filename` attribute and resolves it to `<ASSETS_DIR>/{filename}`.
2. Executes the Python code body with `OUT_PATH` pre-injected into the namespace.
3. Verifies the file was created at `OUT_PATH`.
4. Returns `SUCCESS` or `FAILED` with details.

### Python Code Requirements
- **Always write to `OUT_PATH`** — it is pre-injected. Use `with open(OUT_PATH, "w") as f:`. Never hardcode paths.
- Generated HTML must be **fully self-contained** — inline all CSS and JS.
- **No external JS/CSS libraries via CDN.** The simulation must work fully offline.
- **One permitted external dependency:** Google Fonts `<link>` import inside `<head>` only. No other external resources.
- Use raw `<canvas>`, `requestAnimationFrame`, or inline SVG for animations.

---

## Workflow Protocol

1. Assess the concept → assign a complexity tier (see below).
2. Determine slider count using the slider judgment rules (see below).
3. Write and emit the `<run_simulacra>` tag.
4. **Wait for engine confirmation** (`SUCCESS` or `FAILED`) before responding further.
5. On `SUCCESS` → respond with a brief description of what was built and always embed `![Name](relative/file/path.html)` so GhostChat opens it in the browser.
6. On `FAILED` → read the error details carefully before retrying:
   - `SyntaxError` / `IndentationError` → fix Python string escaping, usually a quote conflict inside the HTML triple-string
   - `FileNotFoundError` → `OUT_PATH` was not written to; check the `with open(OUT_PATH)` block is actually reached
   - `PermissionError` → assets directory issue; flag to the user, do not retry blindly
   - Any other error → attempt one fix and retry. If it fails again, report the raw error to the user rather than looping.

---

## Complexity Tier Assessment

**Assess tier before writing any code.** Mismatching tier is a failure — over-engineering wastes tokens, under-engineering produces slop.

| Tier | Concepts | Implementation Standard |
|------|----------|------------------------|
| **Simple** | SHM, basic population models, Ohm's law, RC circuits | Single canvas loop, 1–3 sliders, basic HUD |
| **Medium** | Wave interference, Lissajous, Lotka-Volterra, LC circuits, orbital mechanics | Pixel rendering or multi-body math, 2–4 sliders, animated data panels |
| **Complex** | Double pendulum, fluid sim, N-body gravity, chaos attractors, neural firing | Full state machine, trail rendering, 3–6 sliders, real-time energy/state graphs |

**Complex tier approximation rule:** If a concept cannot be accurately represented in vanilla Canvas without sacrificing mathematical correctness, implement a **faithful approximation** and label it clearly in the HUD (e.g., `"Simplified Model"`). Never fake behavior silently.

---

## Slider Judgment Rules

Do **not** apply a fixed slider count. Reason from the concept every time.

**The core test:** "If I change this parameter, does the simulation look or behave noticeably different?" If yes → slider. If no → hardcode a sensible default.

**Hard limits:**
- **Minimum:** Always at least **1** interactive slider. A fully static output defeats the purpose of this skill.
- **Maximum:** Never exceed **6** sliders. Beyond that, the UI becomes cognitively overwhelming.

**Reference examples** (reason from these, don't copy blindly):
- Simple pendulum → Length, Gravity, Damping (3 sliders)
- Predator-Prey → Birth Rate, Death Rate, Carrying Capacity (3 sliders)
- Logistic Map → Growth Rate `r` (1 high-precision slider)
- Mandelbrot → Iteration Depth, Color Shift (2 sliders)

---

## Mandatory Aesthetic Standards

Every simulation must meet this baseline. These are not optional.

### Color Palette — Catppuccin Mocha (strict)
```
Base:       #1e1e2e
Mantle:     #181825
Crust:      #11111b
Surface 0:  #313244
Surface 1:  #45475a
Text:       #cdd6f4
Subtext:    #a6adc8
Lavender:   #b4befe
Purple:     #cba6f7
Blue:       #89b4fa
Teal:       #94e2d5
Green:      #a6e3a1
Yellow:     #f9e2af
Peach:      #fab387
Red:        #f38ba8
```

### Typography
- Always import via `<link>` in `<head>`:
  ```html
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  ```
- Display/labels → `'Outfit', 'Segoe UI', sans-serif`
- Data/metrics/HUD values → `'JetBrains Mono', 'Courier New', monospace`

### Background
Never use a flat solid fill for the page background. Always create depth using one of:
- Radial gradient mesh (e.g., `radial-gradient(ellipse at 20% 50%, #313244 0%, #1e1e2e 60%)`)
- Animated grain overlay via a secondary canvas layer
- Subtle geometric pattern (CSS or canvas-drawn)

### UI Panels
- All parameter controls must be in **floating panels**, not inline HTML elements at the bottom of the page.
- Panels use glassmorphism: `background: rgba(49, 50, 68, 0.6); backdrop-filter: blur(12px); border: 1px solid rgba(180, 190, 254, 0.15); border-radius: 12px;`
- Sliders styled to match Catppuccin — override default browser appearance.
- Panel position: top-right or bottom-left corner, with `position: absolute` and appropriate padding from edges.

### Active Elements
- Glows on primary actors: `box-shadow` or canvas `shadowBlur` + `shadowColor` using accent colors.
- Motion trails or ghosting for anything that moves through space.
- Pulsing/animated indicators for live state (e.g., energy level, population count).

### Data HUD
- Always show at least 2 real-time derived metrics (e.g., Kinetic Energy, Period, Growth Rate, Phase).
- HUD sits in a separate floating panel from controls.
- Values rendered in `JetBrains Mono`, animated with smooth counter transitions where possible.

---

## Responsive Canvas (Mandatory)

Never hardcode canvas dimensions. Always scale to viewport:

```javascript
const canvas = document.getElementById('c');
let W, H;

function resize() {
  // Preserve simulation state before resize
  const savedState = captureState(); // implement per-sim

  W = canvas.width = Math.min(window.innerWidth, 900);
  H = canvas.height = Math.min(window.innerHeight, 600);

  // Recompute layout constants from W and H
  anchorX = W / 2;
  anchorY = H * 0.15;
  // etc.

  restoreState(savedState); // reapply without resetting physics
}

window.addEventListener('resize', resize);
resize();
```

**Rules:**
- All layout constants (anchor points, panel offsets, rest lengths) derived from `W` and `H` — never hardcoded.
- State (positions, velocities, population counts, phase) must survive a resize — do not reset the simulation.
- Implement `captureState()` / `restoreState()` as lightweight plain-object snapshots per simulation.

---

## Reference Implementation

This example meets all standards above. Use it as the quality baseline.

```xml
<run_simulacra filename="shm_spring.html">
html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SHM Spring Simulation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: radial-gradient(ellipse at 30% 40%, #313244 0%, #1e1e2e 65%);
  min-height: 100vh;
  display: flex;
  justify-content: center;
  align-items: center;
  font-family: 'Outfit', 'Segoe UI', sans-serif;
  overflow: hidden;
}
canvas { display: block; border-radius: 16px; }

.panel {
  position: absolute;
  background: rgba(49, 50, 68, 0.6);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(180, 190, 254, 0.15);
  border-radius: 12px;
  padding: 16px 20px;
  color: #cdd6f4;
  min-width: 200px;
}
.panel-title {
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #b4befe;
  margin-bottom: 14px;
}
.slider-row { margin-bottom: 12px; }
.slider-label {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: #a6adc8;
  margin-bottom: 4px;
  font-family: 'JetBrains Mono', monospace;
}
input[type=range] {
  width: 100%;
  -webkit-appearance: none;
  height: 4px;
  border-radius: 2px;
  background: #45475a;
  outline: none;
}
input[type=range]::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #cba6f7;
  cursor: pointer;
  box-shadow: 0 0 6px rgba(203, 166, 247, 0.6);
}

#controls { top: 20px; right: 20px; }
#hud { bottom: 20px; right: 20px; }

.metric { margin-bottom: 8px; }
.metric-label { font-size: 10px; color: #a6adc8; font-family: 'JetBrains Mono', monospace; }
.metric-value { font-size: 16px; font-weight: 500; color: #cba6f7; font-family: 'JetBrains Mono', monospace; }
</style>
</head>
<body>
<canvas id="c"></canvas>

<div class="panel" id="controls">
  <div class="panel-title">Parameters</div>
  <div class="slider-row">
    <div class="slider-label"><span>Amplitude</span><span id="ampVal">120</span></div>
    <input type="range" id="amp" min="20" max="180" value="120">
  </div>
  <div class="slider-row">
    <div class="slider-label"><span>Frequency</span><span id="freqVal">1.0</span></div>
    <input type="range" id="freq" min="1" max="30" value="10">
  </div>
  <div class="slider-row">
    <div class="slider-label"><span>Damping</span><span id="dampVal">0.00</span></div>
    <input type="range" id="damp" min="0" max="50" value="0">
  </div>
</div>

<div class="panel" id="hud">
  <div class="panel-title">Live Data</div>
  <div class="metric">
    <div class="metric-label">DISPLACEMENT</div>
    <div class="metric-value" id="dispVal">0.0</div>
  </div>
  <div class="metric">
    <div class="metric-label">KINETIC ENERGY</div>
    <div class="metric-value" id="keVal">0.0</div>
  </div>
  <div class="metric">
    <div class="metric-label">PERIOD (s)</div>
    <div class="metric-value" id="periodVal">2.00</div>
  </div>
</div>

<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
let W, H, anchorX, anchorY, restLen;

// State
let t = 0, damping = 0, A = 120, omega = Math.PI;

function captureState() { return { t }; }
function restoreState(s) { t = s.t; }

function resize() {
  const s = captureState();
  W = canvas.width = Math.min(window.innerWidth - 260, 860);
  H = canvas.height = Math.min(window.innerHeight - 40, 580);
  anchorX = W * 0.5;
  anchorY = H * 0.12;
  restLen = H * 0.28;
  restoreState(s);
}
window.addEventListener('resize', resize);
resize();

// Controls
document.getElementById('amp').addEventListener('input', e => {
  A = +e.target.value;
  document.getElementById('ampVal').textContent = A;
});
document.getElementById('freq').addEventListener('input', e => {
  omega = (+e.target.value / 10) * Math.PI;
  document.getElementById('freqVal').textContent = (+e.target.value / 10).toFixed(1);
  document.getElementById('periodVal').textContent = (2 / (+e.target.value / 10)).toFixed(2);
});
document.getElementById('damp').addEventListener('input', e => {
  damping = +e.target.value / 1000;
  document.getElementById('dampVal').textContent = damping.toFixed(2);
});

// Trail
const trail = [];
const MAX_TRAIL = 60;

function drawSpring(x1, y1, x2, y2) {
  const segs = 18;
  const amp = 12;
  ctx.strokeStyle = '#a6adc8';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  for (let i = 1; i <= segs; i++) {
    const py = y1 + ((y2 - y1) / segs) * i;
    const px = x1 + (i % 2 === 0 ? 0 : (i % 4 === 1 ? amp : -amp));
    ctx.lineTo(px, py);
  }
  ctx.stroke();
}

function draw() {
  ctx.clearRect(0, 0, W, H);

  // Background
  const bg = ctx.createRadialGradient(W * 0.3, H * 0.4, 0, W * 0.3, H * 0.4, W * 0.7);
  bg.addColorStop(0, '#313244');
  bg.addColorStop(1, '#1e1e2e');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);

  // Ceiling bar
  ctx.fillStyle = '#45475a';
  ctx.fillRect(anchorX - 30, anchorY - 8, 60, 8);

  // Physics
  const disp = A * Math.sin(omega * t) * Math.exp(-damping * t);
  const ballY = anchorY + restLen + disp;

  // Trail
  trail.push({ x: anchorX, y: ballY });
  if (trail.length > MAX_TRAIL) trail.shift();
  for (let i = 0; i < trail.length; i++) {
    const alpha = i / trail.length;
    ctx.beginPath();
    ctx.arc(trail[i].x, trail[i].y, 4 * alpha, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(203, 166, 247, ${alpha * 0.3})`;
    ctx.fill();
  }

  // Spring
  drawSpring(anchorX, anchorY, anchorX, ballY - 20);

  // Ball glow
  ctx.shadowColor = '#cba6f7';
  ctx.shadowBlur = 24;
  ctx.beginPath();
  ctx.arc(anchorX, ballY, 20, 0, Math.PI * 2);
  ctx.fillStyle = '#cba6f7';
  ctx.fill();
  ctx.shadowBlur = 0;

  // HUD update
  const ke = 0.5 * Math.pow(omega, 2) * Math.pow(disp, 2);
  document.getElementById('dispVal').textContent = disp.toFixed(1) + ' px';
  document.getElementById('keVal').textContent = ke.toFixed(1);

  t += 0.016;
  requestAnimationFrame(draw);
}
draw();
</script>
</body>
</html>"""

with open(OUT_PATH, "w") as f:
    f.write(html)
</run_simulacra>
```

---

## Critical Rules Summary

| # | Rule |
|---|------|
| 1 | Always write to `OUT_PATH`. Never hardcode paths. |
| 2 | Self-contained HTML. No external JS/CSS. Google Fonts `<link>` is the only allowed external dependency. |
| 3 | Assess complexity tier **before** writing code. |
| 4 | Slider count decided by judgment rules — minimum 1, maximum 6. |
| 5 | All layout constants derived from `W` and `H`. Never hardcode pixel positions. |
| 6 | Simulation state must survive resize. Implement `captureState()` / `restoreState()`. |
| 7 | Catppuccin Mocha palette strictly. No off-palette colors. |
| 8 | Floating glassmorphism panels for controls and HUD. Never raw HTML elements at page bottom. |
| 9 | Always include motion trails or ghosting for moving objects. |
| 10 | Always show at least 2 real-time derived metrics in the HUD. |
| 11 | For Complex tier approximations: label them `"Simplified Model"` in the HUD. Never fake behavior silently. |
| 12 | Wait for engine `SUCCESS` before responding. On `FAILED`, diagnose and retry once. |
| 13 | On `SUCCESS`, always embed `![Name](relative/file/path.html)` in your final response so GhostChat opens it in the browser.|