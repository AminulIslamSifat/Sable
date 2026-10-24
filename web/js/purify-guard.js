/* purify-guard.js — one-time DOMPurify hardening for model-generated HTML.
 *
 * WHY THIS EXISTS
 * ---------------
 * Every sanitize() call in the app used DOMPurify's DEFAULTS, which permit
 * things that are harmless in a blog post and fatal in a single-page app:
 *
 *   <div id="chat">                        -> clobbers an app node by id
 *   <div id="fsOverlay">                   -> clobbers an app overlay
 *   <p style="position:fixed;inset:0;      -> full-screen cover over the UI
 *       z-index:99999;background:red">
 *   <div><style>.main{display:none}</style></div>
 *
 * Verified against this repo's vendor/purify.min.js: the top-level <style> is
 * stripped, but a <style> nested inside a div or an svg SURVIVES, as do id and
 * style attributes. One model reply containing any of the above makes Sable
 * look permanently broken until reload — which is exactly the reported bug.
 *
 * WHAT IT DOES
 * ------------
 * Installs global DOMPurify hooks, so every existing and future sanitize()
 * call inherits the policy without editing a single call site:
 *
 *   1. remove every <style> element, at any depth
 *   2. drop `id` unless it looks like a footnote/anchor anchor
 *   3. filter `style` down to declarations that cannot position or hide
 *   4. drop `class` unless it matches a renderer-owned prefix (language-,
 *      hljs-, katex, math, footnote, task-list, mermaid, ...)
 *   5. keep aria- and data- attributes, plus a small SVG allowlist
 *
 * Deliberately NOT stripped: `language-python` on code blocks (syntax
 * highlighting), katex/math classes, and footnote ids — all of those are
 * emitted by the renderer itself and break real features if removed.
 *
 * Loaded immediately AFTER vendor/purify.min.js and BEFORE any module that
 * calls sanitize(). Idempotent.
 */
(function () {
  'use strict';

  if (!window.DOMPurify) return;          // vendor missing: nothing to harden
  if (window.__sablePurifyGuard) return;  // already installed
  window.__sablePurifyGuard = true;

  // ---- id: keep only renderer-owned anchors (footnotes, citations) ---------
  // marked/GFM emit id="fn1", id="fnref1", and DOMPurify prefixes with
  // "user-content-". Nothing else in model output needs an id, and any id
  // that matches an app node (#chat, #input, #fsOverlay) is a hijack.
  const SAFE_ID = /^(user-content-)?(fn|fnref|cite|cite-ref|ref|toc)[-_\w]*$/i;

  // ---- class: keep only renderer-owned prefixes ----------------------------
  // The app stylesheet defines .hidden, .rdr, .agent-panel, .fs-overlay and
  // hundreds more; a model-supplied class inherits real layout. But these
  // prefixes are emitted by marked / the highlighter / katex and are needed.
  const SAFE_CLASS = /^(language-|hljs|katex|math|mermaid|footnote|task-list|contains-task-list|callout|admonition|table|code-|md-|sn-)/i;

  // ---- style: allow only declarations that cannot position or hide ---------
  // position/inset/top/left/right/bottom/z-index/display/visibility/float are
  // the overlay-and-hide primitives. Everything else (text-align, color,
  // background-color, border, font-*) is cosmetic and safe to keep.
  const DANGEROUS_DECL = /(^|;)\s*(position|inset|top|right|bottom|left|z-index|display|visibility|float|transform|translate|will-change|pointer-events)\s*:/i;

  function filterStyle(value) {
    const kept = String(value || '')
      .split(';')
      .map(function (d) { return d.trim(); })
      .filter(function (d) { return d && !DANGEROUS_DECL.test(d); });
    return kept.join('; ');
  }

  // A <form> with no action submits to the current URL — one Enter in a
  // model-authored form reloads Sable and loses the chat. Task-list
  // checkboxes are <input> and MUST survive, so only the form is forbidden.
  DOMPurify.addHook('uponSanitizeElement', function (node, data) {
    if (data.tagName === 'form') data.allowedTags[data.tagName] = false;
  });

  DOMPurify.addHook('uponSanitizeAttribute', function (node, data) {
    const name = String(data.attrName || '').toLowerCase();

    if (name === 'id') {
      if (!SAFE_ID.test(String(data.attrValue || ''))) data.keepAttr = false;
      return;
    }

    if (name === 'class') {
      const keep = String(data.attrValue || '')
        .split(/\s+/)
        .filter(function (c) { return c && SAFE_CLASS.test(c); });
      if (keep.length) data.attrValue = keep.join(' ');
      else data.keepAttr = false;
      return;
    }

    if (name === 'style') {
      const filtered = filterStyle(data.attrValue);
      if (filtered) data.attrValue = filtered;
      else data.keepAttr = false;
      return;
    }

    // Everything else: SVG presentation attrs, table attrs, aria-*, data-*.
    // DOMPurify already restricts the element/attr universe, so no further
    // filtering is needed here — the hooks above are the app-specific layer.
  });

  // <style> nested inside a div or svg survives DOMPurify's default pass even
  // though a top-level one is stripped. Remove every occurrence, any depth.
  DOMPurify.addHook('afterSanitizeAttributes', function (node) {
    if (!node || !node.querySelectorAll) return;
    const styles = node.querySelectorAll('style');
    for (let i = 0; i < styles.length; i++) styles[i].remove();
  });
})();
