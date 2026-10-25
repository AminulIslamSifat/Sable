"use strict"

/**
 * search-engines.js — multi-engine web search driven by real Chromium tabs.
 *
 * Each engine gets its own hidden BrowserWindow, the page runs for real, and
 * results are read out of the live DOM. Engines are then fused with weighted
 * Reciprocal Rank Fusion.
 *
 * Measured engine reality (not assumed):
 *
 *   google      browser   JS shell. Result anchors are opaque /goto?url=CAES…
 *                         blobs that only resolve by real navigation (fetch is
 *                         CORS-blocked, /url?q= no longer emitted). Titles and
 *                         snippets are readable directly, so the top results are
 *                         resolved in parallel windows (~3s for the batch, not
 *                         per result). CAPTCHA-gated on some IPs → reported.
 *   duckduckgo  browser   html.duckduckgo.com serves results without the JS app.
 *                         Links are //duckduckgo.com/l/?uddg=<encoded> → unwrapped.
 *   bing        browser   li.b_algo blocks. Hrefs are /ck/a?…&u=a1<base64>
 *                         click-trackers → base64-decoded to the real URL.
 *                         &mkt/&setlang are mandatory: from a BD IP without
 *                         them Bing resolves the Bengali locale and returns a
 *                         near-empty SERP (measured 0 → 10 results). Under
 *                         rapid-request rate limiting Bing also serves wholly
 *                         unrelated "poisoned" results — see looksPoisoned.
 *   brave       browser   full results in the DOM.
 *   yandex      browser   CAPTCHA (/showcaptcha) on datacenter/VPN IPs.
 *
 *   score(d) = sum_e weight(e)/(K + rank_e(d)) * (1 + AGREEMENT*(engines-1))
 */

const { BrowserWindow } = require("electron")

const K = 12
const AGREEMENT = 0.35
const NAV_TIMEOUT_MS = 18_000
const RESULT_TIMEOUT_MS = 12_000
const POLL_MS = 300
const RESOLVE_LIMIT = 8
// Per-target redirect wait. The fast path is a single net.request whose 302
// carries the answer (~130ms for a batch), so 8s was two orders of magnitude
// too generous and let one slow /goto consume the whole engine bucket.
const RESOLVE_TIMEOUT_MS = 1_500
// Hard ceiling for the entire resolve phase. When it fires we keep the rows
// that DID resolve instead of dropping every one of them — see the map() at
// the end of resolveGotos.
const RESOLVE_PHASE_MS = 2_500
// Google injects the result container skeleton before the anchors, so a
// container-only match can precede every <h3>. This is the grace window used
// only when the extractor comes back empty; a healthy hit never pays it.
const SETTLE_MS = 1_500


const CHROME_UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

// Priority ladder: google > ddg > bing > brave > yandex.
const ENGINES = [
  {
    id: "google",
    weight: 1.0,
    kind: "browser",
    url: (q) => `https://www.google.com/search?q=${encodeURIComponent(q)}&num=20&hl=en&pws=0`,
    // Match the container, NOT an h3. Container-first is the fast path; the
    // settle loop in runBrowserEngine covers the gap where the skeleton lands
    // before the anchors. Requiring an h3 here deadlocks on queries where
    // Google serves a degenerate layout (see DEGENERATE_SERP) and no organic
    // anchor ever arrives.
    ready: "#search, #rso, div.MjjYud",
    extractor: "google",
    resolve: true,
    // Query params that do NOT fix the poisoned-profile layout, all measured:
    //   &gl=us / &gbv=1 / &filter=0 / &nfpr=1  -> still degenerate
    //   &udm=14                                -> "did not match any documents"
    // The profile, not the URL, was the variable.
    // MUST be an in-memory session. The old `persist:sable-search-google`
    // profile accumulated Google cookies (SNID, AEC, __Secure-ROLLOUT_TOKEN,
    // uid) and once that happened the account-less profile was pinned into an
    // experiment bucket serving an AI-Overview-only SERP: 1 h3, 1 .MjjYud,
    // ZERO organic results, permanently. Same query, same machine, same
    // second:
    //
    //   persist + 15 cookies -> 1 h3 after 9000ms (nothing)
    //   persist, cookies cleared -> 9 h3 in 4977ms
    //   in-memory session -> 9 h3 in 4513ms
    //
    // That is why the same search works in his real browser and not here:
    // different cookie jar, different bucket. Dropping the `persist:` prefix
    // makes the session in-memory, so it starts clean every run and cannot be
    // poisoned by anything it picks up. Verified 5/5 sequential (923ms-4.5s)
    // and 5 concurrent engines in 4.35s wall — under the 6s cap, no fallback.
    ephemeral: true,
  },
  {
    id: "duckduckgo",
    weight: 0.9,
    kind: "browser",
    url: (q) => `https://html.duckduckgo.com/html/?q=${encodeURIComponent(q)}`,
    ready: ".result, #links",
    extractor: "generic",
    unwrap: "ddg",
    selectors: [
      { container: ".result", link: "a.result__a", title: "a.result__a", snippet: ".result__snippet" },
    ],
  },
  {
    id: "bing",
    weight: 0.8,
    kind: "browser",
    // &mkt/&setlang are load-bearing, not cosmetic. Without them a BD IP
    // resolves to the Bengali locale, whose SERP comes back near-empty.
    url: (q) => `https://www.bing.com/search?q=${encodeURIComponent(q)}&mkt=en-US&setlang=en`,
    ready: "li.b_algo, #b_results",
    extractor: "generic",
    unwrap: "bing",
    selectors: [
      { container: "li.b_algo", link: "h2 a", title: "h2", snippet: ".b_caption p, .b_lineclamp2, .b_lineclamp3, .b_algoSlug" },
    ],
  },
  {
    id: "brave",
    weight: 0.7,
    kind: "browser",
    url: (q) => `https://search.brave.com/search?q=${encodeURIComponent(q)}&source=web`,
    ready: "#results, .snippet",
    extractor: "generic",
    selectors: [
      { container: "#results .snippet, .snippet[data-type=\"web\"]", link: 'a[href^="http"]', title: ".title, .snippet-title", snippet: ".snippet-description, .snippet-content" },
    ],
  },
  {
    id: "yandex",
    weight: 0.6,
    kind: "browser",
    // .com is NOT reachable from this network: TLS handshake is reset
    // (`SSL_connect: Connection reset by peer`), curl reports http=000, and
    // Electron lands on chrome-error://chromewebdata with an empty body. It
    // therefore burned the full 6s SOFT_DEADLINE_MS on every single search
    // and that was the 6.0s wall time. Measured alternates:
    //   yandex.ru      -> /showcaptcha redirect, 12s, 0 results
    //   yandex.com.tr  -> 13 results; cold 8-10s, warm 1.0s
    // .com.tr is the only one that answers, and it settles to ~1s once its
    // persistent partition has warmed up. The 6s cap still guards the cold
    // first search of a session.
    url: (q) => `https://yandex.com.tr/search/?text=${encodeURIComponent(q)}`,
    ready: ".serp-item, li.serp-item",
    extractor: "generic",
    selectors: [
      { container: ".serp-item, li.serp-item", link: 'a.OrganicTitle-Link, a[href^="http"]', title: ".OrganicTitleContentSpan, .OrganicTitle-Link", snippet: ".OrganicTextContentSpan" },
    ],
  },
]

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms))
}

/** Raised when an engine answers with a bot challenge instead of results. */
class GatedError extends Error {
  constructor(engine, where) {
    super(`${engine} served a bot challenge (${where})`)
    this.name = "GatedError"
    this.gated = true
  }
}

/**
 * The engine never answered at all — DNS failure, TLS reset, refused, timeout.
 * Chromium shows its OWN error page for this (`chrome-error://chromewebdata/`),
 * which is not a captcha and matches neither GATE_URL nor GATE_TEXT. The ready
 * poll therefore sat out the entire NAV_TIMEOUT_MS before the bucket race
 * reported the engine as merely slow, and that was the 6s wall time.
 *
 * Measured: yandex.com resets the TLS handshake from this network every time
 * (`SSL_connect: Connection reset by peer`, curl http=000). It must be dropped
 * on sight, not waited on.
 */
const BLOCKED_URL = /^(chrome-error:|about:neterror)/i
const BLOCKED_TEXT =
  /This site can.t be reached|took too long to respond|refused to connect|DNS_PROBE|net::ERR_|ERR_[A-Z]+|Check your internet connection|connection was reset|This page isn.t working/i

class BlockedError extends Error {
  constructor(engine, detail) {
    super(`${engine} unreachable (${detail})`)
    this.name = "BlockedError"
    this.blocked = true
    this.gated = false
  }
}

/**
 * Generic extractor. A real function (stringified over the wire) so its
 * regexes survive — a `\s` inside a template literal collapses to a bare `s`.
 */
function pageExtract(selectors, unwrapKind) {
  const out = []
  const seen = new Set()
  const clean = (v) => (v || "").replace(/\s+/g, " ").trim()

  const unwrap = (href) => {
    let h = href || ""
    if (unwrapKind === "ddg") {
      const m = h.match(/[?&]uddg=([^&]+)/)
      if (m) { try { h = decodeURIComponent(m[1]) } catch (e) {} }
    }
    // Bing wraps every organic href in /ck/a?…&u=a1<urlsafe-base64>. Strip the
    // a1 marker, pad to a 4-char boundary, and decode.
    if (unwrapKind === "bing") {
      const m = h.match(/[?&]u=a1([A-Za-z0-9_-]+)/)
      if (m) {
        try {
          let b = m[1].replace(/-/g, "+").replace(/_/g, "/")
          b += "=".repeat((4 - (b.length % 4)) % 4)
          h = atob(b)
        } catch (e) {}
      }
    }
    if (h.startsWith("//")) h = "https:" + h
    return h
  }

  for (const sel of selectors) {
    let nodes = []
    try { nodes = document.querySelectorAll(sel.container) } catch (e) { continue }
    if (!nodes.length) continue
    for (const n of nodes) {
      let a = null
      try { a = n.querySelector(sel.link) } catch (e) {}
      if (!a) continue
      const url = unwrap(a.getAttribute("href") || a.href || "")
      let t = a
      try { t = n.querySelector(sel.title) || a } catch (e) {}
      const title = clean(t.innerText)
      let s = null
      try { s = sel.snippet ? n.querySelector(sel.snippet) : null } catch (e) {}
      if (!title || !url || !/^https?:/i.test(url)) continue
      if (/duckduckgo\.com\/l\//i.test(url)) continue
      // A click-tracker we failed to decode is not a usable result.
      if (/bing\.com\/ck\/a/i.test(url)) continue
      if (seen.has(url)) continue
      seen.add(url)
      out.push({ title, url, snippet: clean(s ? s.innerText : "").slice(0, 320) })
    }
    if (out.length >= 5) break
  }

  return out.slice(0, 25)
}

/** Google: h3-anchored results whose hrefs still need resolving. */
function pageExtractGoogle() {
  const out = []
  const seen = new Set()
  const clean = (v) => (v || "").replace(/\s+/g, " ").trim()

  for (const h of document.querySelectorAll("h3")) {
    const a = h.closest("a")
    if (!a) continue
    const href = a.getAttribute("href") || a.href || ""
    if (!href) continue
    const title = clean(h.innerText)
    if (!title) continue
    const key = href
    if (seen.has(key)) continue
    seen.add(key)

    // Climb to a block with enough text to hold a snippet.
    let block = h
    for (let i = 0; i < 6 && block; i++) {
      block = block.parentElement
      if (block && block.innerText && block.innerText.length > title.length + 40) break
    }
    let snippet = ""
    if (block) snippet = clean(block.innerText).replace(title, "").trim().slice(0, 320)

    const absolute = href.startsWith("/") ? "https://www.google.com" + href : href
    const needsResolve = /\/goto\?|\/url\?/.test(href)
    out.push({ title, url: needsResolve ? "" : absolute, href: absolute, snippet, needsResolve })
  }

  return out.slice(0, 20)
}

function extractorSource(engine) {
  if (engine.extractor === "google") return `(${pageExtractGoogle.toString()})()`
  return `(${pageExtract.toString()})(${JSON.stringify(engine.selectors || [])}, ${JSON.stringify(engine.unwrap || null)})`
}

/** Detect a bot wall from URL and visible text. */
const GATE_URL = /\/sorry\/index|\/showcaptcha|\/checkcaptcha|captcha/i
const GATE_TEXT =
  /unusual traffic|captcha|not a robot|are you a robot|verify you are human|confirm you are human|showcaptcha|complete the challenge/i

/**
 * Resolve Google's opaque /goto links by navigating a small pool of hidden
 * windows in parallel. ~3s for the whole batch instead of per result.
 */
const GOOGLE_HOP = /(^|\.)google\.[a-z.]+\/(goto|url|sorry|search)/i

/**
 * Google answers some ambiguous / adult-adjacent phrasings with a degenerate
 * SERP: one AI Overview block plus "please clarify your request", and a single
 * h3 that is the AI Mode reply (href=""). No organic anchors at all. A normal
 * SERP carries 9-20 h3 and 20 div.MjjYud; this layout carries 1 and 1.
 *
 * It is bimodal — the same query returns real organics on another attempt —
 * so it must never be treated as "wait longer" or "engine is broken".
 */
const DEGENERATE_SERP = /clarify your request|AI Overview/i
const RESOLVE_CACHE = new Map()
const RESOLVE_CACHE_MAX = 800

/**
 * Turn Google's opaque /goto?url=CAES... hrefs into real destination URLs.
 *
 * Fast path uses net.request and resolves on the FIRST `redirect` event whose
 * destination is no longer a google.* hop, then aborts — the 302 carries the
 * answer, so downloading the destination page is pure waste. Measured ~130ms
 * for a batch of 8, versus ~1800ms for the window-per-result approach this
 * replaces.
 *
 * net.fetch is NOT usable here: it reports res.url === "" and redirected ===
 * false, so the final URL is simply not observable.
 *
 * The hidden-window navigation is kept as a correctness backstop for the rare
 * case where Google answers with a JS interstitial instead of a 3xx.
 */
async function resolveGotos(items, session) {
  const targets = items.filter((i) => i.needsResolve && i.href).slice(0, RESOLVE_LIMIT)
  if (!targets.length) return items

  const { net } = require("electron")
  const resolved = new Map()

  function viaRedirectEvent(href) {
    return new Promise((resolve) => {
      let settled = false
      let req = null
      const finish = (url) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        try { if (req) req.abort() } catch (_) {}
        resolve(url || "")
      }
      const timer = setTimeout(() => finish(""), RESOLVE_TIMEOUT_MS)

      try {
        req = net.request({
          url: href,
          redirect: "follow",
          session,
          useSessionCookies: true,
        })
      } catch (_) {
        return finish("")
      }

      req.setHeader("User-Agent", CHROME_UA)
      req.on("redirect", (statusCode, method, redirectUrl) => {
        if (redirectUrl && /^https?:/i.test(redirectUrl) && !GOOGLE_HOP.test(redirectUrl)) {
          finish(redirectUrl)
        }
      })
      req.on("response", (res) => {
        // No redirect at all — the destination is the response itself.
        if (res.url && /^https?:/i.test(res.url) && !GOOGLE_HOP.test(res.url)) {
          finish(res.url)
          return
        }
        res.on("data", () => {})
        res.on("end", () => finish(""))
      })
      req.on("error", () => finish(""))
      try { req.end() } catch (_) { finish("") }
    })
  }

  async function viaWindow(href) {
    let win = null
    try {
      win = new BrowserWindow({
        show: false,
        width: 1100,
        height: 800,
        webPreferences: {
          partition: "persist:sable-search-resolve",
          sandbox: true,
          contextIsolation: true,
          nodeIntegration: false,
          backgroundThrottling: false,
          images: false,
        },
      })
      const wc = win.webContents
      wc.setAudioMuted(true)
      wc.setUserAgent(CHROME_UA)
      wc.setWindowOpenHandler(() => ({ action: "deny" }))
      wc.loadURL(href).catch(() => {})

      const deadline = Date.now() + RESOLVE_TIMEOUT_MS
      while (Date.now() < deadline) {
        const now = wc.getURL()
        if (now && /^https?:/i.test(now) && !GOOGLE_HOP.test(now)) return now
        await sleep(60)
      }
    } catch (_) {
    } finally {
      if (win && !win.isDestroyed()) { try { win.destroy() } catch (_) {} }
    }
    return ""
  }

  // Bound the whole phase, not just each target. Without this a single slow
  // /goto could hold the batch past the engine's soft deadline, the race would
  // resolve with an empty google bucket, and the caller would be told google
  // had no results — discarding 8 rows that were already sitting in `items`.
  await Promise.race([
    Promise.allSettled(
      targets.map(async (t) => {
        const cached = RESOLVE_CACHE.get(t.href)
        if (cached) { resolved.set(t.href, cached); return }
        let real = await viaRedirectEvent(t.href)
        if (!real) real = await viaWindow(t.href)
        if (!real) return
        resolved.set(t.href, real)
        if (RESOLVE_CACHE.size >= RESOLVE_CACHE_MAX) RESOLVE_CACHE.clear()
        RESOLVE_CACHE.set(t.href, real)
      })
    ),
    sleep(RESOLVE_PHASE_MS),
  ])

  // Keep the rows that resolved, and keep the unresolved ones too: `href` is
  // already an absolute google.com/goto URL, which is a real, followable link.
  // Degrading to it is strictly better than reporting zero results for an
  // engine that clearly answered.
  return items
    .map((i) => {
      if (!i.needsResolve) return i
      const real = resolved.get(i.href)
      if (real) return { ...i, url: real }
      return i.href ? { ...i, url: i.href } : null
    })
    .filter(Boolean)
}

/** One browser engine, one hidden window. Resolves to a bucket, never rejects. */
const STOPWORDS = new Set([
  "the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "with", "how", "what",
  "is", "are", "best", "vs", "my", "your", "from", "that", "this", "you", "can", "not",
])

/**
 * Detect an anti-bot poisoned SERP: Bing occasionally answers a query with
 * completely unrelated results (a "hyprland keybind" search returning pet-food
 * and travel sites). Not a layout quirk — it is deliberate, and fusing those
 * rows into the answer set poisons the whole response. The test is deliberately
 * blunt: if NOT ONE result shares a meaningful term with the query, reject the
 * bucket. A genuine zero-overlap answer is vanishingly rare, and a false
 * rejection only costs one engine's contribution instead of corrupting all of
 * them.
 */
function looksPoisoned(query, rows) {
  if (!rows || !rows.length) return false
  const terms = String(query || "")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 2 && !STOPWORDS.has(t))
  if (!terms.length) return false

  for (const r of rows) {
    const hay = `${r.title || ""} ${r.snippet || ""}`.toLowerCase()
    if (terms.some((t) => hay.includes(t))) return false
  }
  return true
}

async function runBrowserEngine(engine, query) {
  const started = Date.now()
  const bucket = { id: engine.id, weight: engine.weight, results: [], error: null, gated: false, ms: 0 }
  let win = null

  try {
    win = new BrowserWindow({
      show: false,
      width: 1280,
      height: 900,
      webPreferences: {
        // An engine marked ephemeral gets a non-persistent session (no
        // `persist:` prefix), so it starts with a clean cookie jar every run
        // and cannot get pinned into a server-side experiment bucket. See the
        // comment on the google engine for the measurement that proved this.
        ...(engine.ephemeral
          ? { partition: `sable-search-${engine.id}` }
          : { partition: `persist:sable-search-${engine.id}` }),
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        backgroundThrottling: false,
      },
    })

    const wc = win.webContents
    wc.setAudioMuted(true)
    wc.setUserAgent(CHROME_UA)
    wc.setWindowOpenHandler(() => ({ action: "deny" }))

    // Fire the navigation but do not await the full load event: the ready
    // poll below only needs the results container, which is in the DOM long
    // before subresources finish. Awaiting loadURL serialised the two and
    // cost ~1-2s per search.
    // Fail fast on a transport failure. Without this a blocked host shows
    // Chromium's own error page and the poll below has nothing to match, so
    // it waits out NAV_TIMEOUT_MS for a page that will never arrive.
    const navFailure = { reason: null }
    wc.once("did-fail-load", (_e, errorCode, errorDesc, validatedURL, isMainFrame) => {
      if (isMainFrame === false) return
      navFailure.reason = `${errorDesc || "load failed"} (${errorCode}) @ ${validatedURL || "?"}`
    })

    wc.loadURL(engine.url(query)).catch(() => {})

    const landed = wc.getURL()
    if (GATE_URL.test(landed)) throw new GatedError(engine.id, new URL(landed).pathname)

    const deadline = Date.now() + NAV_TIMEOUT_MS
    let ready = false
    while (Date.now() < deadline) {
      try {
        // Bail the moment Chromium reports a transport failure rather than
        // polling a dead tab until the deadline.
        if (navFailure.reason) throw new BlockedError(engine.id, navFailure.reason)

        const probe = await wc.executeJavaScript(
          `({ready: !!document.querySelector(${JSON.stringify(engine.ready)}),` +
            ` body: (document.body ? document.body.innerText.slice(0, 400) : ""),` +
            ` url: location.href})`,
          true
        )
        if (GATE_URL.test(probe.url) || GATE_TEXT.test(probe.body)) {
          throw new GatedError(engine.id, new URL(probe.url).pathname)
        }
        // Chromium's error page: host unreachable, TLS reset, DNS failure.
        if (BLOCKED_URL.test(probe.url) || BLOCKED_TEXT.test(probe.body)) {
          throw new BlockedError(engine.id, probe.url || "error page")
        }
        if (probe.ready) { ready = true; break }
      } catch (exc) {
        if (exc instanceof GatedError || exc instanceof BlockedError) throw exc
        ready = false
      }
      await sleep(100)
    }

    await sleep(50)

    let rows = await wc.executeJavaScript(extractorSource(engine), true)
    if (!Array.isArray(rows)) rows = []

    // Settle: if the ready probe matched a container that hydrated ahead of
    // the anchors, the extractor comes back empty. Take rows as soon as the
    // count stops growing, so a healthy hit exits on the first iteration.
    const settleEnd = Date.now() + SETTLE_MS
    let prevCount = -1
    while (!rows.length && Date.now() < settleEnd) {
      // A transport failure can arrive after the ready probe matched (the
      // skeleton can render from cache before the network error lands).
      if (navFailure.reason) throw new BlockedError(engine.id, navFailure.reason)
      await sleep(150)
      rows = await wc.executeJavaScript(extractorSource(engine), true)
      if (!Array.isArray(rows)) rows = []
      if (rows.length && rows.length === prevCount) break
      prevCount = rows.length
    }

    // Final gate check, run after the settle so it sees the real page (a
    // challenge page can carry decoy #search/#rso nodes).
    const final = await wc.executeJavaScript(
      `({url: location.href, body: (document.body ? document.body.innerText.slice(0, 600) : "")})`,
      true
    )
    if (GATE_URL.test(final.url) || GATE_TEXT.test(final.body)) {
      throw new GatedError(engine.id, new URL(final.url).pathname)
    }

    // Google hands back opaque blobs; turn them into real URLs.
    if (engine.resolve && rows.length) {
      rows = await resolveGotos(rows, wc.session)
      rows = rows.filter((r) => r.url && /^https?:/i.test(r.url))
    }

    if (rows.length && looksPoisoned(query, rows)) {
      bucket.poisoned = true
      rows = []
    }

    if (rows.length) {
      bucket.results = rows
    } else {
      let where = ""
      try { where = ` @ ${new URL(final.url).host}${new URL(final.url).pathname}` } catch (_) {}
      const sniff = (final.body || "").replace(/\s+/g, " ").trim().slice(0, 120)

      // Distinguish "we looked too early" from "Google chose not to give us
      // organics". The second is a real, correct answer with zero results and
      // must not strike the engine's health — it would disable google for ten
      // minutes over a layout choice it is entitled to make.
      const degenerate = !!engine.resolve && DEGENERATE_SERP.test(final.body || "")
      if (degenerate) bucket.degenerate = true

      bucket.error =
        (bucket.poisoned
          ? "results unrelated to query (anti-bot poisoned SERP)"
          : degenerate
          ? "no organic results (degenerate SERP: AI Overview only)"
          : ready
            ? "no results parsed"
            : "results container never appeared") +
        where +
        (sniff ? ` — "${sniff}"` : "")
    }
  } catch (exc) {
    const msg = String(exc.message || exc)
    // A captcha redirect can surface as a navigation abort, not as a landed
    // page. Classify it as gated so callers can tell "blocked" from "broken".
    const looksGated = GATE_URL.test(msg) || GATE_TEXT.test(msg)
    if (exc instanceof BlockedError || exc.blocked) {
      // Unreachable host. Not gated — nothing was served at all — but still a
      // strike against health, so the next three searches skip this engine
      // instead of paying even the fast-fail cost on every request.
      bucket.blocked = true
      bucket.error = exc.message
    } else if (exc instanceof GatedError || exc.gated || looksGated) {
      bucket.gated = true
      bucket.error = looksGated && !exc.gated ? `${engine.id} served a bot challenge (redirect)` : exc.message
    } else {
      bucket.error = `${exc.name || "Error"}: ${msg.slice(0, 160)}`
    }
  } finally {
    bucket.ms = Date.now() - started
    if (win && !win.isDestroyed()) { try { win.destroy() } catch (_) {} }
  }

  return bucket
}

/** Bing over its RSS feed — HTML search refuses headless clients. */
async function runRssEngine(engine, query) {
  const started = Date.now()
  const bucket = { id: engine.id, weight: engine.weight, results: [], error: null, gated: false, ms: 0 }

  try {
    const res = await fetch(engine.rssUrl(query), {
      headers: { "User-Agent": CHROME_UA, Accept: "application/rss+xml, application/xml, text/xml" },
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const xml = await res.text()

    const items = xml.match(/<item[\s>][\s\S]*?<\/item>/gi) || []
    const decode = (s) =>
      (s || "")
        .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&amp;/g, "&")
        .replace(/<[^>]+>/g, "")
        .replace(/\s+/g, " ")
        .trim()

    // Slice between tags by hand: a `new RegExp` from a template literal loses
    // its backslashes ([\s\S] arrives as [sS]) and silently matches nothing.
    const field = (item, tag) => {
      const open = item.indexOf(`<${tag}`)
      if (open < 0) return ""
      const start = item.indexOf(">", open)
      if (start < 0) return ""
      const end = item.indexOf(`</${tag}>`, start)
      if (end < 0) return ""
      return decode(item.slice(start + 1, end))
    }

    for (const item of items) {
      const title = field(item, "title")
      const url = field(item, "link")
      const snippet = field(item, "description")
      if (!title || !url || !/^https?:/i.test(url)) continue
      bucket.results.push({ title, url, snippet: snippet.slice(0, 320) })
    }

    if (!bucket.results.length) bucket.error = "RSS feed returned no items"
  } catch (exc) {
    bucket.error = `${exc.name || "Error"}: ${String(exc.message || exc).slice(0, 160)}`
  } finally {
    bucket.ms = Date.now() - started
  }

  return bucket
}

function runEngine(engine, query) {
  return engine.kind === "rss" ? runRssEngine(engine, query) : runBrowserEngine(engine, query)
}

function normalizeUrl(u) {
  try {
    const url = new URL(u)
    const drop = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "ref", "ref_src", "rut"]
    drop.forEach((k) => url.searchParams.delete(k))
    url.hash = ""
    let s = url.toString()
    if (s.endsWith("/")) s = s.slice(0, -1)
    return s.toLowerCase()
  } catch (_) {
    return String(u || "").toLowerCase()
  }
}

/** Weighted RRF with an agreement bonus. Gated/empty engines contribute nothing. */
function fuse(buckets) {
  const scores = new Map()
  const meta = new Map()

  for (const b of buckets) {
    if (!b.results.length) continue
    b.results.forEach((r, i) => {
      if (!r.url || !/^https?:/i.test(r.url)) return
      const key = normalizeUrl(r.url)
      scores.set(key, (scores.get(key) || 0) + b.weight / (K + i + 1))
      const prev = meta.get(key)
      if (prev) {
        prev.engines.add(b.id)
        if ((r.snippet || "").length > (prev.snippet || "").length) prev.snippet = r.snippet
      } else {
        meta.set(key, { title: r.title, url: r.url, snippet: r.snippet || "", engines: new Set([b.id]) })
      }
    })
  }

  const out = []
  for (const [key, score] of scores) {
    const m = meta.get(key)
    if (!m) continue
    const n = m.engines.size
    out.push({
      title: m.title,
      url: m.url,
      snippet: m.snippet,
      engines: [...m.engines],
      score: Number((score * (1 + AGREEMENT * (n - 1))).toFixed(6)),
    })
  }

  out.sort((a, b) => b.score - a.score)
  return out
}

/**
 * Search every engine in parallel and return the fused ranking.
 *
 * @param {string} query
 * @param {{count?: number, engines?: string[]}} opts
 */
// ── Engine health ───────────────────────────────────────────────────────────
// A captcha-gated engine returns nothing but still costs a full window load,
// and because every search is Promise.all'd its latency becomes the request's
// latency. Track consecutive empty/gated results and sidestep an engine for a
// while once it is clearly not answering. It comes back on its own.

const ENGINE_HEALTH = new Map()
const SKIP_AFTER_STRIKES = 3
const SKIP_FOR_MS = 10 * 60 * 1000
const SOFT_DEADLINE_MS = 6000

// Per-engine cap override. Empty: every engine shares SOFT_DEADLINE_MS.
//
// Google needs no extra headroom once its session is ephemeral — measured
// 923ms-4.5s sequential and 4.35s wall for 5 concurrent engines, all under
// the 6s cap with no fallback path. An earlier 9s override existed only to
// cover a second frontend retry that turned out to be treating a poisoned
// cookie jar rather than a regional index difference.
const ENGINE_BUDGET_MS = {}

function budgetFor(engine) {
  return ENGINE_BUDGET_MS[engine.id] || SOFT_DEADLINE_MS
}

function isEngineSkipped(id) {
  const h = ENGINE_HEALTH.get(id)
  return !!(h && h.until && h.until > Date.now())
}

// How many consecutive soft-deadline hits before we stop waiting on an engine.
const SLOW_STRIKES = 3

function noteEngineHealth(id, bucket) {
  const h = ENGINE_HEALTH.get(id) || { strikes: 0, slow: 0, until: 0 }

  // Only a definitive "this engine is not answering for us" may strike it out.
  // An empty bucket alone is NOT enough: these engines are bimodal, not dead.
  // Measured warm: yandex-image returns 40 results in 1.1s and google-image
  // alternates between a captcha and 26 results. Striking on "empty" would
  // quietly disable a working engine for ten minutes.
  //
  //   results > 0            -> healthy, reset
  //   gated                  -> real block, strike
  //   empty + soft deadline  -> OUR OWN cap fired, never strike
  //   empty + real error     -> engine is failing (e.g. bing RSS), strike
  const softCapped = /soft deadline/i.test(bucket.error || "")
  const healthy = bucket.results.length > 0

  if (healthy) {
    h.strikes = 0
    h.slow = 0
    h.until = 0
  } else if (softCapped) {
    // Not broken — just slower than we are willing to wait. Measured: yandex
    // web returns 0 results and needs >6s, so it caps every single request.
    // Three of those in a row means it is not worth waiting for, which is a
    // different judgement from "this engine is failing".
    h.slow += 1
    if (h.slow >= SLOW_STRIKES) {
      h.until = Date.now() + SKIP_FOR_MS
      h.slow = 0
    }
  } else if (bucket.degenerate) {
    // Google answered; it simply had no organics for this phrasing. Healthy.
    h.strikes = 0
    h.slow = 0
  } else if (bucket.blocked) {
    // Host unreachable. Strike it like a gate: after SKIP_AFTER_STRIKES the
    // engine is skipped outright, so a dead host costs nothing at all rather
    // than a fast-fail round trip on every search.
    h.strikes += 1
    if (h.strikes >= SKIP_AFTER_STRIKES) {
      h.until = Date.now() + SKIP_FOR_MS
      h.strikes = 0
    }
  } else if (bucket.gated || bucket.error) {
    h.strikes += 1
    if (h.strikes >= SKIP_AFTER_STRIKES) {
      h.until = Date.now() + SKIP_FOR_MS
      h.strikes = 0
    }
  }
  ENGINE_HEALTH.set(id, h)
}

async function runEngineTracked(runner, engine, query) {
  const bucket = await runner(engine, query)
  noteEngineHealth(engine.id, bucket)
  return bucket
}

/** Race a bucket against a soft deadline so one straggler cannot define the request. */
function withSoftDeadline(promise, engine, budgetMs) {
  return Promise.race([
    promise,
    sleep(budgetMs).then(() => ({
      id: engine.id,
      weight: engine.weight,
      results: [],
      error: `soft deadline ${budgetMs}ms`,
      gated: false,
      ms: budgetMs,
    })),
  ])
}

async function multiSearch(query, opts = {}) {
  const q = String(query || "").trim()
  if (!q) return { query: "", results: [], engines: [] }

  const wanted =
    Array.isArray(opts.engines) && opts.engines.length
      ? ENGINES.filter((e) => opts.engines.includes(e.id))
      : ENGINES

  const live = wanted.filter((e) => !isEngineSkipped(e.id))
  const active = live.length ? live : wanted
  const buckets = await Promise.all(
    active.map((e) =>
      withSoftDeadline(runEngineTracked(runEngine, e, q), e, budgetFor(e))
    )
  )
  const fused = fuse(buckets)
  const count = Math.max(1, Math.min(Number(opts.count) || 10, 50))

  return {
    query: q,
    results: fused.slice(0, count).map((r) => ({
      title: r.title,
      url: r.url,
      snippet: r.snippet,
      engine: r.engines,
      score: r.score,
    })),
    engines: buckets.map((b) => ({
      id: b.id,
      ok: b.results.length > 0,
      gated: b.gated,
      count: b.results.length,
      ms: b.ms,
      error: b.error,
    })),
  }
}

// ── Image search ────────────────────────────────────────────────────────────
// Same fused-ranking idea, different unit: an "image result" is the media URL,
// not the page. Google Images is weighted double (2.0) — it is the widest image
// index and the caller asked for it to lead.

const IMAGE_ENGINES = [
  {
    id: "google-image",
    weight: 2.0,
    url: (q) => `https://www.google.com/search?q=${encodeURIComponent(q)}&tbm=isch&hl=en&safe=off`,
    // Do not include a bare "img" here. The page has dozens of icons and
    // logos that satisfy that selector instantly, so the readiness poll
    // exits before the actual result grid has rendered.
    ready: "div.H8Rx8c, div.isv-r, [data-ri]",
    extractor: "google",
  },
  {
    id: "duckduckgo-image",
    weight: 0.9,
    url: (q) => `https://duckduckgo.com/?q=${encodeURIComponent(q)}&iax=images&ia=images&kp=-2`,
    ready: ".tile--img, [data-testid=\"result\"]",
    extractor: "generic",
  },
  {
    id: "bing-image",
    weight: 0.8,
    url: (q) => `https://www.bing.com/images/search?q=${encodeURIComponent(q)}&form=HDRSC2&adlt=off&safeSearch=off`,
    ready: "a.iusc, .mimg",
    extractor: "bing",
  },
  {
    id: "brave-image",
    weight: 0.7,
    url: (q) => `https://search.brave.com/images?q=${encodeURIComponent(q)}&safesearch=off`,
    ready: "img, .image",
    extractor: "generic",
  },
  {
    id: "yandex-image",
    weight: 0.6,
    url: (q) => `https://yandex.com/images/search?text=${encodeURIComponent(q)}&family=0`,
    ready: ".serp-item, img",
    extractor: "generic",
  },
]

/**
 * Runs INSIDE the page. Real function, stringified — see pageExtract for why.
 * Bing is the only engine that hands back full-resolution media URLs directly
 * (a.iusc[data-m] carries {murl, turl, t, purl}); the rest only expose
 * thumbnails, so `image` and `thumbnail` are the same URL there.
 */
function pageExtractImages(spec) {
  const out = []
  const seen = new Set()
  const clean = (v) => (v || "").replace(/\s+/g, " ").trim()
  const abs = (u) => {
    u = String(u || "").trim()
    if (!u) return ""
    if (u.startsWith("//")) return "https:" + u
    return u
  }
  // Reject the 40–90px grid furniture that image engines interleave with real
  // results; a thumbnail that small is never the media the caller wants.
  const tooSmall = (u) => {
    const m = u.match(/[?&](?:w|width)=(\d+)/i) || u.match(/[?&](?:h|height)=(\d+)/i)
    return !!(m && Number(m[1]) < 150)
  }

  const push = (title, image, thumb, page) => {
    image = abs(image)
    thumb = abs(thumb) || image
    page = abs(page)

    // Bing hides the original full-resolution asset inside its detail-page URL
    // as ?mediaurl=<encoded>. Prefer it over the resized th.bing.com thumbnail —
    // that is the difference between a 302px crop and the real image.
    if (page) {
      const mm = page.match(/[?&]mediaurl=([^&]+)/i)
      if (mm) {
        try {
          const full = decodeURIComponent(mm[1])
          if (/^https?:/i.test(full)) image = full
        } catch (e) {}
      }
    }

    // Brave serves through a resizer and appends the original URL base64-encoded
    // after /g:ce/. Decode it so callers get the source asset, not a crop.
    const bm = image.match(/\/g:ce\/([A-Za-z0-9_-]+)$/)
    if (bm) {
      try {
        let b64 = bm[1].replace(/-/g, "+").replace(/_/g, "/")
        while (b64.length % 4) b64 += "="
        const decoded = atob(b64)
        if (/^https?:/i.test(decoded)) image = decoded
      } catch (e) {}
    }

    if (!image || !/^https?:/i.test(image)) return
    if (/\.(svg)(\?|$)/i.test(image)) return
    if (tooSmall(image) && tooSmall(thumb)) return
    // Google and DDG proxy every image through one host and put the unique
    // asset id in the query string. Splitting on "?" collapsed all of them
    // into a single key, so only the first result survived deduplication.
    const key = image
    if (seen.has(key)) return
    seen.add(key)
    out.push({ title: clean(title).slice(0, 200), image, thumbnail: thumb, url: page })
  }

  if (spec === "bing") {
    for (const a of document.querySelectorAll("a.iusc[data-m]")) {
      let m = null
      try { m = JSON.parse(a.getAttribute("data-m") || "{}") } catch (e) { continue }
      if (!m || !m.murl) continue
      push(m.t || m.desc || "", m.murl, m.turl || m.murl, m.purl || "")
    }
    if (out.length) return out.slice(0, 40)
  }

  if (spec === "google") {
    // Google Images only ever exposes a thumbnail (encrypted-tbn*.gstatic.com)
    // in the DOM — the full-res asset needs a click per image. The result
    // anchor sometimes carries the real media URL in an imgres?imgurl= param;
    // prefer that when present, else keep the thumbnail and say so.
    for (const box of document.querySelectorAll("div.H8Rx8c, div.isv-r, [data-ri]")) {
      const img = box.querySelector("img")
      if (!img) continue
      const a = box.closest("a") || (box.parentElement ? box.parentElement.querySelector("a") : null)
      const thumb = img.getAttribute("src") || ""
      let full = img.getAttribute("data-iurl") || img.getAttribute("data-src") || ""
      if (!full && a) {
        const href = a.getAttribute("href") || ""
        const m = href.match(/[?&]imgurl=([^&]+)/)
        if (m) { try { full = decodeURIComponent(m[1]) } catch (e) {} }
      }
      push(img.getAttribute("alt"), full || thumb, thumb, a ? a.href : "")
    }
    if (out.length) return out.slice(0, 40)
  }

  // Generic: any image big enough to be a real result, with its link and alt.
  for (const img of document.querySelectorAll("img")) {
    const src = img.getAttribute("data-src") || img.getAttribute("data-original") || img.getAttribute("src") || ""
    if (!src) continue
    const w = img.naturalWidth || img.width || 0
    if (w && w < 40) continue
    const a = img.closest("a")
    push(img.getAttribute("alt") || img.getAttribute("title"), src, src, a ? a.href : "")
  }
  return out.slice(0, 40)
}

function imageExtractorSource(engine) {
  return `(${pageExtractImages.toString()})(${JSON.stringify(engine.extractor || "generic")})`
}

/** One image engine, one hidden window. Resolves to a bucket, never rejects. */
async function runImageEngine(engine, query) {
  const started = Date.now()
  const bucket = { id: engine.id, weight: engine.weight, results: [], error: null, gated: false, ms: 0 }
  let win = null

  try {
    win = new BrowserWindow({
      show: false,
      width: 1400,
      height: 1000,
      webPreferences: {
        partition: `persist:sable-image-${engine.id}`,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        backgroundThrottling: false,
      },
    })

    const wc = win.webContents
    wc.setAudioMuted(true)
    wc.setUserAgent(CHROME_UA)
    wc.setWindowOpenHandler(() => ({ action: "deny" }))

    await Promise.race([
      wc.loadURL(engine.url(query)),
      sleep(NAV_TIMEOUT_MS).then(() => {
        throw new Error(`navigation timed out after ${NAV_TIMEOUT_MS}ms`)
      }),
    ])

    const landed = wc.getURL()
    if (GATE_URL.test(landed)) throw new GatedError(engine.id, new URL(landed).pathname)

    // Image grids fill in lazily, so poll until enough tiles exist.
    const deadline = Date.now() + RESULT_TIMEOUT_MS
    let count = 0
    while (Date.now() < deadline) {
      try {
        // The per-engine `ready` class names rot constantly (Google's
        // H8Rx8c/isv-r are long gone). Counting real, large <img> nodes is
        // stable across all five image engines and cannot be satisfied by
        // favicons or sprite furniture.
        const probe = await wc.executeJavaScript(
          `(() => {
            const sel = document.querySelectorAll(${JSON.stringify(engine.ready)}).length
            let big = 0
            for (const im of document.images) {
              const s = im.currentSrc || im.src || ""
              if (/^https?:/i.test(s) && (im.naturalWidth || 0) >= 120) big++
            }
            return {n: sel, big, body: (document.body ? document.body.innerText.slice(0, 300) : ""), url: location.href}
          })()`,
          true
        )
        if (GATE_URL.test(probe.url) || GATE_TEXT.test(probe.body)) {
          throw new GatedError(engine.id, new URL(probe.url).pathname)
        }
        count = Math.max(probe.n || 0, probe.big || 0)
        if (count >= 5) break
      } catch (exc) {
        if (exc instanceof GatedError) throw exc
      }
      await sleep(POLL_MS)
    }

    await sleep(250)
    let rows = await wc.executeJavaScript(imageExtractorSource(engine), true)
    if (!Array.isArray(rows)) rows = []

    if (rows.length) bucket.results = rows
    else bucket.error = count ? "tiles present but no usable image URLs" : "no image tiles appeared"
  } catch (exc) {
    const msg = String(exc.message || exc)
    if (exc instanceof GatedError || exc.gated || GATE_URL.test(msg) || GATE_TEXT.test(msg)) {
      bucket.gated = true
      bucket.error = `${engine.id} served a bot challenge`
    } else {
      bucket.error = `${exc.name || "Error"}: ${msg.slice(0, 160)}`
    }
  } finally {
    bucket.ms = Date.now() - started
    if (win && !win.isDestroyed()) { try { win.destroy() } catch (_) {} }
  }

  return bucket
}

/**
 * Weighted RRF over image media URLs.
 *
 * Image engines pad their grids aggressively — Yandex in particular returns
 * tag-cloud art unrelated to the query. A result whose title and page URL
 * share no term with the query is dropped rather than fused, otherwise noise
 * from a weak engine outranks real hits from a strong one.
 */
function fuseImages(buckets, query) {
  const scores = new Map()
  const meta = new Map()

  const terms = String(query || "")
    .toLowerCase()
    .split(/\s+/)
    .filter((t) => t.length > 2)

  const relevant = (r) => {
    if (!terms.length) return true
    const hay = `${r.title || ""} ${r.url || ""}`.toLowerCase()
    return terms.some((t) => hay.includes(t))
  }

  for (const b of buckets) {
    if (!b.results.length) continue
    b.results.forEach((r, i) => {
      if (!r.image || !/^https?:/i.test(r.image)) return
      if (!relevant(r)) return
      const key = r.image.split("?")[0].toLowerCase()
      scores.set(key, (scores.get(key) || 0) + b.weight / (K + i + 1))
      const prev = meta.get(key)
      if (prev) {
        prev.engines.add(b.id)
        if (!prev.title && r.title) prev.title = r.title
      } else {
        meta.set(key, {
          title: r.title || "",
          image: r.image,
          thumbnail: r.thumbnail || r.image,
          url: r.url || "",
          engines: new Set([b.id]),
        })
      }
    })
  }

  const out = []
  for (const [key, score] of scores) {
    const m = meta.get(key)
    if (!m) continue
    const n = m.engines.size
    out.push({
      title: m.title,
      image: m.image,
      thumbnail: m.thumbnail,
      url: m.url,
      engines: [...m.engines],
      score: Number((score * (1 + AGREEMENT * (n - 1))).toFixed(6)),
    })
  }

  out.sort((a, b) => b.score - a.score)
  return out
}

/**
 * Confirm a URL actually serves an image.
 *
 * Image indexes routinely return hotlink-blocked hosts (403) and URLs that
 * decay within days. Returning those unchecked means the caller renders broken
 * images, so every candidate is probed with a 1-byte ranged GET — cheap, and it
 * proves the bytes are an image rather than an HTML error page.
 */
async function probeImage(url) {
  if (!url || !/^https?:/i.test(url)) return false
  try {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 2500)
    const res = await fetch(url, {
      method: "GET",
      headers: { "User-Agent": CHROME_UA, Accept: "image/*,*/*", Range: "bytes=0-1024" },
      redirect: "follow",
      signal: ctrl.signal,
    })
    clearTimeout(timer)
    if (!res.ok && res.status !== 206) return false
    const type = res.headers.get("content-type") || ""
    try { await res.body?.cancel() } catch (_) {}
    return type.startsWith("image/")
  } catch (_) {
    return false
  }
}

/**
 * Drop or repair results whose media URL does not resolve.
 *
 * Order of preference: keep the full-res URL if it works; otherwise substitute
 * the thumbnail (usually a CDN proxy that is not hotlink-blocked); otherwise
 * drop the result entirely rather than hand back a broken link.
 */
async function verifyImages(results, limit) {
  const batch = results.slice(0, limit)
  const rest = results.slice(limit)

  const checked = await Promise.all(
    batch.map(async (r) => {
      if (await probeImage(r.image)) return r
      if (r.thumbnail && r.thumbnail !== r.image && (await probeImage(r.thumbnail))) {
        return { ...r, image: r.thumbnail }
      }
      return null
    })
  )

  // Anything past the probe limit is returned unverified — it is a fallback
  // tail, and probing every long-tail URL would dominate the request time.
  return [...checked.filter(Boolean), ...rest]
}

/**
 * Image search across engines, fused.
 *
 * @param {string} query
 * @param {{count?: number, engines?: string[]}} opts
 * @returns {Promise<{query: string, results: object[], engines: object[]}>}
 */
async function imageSearch(query, opts = {}) {
  const q = String(query || "").trim()
  if (!q) return { query: "", results: [], engines: [] }

  const wanted =
    Array.isArray(opts.engines) && opts.engines.length
      ? IMAGE_ENGINES.filter((e) => opts.engines.includes(e.id))
      : IMAGE_ENGINES

  const live = wanted.filter((e) => !isEngineSkipped(e.id))
  const active = live.length ? live : wanted
  const buckets = await Promise.all(
    active.map((e) =>
      withSoftDeadline(runEngineTracked(runImageEngine, e, q), e, budgetFor(e))
    )
  )
  const fused = fuseImages(buckets, q)
  const count = Math.max(1, Math.min(Number(opts.count) || 10, 40))

  // Probe enough candidates to still fill `count` after hotlink-blocked hosts
  // are dropped or downgraded to their thumbnail.
  const verified = opts.verify === false
    ? fused.slice(0, count)
    : await verifyImages(fused, Math.min(fused.length, count * 2))

  return {
    query: q,
    results: verified.slice(0, count).map((r) => ({
      title: r.title,
      image: r.image,
      thumbnail: r.thumbnail,
      url: r.url,
      engine: r.engines,
      score: r.score,
    })),
    engines: buckets.map((b) => ({
      id: b.id,
      ok: b.results.length > 0,
      gated: b.gated,
      count: b.results.length,
      ms: b.ms,
      error: b.error,
    })),
  }
}

module.exports = { multiSearch, imageSearch, ENGINES, IMAGE_ENGINES }
