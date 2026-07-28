---
name: frontend-design
description: Create distinctive, production-grade frontend interfaces with high design quality. Use this skill when the user asks to build web components, pages, artifacts, posters, or applications (examples include websites, landing pages, dashboards, React components, HTML/CSS layouts, or when styling/beautifying any web UI). Generates creative, polished code and UI design that avoids generic AI aesthetics.
---

# Frontend Design: The Aesthetic Architect (v2.1)

Create distinctive, production-grade frontend interfaces that avoid generic "AI slop"
aesthetics. Use this when Sifat needs web components, pages, dashboards, landing pages,
or any UI that demands visual excellence.

---

##  Design Thinking (Before Code)

Before writing a single line of CSS, commit to a **BOLD** aesthetic direction.
Intentionality > intensity.

| Decision | Guidance |
|---|---|
| **Purpose** | What problem does this interface solve? Who uses it? |
| **Tone** | **Pick an extreme:** Brutally minimal, maximalist chaos, retro-futuristic, cyberpunk, luxury/refined, organic/natural, editorial/magazine, brutalist/raw, art deco, soft/pastel, industrial/utilitarian, playful/toy-like. Use these as inspiration — design something true to the direction, not a copy of the label. |
| **Constraints** | Technical requirements: framework, performance, accessibility. |
| **Differentiation** | What is the **ONE** thing someone will remember? (The "Hook") |

> [!IMPORTANT]
> **Vision Statement:** State your aesthetic intent before implementation.
> *Example: "Going for a 'Brutalist Editorial' vibe — heavy typography, zero borders,
> extreme negative space."*

Then implement working code (HTML/CSS/JS, React, Vue, etc.) that is:
- Production-grade and functional
- Visually striking and memorable
- Cohesive with a clear aesthetic point-of-view
- Meticulously refined in every detail

**Match implementation complexity to the aesthetic vision.** Maximalist designs need
elaborate code with extensive animations and effects. Minimalist designs need restraint,
precision, and careful attention to spacing, typography, and subtle details. Elegance
comes from executing the vision well, not from adding more.

---

##  Aesthetics Guidelines

###  Typography

- **NEVER** use generic fonts: Inter, Roboto, Arial, system fonts.
- **NEVER** converge on the same choices across generations. Every UI gets a unique
  soul. Avoid defaulting to Space Grotesk or other currently overused "distinctive"
  fonts — a font that was unexpected last month is a cliché today.
- Pair a distinctive **Display Font** (e.g., *Outfit, Syne, Cabinet Grotesk*) with a
  refined **Body Font** (e.g., *JetBrains Mono, Satoshi*). These are examples, not
  defaults — vary freely.
- Font choice should be the "Unforgettable" element when the layout is minimal.

**Font loading rule**: External fonts require a CDN. Use the permitted font CDNs
(Google Fonts, Bunny Fonts) for loading. If the project explicitly requires full
offline capability, use a curated inline `@font-face` block with base64-encoded WOFF2
data, or fall back to a system font stack that is deliberately styled to feel
intentional rather than default.

###  Color & Theme

- Commit to a cohesive palette using CSS variables for consistency and tweakability.
- Dominant colors with sharp accents > timid, evenly-distributed palettes.
- **NEVER** use cliché purple gradients on white backgrounds or other overused
  AI-generated color schemes.
- Vary between light and dark themes across generations — never converge on a default.

**RICE MATCHING**: When building system components, panels, or tools for Sifat's
personal setup, default to the **Noctalia Dark / Catppuccin Mocha** aesthetic unless
Sifat specifies a different direction for this project. For standalone UIs, landing
pages, or anything with its own identity, treat this as irrelevant and design freely.

###  Motion & Animation

- **Intentionality:** Focus on high-impact moments. One well-orchestrated entrance >
  scattered micro-interactions.
- Prioritize **CSS-only** solutions for HTML. Reach for the **Motion library** (React)
  only for complex state-driven transitions.
- Use staggered reveals (`animation-delay`), scroll-triggered surprises, and hover
  states that feel "alive."

###  Spatial Composition

- **Break the Grid:** Use asymmetry, overlapping elements, diagonal flows, and
  grid-breaking visuals.
- Generous negative space OR controlled density — pick one and commit fully.
- Unexpected layouts. Overlap. Diagonal flow. No design should look like another.

###  Backgrounds & Depth

- Create atmosphere and depth rather than defaulting to flat solid colors.
- Apply creative forms: gradient meshes, noise textures, geometric patterns, layered
  transparencies, dramatic shadows, decorative borders, grain overlays, custom cursors.
- `backdrop-filter: blur()` for glassmorphism depth when it fits the aesthetic.
- Add texture and context-specific effects that match the overall vision.

---

##  Implementation Rules

1. **CSS Variables First**: Every color, spacing token, and font must be tweakable via
   variables. No magic numbers scattered through the stylesheet.
2. **Font CDNs are permitted** via Google Fonts or Bunny Fonts. Full offline builds
   require inline `@font-face` with WOFF2 — document this choice explicitly.
3. **No other CDN dependencies**: All scripts, libraries, and assets beyond font loading
   must be inline or local. No jQuery, no Bootstrap, no utility CDNs.
4. **Responsive**: Must look exceptional on mobile AND on Sifat's ultra-wide Hyprland
   setup. Test both extremes mentally before finalizing layout.
5. **Self-Contained**: Default to a single portable HTML file unless project size
   genuinely demands a directory structure.
6. **No Placeholders**: Use the `Imagen` skill for real visuals and `SVG Creator` for
   icons. Never use gray boxes or lorem ipsum in final output.

---

##  Critical Evaluation (Pre-Output Checklist)

Before finalizing output, verify:

| Check | Pass condition |
|---|---|
| **Font uniqueness** | At least 2 non-generic fonts used; neither has appeared in a recent generation |
| **Layout distinction** | At least one non-rectangular layout element, asymmetric section, or grid-breaking component |
| **Color intentionality** | Palette has a clearly dominant hue + at least one sharp accent; no purple-on-white |
| **Animation quality** | At least one high-impact transition; no scattered unrelated micro-animations |
| **Variable coverage** | All colors and spacing defined as CSS variables |
| **Anti-slop** | Would this be mistaken for a generic Bootstrap or Tailwind template? If yes, redesign. |

If any check fails, fix before output — do not ship and note the issue.

---

##  Output Format

Use `<create_note>` for standalone HTML files:

```xml
<create_note path="Projects/UI/filename.html">
<!DOCTYPE html>
<html>
<!-- Full self-contained HTML with inline CSS/JS -->
</html>
</create_note>
```

---

##  Embedding & Finalization

After the file is confirmed saved to disk, embed the result:

```markdown
Here's your [Project Name]:
![Name](relative/file/path.html)
```

**Do not embed the wikilink until the file is physically saved.** The wikilink is
meaningless before the file exists.

---

## Global Rules

1. **Design thinking before code** — always. State the vision, then implement.
2. **Every generation is unique.** Font, palette, layout, and motion must vary. No
   design should resemble a previous one.
3. **Intentionality over intensity.** A precise minimalist design is as valid as
   elaborate maximalism — what fails is being generic.
4. **Claude is capable of extraordinary creative work.** Don't hold back. Commit fully
   to a distinctive vision and execute it without compromise.