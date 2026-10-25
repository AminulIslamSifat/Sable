"use strict"

/**
 * In-app Perchance userKey capture.
 *
 * Why this exists: the Python image generator needs a 64-char Perchance
 * userKey to call their API. Without a desktop shell it spawns an external
 * helium/chrome with --remote-debugging-port and scrapes CDP — which drops a
 * separate browser window on the user's screen.
 *
 * The shell can do better: open a hidden child window on the real generator
 * page, let the page mint its own key, and read it straight out of the
 * session's network layer. Nothing external, no CDP port, no window stealing
 * focus. This is a direct port of desktop/main.js so the packaged app and the
 * thin wrapper behave identically.
 */

const { BrowserWindow } = require("electron")

const PERCHANCE_URL = "https://perchance.org/ai-text-to-image-generator"
const PERCHANCE_PARTITION = "persist:sable-perchance-key"
const PERCHANCE_TIMEOUT_MS = 120_000
const PERCHANCE_UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

// Runs INSIDE the generator iframe. Perchance only asks for a userKey once a
// generation is actually started, so loading the page is not enough — this
// types a prompt and clicks Generate.
const PERCHANCE_TRIGGER = `(() => {
  var ta = null;
  var tas = document.querySelectorAll('textarea.paragraph-input');
  for (var i = 0; i < tas.length; i++) {
    if (tas[i].offsetParent !== null) { ta = tas[i]; break; }
  }
  if (!ta) ta = document.querySelector('textarea');
  if (!ta) return JSON.stringify({ error: 'no-textarea' });

  ta.focus();
  ta.value = 'test';
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.dispatchEvent(new Event('change', { bubbles: true }));
  try {
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, 'test');
  } catch (e) {}

  var btns = document.querySelectorAll('button, input[type=submit], [onclick]');
  var clicked = false;
  for (var j = 0; j < btns.length; j++) {
    var b = btns[j];
    if (/generate/i.test(b.textContent || b.value || '')) {
      b.click();
      clicked = true;
      break;
    }
  }
  return JSON.stringify({ typed: true, clicked: clicked });
})()`

let inFlight = null

/**
 * Capture a fresh Perchance userKey in a hidden child window.
 * Resolves { key, error } and never rejects. Concurrent callers share one run.
 */
function capturePerchanceKey(opts = {}) {
  if (inFlight) return inFlight
  const parent = opts.parentWindow || null

  const run = new Promise((resolve) => {
    let settled = false
    let keyWin = null
    let ses = null
    let timer = null
    let triggerTimer = null

    const finish = (key, error) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      if (triggerTimer) clearInterval(triggerTimer)
      try {
        if (ses) ses.webRequest.onBeforeRequest(null)
      } catch {}
      try {
        if (keyWin && !keyWin.isDestroyed()) keyWin.destroy()
      } catch {}
      resolve({ key: key || null, error: error || null })
    }

    try {
      keyWin = new BrowserWindow({
        show: false,
        width: 1120,
        height: 840,
        title: "Perchance verification",
        parent: parent && !parent.isDestroyed() ? parent : undefined,
        autoHideMenuBar: true,
        webPreferences: {
          partition: PERCHANCE_PARTITION,
          sandbox: true,
          contextIsolation: true,
          nodeIntegration: false,
          backgroundThrottling: false,
        },
      })
    } catch (exc) {
      return finish(null, "could not open window: " + String(exc.message || exc))
    }

    const wc = keyWin.webContents
    wc.setAudioMuted(true)
    wc.setUserAgent(PERCHANCE_UA)
    wc.setWindowOpenHandler(() => ({ action: "deny" }))

    ses = wc.session

    // Every frame's requests pass through here, including the nested generator
    // iframe that actually carries the key.
    ses.webRequest.onBeforeRequest({ urls: ["*://*.perchance.org/*"] }, (details, callback) => {
      callback({})
      if (settled) return
      const m = /[?&]userKey=([^&]+)/.exec(details.url)
      if (!m) return
      let key = ""
      try {
        key = decodeURIComponent(m[1])
      } catch {
        return
      }
      if (key.length === 64) finish(key)
    })

    timer = setTimeout(
      () => finish(null, "timed out waiting for userKey (" + PERCHANCE_TIMEOUT_MS + "ms)"),
      PERCHANCE_TIMEOUT_MS,
    )

    wc.on("did-fail-load", (_e, code, desc) => {
      // -3 is ERR_ABORTED, which fires on ordinary redirects — ignore it.
      if (code === -3) return
      if (!settled) console.error("[perchance] load failed:", code, desc)
    })

    const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

    // The generator is served from a per-generator subdomain frame, NOT the
    // top-level perchance.org page. Re-resolved on every use because a reload
    // replaces the frame object.
    const findGeneratorFrame = () => {
      let frames = []
      try {
        frames = wc.mainFrame.framesInSubtree
      } catch {
        return null
      }
      for (const f of frames) {
        if (f === wc.mainFrame) continue
        const u = f.url || ""
        if (u.includes("perchance.org") && u.includes("ai-text-to-image")) return f
      }
      return null
    }

    const flow = async () => {
      try {
        await wc.loadURL(PERCHANCE_URL)
      } catch {}
      if (settled) return

      let frame = null
      for (let i = 0; i < 40 && !settled; i++) {
        frame = findGeneratorFrame()
        if (frame) break
        await sleep(250)
      }
      if (settled) return
      if (!frame) return finish(null, "generator frame never appeared")
      console.log("[perchance] generator frame:", (frame.url || "").slice(0, 90))

      // Wipe any stored key, then reload so Perchance has to issue a new one.
      try {
        await frame.executeJavaScript('localStorage.clear(); "ok"', true)
      } catch {}
      if (settled) return
      try {
        await frame.executeJavaScript('location.reload(); "ok"', true)
      } catch {}
      await sleep(4000)
      if (settled) return

      // A Turnstile wall may still be up, so keep nudging the generator until
      // the key arrives or the outer timeout fires.
      const attempt = async () => {
        if (settled) return
        const f = findGeneratorFrame()
        if (!f) return
        try {
          const out = await f.executeJavaScript(PERCHANCE_TRIGGER, true)
          console.log("[perchance] trigger:", out)
        } catch (exc) {
          console.log("[perchance] trigger failed:", String(exc.message || exc).slice(0, 120))
        }
      }

      await attempt()
      triggerTimer = setInterval(attempt, 6000)
    }

    flow().catch((exc) => finish(null, String(exc.message || exc)))
  })

  inFlight = run
  run.finally(() => {
    if (inFlight === run) inFlight = null
  })
  return run
}

module.exports = { capturePerchanceKey }
