"use strict"
// preload — mirrors desktop/preload.js so the shipped web UI finds the exact
// API surface it already knows (`window.sableDesktop`).

const { contextBridge, ipcRenderer } = require("electron")

contextBridge.exposeInMainWorld("sableDesktop", {
  // boot / splash
  onStatus: (cb) => ipcRenderer.on("boot:status", (_e, payload) => cb(payload)),
  retry: () => ipcRenderer.invoke("boot:retry"),
  info: () => ipcRenderer.invoke("boot:info"),
  log: () => ipcRenderer.invoke("boot:log"),

  // native browser pane — the web UI gates on hasNativeBrowser
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
