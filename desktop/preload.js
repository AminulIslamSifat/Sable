"use strict"

const { contextBridge, ipcRenderer } = require("electron")

contextBridge.exposeInMainWorld("sableDesktop", {
  onStatus: (cb) => ipcRenderer.on("sable:status", (_e, payload) => cb(payload)),
  onLog: (cb) => ipcRenderer.on("sable:log", (_e, payload) => cb(payload)),
  retry: () => ipcRenderer.send("sable:retry"),
  openExternal: (url) => ipcRenderer.send("sable:open-external", url),
  info: () => ipcRenderer.invoke("sable:info"),
})
