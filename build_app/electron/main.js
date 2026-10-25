"use strict"
// Sable Desktop — Electron shell around the bundled sable-server binary.
// Ships with the Python server baked in. User installs, double-clicks, done.

const { app, BrowserWindow, shell, ipcMain, dialog, Menu, WebContentsView } = require("electron")
const { ensureInstalled } = require("./installer")
const { capturePerchanceKey } = require("./perchance")
const { spawn } = require("node:child_process")
const path = require("node:path")
const fs = require("node:fs")
const net = require("node:net")
const os = require("node:os")
const http = require("node:http")
const ncrypto = require("node:crypto")

const IS_WIN = process.platform === "win32"
const IS_MAC = process.platform === "darwin"

// ── paths ───────────────────────────────────────────────────────
// resources/ holds the PyInstaller output; userData holds mutable state.
function resourceRoot() {
  return app.isPackaged ? process.resourcesPath : path.join(__dirname, "..")
}

function serverDir() {
  return path.join(resourceRoot(), "server")
}

function serverBin() {
  const name = IS_WIN ? "sable-server.exe" : "sable-server"
  return path.join(serverDir(), name)
}

// Everything the server writes goes here — survives app updates.
function persistentRoot() {
  return path.join(app.getPath("userData"), "data")
}

// ── port ────────────────────────────────────────────────────────
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.unref()
    srv.on("error", reject)
    srv.listen(0, "127.0.0.1", () => {
      const p = srv.address().port
      srv.close(() => resolve(p))
    })
  })
}

async function waitForPort(port, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const ok = await new Promise((res) => {
      const sock = net.connect({ host: "127.0.0.1", port }, () => {
        sock.destroy()
        res(true)
      })
      sock.on("error", () => res(false))
      sock.setTimeout(800, () => {
        sock.destroy()
        res(false)
      })
    })
    if (ok) return true
    await new Promise((r) => setTimeout(r, 250))
  }
  return false
}

// ── state ───────────────────────────────────────────────────────
let win = null
let child = null
let serverPort = null
let quitting = false
let logStream = null

function logPath() {
  const dir = path.join(app.getPath("userData"), "logs")
  fs.mkdirSync(dir, { recursive: true })
  return path.join(dir, "server.log")
}

// ── splash status plumbing ──────────────────────────────────────
// The shipped loader.html drives itself from status TEXT (it regexes for
// "looking for", "booting", "server ready"). Sending the wrong string does
// not just look wrong — it leaves the step rail stuck.
let splashLoaded = false
let pendingStatus = null
let lastLogSent = ""

function send(text, tone) {
  if (!win || win.isDestroyed()) return
  const payload = { text, tone: tone || "info" }
  if (!splashLoaded) {
    pendingStatus = payload
    return
  }
  win.webContents.send("sable:status", payload)
}

function readLogTail(maxLines) {
  try {
    const raw = fs.readFileSync(logPath(), "utf8")
    const lines = raw.split(/\r?\n/).filter((l) => l.length > 0)
    return lines.slice(-(maxLines || 18)).join("\n")
  } catch {
    return ""
  }
}

function pushLog(force) {
  if (!win || win.isDestroyed() || !splashLoaded) return
  const tail = readLogTail()
  if (!force && tail === lastLogSent) return
  lastLogSent = tail
  win.webContents.send("sable:log", { text: tail })
}

// ── server lifecycle ────────────────────────────────────────────
async function ensureBrowser(bin, env) {
  // Ask the bundled server whether a Playwright-controllable browser exists.
  // It prints SABLE_BROWSER <stage> <detail> lines and exits 0/1.
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(bin, ["--ensure-browser"], {
        env,
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      })
    } catch (e) {
      send(`browser check skipped: ${e.message}`)
      return resolve(true)
    }

    let out = ""
    let downloaded = false

    child.stdout.on("data", (buf) => {
      out += buf.toString()
      for (const line of out.split("\n")) {
        if (!line.startsWith("SABLE_BROWSER ")) continue
        const [, stage, ...rest] = line.split(" ")
        const detail = rest.join(" ").replace(/_/g, " ")
        if (stage === "found" || stage === "ready") {
          send(`using system browser: ${detail}`)
        } else if (stage === "missing") {
          send("no browser found — downloading Chromium (one time)…")
        } else if (stage === "download") {
          downloaded = true
          if (detail === "starting") send("downloading Chromium…")
          else if (detail === "done") send("browser download complete")
        } else if (stage === "progress") {
          send(`downloading Chromium: ${detail}`)
        } else if (stage === "error") {
          send(`browser setup warning: ${detail}`)
        }
      }
      out = out.split("\n").pop() || ""
    })

    // If it stalls for 4 minutes, move on — the server still runs without a browser.
    const timer = setTimeout(() => {
      try { child.kill() } catch {}
      send("browser setup timed out — continuing")
      resolve(true)
    }, 240_000)

    child.on("exit", (code) => {
      clearTimeout(timer)
      if (code !== 0) send("browser setup failed — continuing without one")
      resolve(true)
    })
    child.on("error", () => {
      clearTimeout(timer)
      resolve(true)
    })
  })
}

async function startServer() {
  const bin = serverBin()
  if (!fs.existsSync(bin)) {
    send(`server binary missing:\n${bin}`, "error")
    return null
  }

  fs.mkdirSync(persistentRoot(), { recursive: true })
  serverPort = await freePort()

  const env = {
    ...process.env,
    SABLE_HOST: "127.0.0.1",
    SABLE_PORT: String(serverPort),
    SABLE_PERSISTENT_ROOT: persistentRoot(),
    PYTHONUNBUFFERED: "1",
    PYTHONDONTWRITEBYTECODE: "1",
  }
  if (IS_WIN) delete env.PYTHONHOME

  // One-time browser check before the server starts. Uses an existing
  // Chrome/Edge if present; downloads Playwright Chromium only if not.
  send("checking for a usable browser…")
  await ensureBrowser(bin, env)

  logStream = fs.createWriteStream(logPath(), { flags: "a" })
  logStream.write(`\n=== ${new Date().toISOString()} booting on :${serverPort} ===\n`)

  child = spawn(bin, [], {
    cwd: persistentRoot(),
    env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  })

  child.stdout.pipe(logStream)
  child.stderr.pipe(logStream)

  child.on("exit", (code, signal) => {
    if (!quitting)
      send(`server exited (code ${code}${signal ? `, ${signal}` : ""})`, "error")
  })
  child.on("error", (e) => send(`failed to spawn server: ${e.message}`, "error"))

  send("booting — server starting, waiting for port…")
  // Stream the log into the splash while we wait — a static "starting" line
  // for 40s reads as a hang, and the loader has a live log pane for exactly
  // this. Poll cheaply; pushLog() no-ops when the tail hasn't changed.
  const logPump = setInterval(() => pushLog(), 400)
  const up = await waitForPort(serverPort)
  clearInterval(logPump)
  pushLog(true)
  if (!up) {
    send("server did not open its port in time — check the log", "error")
    return null
  }
  send(`server ready — listening on 127.0.0.1:${serverPort}`)
  return `http://127.0.0.1:${serverPort}`
}

function stopServer() {
  if (!child) return
  try {
    if (IS_WIN) child.kill()
    else child.kill("SIGTERM")
  } catch {}
  child = null
}

// ── window ──────────────────────────────────────────────────────
function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 620,
    show: false,
    backgroundColor: "#0d1117",
    title: "Sable",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: true,
    },
  })

  win.once("ready-to-show", () => win.show())

  // external links open in the real browser, not inside the app
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url)
    return { action: "deny" }
  })

  win.on("closed", () => {
    win = null
  })

  return win
}

function loadSplash() {
  splashLoaded = false
  lastLogSent = ""
  win.webContents.once("did-finish-load", () => {
    splashLoaded = true
    if (pendingStatus) {
      win.webContents.send("sable:status", pendingStatus)
      pendingStatus = null
    }
    pushLog(true)
  })
  win.loadFile(path.join(__dirname, "splash.html"))
}

async function boot() {
  loadSplash()
  const url = await startServer()
  if (!url) return
  // give the splash a beat so it doesn't flash
  await new Promise((r) => setTimeout(r, 300))
  if (win && !win.isDestroyed()) win.loadURL(url).catch((e) => send(e.message, "error"))
}

// ── IPC ─────────────────────────────────────────────────────────
ipcMain.on("sable:retry", async () => {
  stopServer()
  await boot()
})

ipcMain.on("sable:open-external", (_e, url) => {
  try {
    if (url) shell.openExternal(url)
  } catch {}
})

ipcMain.handle("sable:info", () => ({
  port: serverPort,
  persistentRoot: persistentRoot(),
  log: logPath(),
  packaged: app.isPackaged,
  version: app.getVersion(),
}))

// ── native browser pane (WebContentsView) ───────────────────────
// The web UI asks for `window.sableDesktop.hasNativeBrowser` and renders an
// iframe placeholder; it then measures that placeholder and sends the rect
// here. We layer a real Chromium view on top so in-app browsing keeps its own
// cookies, history, and login state instead of fighting the app renderer.

let browserView = null
let browserViewAttached = false
let browserBounds = null

function pushBrowserState(patch) {
  if (win && !win.isDestroyed()) win.webContents.send("sable:browser-state", patch)
}

function ensureBrowserView() {
  if (browserView && !browserView.webContents.isDestroyed()) return browserView
  browserView = new WebContentsView({
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      partition: "persist:sable-browser",
    },
  })
  const wc = browserView.webContents
  wc.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) wc.loadURL(url)
    return { action: "deny" }
  })
  wc.on("did-navigate", (_e, url) => pushBrowserState({ url }))
  wc.on("did-navigate-in-page", (_e, url) => pushBrowserState({ url }))
  wc.on("page-title-updated", (_e, title) => pushBrowserState({ title }))
  wc.on("did-start-loading", () => pushBrowserState({ loading: true }))
  wc.on("did-stop-loading", () => {
    pushBrowserState({ loading: false })
    try {
      const nav = wc.navigationHistory
      pushBrowserState({ canBack: nav.canGoBack(), canForward: nav.canGoForward() })
    } catch {
      pushBrowserState({ canBack: wc.canGoBack(), canForward: wc.canGoForward() })
    }
  })
  return browserView
}

function applyBrowserBounds(rect) {
  if (!win || !browserView || browserView.webContents.isDestroyed()) return
  if (!rect || typeof rect.x !== "number" || rect.width <= 0 || rect.height <= 0) {
    browserView.setVisible(false)
    return
  }
  // getBoundingClientRect gives CSS px; WebContentsView wants DIPs.
  const zoom = win.webContents.getZoomFactor() || 1
  browserView.setBounds({
    x: Math.round(rect.x * zoom),
    y: Math.round(rect.y * zoom),
    width: Math.round(rect.width * zoom),
    height: Math.round(rect.height * zoom),
  })
  browserView.setVisible(true)
}

function attachBrowserView() {
  if (!win || !browserView || browserViewAttached) return
  win.contentView.addChildView(browserView)
  browserViewAttached = true
  browserView.setVisible(false)
  if (browserBounds) applyBrowserBounds(browserBounds)
}

ipcMain.handle("sable:browser-open", (_e, url) => {
  if (!win || typeof url !== "string" || !/^https?:/i.test(url)) return false
  const view = ensureBrowserView()
  attachBrowserView()
  view.webContents.loadURL(url)
  return true
})

ipcMain.on("sable:browser-bounds", (_e, rect) => {
  if (!win) return
  if (!rect || typeof rect.x !== "number") return
  browserBounds = rect
  applyBrowserBounds(rect)
})

ipcMain.on("sable:browser-close", () => {
  if (browserView) browserView.setVisible(false)
})

ipcMain.on("sable:browser-back", () => {
  if (!browserView) return
  try {
    const nav = browserView.webContents.navigationHistory
    if (nav.canGoBack()) nav.goBack()
  } catch {
    if (browserView.webContents.canGoBack()) browserView.webContents.goBack()
  }
})

ipcMain.on("sable:browser-forward", () => {
  if (!browserView) return
  try {
    const nav = browserView.webContents.navigationHistory
    if (nav.canGoForward()) nav.goForward()
  } catch {
    if (browserView.webContents.canGoForward()) browserView.webContents.goForward()
  }
})

ipcMain.on("sable:browser-reload", () => {
  if (browserView) browserView.webContents.reload()
})

ipcMain.on("sable:browser-system-open", (_e, url) => {
  if (typeof url === "string" && /^https?:/i.test(url)) shell.openExternal(url)
})

// ── search bridge ───────────────────────────────────────────────
// A localhost HTTP server the Python backend calls to run multi-engine search
// through real Chromium tabs. The marker file is the handshake: the backend
// only advertises "sable" as a search provider when the marker exists, so a
// plain web build never shows it.

const SEARCH_PORT = Number(process.env.SABLE_SEARCH_PORT || 61772)
const SEARCH_TOKEN = ncrypto.randomBytes(24).toString("hex")
const SEARCH_MARKER = path.join(os.homedir(), ".cache", "sable", "search-bridge.json")

function writeSearchMarker() {
  try {
    fs.mkdirSync(path.dirname(SEARCH_MARKER), { recursive: true })
    fs.writeFileSync(
      SEARCH_MARKER,
      JSON.stringify(
        { url: `http://127.0.0.1:${SEARCH_PORT}`, token: SEARCH_TOKEN, pid: process.pid, at: Date.now() },
        null,
        2,
      ) + "\n",
      { mode: 0o600 },
    )
  } catch (exc) {
    console.error("[search-bridge] marker write failed:", exc.message)
  }
}

function removeSearchMarker() {
  try { fs.unlinkSync(SEARCH_MARKER) } catch {}
}

function startSearchBridge() {
  const server = http.createServer((req, res) => {
    const send = (code, body) => {
      const json = JSON.stringify(body)
      res.writeHead(code, {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(json),
      })
      res.end(json)
    }

    if (req.headers["x-sable-token"] !== SEARCH_TOKEN) return send(403, { error: "forbidden" })
    if (req.method === "GET" && req.url === "/health") return send(200, { ok: true, pid: process.pid })

    // The Python side calls this before falling back to spawning an external
    // Chrome. Without it, perchance key refresh drops a visible browser window
    // on the user's desktop.
    if (req.method === "POST" && req.url === "/perchance-key") {
      // Body is unused; drain it so the socket closes cleanly.
      req.on("data", () => {})
      req.on("end", async () => {
        try {
          const out = await capturePerchanceKey({ parentWindow: win })
          if (out.key) return send(200, { userKey: out.key })
          return send(504, { error: out.error || "no userKey captured" })
        } catch (exc) {
          send(500, { error: String(exc.message || exc) })
        }
      })
      return
    }

    if (req.method === "POST" && (req.url === "/search" || req.url === "/images")) {
      const isImages = req.url === "/images"
      let raw = ""
      req.on("data", (c) => {
        raw += c
        if (raw.length > 64_000) req.destroy()
      })
      req.on("end", async () => {
        let body = {}
        try { body = JSON.parse(raw || "{}") } catch { return send(400, { error: "bad json" }) }
        const query = String(body.query || "").trim()
        if (!query) return send(400, { error: "query required" })
        try {
          const mod = require("./search-engines")
          const out = isImages
            ? await mod.imageSearch(query, { count: body.count, engines: body.engines })
            : await mod.multiSearch(query, {
                count: body.count,
                engines: body.engines,
                timeFilter: body.time_filter,
              })
          send(200, out)
        } catch (exc) {
          send(500, { error: String(exc.message || exc), results: [], engines: [] })
        }
      })
      return
    }

    send(404, { error: "not found" })
  })

  server.on("error", (exc) => {
    console.error("[search-bridge] listen failed:", exc.message)
    removeSearchMarker()
  })

  server.listen(SEARCH_PORT, "127.0.0.1", () => {
    writeSearchMarker()
    console.log(`[search-bridge] listening on 127.0.0.1:${SEARCH_PORT}`)
  })

  return server
}

let searchServer = null

// ── lifecycle ───────────────────────────────────────────────────
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore()
      win.focus()
    }
  })

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null)

    // Register with the desktop on first run. An AppImage does not do this by
    // itself — without it there is no menu entry and no icon, and the app is
    // effectively invisible after the first close. No-op outside an AppImage.
    try {
      const res = ensureInstalled({ log: (m) => console.log("[install] " + m) })
      if (res.skipped) console.log("[install] skipped: " + res.reason)
    } catch (err) {
      console.error("[install] failed:", err)
    }

    createWindow()
    searchServer = startSearchBridge()
    await boot()
  })

  app.on("window-all-closed", () => {
    quitting = true
    stopServer()
    app.quit()
  })

  app.on("will-quit", () => {
    removeSearchMarker()
    if (searchServer) {
      try { searchServer.close() } catch {}
      searchServer = null
    }
  })

  app.on("before-quit", () => {
    quitting = true
    stopServer()
  })

  app.on("activate", () => {
    if (!win) {
      createWindow()
      boot()
    }
  })
}
