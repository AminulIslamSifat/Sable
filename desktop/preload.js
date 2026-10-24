"use strict"

const { contextBridge, ipcRenderer } = require("electron")

contextBridge.exposeInMainWorld("sableDesktop", {
  onStatus: (cb) => ipcRenderer.on("sable:status", (_e, payload) => cb(payload)),
  onLog: (cb) => ipcRenderer.on("sable:log", (_e, payload) => cb(payload)),
  retry: () => ipcRenderer.send("sable:retry"),
  openExternal: (url) => ipcRenderer.send("sable:open-external", url),
  info: () => ipcRenderer.invoke("sable:info"),

  // ── Native browser pane (Electron only) ──────────────────────────────
  // These are absent on the plain web build, so the web UI can check
  // `window.sableDesktop?.hasNativeBrowser` and fall back to window.open.
  hasNativeBrowser: true,
  openBrowser: (url) => ipcRenderer.invoke("sable:browser-open", url),
  setBrowserBounds: (rect) => ipcRenderer.send("sable:browser-bounds", rect),
  closeBrowser: () => ipcRenderer.send("sable:browser-close"),
  browserBack: () => ipcRenderer.send("sable:browser-back"),
  browserForward: () => ipcRenderer.send("sable:browser-forward"),
  browserReload: () => ipcRenderer.send("sable:browser-reload"),
  openInSystemBrowser: (url) => ipcRenderer.send("sable:browser-system-open", url),
  onBrowserState: (cb) => ipcRenderer.on("sable:browser-state", (_e, patch) => cb(patch)),
})
