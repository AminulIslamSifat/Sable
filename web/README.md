
# Web — Frontend PWA & Integrated IDE

The `web/` directory contains Sable's complete browser-based interface: a vanilla JavaScript PWA with an embedded Monaco IDE, integrated terminal, multi-agent visualization, and glassmorphism design system. No framework — pure JS, HTML, and CSS with bundled vendor libraries.

***

## Entry Points

| File | Size | Purpose |
|:--|:--|:--|
| `index.html` | 90KB | Single-page application shell with all HTML structure |
| `app.js` | 350KB | Main application: markdown rendering, SSE client, auth, chat logic, UI state |

These two files form the core of the frontend. `app.js` is intentionally monolithic to avoid module loading complexity in a no-build-tool setup.

***

## JavaScript Modules (`js/`)

| File | Size | Purpose |
|:--|:--|:--|
| `filesystem.js` | 54KB | File tree explorer, Monaco editor integration, diff review, root picker |
| `agents.js` | 53KB | Multi-agent visualization: top bar status, agent panel, todo progress, notifications |
| `cookbook.js` | 31KB | Local model serving UI: download, serve, stop, hardware info, presets |
| `mode.js` | 23KB | Agent ↔ IDE layout switching, panel management, responsive mode transitions |
| `terminal.js` | 14KB | xterm.js integrated terminal with WebSocket PTY bridge |
| `resize-panels.js` | 5KB | Draggable panel resizing (VS Code-style splitters) |

### Research Module (`src/`)

| File | Size | Purpose |
|:--|:--|:--|
| `research.js` | 35KB | Deep research UI: query input, progress tracking, result display, source citations |

***

## Stylesheets (`css/`)

10 CSS files implementing a glassmorphism design system with 11 themes:

| File | Size | Purpose |
|:--|:--|:--|
| `panels.css` | 50KB | Settings panels, file system overlay, tree view, viewer pane |
| `components.css` | 38KB | Sidebar, navigation, buttons, inputs, modals, tooltips |
| `layout.css` | 33KB | Grid layout, IDE mode overrides, panel positioning |
| `chat.css` | 24KB | Message bubbles, shimmer/processing indicators, code blocks |
| `agents.css` | 20KB | Agent cards, status indicators, todo lists, progress bars |
| `ide.css` | 16KB | Monaco editor container, file tree sidebar, tab bar |
| `cookbook.css` | 11KB | Model serving UI cards, download progress, hardware badges |
| `variables.css` | 9KB | CSS custom properties, theme variables, font sizes, spacing scale |
| `base.css` | 3KB | Reset, typography, scrollbar styling, global defaults |
| `responsive.css` | 2KB | Mobile/tablet breakpoints, collapsed sidebar, stacked layout |

### Theme System
Themes are defined as CSS custom property overrides in `variables.css`. 11 built-in themes with dark/light variants. Theme selection persists in localStorage.

***

## Vendor Libraries (`vendor/`)

Bundled directly (no CDN dependency — works fully offline):

| Library | Size | Purpose |
|:--|:--|:--|
| `mermaid.min.js` | 3.4MB | Diagram rendering (flowcharts, sequence, Gantt, class, mindmap) |
| `mathjax-tex-chtml.js` | 1.1MB | LaTeX math rendering (inline `$...$` and block `$$...$$`) |
| `lucide.min.js` | 405KB | Icon library (30+ file type icons, UI elements) |
| `marked.min.js` | 35KB | Markdown → HTML parsing |
| `purify.min.js` | 29KB | DOMPurify XSS sanitization for rendered markdown |
| `monaco/` | — | Full VS Code editor (language detection, syntax highlighting, IntelliSense) |
| `xterm/` | — | Terminal emulator frontend (connects to server PTY via WebSocket) |

***

## Key Features

### Chat Interface
- SSE-based streaming with typewriter animation (adaptive batch size for low-memory devices)
- Thinking mode toggle (Fast/Auto/Thinking) per model
- Multi-tab chat with independent scroll positions
- Context injection display (timestamp, cwd, open file metadata)
- Markdown rendering with syntax highlighting, Mermaid diagrams, and LaTeX math

### Monaco Editor (IDE Mode)
- Full VS Code editor loaded from `/static/vendor/monaco/`
- Language detection by extension (Python, JS, TS, HTML, CSS, JSON, YAML, Markdown, TOML, SQL, XML, SVG, shell)
- Configurable font size (persisted to localStorage)
- Dirty state tracking with auto-save on file switch
- Ctrl+S keyboard shortcut
- Binary file detection with placeholder display

### File Tree Explorer
- Recursive directory browsing with expand/collapse
- 30+ file type icon mappings (Lucide icons)
- Root picker with server-side folder selection
- Recent folders history (localStorage, max 8)
- New file/folder creation from toolbar
- File size display, active file highlighting, path bar

### Integrated Terminal
- Real PTY via `os.forkpty()` (same mechanism as VS Code/node-pty)
- Fish shell with terminal capability probe interception
- WebSocket bridge with JSON protocol (input/output/resize/exit)
- Window resize via SIGWINCH forwarding + TIOCSWINSZ ioctl
- xterm.js frontend with resizable panel
- Multiple views: Terminal, Output, Problems tabs

### Multi-Agent Visualization
- Top bar agent status indicators with live progress
- Expandable agent panel showing all active agents
- Todo tracking with checkbox progress
- Notification queue drained at turn start
- Role-based icons and color coding

### Cookbook (Local Models)
- Model browsing and download with progress tracking
- Serve/stop controls with hardware compatibility checks
- Preset configurations for common setups
- Hardware detection display (GPU, RAM, VRAM)

***

## Communication with Server

| Protocol | Usage |
|:--|:--|
| **SSE** | Chat message streaming, agent progress events |
| **WebSocket** | Terminal PTY bidirectional communication |
| **REST API** | All CRUD operations, settings, file browsing, uploads |
| **LocalStorage** | Theme, font size, recent folders, UI preferences |

***

## Design Decisions

- **No build tools** — vanilla JS/CSS, no webpack/vite/bundler. Files served directly by FastAPI
- **Monolithic app.js** — avoids module loading complexity; trade-off is file size
- **Bundled vendors** — works fully offline, no CDN dependency
- **Glassmorphism design** — frosted glass aesthetic with backdrop-filter effects
- **Adaptive streaming** — batch size adjusts based on device memory for smooth rendering
- **Single-page architecture** — all views rendered client-side with JS-driven routing
