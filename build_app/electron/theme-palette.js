"use strict"
// Palette mirrored from web/css/variables.css. The splash/loader load as
// file:// and cannot read the app's localStorage, so the main process resolves
// the app's theme into flat colors here and hands them to the splash on boot.

const DARK = {
  default:  { bg: "#0c0c0d", panel: "#17171a", border: "#26262a", text: "#eaeaea", dim: "#c4c4c8", muted: "#85858c", faint: "#616167", accent: "#9a7d4a", ok: "#6fcf97", danger: "#e5646a" },
  noctalia: { bg: "#0f0d15", panel: "#1a1625", border: "#2d2640", text: "#e0dce8", dim: "#b8b0c8", muted: "#8b7fa8", faint: "#6b6180", accent: "#8b5cf6", ok: "#34d399", danger: "#f87171" },
  ember:    { bg: "#0d0b0a", panel: "#1a1614", border: "#2e2622", text: "#eae4de", dim: "#c8beb4", muted: "#9a8878", faint: "#7a6a5c", accent: "#e05a2b", ok: "#4ade80", danger: "#ef4444" },
  ocean:    { bg: "#0a0d12", panel: "#141a22", border: "#243040", text: "#dde6f0", dim: "#b0c0d4", muted: "#7088a0", faint: "#54687c", accent: "#2d9cdb", ok: "#34d399", danger: "#f87171" },
  forest:   { bg: "#0a0d0a", panel: "#141a14", border: "#243024", text: "#dde8dd", dim: "#b0c8b0", muted: "#709870", faint: "#547054", accent: "#3daa5c", ok: "#6fcf97", danger: "#f87171" },
  rose:     { bg: "#0d0a0c", panel: "#1a1418", border: "#30242c", text: "#e8dde4", dim: "#c8b0be", muted: "#9a7088", faint: "#7a5468", accent: "#d4548a", ok: "#6fcf97", danger: "#f87171" },
  indigo:   { bg: "#0a0b12", panel: "#141625", border: "#262a44", text: "#dfe2f0", dim: "#b4b9d4", muted: "#7880a8", faint: "#5a6184", accent: "#5b6ee1", ok: "#34d399", danger: "#f87171" },
  crimson:  { bg: "#0d0a0b", panel: "#1a1416", border: "#302426", text: "#e8dede", dim: "#c8b4b4", muted: "#9a7878", faint: "#7a5c5c", accent: "#cf3b52", ok: "#4ade80", danger: "#ef4444" },
  mono:     { bg: "#0b0b0b", panel: "#161616", border: "#2a2a2a", text: "#e8e8e8", dim: "#c0c0c0", muted: "#8a8a8a", faint: "#666666", accent: "#d4d4d4", ok: "#6fcf97", danger: "#e5646a" },
  teal:     { bg: "#090d0d", panel: "#131a1a", border: "#233030", text: "#dce8e6", dim: "#b0c8c4", muted: "#709890", faint: "#547068", accent: "#14b8a6", ok: "#34d399", danger: "#f87171" },
  blue:     { bg: "#080b12", panel: "#121824", border: "#243048", text: "#dce4f4", dim: "#b0bcd8", muted: "#7084a8", faint: "#546484", accent: "#3b82f6", ok: "#34d399", danger: "#f87171" },
}

const LIGHT = {
  default:  { bg: "#f8f8f9", panel: "#ffffff", border: "#d8d8dc", text: "#1a1a1c", dim: "#48484c", muted: "#78787e", faint: "#a0a0a6", accent: "#9a7d4a", ok: "#2da44e", danger: "#d44048" },
  noctalia: { bg: "#f8f6fc", panel: "#fdfcff", border: "#d8d0e8", text: "#1c1824", dim: "#4a4460", muted: "#7a70a0", faint: "#a49cc0", accent: "#8b5cf6", ok: "#1aa868", danger: "#e04848" },
  ember:    { bg: "#fdf8f5", panel: "#fffdfb", border: "#e0d4cc", text: "#1e1814", dim: "#504438", muted: "#887468", faint: "#b0a096", accent: "#e05a2b", ok: "#2da44e", danger: "#d44048" },
  ocean:    { bg: "#f5f8fc", panel: "#fbfdff", border: "#ccd8e8", text: "#141c24", dim: "#3c4c60", muted: "#6880a0", faint: "#98b0cc", accent: "#2d9cdb", ok: "#1aa868", danger: "#e04848" },
  forest:   { bg: "#f5faf5", panel: "#fbfffb", border: "#cce0cc", text: "#141e14", dim: "#3c503c", muted: "#689068", faint: "#98c098", accent: "#3daa5c", ok: "#2da44e", danger: "#e04848" },
  rose:     { bg: "#fdf5f8", panel: "#fffbfd", border: "#e0ccd8", text: "#1e141a", dim: "#503c48", muted: "#886878", faint: "#c0a0b0", accent: "#d4548a", ok: "#2da44e", danger: "#e04848" },
  indigo:   { bg: "#f5f6fc", panel: "#fbfcff", border: "#ccd0e8", text: "#141624", dim: "#3c4060", muted: "#6870a0", faint: "#98a0c8", accent: "#5b6ee1", ok: "#1aa868", danger: "#e04848" },
  crimson:  { bg: "#fdf5f6", panel: "#fffbfc", border: "#e0ccd0", text: "#1e1416", dim: "#503c40", muted: "#886868", faint: "#c0a0a0", accent: "#cf3b52", ok: "#2da44e", danger: "#d44048" },
  mono:     { bg: "#f8f8f8", panel: "#ffffff", border: "#d4d4d4", text: "#1a1a1a", dim: "#484848", muted: "#787878", faint: "#a6a6a6", accent: "#555555", ok: "#2da44e", danger: "#d44048" },
  teal:     { bg: "#f4faf9", panel: "#fbfffe", border: "#c8e0dc", text: "#141e1c", dim: "#3c504c", muted: "#689088", faint: "#98c0b8", accent: "#14b8a6", ok: "#1aa868", danger: "#e04848" },
  blue:     { bg: "#f4f7fc", panel: "#fbfdff", border: "#c8d4e8", text: "#141a24", dim: "#3c4860", muted: "#687ca0", faint: "#98b0d8", accent: "#3b82f6", ok: "#1aa868", danger: "#e04848" },
}

function rgb(hex) {
  let h = String(hex || "").replace("#", "")
  if (h.length === 3) h = h.split("").map((c) => c + c).join("")
  const n = parseInt(h, 16)
  if (!Number.isFinite(n)) return "154,125,74"
  return `${(n >> 16) & 255},${(n >> 8) & 255},${n & 255}`
}

// resolve(theme, mode) -> flat colors + rgb helpers the splash can consume.
function resolve(theme, mode) {
  const m = mode === "light" ? "light" : "dark"
  const table = m === "light" ? LIGHT : DARK
  const entry = table[theme] || table.default
  return {
    theme: table[theme] ? theme : "default",
    mode: m,
    bg: entry.bg,
    panel: entry.panel,
    border: entry.border,
    text: entry.text,
    dim: entry.dim,
    muted: entry.muted,
    faint: entry.faint || entry.muted,
    accent: entry.accent,
    accentRgb: rgb(entry.accent),
    ok: entry.ok,
    okRgb: rgb(entry.ok),
    warn: m === "light" ? "#b07400" : "#e8c07d",
    danger: entry.danger,
  }
}

module.exports = { resolve, PALETTES: { dark: DARK, light: LIGHT } }