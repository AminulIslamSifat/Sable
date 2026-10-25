"use strict"

/**
 * Self-install for the AppImage build.
 *
 * Symptom this exists to prevent: an AppImage does not register itself.
 * The user double-clicks Sable-*.AppImage, gets a window, closes it — and the
 * app is gone from their life. No menu entry, no icon, no launcher hit.
 * The .desktop file is sitting inside the AppImage; nothing ever wrote it out.
 *
 * On launch we copy the AppImage into XDG data home and write both the
 * .desktop entry and the icon, so Sable shows up in the launcher like a real
 * install. Idempotent: re-runs every launch, only rewrites what changed.
 */

const fs = require("node:fs")
const path = require("node:path")
const os = require("node:os")
const { execFileSync } = require("node:child_process")

const APP_ID = "sable"
const APP_NAME = "Sable"
const COMMENT = "Sable - agentic chat platform"

function dataHome() {
  return process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share")
}

function targetPaths() {
  const base = dataHome()
  return {
    installed: path.join(base, APP_ID, "Sable.AppImage"),
    desktop: path.join(base, "applications", APP_ID + ".desktop"),
    icon: path.join(base, "icons", "hicolor", "256x256", "apps", APP_ID + ".png"),
  }
}

function runningAppImage() {
  const p = process.env.APPIMAGE
  if (!p) return null
  try {
    if (!fs.existsSync(p)) return null
  } catch (e) {
    return null
  }
  return p
}

function sourceIcon() {
  const dir = process.env.APPDIR
  if (!dir) return null
  const candidate = path.join(dir, APP_ID + ".png")
  try {
    return fs.existsSync(candidate) ? candidate : null
  } catch (e) {
    return null
  }
}

// Quote a path for a .desktop Exec line. Only quote when needed.
function shellQuote(p) {
  if (p.indexOf(" ") === -1 && p.indexOf('"') === -1 && p.indexOf("'") === -1) {
    return p
  }
  const escaped = p.split('"').join('\"')
  return '"' + escaped + '"'
}

function desktopEntryBody(execPath, iconPath) {
  return [
    "[Desktop Entry]",
    "Type=Application",
    "Version=1.0",
    "Name=" + APP_NAME,
    "Comment=" + COMMENT,
    "Exec=" + shellQuote(execPath) + " --no-sandbox %U",
    "Icon=" + iconPath,
    "Terminal=false",
    "Categories=Development;",
    "StartupWMClass=Sable",
    "StartupNotify=true",
    "",
  ].join("\n")
}

function writeIfChanged(file, content, mode) {
  try {
    if (fs.existsSync(file)) {
      const current = fs.readFileSync(file, "utf8")
      if (current === content) {
        if (mode !== undefined) fs.chmodSync(file, mode)
        return false
      }
    }
  } catch (e) {
    /* fall through and rewrite */
  }
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, content, { mode: mode })
  return true
}

function refreshDesktopDatabase(dir) {
  try {
    execFileSync("update-desktop-database", [dir], { stdio: "ignore", timeout: 5000 })
  } catch (e) {
    /* absent on minimal systems */
  }
  try {
    const iconRoot = path.dirname(path.dirname(path.dirname(dir)))
    execFileSync("gtk-update-icon-cache", [iconRoot], { stdio: "ignore", timeout: 5000 })
  } catch (e) {
    /* absent on minimal systems */
  }
}

function ensureInstalled(opts) {
  const options = opts || {}
  const log = options.log || function () {}

  const src = runningAppImage()
  if (!src) return { skipped: true, reason: "not running from an AppImage" }

  const dest = targetPaths()
  let copied = false

  try {
    let needsCopy = true
    try {
      const a = fs.statSync(src)
      const b = fs.statSync(dest.installed)
      needsCopy = a.size !== b.size || a.mtimeMs > b.mtimeMs
    } catch (e) {
      needsCopy = true
    }

    if (needsCopy && path.resolve(src) !== path.resolve(dest.installed)) {
      fs.mkdirSync(path.dirname(dest.installed), { recursive: true })
      const tmp = dest.installed + ".tmp-" + process.pid
      fs.copyFileSync(src, tmp)
      fs.chmodSync(tmp, 0o755)
      fs.renameSync(tmp, dest.installed)
      copied = true
      log("installed appimage -> " + dest.installed)
    } else {
      fs.chmodSync(dest.installed, 0o755)
    }
  } catch (err) {
    const msg = err && err.message ? err.message : String(err)
    log("appimage copy failed: " + msg)
    return { skipped: true, reason: "copy failed: " + msg }
  }

  let iconWritten = false
  const iconSrc = sourceIcon()
  if (iconSrc) {
    try {
      fs.mkdirSync(path.dirname(dest.icon), { recursive: true })
      fs.copyFileSync(iconSrc, dest.icon)
      iconWritten = true
    } catch (err) {
      const msg = err && err.message ? err.message : String(err)
      log("icon copy failed: " + msg)
    }
  }

  let wroteDesktop = false
  try {
    const iconRef = iconWritten ? dest.icon : APP_ID
    wroteDesktop = writeIfChanged(dest.desktop, desktopEntryBody(dest.installed, iconRef), 0o644)
    if (wroteDesktop) log("desktop entry -> " + dest.desktop)
  } catch (err) {
    const msg = err && err.message ? err.message : String(err)
    log("desktop entry failed: " + msg)
    return { skipped: true, reason: "desktop entry failed: " + msg }
  }

  if (copied || wroteDesktop || iconWritten) {
    refreshDesktopDatabase(path.dirname(dest.desktop))
  }

  return {
    skipped: false,
    installed: dest.installed,
    desktop: dest.desktop,
    copied: copied,
    wroteDesktop: wroteDesktop,
  }
}

module.exports = { ensureInstalled: ensureInstalled, targetPaths: targetPaths }
