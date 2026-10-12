// render-html.js — sandboxed HTML pane for <render_html> blocks.
//
// The model writes a self-contained HTML+JS+CSS document. We NEVER inject it
// into the host DOM. It goes into an <iframe sandbox="allow-scripts"> WITHOUT
// allow-same-origin, which gives it an opaque origin: no access to the parent
// DOM, no cookies, no localStorage on our origin, no fetch to our app. That is
// the whole security model — isolation, not sanitisation.
//
// All parent<->iframe traffic is postMessage, and every inbound message is
// checked against the specific iframe's contentWindow.
(function () {
  "use strict";

  var CDN = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net";

  // Desktop-mode trick. The frame lays out at a VIRTUAL viewport width —
  // roughly the host window, i.e. what the "Open" tab would show — and is
  // then scaled down to fit the pane. A narrow chat column therefore gets
  // the same layout a full-screen browser produces, just smaller.
  // Re-derived per pane from its real width (see applyScale in mount).
  // Ceiling on desktop-mode scaling. The frame may lay out at most this many
  // times wider than the pane, i.e. content never renders smaller than
  // 1 / MAX_SCALE of its real size. 1.25 -> never below 80%. Raise it for a
  // wider virtual viewport at the cost of smaller text.
  var MAX_SCALE = 1.0;

  function virtualWidth() {
    var w = window.innerWidth || 1280;
    return Math.max(1024, Math.min(1600, w));
  }

  // The bridge runs INSIDE the sandboxed iframe. It has no origin powers and
  // can only talk to the parent via postMessage. Built as an array join so the
  // source never fights JS string escaping.
  function bridgeScript() {
    return [
      'var root=document.getElementById("root");',
      'var errEl=document.getElementById("err");',
      'var buf="";',
      'function showErr(msg){',
      '  errEl.style.display="block";',
      '  errEl.textContent+=msg+"\\n";',
      '  try{parent.postMessage({rc:"error",message:String(msg)},"*");}catch(e){}',
      '}',
      'window.onerror=function(m,s,l){showErr("Error: "+m+" (line "+l+")");return false;};',
      'window.onunhandledrejection=function(e){',
      '  showErr("Unhandled rejection: "+((e.reason&&e.reason.message)||e.reason));',
      '};',
      'function stripIncomplete(html){',
      '  var S="scr"+"ipt";',
      '  html=html.replace(new RegExp("<"+S+".*?</"+S+">","gis"),"");',
      '  var lt=html.lastIndexOf("<");',
      '  if(lt>html.lastIndexOf(">"))html=html.slice(0,lt);',
      '  return html;',
      '}',
      'function runScripts(){',
      '  var scripts=root.querySelectorAll("script");',
      '  for(var i=0;i<scripts.length;i++){',
      '    var old=scripts[i];',
      '    var s=document.createElement("script");',
      '    for(var a=0;a<old.attributes.length;a++){s.setAttribute(old.attributes[a].name,old.attributes[a].value);}',
      '    if(old.src){s.src=old.src;}else{s.textContent=old.textContent;}',
      '    old.parentNode.replaceChild(s,old);',
      '  }',
      '}',
      'function reportSize(){',
      '  try{parent.postMessage({rc:"resize",height:document.documentElement.scrollHeight},"*");}catch(e){}',
      '}',
      'var renderTimer=null;',
      'function scheduleRender(){',
      '  if(renderTimer!==null)return;',
      '  renderTimer=setTimeout(function(){renderTimer=null;root.innerHTML=stripIncomplete(buf);reportSize();},100);',
      '}',
      'window.addEventListener("message",function(ev){',
      '  var d=ev.data||{};',
      '  if(d.rc!=="chunk"&&d.rc!=="finish")return;',
      '  if(d.rc==="chunk"){buf+=d.text||"";scheduleRender();return;}',
      '  if(renderTimer!==null){clearTimeout(renderTimer);renderTimer=null;}',
      '  buf=(d.html!=null)?d.html:buf;',
      '  root.innerHTML=buf;',
      '  runScripts();',
      '  reportSize();',
      '  setTimeout(reportSize,60);',
      '  setTimeout(reportSize,300);',
      '},false);',
      'if(window.ResizeObserver){new ResizeObserver(reportSize).observe(document.documentElement);}',
      // Forward scroll intent to the parent. The iframe is its own document,
      // so its wheel/touch events do not bubble to the chat pane; without
      // this the pane thinks the user is still pinned to the bottom and
      // re-scrolls on every streamed chunk.
      'window.addEventListener("wheel",function(e){try{parent.postMessage({rc:"usermove",deltaY:e.deltaY},"*");}catch(err){}},{passive:true});',
      'var _rcLastY=0;',
      'window.addEventListener("touchstart",function(e){_rcLastY=e.touches[0].clientY;},{passive:true});',
      'window.addEventListener("touchmove",function(e){var _dy=e.touches[0].clientY-_rcLastY;_rcLastY=e.touches[0].clientY;try{parent.postMessage({rc:"usermove",deltaY:-_dy},"*");}catch(err){}},{passive:true});',
      ''
    ].join("\n");
  }

  // Static srcdoc shell. Only the bridge changes it at runtime; the model's
  // code never sees this unless it looks at its own document.
  function shell() {
    var csp = [
      "default-src 'none'",
      "script-src 'unsafe-inline' " + CDN,
      "style-src 'unsafe-inline' " + CDN,
      "img-src data: https:",
      "font-src data: " + CDN,
      "connect-src 'none'",
      "base-uri 'none'",
      "form-action 'none'"
    ].join("; ");
    var css = [
      ":root{color-scheme:light dark;}",
      "html,body{margin:0;padding:0;background:transparent;font:14px/1.5 system-ui,sans-serif;color:#d4d4d8;}",
      "#root{min-height:10px;padding:2px;}",
      "#root>*{max-width:100%;}",
      "#err{display:none;margin:8px 0 0;padding:8px 10px;border-radius:8px;background:rgba(220,38,38,.12);border:1px solid rgba(220,38,38,.4);color:#fca5a5;font:12px/1.4 ui-monospace,monospace;white-space:pre-wrap;}"
    ].join("\n");
    return [
      '<!doctype html><html><head><meta charset="utf-8">',
      '<meta http-equiv="Content-Security-Policy" content="' + csp + '">',
      "<style>" + css + "</style>",
      '</head><body><div id="root"></div><pre id="err"></pre>',
      "<script>" + bridgeScript() + "<\/script>",
      "</body></html>"
    ].join("");
  }

  var blocks = Object.create(null);

  function mount(id, title, height, url) {
    // A duplicate id means an upstream bug (ids must be session-unique).
    // Returning the stale block would silently graft the new render onto the
    // OLD pane's DOM node and corrupt both. Tear the old one down instead.
    if (blocks[id]) {
      try {
        var _old = blocks[id];
        if (_old.wrap && _old.wrap.parentNode) _old.wrap.parentNode.removeChild(_old.wrap);
      } catch (e) {}
      delete blocks[id];
    }
    var wrap = document.createElement("div");
    wrap.className = "rc-html";

    var bar = document.createElement("div");
    bar.className = "rc-html-bar";
    var ttl = document.createElement("span");
    ttl.className = "rc-html-title";
    ttl.textContent = title || "Interactive";
    bar.appendChild(ttl);

    var spacer = document.createElement("span");
    spacer.className = "rc-html-spacer";
    bar.appendChild(spacer);

    var frame = document.createElement("iframe");
    frame.className = "rc-html-frame";
    // URL mode frames a LIVE page (local dev server, external site). It needs a
    // couple more sandbox tokens than the inline shell so ordinary pages work,
    // but still WITHOUT allow-same-origin — the framed page stays in an opaque
    // origin and cannot reach the host DOM.
    if (url) {
      frame.setAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-modals allow-downloads");
    } else {
      frame.setAttribute("sandbox", "allow-scripts");
    }
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.setAttribute("loading", "lazy");
    if (url) {
      frame.setAttribute("src", url);
    } else {
      frame.srcdoc = shell();
    }

    // Wrapper that clips the oversized frame back to pane size.
    var box = document.createElement("div");
    box.className = "rc-html-box";
    box.appendChild(frame);

    var blk = { id: id, wrap: wrap, frame: frame, box: box,
                title: title || "Interactive",
                src: "", ready: false, pending: [], height: height || null };

    // Explicit height from <render_html height="N"> — fixed pane, never
    // auto-resizes. Invalid/absent values fall back to the CSS default.
    if (height) {
      var _px = parseInt(height, 10);
      if (_px > 0) {
        _px = Math.min(2400, Math.max(120, _px));
        box.style.height = _px + "px";
      }
    }

    // --- desktop-mode scaling -------------------------------------------
    // --rc-scale makes the frame WIDER than the box (so it lays out at the
    // virtual width); --rc-inv shrinks it back to fit. Both are plain ratios,
    // so they stay correct at any pane width. Re-derived on every width
    // change — a height-only resize is ignored, which is what keeps this
    // from feeding back into itself.
    var _lastW = -1;
    function applyScale() {
      var pw = box.clientWidth || 0;
      if (!pw || pw === _lastW) return;
      _lastW = pw;
      var vw = virtualWidth();
      // k is the shrink factor applied to the oversized frame. Clamp it so
      // the pane never scales content down further than MAX_SCALE allows.
      var k = Math.max(pw / vw, 1 / MAX_SCALE);
      box.style.setProperty("--rc-scale", String(1 / k));
      box.style.setProperty("--rc-inv", String(k));
      blk.inv = k;
    }
    applyScale();
    if (window.ResizeObserver) {
      try { new ResizeObserver(applyScale).observe(box); } catch (e) {}
    }

    function mkBtn(label, fn) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "rc-html-btn";
      b.textContent = label;
      b.addEventListener("click", fn);
      return b;
    }

    bar.appendChild(mkBtn("Copy", function () {
      try { navigator.clipboard.writeText(blk.src); } catch (e) {}
    }));
    bar.appendChild(mkBtn("Source", function () {
      var on = wrap.classList.toggle("rc-html-showsrc");
      if (on) {
        var pre = wrap.querySelector(".rc-html-src");
        if (!pre) {
          pre = document.createElement("pre");
          pre.className = "rc-html-src";
          wrap.appendChild(pre);
        }
        pre.textContent = blk.src || "(empty)";
      }
    }));
    bar.appendChild(mkBtn("Open", function () {
      try {
        var doc = blk.src || "";
        if (!/charset/i.test(doc)) {
          if (/<head[^>]*>/i.test(doc)) {
            doc = doc.replace(/<head[^>]*>/i, function (m) { return m + '<meta charset="utf-8">'; });
          } else {
            doc = '<meta charset="utf-8">' + doc;
          }
        }
        var blob = new Blob([doc], { type: "text/html;charset=utf-8" });
        var url = URL.createObjectURL(blob);
        window.open(url, "_blank", "noopener");
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
      } catch (e) {}
    }));

    wrap.appendChild(bar);
    wrap.appendChild(box);

    // Flush anything that streamed before the iframe signalled ready.
    frame.addEventListener("load", function () {
      blk.ready = true;
      var q = blk.pending; blk.pending = [];
      for (var i = 0; i < q.length; i++) post(blk, q[i]);
    });

    blocks[id] = blk;
    return blk;
  }

  function post(blk, msg) {
    if (!blk.ready) { blk.pending.push(msg); return; }
    try { blk.frame.contentWindow.postMessage(msg, "*"); } catch (e) {}
  }

  // Inbound: resize + error + scroll intent. Verify the source is this frame.
  window.addEventListener("message", function (ev) {
    var d = ev.data || {};
    if (d.rc !== "resize" && d.rc !== "error" && d.rc !== "usermove") return;
    for (var id in blocks) {
      var blk = blocks[id];
      if (!blk.frame.contentWindow || ev.source !== blk.frame.contentWindow) continue;
      if (d.rc === "usermove") {
        if (window._sableNotifyWheel) window._sableNotifyWheel(Number(d.deltaY) || 0);
      } else if (d.rc === "resize") {
        // Explicit height wins — a fixed pane never chases its content.
        if (blk.height) continue;
        // The frame reports content height in its virtual viewport, so scale
        // it down before applying it to the visible pane.
        var k = blk.inv || 1;
        var h = Math.max(40, Math.min(2400, (Number(d.height) || 0) * k));
        blk.box.style.height = h + "px";
      } else if (d.rc === "error") {
        blk.wrap.classList.add("rc-html-haserror");
        // Surface the message the iframe forwarded instead of just recoloring
        // a border. Show it once, and offer a one-shot "Fix this" that asks
        // the model to repair the pane.
        var msg = String(d.message || "Unknown error");
        var banner = blk.wrap.querySelector(".rc-html-errbar");
        if (!banner) {
          banner = document.createElement("div");
          banner.className = "rc-html-errbar";
          var txt = document.createElement("span");
          txt.className = "rc-html-errtxt";
          banner.appendChild(txt);
          var fix = document.createElement("button");
          fix.type = "button";
          fix.className = "rc-html-btn";
          fix.textContent = "Fix this";
          fix.addEventListener("click", function () {
            if (blk.wrap.dataset.fixSent === "1") return;
            blk.wrap.dataset.fixSent = "1";
            try {
              if (window.GenUI && window.GenUI.issueNewTurn) {
                window.GenUI.issueNewTurn(
                  "The HTML pane (" + blk.title + ") threw: " + msg +
                  ". Fix the <render_html> block."
                );
              } else if (window.SableInteract && window.SableInteract.send) {
                window.SableInteract.send(
                  "The HTML pane (" + blk.title + ") threw: " + msg +
                  ". Fix the <render_html> block."
                );
              }
            } catch (e) {}
          });
          banner.appendChild(fix);
          var bar = blk.wrap.querySelector(".rc-html-bar");
          if (bar && bar.nextSibling) blk.wrap.insertBefore(banner, bar.nextSibling);
          else blk.wrap.appendChild(banner);
        }
        banner.querySelector(".rc-html-errtxt").textContent = msg;
      }
      return;
    }
  });

  window.SableHtml = {
    mount: function (id, title, height, url) { return mount(id, title, height, url); },
    push: function (id, text) {
      var blk = blocks[id];
      if (!blk) return;
      blk.src += text || "";
      post(blk, { rc: "chunk", text: text || "" });
    },
    finish: function (id, error) {
      var blk = blocks[id];
      if (!blk) return;
      post(blk, { rc: "finish", html: blk.src });
      if (error) blk.wrap.classList.add("rc-html-haserror");
      blk.wrap.classList.add("rc-html-done");
    },
    element: function (id) { var b = blocks[id]; return b ? b.wrap : null; },
    reset: function () { blocks = Object.create(null); }
  };
})();
