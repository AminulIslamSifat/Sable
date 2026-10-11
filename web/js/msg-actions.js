/* =========================================================================
   msg-actions.js — single source of truth for message toolbars.

   There used to be FOUR separate toolbar builders:
     1. chat.js  addMessage()          — user, live
     2. sse.js   history replay        — user, from DB
     3. sse.js   _attachBotToolbar()   — bot, history
     4. sse.js   stream-end forEach    — bot, live (+ regen + speed badge)

   Each carried its OWN copy of the copy-button and the ~50-line TTS click
   handler, all poking the same globals (_ttsActive, _activeTTS, …) defined
   in tts.js. And each appended the toolbar at a different moment — which is
   why the toolbar kept ending up stranded mid-message whenever a rich UI
   card mounted after it.

   CSS pins .msg-toolbar with `order:9999` so DOM position no longer matters
   for layout. This module removes the duplication so there is exactly one
   handler to fix when something breaks.

   Load order: after tts.js (needs _ttsActive / stopGlobalTTS / TTSStreamPlayer),
   before chat.js and sse.js (both call into it).
   ========================================================================= */
(function () {
  "use strict";

  // ── helpers ────────────────────────────────────────────────────────────

  /** Pull the copyable/speakable text out of a message element. */
  function textOf(msgEl, kind) {
    if (!msgEl) return "";
    if (kind === "user") {
      var u = msgEl.querySelector(".user-text");
      return u ? u.textContent : "";
    }
    var md = msgEl.querySelector(".md-content");
    if (!md) return "";
    return md.dataset.raw || md.innerText || "";
  }

  /**
   * Get the toolbar for a message, creating it if absent, and always move it
   * to the end of the message so DOM order matches visual order.
   */
  function ensureToolbar(msgEl) {
    if (!msgEl) return null;
    var tb = msgEl.querySelector(":scope > .msg-toolbar");
    if (!tb) {
      tb = document.createElement("div");
      tb.className = "msg-toolbar";
    }
    msgEl.appendChild(tb); // appendChild moves an existing node — intentional
    return tb;
  }

  function button(icon, title) {
    var b = document.createElement("button");
    b.innerHTML = '<i data-lucide="' + icon + '"></i>';
    b.title = title;
    return b;
  }

  function flash(btn, icon) {
    btn.innerHTML = '<i data-lucide="' + icon + '"></i>';
    if (typeof activateLucideIcons === "function") activateLucideIcons(btn);
  }

  // ── copy ───────────────────────────────────────────────────────────────

  function addCopy(toolbar, msgEl, kind) {
    if (toolbar.querySelector('[title="Copy"]')) return;
    var btn = button("copy", "Copy");
    btn.addEventListener("click", function () {
      var text = textOf(msgEl, kind);
      if (!text) return;
      var ok = function () {
        flash(btn, "check");
        setTimeout(function () { flash(btn, "copy"); }, 1500);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(ok).catch(function () { legacyCopy(text, ok); });
      } else {
        legacyCopy(text, ok);
      }
    });
    toolbar.appendChild(btn);
  }

  function legacyCopy(text, onSuccess) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    if (onSuccess) onSuccess();
  }

  // ── read aloud (TTS) ───────────────────────────────────────────────────

  function addTTS(toolbar, msgEl, kind) {
    if (toolbar.querySelector('[title="Read aloud"]')) return;
    var btn = button("volume-2", "Read aloud");

    btn.addEventListener("click", function () {
      var now = Date.now();
      if (now - _ttsLastAction < TTS_DEBOUNCE_MS) return;   // debounce
      if (_ttsActive) { stopGlobalTTS(); return; }          // stop if playing
      _ttsLastAction = now;

      var text = textOf(msgEl, kind);
      if (!text) return;

      _ttsActive = true;
      var gen = ++_ttsGeneration;
      _activeTTS.gen = gen;

      var player = new TTSStreamPlayer(function (state) {
        if (_ttsGeneration !== gen) return;                 // stale callback
        if (state === "loading") {
          btn.innerHTML = '<i data-lucide="loader-circle"></i>';
          btn.title = "Loading...";
        } else if (state === "playing") {
          btn.innerHTML = '<i data-lucide="square"></i>';
          btn.title = "Stop";
          btn.classList.add("tts-playing");
        } else {
          btn.innerHTML = '<i data-lucide="volume-2"></i>';
          btn.title = "Read aloud";
          btn.classList.remove("tts-playing");
          if (!_ttsStopping) _ttsActive = false;            // natural end only
          _activeTTS.player = null;
          _activeTTS.btn = null;
          _activeTTS.gen = -1;
        }
        if (typeof activateLucideIcons === "function") activateLucideIcons(btn);
      });

      _activeTTS.player = player;
      _activeTTS.btn = btn;
      player.play(text);
    });

    toolbar.appendChild(btn);
  }

  // ── public API ─────────────────────────────────────────────────────────

  /**
   * Build (or top up) a message toolbar.
   *   kind: "user" | "bot"
   *   opts.copy / opts.tts — default true
   *   opts.regen          — function, adds a Regenerate button
   *   opts.speedTurn      — element, adds the response-speed badge
   *   opts.skipIcons      — true during bulk history render, where the caller
   *                         activates icons once at the end instead
   */
  function attach(msgEl, kind, opts) {
    if (!msgEl) return null;
    opts = opts || {};
    var toolbar = ensureToolbar(msgEl);
    if (opts.copy !== false) addCopy(toolbar, msgEl, kind);
    if (opts.tts !== false) addTTS(toolbar, msgEl, kind);
    if (typeof opts.regen === "function") {
      var rb = button("refresh-cw", "Regenerate");
      rb.addEventListener("click", opts.regen);
      toolbar.appendChild(rb);
    }
    if (opts.speedTurn && typeof attachSpeedBadge === "function") {
      attachSpeedBadge(toolbar, opts.speedTurn);
    }
    if (!opts.skipIcons && typeof activateLucideIcons === "function") {
      activateLucideIcons(toolbar);
    }
    return toolbar;
  }

  window.MsgActions = {
    attach: attach,
    ensureToolbar: ensureToolbar,
    addCopy: addCopy,
    addTTS: addTTS,
    textOf: textOf
  };
})();
