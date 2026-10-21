"use strict"

const { app, BrowserWindow, Menu, ipcMain, shell } = require("electron")
const { spawn } = require("node:child_process")
const fs = require("node:fs")
const http = require("node:http")
const path = require("node:path")

// ── Constants ───────────────────────────────────────────────────────────────
// SABLE_ROOT lets you point the shell at any Sable tree (e.g. the non-exp copy).
const ROOT = process.env.SABLE_ROOT
  ? path.resolve(process.env.SABLE_ROOT)
  : path.resolve(__dirname, "..")
const HOST = "127.0.0.1"
const DEFAULT_PORT = 61771
const BOOT_TIMEOUT_MS = 180_000
const POLL_MS = 300
const LOADER = path.join(__dirname, "loader.html")

// ── Port resolution: --port=N > SABLE_DESKTOP_PORT > SABLE_PORT > PORT > 61771
function readPort() {
  for (const arg of process.argv) {
    const m = /^--port=(\d+)$/.exec(arg)
    if (m) return Number(m[1])
  }
  for (const key of ["SABLE_DESKTOP_PORT", "SABLE_PORT", "PORT"]) {
    const v = process.env[key]
    if (v && /^\d+$/.test(v)) return Number(v)
  }
  return DEFAULT_PORT
}

const PORT = readPort()
const SERVER_URL = `http://${HOST}:${PORT}`

// ── State ───────────────────────────────────────────────────────────────────
let win = null
let splashLoaded = false
let booting = false
let quitting = false
let ownedChild = null // only set when *we* started the server
let logPath = null
let pendingStatus = null // status sent before the splash finished loading
let childExit = null // { code, signal } once the spawned server exits
let lastLogSent = ""

app.setName("Sable")

// Wayland hands Chromium chunky wheel-emulation scroll steps instead of the
// high-resolution axis_value120 events Gecko consumes (upstream Chromium bug
// 712737). Content then teleports in quantized jumps and every jump forces a
// synchronous decode+raster of the whole region. Smooth scrolling interpolates
// those steps so the view tracks the input instead of snapping to it.
app.commandLine.appendSwitch("enable-smooth-scrolling")

// Single instance: focus the existing window instead of booting twice.
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on("second-instance", () => {
    if (!win) return
    if (win.isMinimized()) win.restore()
    win.focus()
  })
}

// ── Splash status plumbing ──────────────────────────────────────────────────
function status(text, tone = "info") {
  if (!win || win.isDestroyed()) return
  const payload = { text, tone }
  if (!splashLoaded) {
    // Splash hasn't finished loading yet — replay it once it does.
    pendingStatus = payload
    return
  }
  win.webContents.send("sable:status", payload)
}

// ── Log tailing ─────────────────────────────────────────────────────────────
function readLogTail(maxLines = 18) {
  if (!logPath) return ""
  try {
    const raw = fs.readFileSync(logPath, "utf8")
    const lines = raw.split(/\r?\n/).filter((l) => l.length > 0)
    return lines.slice(-maxLines).join("\n")
  } catch {
    return ""
  }
}

function pushLog(force = false) {
  if (!win || win.isDestroyed() || !splashLoaded) return
  const tail = readLogTail()
  if (!force && tail === lastLogSent) return
  lastLogSent = tail
  win.webContents.send("sable:log", { text: tail })
}

// ── Health probe ────────────────────────────────────────────────────────────
function probe(timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.request(
      { host: HOST, port: PORT, path: "/api/health", method: "GET", timeout: timeoutMs },
      (res) => {
        res.resume()
        // /api/health is auth-exempt and returns 200 when the app is live.
        resolve(res.statusCode >= 200 && res.statusCode < 500)
      },
    )
    req.on("timeout", () => { req.destroy(); resolve(false) })
    req.on("error", () => resolve(false))
    req.end()
  })
}

// ── Spawn the server (only if nothing is listening) ─────────────────────────
function startServer(attempt = "launcher") {
  const logDir = path.join(ROOT, "system", "logs")
  fs.mkdirSync(logDir, { recursive: true })
  logPath = path.join(logDir, "desktop-server.log")

  const fd = fs.openSync(logPath, "a")
  fs.writeSync(
    fd,
    `\n===== ${new Date().toISOString()} — desktop shell starting Sable on :${PORT} (${attempt}) =====\n`,
  )

  childExit = null

  // A .desktop launch gets a minimal PATH — rebuild the useful parts.
  const env = { ...process.env, SABLE_PORT: String(PORT), PYTHONUNBUFFERED: "1" }
  // An inherited VIRTUAL_ENV from the launching shell can point at a different
  // Sable tree; `uv run` would warn and ignore it. Drop it so the child resolves
  // its own .venv cleanly.
  delete env.VIRTUAL_ENV
  delete env.PYTHONHOME
  env.PATH = [
    path.join(process.env.HOME || "", ".local", "bin"),
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    env.PATH || "",
  ].filter(Boolean).join(":")

  // SABLE_HOST is deliberately left alone — the server may bind 0.0.0.0 for LAN
  // access, and we always talk to it over loopback anyway.
  const launcher = path.join(ROOT, "sable-launch.sh")
  const useLauncher = attempt === "launcher" && fs.existsSync(launcher)
  const cmd = useLauncher ? "bash" : "uv"
  const args = useLauncher
    ? [launcher, "--no-browser", `--port=${PORT}`]
    : ["run", "python", "server.py"]

  const child = spawn(cmd, args, {
    cwd: ROOT,
    env,
    detached: true, // own process group → we can kill the whole tree
    stdio: ["ignore", fd, fd],
  })

  child.on("error", (err) => {
    childExit = { code: -1, signal: null, message: err.message }
    status(`Could not start server: ${err.message}`, "error")
  })
  child.on("exit", (code, signal) => {
    if (quitting) return
    // Record it; waitForServer() uses this to fail fast instead of polling a corpse.
    childExit = { code, signal }
  })

  ownedChild = child
  return logPath
}

function stopServer() {
  const child = ownedChild
  if (!child || child.exitCode !== null) return
  const pid = child.pid
  if (!pid) return
  try { process.kill(-pid, "SIGTERM") } catch { try { child.kill("SIGTERM") } catch {} }
  const t = setTimeout(() => {
    try { process.kill(-pid, "SIGKILL") } catch {}
  }, 3000)
  if (typeof t.unref === "function") t.unref()
}

// ── Boot sequence ───────────────────────────────────────────────────────────
async function waitForServer() {
  const startedAt = Date.now()
  let lastReport = 0
  while (Date.now() - startedAt < BOOT_TIMEOUT_MS) {
    if (quitting) return false

    // A server that died 40s ago is never going to answer. Stop waiting.
    if (childExit && childExit.code !== 0) {
      const why = childExit.signal || childExit.code
      status(`Server exited before becoming healthy (${why}).`, "error")
      pushLog(true)
      return false
    }

    if (await probe()) return true

    const elapsed = Math.round((Date.now() - startedAt) / 1000)
    if (elapsed >= lastReport + 5) {
      lastReport = elapsed
      status(`Server starting… ${elapsed}s`)
    }
    pushLog()
    await new Promise((r) => setTimeout(r, POLL_MS))
  }
  return false
}

async function boot() {
  if (booting || !win) return
  booting = true
  try {
    status(`Looking for Sable on ${HOST}:${PORT}…`)

    if (await probe()) {
      status("Connected to a running Sable server — loading UI…")
      await win.loadURL(SERVER_URL)
      return
    }

    status("No server on that port — starting one…")
    // SABLE_SKIP_STARTER=1 jumps straight to the plaintext server (useful when
    // the sealed vault's token bridge on :8765 isn't running).
    const skipStarter = process.env.SABLE_SKIP_STARTER === "1"
    logPath = startServer(skipStarter ? "python" : "launcher")
    status("Booting Sable (first run can take a while)…")

    if (await waitForServer()) {
      status("Server ready — loading Sable…")
      await win.loadURL(SERVER_URL)
      return
    }
    if (quitting) return

    // The sealed starter needs a token bridge (default :8765). If that isn't
    // running it dies instantly — fall back to the plaintext dev tree.
    if (skipStarter) {
      status(`Sable never answered on :${PORT}.\nCheck the log: ${logPath}`, "error")
      pushLog(true)
      return
    }
    status("Starter failed — falling back to plaintext server…", "warn")
    const log = startServer("python")
    status("Booting plaintext server…")

    if (await waitForServer()) {
      status("Server ready — loading Sable…")
      await win.loadURL(SERVER_URL)
    } else {
      status(`Sable never answered on :${PORT}.\nCheck the log: ${log}`, "error")
      pushLog(true)
    }
  } catch (err) {
    status(`Failed to load Sable: ${err && err.message ? err.message : err}`, "error")
  } finally {
    booting = false
  }
}

// ── Window ──────────────────────────────────────────────────────────────────
function createWindow() {
  win = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: "Sable",
    backgroundColor: "#0b0f14",
    autoHideMenuBar: true,
    icon: path.join(ROOT, "web", "assets", "icon-512.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  win.once("ready-to-show", () => win.show())

  win.webContents.on("did-finish-load", () => {
    splashLoaded = win.webContents.getURL().startsWith("file://")
    if (splashLoaded && pendingStatus) {
      win.webContents.send("sable:status", pendingStatus)
      pendingStatus = null
    }
    if (splashLoaded) {
      lastLogSent = ""
      pushLog(true)
    }
  })

  win.webContents.on("did-fail-load", (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || !url.startsWith(SERVER_URL)) return
    // Don't bounce back to the splash we're already showing (would loop).
    if (win.webContents.getURL().startsWith("file://")) {
      status(`Could not load the Sable UI (${desc}).`, "error")
      return
    }
    splashLoaded = false
    win.loadFile(LOADER)
    status(`Could not load the Sable UI (${desc}).`, "error")
  })

  // External links open in the real browser; in-app navigation stays in-app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(SERVER_URL) || url.startsWith("file://")) return { action: "allow" }
    if (/^https?:/i.test(url)) shell.openExternal(url)
    return { action: "deny" }
  })

  win.webContents.on("will-navigate", (event, url) => {
    if (url.startsWith(SERVER_URL) || url.startsWith("file://")) return
    event.preventDefault()
    if (/^https?:/i.test(url)) shell.openExternal(url)
  })

  win.on("closed", () => { win = null })

  win.loadFile(LOADER)
  void boot()
}

// ── Menu (roles keep copy/paste shortcuts alive) ────────────────────────────
function buildMenu() {
  const template = [
    {
      label: "File",
      submenu: [
        { label: "Reload UI", accelerator: "CmdOrCtrl+R", click: () => win && win.reload() },
        { label: "Restart server + reload", click: () => { stopServer(); void boot() } },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    { label: "Edit", submenu: [
      { role: "undo" }, { role: "redo" }, { type: "separator" },
      { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" },
    ] },
    { label: "View", submenu: [
      { role: "toggleDevTools" }, { type: "separator" },
      { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { type: "separator" },
      { role: "togglefullscreen" },
    ] },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ── IPC ─────────────────────────────────────────────────────────────────────
ipcMain.on("sable:retry", () => {
  if (!win) return
  splashLoaded = false
  win.loadFile(LOADER)
  void boot()
})

ipcMain.on("sable:open-external", (_e, url) => {
  if (typeof url === "string" && /^https?:/i.test(url)) shell.openExternal(url)
})

ipcMain.handle("sable:info", () => ({ port: PORT, url: SERVER_URL, root: ROOT, log: logPath }))

// ── Lifecycle ───────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  buildMenu()
  createWindow()
  app.on("activate", () => { if (!win) createWindow() })
})

app.on("window-all-closed", () => app.quit())

app.on("before-quit", () => {
  quitting = true
  stopServer()
})

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    quitting = true
    stopServer()
    app.quit()
  })
}
