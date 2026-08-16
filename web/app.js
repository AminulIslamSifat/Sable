// app.js has been split into domain-specific modules under web/js/.
// See index.html for the load order.
//
// Modules (load order):
//   js/core.js              — MathJax config, DOM refs, auth gate, global state
//   js/tts.js               — TTS stream player, model capabilities
//   js/markdown.js          — Markdown renderer, mermaid, MathJax, lucide icons
//   js/chat.js              — Toast, dialog, tabs, panes, streaming, skill cards
//   js/tracknote.js         — File edit sidebar, TrackNote panel
//   js/sse.js               — SSE processing, message rendering, typewriter
//   js/sidebar.js           — Chat list, selectChat, project banner
//   js/statusbar.js         — Status bar, context breakdown, model selector
//   js/settings-projects.js — Projects settings, folder dropdown, data management
//   js/settings-init.js     — Service controls, consolidation, main init IIFE
//   js/settings-ui.js       — Settings overlay, keyboard shortcuts, universal save
//   js/library.js           — Library panel
//   js/telegram.js          — Email + Telegram mini client
//   js/memory-panel.js      — Brain/Memory panel, consolidation settings
//   js/skills-panel.js      — Skills management, tools, context pass
//   js/appearance.js        — Accounts, fonts, themes, MCP, mode, browser, context menu