"use strict"
// preload — mirrors desktop/preload.js so the shipped web UI finds the exact
// API surface it already knows (`window.sableDesktop`).

const { contextBridge, ipcRenderer } = require("electron")

contextBridge.exposeInMainWorld("sableDesktop", {
  // boot / splash — channel names match desktop/preload.js so the shipped
  // loader.html drives the boot sequence without modification.
  onStatus: (cb) => ipcRenderer.on("sable:status", (_e, payload) => cb(payload)),
  onLog: (cb) => ipcRenderer.on("sable:log", (_e, payload) => cb(payload)),
  retry: () => ipcRenderer.send("sable:retry"),
  openExternal: (url) => ipcRenderer.send("sable:open-external", url),
  info: () => ipcRenderer.invoke("sable:info"),

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
