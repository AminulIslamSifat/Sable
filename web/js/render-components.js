// render_component — clean rewrite. JS builds the tree; CSS owns all visuals.
// No inline padding/gap/radius/font/color. Inline styles only for dynamic
// values (bar height, progress width, icon px, image dims).
(function () {
  "use strict";

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function arr(v) {
    if (Array.isArray(v)) return v;
    if (v == null) return [];
    if (typeof v === "string") {
      var s = v.trim();
      // JSON array/object literal (legacy attr form)
      if (s && (s[0] === "[" || s[0] === "{")) {
        try {
          var p = JSON.parse(s);
          return Array.isArray(p) ? p : [p];
        } catch (e) { /* fall through */ }
      }
      // "a, b, c" -> ['a','b','c']
      if (s.indexOf(",") >= 0) {
        return s.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
      }
      return [v];
    }
    return [v];
  }
  // options='[{...}]' arrives as a JSON *string* from the parser. arr() would
  // wrap the whole string as ONE element -> raw JSON leaks as a single label.
  // Decode first, then normalise.
  function optArr(v) { return arr(v); }
  // List data: child elements win, else fall back to an attribute.
  function kidsOf(node, keys) {
    if (Array.isArray(node.children) && node.children.length) return node.children;
    for (var i = 0; i < keys.length; i++) {
      var a = arr(node[keys[i]]);
      if (a.length) return a;
    }
    return [];
  }
  function str(v, fb) { return (typeof v === "string" && v.trim()) ? v : (v == null ? (fb || "") : String(v)); }
  function num(v, fb, lo, hi) {
    var n = Number(v);
    if (!isFinite(n)) return fb;
    if (lo != null && n < lo) n = lo;
    if (hi != null && n > hi) n = hi;
    return n;
  }

  var TONES = ["neutral", "accent", "note", "info", "tip", "warning", "danger"];
  var TONE_ALIAS = { success: "tip", ok: "tip", error: "danger", warn: "warning", primary: "accent", secondary: "neutral" };
  function tone(v, fb) {
    var t = String(v || "").toLowerCase();
    t = TONE_ALIAS[t] || t;
    return TONES.indexOf(t) >= 0 ? t : (fb || "neutral");
  }

  var KIND = {
    card: "box", panel: "box", section: "box",
    // JSON-schema vocabulary (sable-ui-renderer-starter): some names collide
    // with DIL ones but carry a different data shape, so they get their own
    // handler only where necessary (barChart, select).
    column: "col", caption: "caption", barchart: "barchart",
    hstack: "row", vstack: "col", column: "col", columns: "col",
    heading: "title", h1: "title", h2: "title", h3: "title", h4: "title",
    paragraph: "text", label: "text", span: "text", p: "text", subtitle: "text",
    metric: "stat", kpi: "stat", bar: "progress", meter: "progress",
    img: "image", pic: "image", avatar: "image",
    pre: "code", codeblock: "code", quote: "quote", blockquote: "quote",
    "grid-item": "griditem", griditem: "griditem", "grid-cell": "griditem",
    segmented: "segmented", "segmented-control": "segmented", tabbar: "segmented",
    btn: "button", cta: "button", hr: "divider", separator: "divider",
    keyvalue: "kv", "key-value": "kv",
    // NOTE: `checkbox` is NOT aliased to `check`. DIL's `check` is a read-only
    // checklist; the JSON schema's `checkbox` is a state-bound input with its
    // own handler. Aliasing them made the checkbox vanish.
    check: "check",
    switch: "toggle",
    collapse: "toggle", accordion: "toggle", options: "choices",
    chip: "chips", steps: "flow",
    checklist: "check",
    // ── full DIL spec coverage ──────────────────────────────────────────
    // Text formatting (inline HTML tags alias to the formatting handlers so
    // `<b>` / `<i>` inside a <text> don't degrade to 'unsupported')
    bold: "bold", strong: "bold", b: "bold",
    italic: "italic", em: "italic", i: "italic",
    underline: "underline", u: "underline",
    strikethrough: "strikethrough", s: "strikethrough", del: "strikethrough",
    math: "math",
    // anchor + media leaves
    a: "link",
    video: "video",
    file: "file", attachment: "file",
    // Media primitives
    favicon: "favicon", svg: "svg",
    asyncimage: "asyncimage", asyncimagegroup: "asyncimagegroup", asyncvideo: "asyncvideo",
    // Layout: collections, navigation, tables
    quiz: "quiz", mcq: "quiz",
    "flow-item": "flowitem", flowitem: "flowitem",
    "list-item": "listitem", listitem: "listitem",
    pressable: "pressable",
    carousel: "carousel", "carousel-item": "carouselitem",
    "carousel-media-item": "carouselmediaitem",
    popover: "popover", "popover-trigger": "popovertrigger",
    "popover-content": "popovercontent",
    "table-section": "tablesection", "table-row": "tablerow", "table-cell": "tablecell",
    // Inputs
    input: "input", textarea: "textarea", form: "form",
    "radio-group": "radiogroup", radiogroup: "radiogroup",
    radio: "radio", slider: "slider", "date-picker": "datepicker", datepicker: "datepicker",
    // Data / citations / links
    cite: "cite", codecite: "codecite", filecite: "filecite", memorycite: "memorycite",
    entity: "entity", filenavlist: "filenavlist", followup: "followup",
    // Raw-content blocks
    appblock: "appblock", writingblock: "writingblock",
    // Visualization
    mapwidgetv2: "mapwidgetv2"
  };
  function kindOf(node) {
    var raw = String((node && (node.kind || node.type)) || "").toLowerCase();
    return KIND[raw] || raw;
  }
  function rawTypeOf(node) {
    return String((node && (node.kind || node.type)) || "").toLowerCase();
  }

  function getPath(path, ctx) {
    if (!path) return undefined;
    var parts = String(path).split(".");
    var cur = (ctx.scope && parts[0] in ctx.scope) ? ctx.scope : ctx.state;
    for (var i = 0; i < parts.length; i++) { if (cur == null) return undefined; cur = cur[parts[i]]; }
    return cur;
  }
  function makeCtx(initial, rerender) {
    var c = {
      state: Object.assign({}, initial || {}), scope: {}, _re: rerender,
      set: function (k, v) { this.state[k] = v; this._re(); },
      toggle: function (k) { this.state[k] = !this.state[k]; this._re(); },
      setIndex: function (k, i) { this.state[k] = i; this._re(); },
      push: function (k, v) { if (!Array.isArray(this.state[k])) this.state[k] = []; this.state[k].push(v); this._re(); }
    };
    return c;
  }
  function noopCtx() { return { state: {}, scope: {}, set: function(){}, toggle: function(){}, setIndex: function(){}, push: function(){} }; }

  // ── client-side expression evaluator ─────────────────────────────────
  // The parser evaluates `{expr}` server-side ONCE, so a state change would
  // leave every top-level prop frozen. It also ships the raw source as
  // `__expr__<prop>`; we re-evaluate that here on each render. (Inside an
  // `{#each}` the parser already resolved and stripped it, so this is a no-op
  // there.) Mirrors engine/skills/handlers/dil_parser.py::_eval.
  function splitTop(s, ch) {
    var out = [], depth = 0, buf = [], q = null;
    for (var i = 0; i < s.length; i++) {
      var c = s[i];
      if (q) { buf.push(c); if (c === q) q = null; continue; }
      if (c === '"' || c === "'") { q = c; buf.push(c); }
      else if (c === "(" || c === "[" || c === "{") { depth++; buf.push(c); }
      else if (c === ")" || c === "]" || c === "}") { depth--; buf.push(c); }
      else if (c === ch && depth === 0) { out.push(buf.join("")); buf = []; }
      else buf.push(c);
    }
    out.push(buf.join(""));
    return out;
  }
  function lookupPath(path, ctx) {
    var parts = String(path).split(".");
    var cur = (ctx.scope && parts[0] in ctx.scope) ? ctx.scope : ctx.state;
    for (var i = 0; i < parts.length; i++) { if (cur == null) return undefined; cur = cur[parts[i]]; }
    return cur;
  }
  function evalExpr(e, ctx) {
    e = String(e == null ? "" : e).trim();
    if (!e) return undefined;
    if (e === "true") return true;
    if (e === "false") return false;
    if (e === "null" || e === "undefined") return null;
    if (/^-?\d+$/.test(e)) return parseInt(e, 10);
    if (/^-?\d+\.\d+$/.test(e)) return parseFloat(e);
    if ((e[0] === '"' && e[e.length - 1] === '"') || (e[0] === "'" && e[e.length - 1] === "'")) return e.slice(1, -1);
    if (/^(\([^)]*\)|[A-Za-z_]\w*)\s*=>/.test(e)) return e;   // arrow literal

    // One layer of balanced outer parens. `((wake + 24 - bed) % 24)` is wholly
    // wrapped, so the depth-aware splitter finds no top-level operator and the
    // expression died as undefined.
    if (e[0] === "(" && e[e.length - 1] === ")") {
      var _d = 0, _bal = true;
      for (var _i = 0; _i < e.length; _i++) {
        if (e[_i] === "(") _d++;
        else if (e[_i] === ")") { _d--; if (_d === 0 && _i !== e.length - 1) { _bal = false; break; } }
      }
      if (_bal && _d === 0) return evalExpr(e.slice(1, -1), ctx);
    }

    // Split on the FIRST `?`, rejoin the remainder, recurse. A chained ternary
    // (`a ? b : c ? d : e`) yields 3+ pieces and bailed under the old
    // `q.length === 2` check.
    var q = splitTop(e, "?");
    if (q.length >= 2) {
      var rest = q.slice(1).join("?");
      var br = splitTop(rest, ":");
      if (br.length >= 2) {
        return evalExpr(q[0], ctx)
          ? evalExpr(br[0], ctx)
          : evalExpr(br.slice(1).join(":"), ctx);
      }
    }
    var m = e.match(/^(.+?)\.filter\(\s*\w+\s*=>\s*\w+\.([\w.]+)\s*===?\s*(.+?)\s*\)\.length$/);
    if (m) {
      var base = evalExpr(m[1], ctx), want = evalExpr(m[3], ctx);
      if (Array.isArray(base)) {
        return base.filter(function (it) {
          var cur = it;
          m[2].split(".").forEach(function (p) { cur = cur == null ? cur : cur[p]; });
          return cur === want;
        }).length;
      }
      return 0;
    }
    m = e.match(/^(.+?)\.filter\(\s*\w+\s*=>\s*\w+\.([\w.]+)\s*\)\.length$/);
    if (m) {
      var b2 = evalExpr(m[1], ctx);
      if (Array.isArray(b2)) {
        return b2.filter(function (it) {
          var cur = it;
          m[2].split(".").forEach(function (p) { cur = cur == null ? cur : cur[p]; });
          return !!cur;
        }).length;
      }
      return 0;
    }
    if (/\.map\(.+?\)\.length$/.test(e)) {
      var b3 = evalExpr(e.replace(/\.map\(.+?\)\.length$/, ""), ctx);
      return Array.isArray(b3) ? b3.length : 0;
    }
    // Comparison. `<`, `>`, `<=`, `>=` were MISSING here while the Python
    // evaluator had them -- so `hrs < 5` resolved to undefined (falsy) in the
    // browser and every ternary silently took its final else-branch. Longest
    // operators first so `<=` is never split as `<`.
    m = e.match(/^(.+?)\s*(===|!==|<=|>=|==|!=|<|>)\s*(.+)$/);
    if (m) {
      var av = evalExpr(m[1], ctx), bv = evalExpr(m[3], ctx);
      var op = m[2];
      var eq = (av === bv) || (av == bv);
      if (op === "===" || op === "==") return eq;
      if (op === "!==" || op === "!=") return !eq;
      if (op === "<=") return av <= bv;
      if (op === ">=") return av >= bv;
      if (op === "<") return av < bv;
      return av > bv;
    }
    // Arithmetic (lowest precedence, left-to-right, +/- before */).
    var mm = splitTop(e, "+");
    if (mm.length > 1) { var s = evalExpr(mm[0], ctx); for (var i = 1; i < mm.length; i++) s = (Number(s) || 0) + (Number(evalExpr(mm[i], ctx)) || 0); return s; }
    mm = splitTop(e, "-");
    if (mm.length > 1 && mm[0] !== "") { var d = evalExpr(mm[0], ctx); for (var j = 1; j < mm.length; j++) d = (Number(d) || 0) - (Number(evalExpr(mm[j], ctx)) || 0); return d; }
    mm = splitTop(e, "*");
    if (mm.length > 1) { var p = evalExpr(mm[0], ctx); for (var a = 1; a < mm.length; a++) p = (Number(p) || 0) * (Number(evalExpr(mm[a], ctx)) || 0); return p; }
    mm = splitTop(e, "/");
    if (mm.length > 1) { var qv = evalExpr(mm[0], ctx); for (var b = 1; b < mm.length; b++) { var dv = Number(evalExpr(mm[b], ctx)) || 1; qv = (Number(qv) || 0) / dv; } return qv; }
    mm = splitTop(e, "%");
    if (mm.length > 1) { var rv = evalExpr(mm[0], ctx); for (var c = 1; c < mm.length; c++) { var md = Number(evalExpr(mm[c], ctx)) || 0; rv = md ? (Number(rv) || 0) % md : 0; } return rv; }
    if (e[0] === "!") return !evalExpr(e.slice(1), ctx);
    if (e.slice(-7) === ".length") {
      var lv = evalExpr(e.slice(0, -7), ctx);
      return (lv && typeof lv.length === "number") ? lv.length : 0;
    }
    // Math.* — must precede the bare-identifier fallback, or `Math.PI` matches
    // /^[A-Za-z_][\w.]*$/ and resolves to undefined via lookupPath.
    // `.toFixed(n)` — must precede the Math block, or `Math.round(x).toFixed(1)`
    // gets swallowed by the Math regex and evaluates to undefined.
    var mfix = e.match(/^(.+?)\.toFixed\(\s*(\d+)\s*\)$/);
    if (mfix) {
      var fv = evalExpr(mfix[1], ctx);
      // Return the STRING, matching both JS semantics and the Python side
      // (which yields round(x, n), str()'d to "9.0"). Returning a Number made
      // the readout flip from "9.0" on first paint to "9" on first re-render.
      if (typeof fv === "number" && isFinite(fv)) return fv.toFixed(parseInt(mfix[2], 10));
      return undefined;
    }
    if (e === "Math.PI") return Math.PI;
    if (e === "Math.E") return Math.E;
    var mmMath = e.match(/^Math[.](\w+)[(]([\s\S]*)[)]$/);
    if (mmMath) {
      var mfn = mmMath[1];
      var margs = mmMath[2].trim() ? splitTop(mmMath[2], ",").map(function (a) { return evalExpr(a, ctx); }) : [];
      var allNum = margs.every(function (a) { return typeof a === "number" && isFinite(a); });
      if (allNum) {
        if (mfn === "cos") return Math.cos(margs[0]);
        if (mfn === "sin") return Math.sin(margs[0]);
        if (mfn === "tan") return Math.tan(margs[0]);
        if (mfn === "atan2") return Math.atan2(margs[0], margs[1]);
        if (mfn === "acos") return Math.acos(margs[0]);
        if (mfn === "asin") return Math.asin(margs[0]);
        if (mfn === "atan") return Math.atan(margs[0]);
        if (mfn === "abs") return Math.abs(margs[0]);
        if (mfn === "sqrt") return Math.sqrt(margs[0]);
        if (mfn === "pow") return Math.pow(margs[0], margs[1]);
        if (mfn === "floor") return Math.floor(margs[0]);
        if (mfn === "ceil") return Math.ceil(margs[0]);
        if (mfn === "round") return Math.round(margs[0]);
        if (mfn === "min") return Math.min.apply(null, margs);
        if (mfn === "max") return Math.max.apply(null, margs);
        if (mfn === "hypot") return Math.hypot.apply(null, margs);
      }
      return undefined;
    }
    if (/^[A-Za-z_][\w.]*$/.test(e)) return lookupPath(e, ctx);
    return undefined;
  }
  // Overwrite `node[k]` with a freshly evaluated `__expr__<k>`, if present.
  function hydrate(node, ctx) {
    if (!node || typeof node !== "object" || !ctx) return node;
    var out = null;
    function ensure() { if (!out) { out = {}; for (var kk in node) out[kk] = node[kk]; } return out; }

    // A text node carrying `__expr__` re-resolves its own `text`.
    if (node.kind === "__text__" && node.__expr__ != null) {
      var tv = evalExpr(node.__expr__, ctx);
      if (tv !== undefined) ensure().text = (tv === null || tv === false) ? "" : String(tv);
    }
    // An element whose single text child was hoisted into `text` keeps the
    // source on `__expr__text` — re-resolve it here.
    if (node.__expr__text != null) {
      var hv = evalExpr(node.__expr__text, ctx);
      if (hv !== undefined) ensure().text = (hv === null || hv === false) ? "" : String(hv);
    }
    // Attr-level `__expr__<prop>` re-resolves the real prop.
    for (var k in node) {
      if (k.indexOf("__expr__") !== 0) continue;
      var real = k.slice(8);
      var v = evalExpr(node[k], ctx);
      if (v === undefined) continue;
      ensure()[real] = v;
    }
    return out || node;
  }

  function interp(s, ctx) {
    if (typeof s !== "string" || s.indexOf("{") === -1) return s;
    // Negative lookarounds keep {{double-brace}} markers untouched — those
    // belong to richText (inline chips), not to variable interpolation.
    return s.replace(/(?<!\{)\{([a-zA-Z0-9_.]+)\}(?!\})/g, function (m, path) {
      var v = getPath(path, ctx);
      return v == null ? m : String(v);
    });
  }
  // TeX -> MathJax-delimited <span>. Backslash built via fromCharCode on
  // PURPOSE: a source literal "\(" has its backslash swallowed, so MathJax
  // never sees a delimiter and silently ignores the span. (See R.math.)
  var _MATH_BS = String.fromCharCode(92);
  function makeMathSpan(tex, display) {
    if (tex == null || tex === "") return null;
    var wrapped = display
      ? _MATH_BS + "[" + tex + _MATH_BS + "]"
      : _MATH_BS + "(" + tex + _MATH_BS + ")";
    var e = el("span", "rc-math", wrapped);
    e.setAttribute("data-tex", tex);
    if (display) e.setAttribute("data-display", "1");
    return e;
  }
  function richText(parent, s) {
    var src = String(s == null ? "" : s);
    // Math comes FIRST in the alternation: TeX like `a*b` or `x_i` would
    // otherwise be eaten by the emphasis branches, and MathJax needs the raw
    // source intact. $$..$$ = display, $..$ = inline (single line, not $$).
    // Then: {{term}} chip · **bold** · *italic* · `code`
    var re = /\$\$([\s\S]+?)\$\$|(?<!\$)\$(?!\$)([^\n$]+?)\$(?!\$)|\{\{([^}]+)\}\}|\*\*([^*]+)\*\*|(?<!\*)\*([^*]+)\*(?!\*)|`([^`]+)`/g;
    var last = 0, m;
    while ((m = re.exec(src)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(src.slice(last, m.index)));
      if (m[1] != null) { var _dm = makeMathSpan(m[1], true); if (_dm) parent.appendChild(_dm); }
      else if (m[2] != null) { var _im = makeMathSpan(m[2], false); if (_im) parent.appendChild(_im); }
      else if (m[3] != null) parent.appendChild(el("span", "rc-chip-inline", m[3]));
      else if (m[4] != null) parent.appendChild(el("strong", "rc-strong", m[4]));
      else if (m[5] != null) parent.appendChild(el("em", "rc-italic", m[5]));
      else if (m[6] != null) parent.appendChild(el("code", "rc-inline-code", m[6]));
      last = re.lastIndex;
    }
    if (last < src.length) parent.appendChild(document.createTextNode(src.slice(last)));
  }
  function richDiv(cls, s) { var d = el("div", cls); richText(d, s); return d; }
  // Flow step body: interpolate first (single braces), then rich-format
  // (chips / bold / code). Order matters — interp must not eat {{chips}}.
  function richLine(cls, s, ctx) {
    var d = el("div", cls);
    richText(d, interp(String(s == null ? "" : s), ctx));
    return d;
  }

  // Unknown kinds degrade to their own content rather than a loud red box — a
  // spec addition should never blow up a whole message. Only genuinely empty
  // unknowns get the badge (and that is a bug signal worth seeing).
  function unknownNode(node, ctx) {
    var kids = arr(node && node.children);
    var t = node && (node.text != null ? node.text : (node.value != null ? node.value : node.label));
    if (t != null || kids.length) {
      var d = el("div", "rc-unknown-soft");
      d.dataset.kind = kindOf(node) || "?";
      if (t != null) d.appendChild(richDiv("rc-text", interp(String(t), ctx || noopCtx())));
      else kids.forEach(function (c) {
        var r = renderNode(c, 0, ctx || noopCtx());
        if (r) d.appendChild(r);
      });
      return d;
    }
    return el("div", "rc-unknown", "unsupported: " + (kindOf(node) || "?"));
  }

  // Reference px values, read off the HTML:
  //   .stat svg{15px}   <- size="lg"   (stat icons)
  //   .agenticon{19px}  <- size="xl"   (agent rows)
  //   .brandicon svg{25px} / .feature svg{25px} <- size="2xl"
  var ICON_PX = { xs: 10, sm: 12, md: 14, lg: 15, xl: 19, "2xl": 25, "3xl": 32 };
  var SAFE_COLOR = /^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20})$/;
  function renderIcon(node) {
    var i = document.createElement("i");
    i.setAttribute("data-lucide", str(node.name, "circle"));
    i.className = "rc-icon";
    var px = ICON_PX[String(node.size || "md")] || ICON_PX.md;
    i.style.width = px + "px";
    i.style.height = px + "px";
    var c = str(node.color, "");
    if (c && SAFE_COLOR.test(c)) i.style.color = c;
    return i;
  }

  // ── design-token resolution (restored — the rewrite dropped this) ─────
  // Values mirror the reference HTML: .agent=8px, .brandicon=12px, .panel=15px
  var RADIUS_TOKENS = { none: "0", sm: "6px", md: "8px", lg: "8px", xl: "12px", "2xl": "15px", "3xl": "24px", full: "999px" };
  var BG_TOKENS = {
    surface: "transparent", "surface-secondary": "transparent",
    "surface-tertiary": "var(--surface, #3a3a3a)",
    "surface-elevated": "color-mix(in srgb, var(--panel-2,#141414) 85%, transparent)",
    panel: "var(--panel,#090909)", "panel-2": "var(--panel-2,#141414)",
    accent: "color-mix(in srgb, var(--purple,#a78bfa) 18%, transparent)",
    success: "color-mix(in srgb, var(--green,#166534) 22%, transparent)",
    danger: "color-mix(in srgb, var(--red,#cf272b) 18%, transparent)",
    info: "color-mix(in srgb, var(--blue,#60a5fa) 18%, transparent)",
    transparent: "transparent"
  };
  var COLOR_TOKENS = {
    primary: "var(--text,#d4d4d8)", secondary: "var(--dim,#b5b5b5)", muted: "var(--muted,#a3a3a3)",
    accent: "var(--purple,#a78bfa)", success: "var(--green,#34d399)", danger: "var(--red,#cf272b)",
    warning: "var(--yellow,#fbbf24)", info: "var(--blue,#60a5fa)", white: "#ffffff", black: "#000000"
  };
  function resolveRadius(v) { if (v == null || v === "") return ""; var s = String(v); if (RADIUS_TOKENS[s]) return RADIUS_TOKENS[s]; if (/^\d+(?:\.\d+)?$/.test(s)) return s + "px"; return s; }
  function resolveBg(v) { if (v == null || v === "") return ""; var s = String(v); return BG_TOKENS[s] !== undefined ? BG_TOKENS[s] : s; }
  function resolveSpace(v) { if (v == null || v === "") return ""; var n = Number(v); if (isFinite(n) && n >= 0 && n <= 12) return (n * 4) + "px"; return String(v); }
  function resolveColorToken(v) { if (v == null || v === "") return ""; var s = String(v); return COLOR_TOKENS[s] || s; }

  // ── event-handler normalisation ──────────────────────────────────────
  // DIL surfaces handlers as `{"raw":"()=>setRunning(!running)"}` after JSON
  // round-trip (or a bare string). Pull out the state key + intent.
  function rawExpr(v) {
    if (v == null) return "";
    if (Array.isArray(v) && v[0] === "raw") return String(v[1] || "");
    if (typeof v === "object" && v.raw) return String(v.raw);
    return String(v);
  }
  function stateKeyOf(v) {
    var e = rawExpr(v).trim();
    if (!e) return "";
    // `setTab` / `setRunning` / `tab` -> `tab` / `running`
    var m = e.match(/^set([A-Z]\w*)$/);
    if (m) { var n = m[1]; return n.charAt(0).toLowerCase() + n.slice(1); }
    if (/^[A-Za-z_]\w*$/.test(e)) return e;
    return "";
  }
  function handlerKind(v) {
    var e = rawExpr(v);
    // Host actions: GenUI.openUrl(...) / copy(...) / issueNewTurn(...) /
    // openEntityDetail(...). Matched first so a call containing `set` isn't
    // mistaken for a state setter.
    var g = e.match(/GenUI[.](\w+)\s*\(([\s\S]*)\)/);
    if (g) {
      return { kind: "host", action: g[1], args: parseCallArgs(g[2]) };
    }
    var m = e.match(/set\w*\(\s*!(\w+)\s*\)/);
    if (m) return { kind: "toggle", key: m[1] };
    m = e.match(/set(\w+)\(\s*(["'])(.*?)\2\s*\)/);
    if (m) { var n = m[1]; return { kind: "set", key: n.charAt(0).toLowerCase() + n.slice(1), value: m[3] }; }
    // Numeric update: `setCount(count+1)` / `setN(prev-2)` / `setN(x*3)`.
    // The RHS references the same state key, so compute it at click time.
    m = e.match(/set(\w+)\(\s*([\w.]+)\s*([+\-*/])\s*(-?[\d.]+)\s*\)/);
    if (m) {
      var inc = m[1];
      return { kind: "inc", key: inc.charAt(0).toLowerCase() + inc.slice(1), op: m[3], by: parseFloat(m[4]) };
    }
    return { kind: "key", key: stateKeyOf(v) };
  }

  // Split `GenUI.copy("a", "b")` args at top-level commas and strip quotes.
  function parseCallArgs(src) {
    var s = String(src == null ? "" : src).trim();
    if (!s) return [];
    return splitTop(s, ",").map(function (a) {
      a = a.trim();
      if ((a[0] === '"' && a[a.length - 1] === '"') || (a[0] === "'" && a[a.length - 1] === "'")) a = a.slice(1, -1);
      return a;
    });
  }

  // ── host actions (window.GenUI) ──────────────────────────────────────
  // Installed on load if the host page hasn't provided its own. Mirrors the
  // host-action contract: openUrl, copy, issueNewTurn, openEntityDetail.
  if (typeof window !== "undefined" && !window.GenUI) {
    window.GenUI = {
      openUrl: function (url) { if (url) window.open(String(url), "_blank", "noopener"); },
      copy: function (text) {
        text = String(text == null ? "" : text);
        if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
        var ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta);
      },
      issueNewTurn: function (text) {
        if (window.SableInteract && window.SableInteract.send) window.SableInteract.send(String(text == null ? "" : text));
      },
      openEntityDetail: function (id) {
        var ev = new CustomEvent("sable:entity", { detail: { id: id } });
        window.dispatchEvent(ev);
      }
    };
  }
  // Pull an onClick/set/toggle/action binding off a node, if any.
  function bindingOf(node) {
    var src = node.onClick != null ? node.onClick : (node.onclick != null ? node.onclick : null);
    if (src != null) {
      return handlerKind(src);
    }
    // A raw `action`/`send` string that names a host action, e.g.
    // action={GenUI.openUrl("https://…")}.
    if (node.action != null && /GenUI[.]/.test(rawExpr(node.action))) {
      return handlerKind(node.action);
    }
    if (node.toggle) return { kind: "toggle", key: stateKeyOf(node.toggle) || String(node.toggle) };
    // `set` arrives as a JSON *string* (`set='{"m":"b"}'`) or an object
    // (JSON-schema trees). Accept both.
    if (node.set != null) {
      var sobj = node.set;
      if (typeof sobj === "string") {
        try { sobj = JSON.parse(sobj); } catch (e) { sobj = null; }
      }
      if (sobj && typeof sobj === "object") return { kind: "setobj", set: sobj };
    }
    return null;
  }
  function bindClick(elNode, binding, ctx) {
    if (!binding) return false;
    // Host actions don't need a reactive ctx — they call out to window.GenUI.
    if (binding.kind === "host") {
      elNode.classList.add("rc-clickable");
      elNode.addEventListener("click", function (e) {
        e.preventDefault();
        var fn = window.GenUI && window.GenUI[binding.action];
        if (typeof fn === "function") fn.apply(window.GenUI, binding.args || []);
      });
      return true;
    }
    if (!ctx || !ctx.set) return false;
    elNode.classList.add("rc-clickable");
    elNode.addEventListener("click", function (e) {
      e.preventDefault();
      if (binding.kind === "toggle") ctx.toggle(binding.key);
      else if (binding.kind === "set") ctx.set(binding.key, binding.value);
      else if (binding.kind === "inc") {
        var cur = Number(ctx.state[binding.key]) || 0;
        var nv = binding.op === "+" ? cur + binding.by : binding.op === "-" ? cur - binding.by : binding.op === "*" ? cur * binding.by : cur / binding.by;
        ctx.set(binding.key, nv);
      }
      else if (binding.kind === "setobj") Object.keys(binding.set).forEach(function (k) { ctx.set(k, binding.set[k]); });
      else if (binding.kind === "key" && binding.key) ctx.set(binding.key, true);
    });
    return true;
  }

  var R = {};
  var MAX_DEPTH = 20, MAX_CHILDREN = 150;

  function renderNode(node, depth, ctx) {
    if (!node || typeof node !== "object" || depth > MAX_DEPTH) return null;
    node = hydrate(node, ctx);
    // JSON-schema trees nest attributes under `props` (`{type:"badge",
    // props:{tone:"success"}}`); DIL puts them flat. Hoist them so one set of
    // handlers reads both. Top-level keys win, so an explicit `node.tone`
    // always beats `props.tone`.
    if (node.props && typeof node.props === "object") {
      var merged = {};
      for (var pk in node.props) merged[pk] = node.props[pk];
      for (var nk in node) if (nk !== "props") merged[nk] = node[nk];
      node = merged;
    }
    var k = kindOf(node);
    node.__rawtype__ = rawTypeOf(node);
    var fn = R[k];
    if (!fn) return unknownNode(node, ctx);
    try { return fn(node, depth, ctx); }
    catch (e) { return el("div", "rc-error", "render error: " + k + " — " + e.message); }
  }
  function renderChildren(node, depth, container, ctx) {
    var kids = arr(node.children).length ? node.children : (Array.isArray(node.content) ? node.content : []);
    var n = 0;
    var textBuf = [];
    function flushText() {
      if (!textBuf.length) return;
      var joined = textBuf.join("");
      textBuf = [];
      if (joined.trim()) { richText(container, joined); n++; }
    }
    function isInlineText(kid) {
      if (!kid || typeof kid !== "object") return false;
      if (kid.kind === "__text__" || kid.kind === "text-run") return true;
      // a bare text node (no styling props) is an inline fragment
      if (kid.kind === "text" || kid.kind === undefined) {
        return Object.keys(kid).every(function (k) { return k === "kind" || k === "text"; });
      }
      return false;
    }
    // `<row>` was removed from the DSL, but a run of adjacent buttons is a
    // button GROUP (a mode switcher, a set of choices) and reads badly stacked
    // one-per-line. Collect consecutive button nodes and lay them out as a row
    // automatically, so the author does not need a layout tag for the common
    // case. A `block` button is explicitly full-width and stays on its own.
    function isButton(kid) {
      if (!kid || typeof kid !== "object") return false;
      var k = kindOf(kid);
      if (k !== "button") return false;
      var hk = hydrate(kid, ctx);
      return !(hk && hk.block);
    }
    function flushButtons(startIdx) {
      var row = el("div", "rc-btn-row");
      var j = startIdx;
      while (j < kids.length && isButton(kids[j])) {
        var rb = renderNode(kids[j], depth + 1, ctx);
        if (rb) { row.appendChild(rb); n++; }
        j++;
      }
      if (row.childNodes.length) container.appendChild(row);
      return j;
    }
    for (var i = 0; i < kids.length && n < MAX_CHILDREN; i++) {
      var kid = kids[i];
      // Collect adjacent inline text so "**" + "Planner" + "**" joins into one
      // string and **bold** actually forms before richText parses it.
      if (isInlineText(kid)) {
        // Inline runs (e.g. `count={count}`) carry `__expr__`; re-resolve them
        // against live state instead of pushing the parse-time text, or the
        // value freezes on every re-render.
        var hk = hydrate(kid, ctx);
        textBuf.push(hk.text || "");
        continue;
      }
      flushText();
      if (isButton(kid)) {
        i = flushButtons(i) - 1;
        continue;
      }
      var r = renderNode(kid, depth + 1, ctx);
      if (r) { container.appendChild(r); n++; }
    }
    flushText();
    return container.childNodes.length ? container : null;
  }

  // `__if__` — server-emitted conditional. The parser ships EVERY branch along
  // with its raw `cond` source, and the client picks the winner against live
  // state on each render. Without this the parser collapsed to the first
  // matching branch at parse time and dropped the rest, so `{#if tab==="B"}`
  // could never appear no matter what the user clicked.
  R.__if__ = function (node, depth, ctx) {
    var frag = document.createDocumentFragment();
    var branches = arr(node.branches);
    for (var i = 0; i < branches.length; i++) {
      var br = branches[i] || {};
      var cond = String(br.cond == null ? "" : br.cond).trim();
      var body = arr(br.nodes).length ? br.nodes : arr(br.children);
      if (cond === "true" || cond === "" || evalExpr(cond, ctx)) {
        renderNodesInto(frag, body, ctx);
        break;
      }
    }
    return frag;
  };

  R.box = function (node, depth, ctx) {
    var bg = resolveBg(node.background || node.bg);
    // `card` (JSON schema) implies a bordered surface; `box` (DIL) only when
    // asked. `__origin__` is stamped by kindOf's caller via node.type/kind.
    var rawType = String(node.__rawtype__ || "").toLowerCase();
    var isCard = node.border === true || rawType === "card" || rawType === "panel" || (bg && bg !== "transparent");
    var d = el("div", "rc-box");
    d.dataset.variant = isCard ? "card" : "plain";
    if (node.tone) d.dataset.tone = tone(node.tone);
    if (node.flex) d.dataset.flex = "1";
    if (node.size && /px$/.test(String(node.size))) {
      d.dataset.square = String(node.size);
      // CSS can't read an attribute value into width/height, so set the square
      // dimensions inline. This is what makes `<box size="9px" />` dots render.
      d.style.width = String(node.size);
      d.style.height = String(node.size);
    }
    if (node.gap != null) d.dataset.gap = String(num(node.gap, 0, 0, 12));
    if (node.align) d.dataset.align = node.align;
    if (node.justify) d.dataset.justify = node.justify;
    if (bg) d.style.background = bg;
    var r = resolveRadius(node.radius); if (r) d.style.borderRadius = r;
    var p = resolveSpace(node.padding); if (p) d.style.padding = p;
    if (node.title) d.appendChild(el("div", "rc-box-title", interp(String(node.title), ctx)));
    var body = el("div", "rc-box-body");
    renderChildren(node, depth, body, ctx);
    if (!body.childNodes.length) {
      var t = node.body != null ? node.body : node.text;
      if (typeof t === "string" && t.trim()) body.appendChild(richDiv("rc-text", interp(t, ctx)));
    }
    if (body.childNodes.length) d.appendChild(body);
    return d;
  };
  R.row = function (node, depth, ctx) {
    var d = el("div", "rc-row");
    if (node.align) d.dataset.align = node.align;
    if (node.justify) d.dataset.justify = node.justify;
    if (node.wrap) d.dataset.wrap = "1";
    if (node.gap != null) d.dataset.gap = String(num(node.gap, 0, 0, 12));
    return renderChildren(node, depth, d, ctx) || d;
  };
  R.col = function (node, depth, ctx) {
    var d = el("div", "rc-col");
    if (node.gap != null) d.dataset.gap = String(num(node.gap, 0, 0, 12));
    return renderChildren(node, depth, d, ctx) || d;
  };
  R.grid = function (node, depth, ctx) {
    var d = el("div", "rc-grid");
    d.style.setProperty("--rc-cols", String(num(node.columns != null ? node.columns : node.cols, 2, 1, 6)));
    if (node.gap != null) d.dataset.gap = String(num(node.gap, 0, 0, 12));
    return renderChildren(node, depth, d, ctx) || d;
  };
  R.griditem = function (node, depth, ctx) {
    var d = el("div", "rc-griditem");
    return renderChildren(node, depth, d, ctx) || d;
  };

  R.text = function (node, depth, ctx) {
    var raw = node.text != null ? node.text : (node.value != null ? node.value : node.content);
    var hasKids = arr(node.children).length > 0;
    if (raw == null && !hasKids) return null;
    var d = el("div", "rc-text");
    // `<text>bp={bp}</text>` parses to inline CHILDREN, not a `text` prop.
    // Render those (so the `__expr__` runs re-resolve); fall back to the prop.
    if (hasKids) renderChildren(node, depth, d, ctx);
    else richText(d, interp(String(raw), ctx));
    if (node.size) d.dataset.size = node.size;
    if (node.tone) d.dataset.tone = tone(node.tone);
    if (node.weight) d.dataset.weight = String(node.weight);
    if (node.color) d.style.color = resolveColorToken(node.color);
    if (node.align) d.style.textAlign = node.align;
    return d;
  };
  R.title = function (node, depth, ctx) {
    var v = interp(str(node.text != null ? node.text : node.value), ctx);
    if (!v) return null;
    var lvl = num(node.level, ({ xl: 1, lg: 2, md: 3, sm: 4, xs: 4 })[node.size] || 4, 1, 4);
    var d = el("div", "rc-heading rc-h" + lvl, v);
    if (node.color) d.style.color = resolveColorToken(node.color);
    if (node.tone) d.dataset.tone = tone(node.tone);
    return d;
  };
  R.badge = function (node, depth, ctx) {
    var v = interp(str(node.text != null ? node.text : node.value), ctx);
    if (!v) return null;
    var d = el("span", "rc-badge", v);
    d.dataset.tone = tone(node.tone || node.color, "neutral");
    return d;
  };
  R.chips = function (node, depth, ctx) {
    var wrap = el("div", "rc-chips");
    kidsOf(node, ["items"]).forEach(function (it) {
      var label = typeof it === "object" ? str(it.text || it.label) : str(it);
      if (label) wrap.appendChild(el("span", "rc-chip", interp(label, ctx)));
    });
    return wrap.childNodes.length ? wrap : null;
  };
  R.stat = function (node, depth, ctx) {
    var d = el("div", "rc-stat");
    if (node.tone) d.dataset.tone = tone(node.tone);
    d.appendChild(el("div", "rc-stat-value", interp(str(node.value, "—"), ctx)));
    if (node.label) d.appendChild(el("div", "rc-stat-label", interp(String(node.label), ctx)));
    if (node.delta) d.appendChild(el("div", "rc-stat-delta", interp(String(node.delta), ctx)));
    return d;
  };
  R.stats = function (node, depth, ctx) {
    var wrap = el("div", "rc-stats");
    kidsOf(node, ["items"]).forEach(function (it) {
      if (!it || typeof it !== "object") return;
      // Route through renderNode, NOT R.stat directly. renderNode runs
      // hydrate(), which re-resolves `__expr__*` against LIVE state; calling
      // the handler straight skipped it, so `<stat value={hrs.toFixed(1)}>`
      // inside <stats> stayed frozen at its parse-time number forever while
      // the sliders moved. Any container that hand-calls a child handler has
      // the same trap -- always go through renderNode.
      var r = renderNode(it, depth + 1, ctx);
      if (r) wrap.appendChild(r);
    });
    return wrap.childNodes.length ? wrap : null;
  };
  R.icon = function (node) { return renderIcon(node); };
  R.caption = function (node, depth, ctx) {
    var v = interp(str(node.text != null ? node.text : node.value), ctx);
    if (!v) return null;
    return el("div", "rc-cap", v);
  };
  // JSON-schema checkbox: a single boolean bound to `stateKey`.
  R.checkbox = function (node, depth, ctx) {
    var key = str(node.stateKey || node.state || node.bind, "");
    var wrap = el("label", "rc-checkbox");
    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!(key && ctx && ctx.state && ctx.state[key]);
    if (key && ctx) box.addEventListener("change", function () { ctx.set(key, box.checked); });
    wrap.appendChild(box);
    wrap.appendChild(el("span", null, interp(str(node.text || node.label), ctx)));
    return wrap;
  };
  // JSON-schema select: options + a `stateKey` holding the chosen value.
  R.select = function (node, depth, ctx) {
    var key = str(node.stateKey || node.state || node.bind, "");
    var opts = optArr(node.options || node.children);
    if (!opts.length) return null;
    var wrap = el("label", "rc-select");
    if (node.label) wrap.appendChild(el("span", "rc-select-label", interp(String(node.label), ctx)));
    var sel = document.createElement("select");
    var cur = (key && ctx && ctx.state) ? ctx.state[key] : undefined;
    if (cur === undefined && opts[0]) cur = (typeof opts[0] === "object" ? opts[0].value : opts[0]);
    opts.forEach(function (o) {
      var val = typeof o === "object" ? o.value : o;
      var lab = typeof o === "object" ? (o.label != null ? o.label : o.value) : o;
      var op = document.createElement("option");
      op.value = String(val);
      op.textContent = String(lab);
      if (String(val) === String(cur)) op.selected = true;
      sel.appendChild(op);
    });
    if (key && ctx) sel.addEventListener("change", function () { ctx.set(key, sel.value); });
    wrap.appendChild(sel);
    return wrap;
  };
  // JSON-schema bar chart: {title, data:[{label,value}]} — a different shape
  // from DIL's {content:{data,series,xKey}}, so it gets its own handler.
  R.barchart = function (node, depth, ctx) {
    var rows = arr(node.data);
    if (!rows.length) return null;
    var labels = rows.map(function (r) { return String(r && r.label); });
    var vals = rows.map(function (r) { return Number(r && r.value) || 0; });
    // Same axis maths as R.chart, and the same class-based styling, so the two
    // chart shapes render identically apart from the axis furniture.
    var TICKS = 4;
    var scale = niceScale(Math.max.apply(null, [0].concat(vals)), TICKS);
    var niceMax = scale.max;
    var X0 = 44, X1 = 680, Y0 = 200, Y1 = 28;
    var plotW = X1 - X0, plotH = Y0 - Y1, n = vals.length, slot = plotW / n;
    var barW = Math.min(48, slot * 0.55);
    function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }
    var p = ['<svg viewBox="0 0 700 230" role="img" aria-label="Bar chart">'];
    for (var g = 0; g <= TICKS; g++) {
      var gy = Y0 - (plotH * g / TICKS);
      var gv = Math.round(scale.step * g * 1000) / 1000;
      p.push('<line class="rc-chart-grid" x1="' + X0 + '" y1="' + gy + '" x2="' + X1 + '" y2="' + gy + '"/>');
      p.push('<text class="rc-chart-axis" x="' + (X0 - 10) + '" y="' + (gy + 4) + '" text-anchor="end">' + gv + '</text>');
    }
    vals.forEach(function (v, i) {
      var cx = X0 + slot * (i + 0.5);
      var h = (v / niceMax) * plotH;
      var by = Y0 - h;
      var btip = labels[i] + ": " + v;
      p.push('<rect class="rc-chart-bar" data-tip="' + esc(btip) + '" aria-label="' + esc(btip) + '" style="--i:' + i + '" x="' + (cx - barW / 2).toFixed(1) + '" y="' + by.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="6"/>');
      p.push('<text class="rc-chart-val" x="' + cx.toFixed(1) + '" y="' + (by - 8).toFixed(1) + '" text-anchor="middle">' + v + '</text>');
      p.push('<text class="rc-chart-cat" x="' + cx.toFixed(1) + '" y="' + (Y0 + 22) + '" text-anchor="middle">' + esc(labels[i]) + '</text>');
    });
    p.push('</svg>');
    var wrap = el("div", "rc-chart");
    if (node.title) wrap.appendChild(el("div", "rc-chart-title", interp(String(node.title), ctx)));
    var holder = el("div", "rc-chart-svg");
    holder.innerHTML = p.join("");
    wrap.appendChild(holder);
    attachChartHover(wrap, holder);
    return wrap;
  };
  // ══ inline text formatting ════════════════════════════════════════════
  function inlineText(node, ctx) {
    var v = node.text != null ? node.text : (node.value != null ? node.value : node.content);
    return v == null ? "" : interp(String(v), ctx);
  }
  R.bold = function (n, d, c) { return el("strong", "rc-strong", inlineText(n, c)); };
  R.italic = function (n, d, c) { return el("em", "rc-italic", inlineText(n, c)); };
  R.underline = function (n, d, c) { return el("span", "rc-underline", inlineText(n, c)); };
  R.strikethrough = function (n, d, c) { return el("span", "rc-strike", inlineText(n, c)); };
  // `math` carries raw TeX. We render it as a labelled inline span and stash the
  // source in `data-tex` so an external typesetter (MathJax, already vendored)
  // can upgrade it in place. Never eval the TeX.
  R.math = function (n, d, c) {
    var v = inlineText(n, c);
    var display = n.display === true || n.display === "true" || n.block === true;
    return makeMathSpan(v, display);
  };

  // ══ media primitives ═══════════════════════════════════════════════════
  R.favicon = function (n) {
    var src = str(n.src || n.url || n.href);
    if (!src) return null;
    var i = document.createElement("img");
    i.className = "rc-favicon"; i.src = src; i.alt = ""; i.loading = "lazy";
    return i;
  };
  // Raw SVG: inlined ONLY when it is a genuine <svg> root with no <script> and
  // no inline event handler. Anything else degrades to a code block.
  var SVG_NS = "http://www.w3.org/2000/svg";
  // Rebuild an SVG element from a live node tree. Used when the subtree
  // carries `__expr__` coordinates and was therefore NOT flattened to markup
  // server-side — `hydrate()` resolves each attr expression against current
  // state here, so dragging a slider actually moves the drawing.
  // Raw-text containers: children are character data, never child elements.
  // `<style>` inside SVG is legitimate and load-bearing — a stylesheet written
  // with `class="peg"` selectors has no inline presentation attribute to fall
  // back on, so losing it renders the drawing in SVG defaults (black fills,
  // `stroke: none`). Treat the content as text and never recurse into it.
  var SVG_RAW_TEXT = { style: 1, script: 1 };

  // Distinguish a bare prose TEXT RUN from a real SVG <text> element. Both
  // arrive with `kind:"text"`, so the tag name alone cannot tell them apart —
  // a collision baked into the DSL by the SVG spec.
  //
  //   {kind:"text", text:"A (start)"}                          -> prose run
  //   {kind:"text", class:"lbl", x:"100", y:"176", text:"A"}   -> real element
  //
  // The discriminator is ATTRIBUTES, and it mirrors the parser's own
  // `_is_inline` test exactly (`set(nd.keys()) <= {"kind","text"}`): a bare run
  // carries nothing but kind/text (plus `__expr__` bookkeeping). Anything else
  // — a presentation attribute, a child — makes it an element.
  //
  // Checking `children` alone was wrong: `<text x='1'>label</text>` has its
  // content hoisted into `.text` with NO children, so every <text> label in
  // every diagram got demoted to a bare text node and vanished.
  function isSvgProseRun(h) {
    if (!h || typeof h !== "object") return false;
    if (h.kind === "__text__") return true;
    if (h.kind !== "text" && h.type !== "text") return false;
    for (var k in h) {
      if (k === "kind" || k === "type" || k === "text" || k === "props") continue;
      if (k.indexOf("__") === 0) continue;   // __expr__, __expr__text, ...
      return false;                          // real attribute or children
    }
    return true;
  }

  function buildSvgTree(node, ctx) {
    var h = hydrate(node, ctx);
    var kind = String(h.kind || h.type || "");
    if (isSvgProseRun(h)) {
      // A stylesheet's rule text, or any other bare character data. MUST be a
      // text node — wrapping it in <text> nests elements where only character
      // data is valid, and the whole sheet stops parsing.
      return document.createTextNode(String(h.text || ""));
    }
    var tag = kind.toLowerCase();
    if (!tag) return null;
    var e = document.createElementNS(SVG_NS, tag);
    for (var k in h) {
      if (k === "kind" || k === "type" || k === "children" || k === "text") continue;
      if (k.indexOf("__") === 0 || k === "props") continue;
      var v = h[k];
      if (v == null || typeof v === "object") continue;
      try { e.setAttribute(k, String(v)); } catch (err) {}
    }
    if (SVG_RAW_TEXT[tag]) {
      // Concatenate the raw characters verbatim. `textContent` on a <style> is
      // the one correct way to install a stylesheet via DOM, and it keeps CSS
      // out of the HTML parser's reach entirely.
      var buf = h.text != null ? String(h.text) : "";
      var rawKids = arr(h.children);
      for (var r = 0; r < rawKids.length; r++) {
        var rk = rawKids[r];
        if (rk && typeof rk === "object") buf += String(rk.text != null ? rk.text : "");
      }
      e.textContent = buf;
      return e;
    }
    if (h.text != null && h.text !== "") e.textContent = String(h.text);
    var kids = arr(h.children);
    for (var i = 0; i < kids.length; i++) {
      var c = buildSvgTree(kids[i], ctx);
      if (c) e.appendChild(c);
    }
    return e;
  }
  // ── SVG sanitizer (ALLOWLIST) ─────────────────────────────────────────
  // Replaces the old regex denylist. That denylist was bypassable: a `/`
  // separates attributes just like whitespace, so `<svg /onload=alert(1)>`
  // passed BOTH gates (`^\s*<svg[\s>]` saw the space; `\son\w+\s*=` never
  // saw a space before `on`) and then innerHTML fired the handler. Denylists
  // for SVG are a dead end. Instead: parse into a detached document, drop any
  // element not on the allowlist, strip every `on*` attr and any href/src that
  // is not a `#`-fragment. Nothing executes because we only touch the parsed
  // tree before it is ever inserted.
  var SVG_OK_ELEMS = {
    svg: 1, g: 1, defs: 1, symbol: 1, use: 1, title: 1, desc: 1,
    // `style` is allowed, but its TEXT content is checked separately in the
    // prune below — the allowlist alone would let a sheet reach out of the
    // sandbox. A <script> is never allowed through on either path.
    style: 1,
    path: 1, rect: 1, circle: 1, ellipse: 1, line: 1, polyline: 1, polygon: 1,
    text: 1, tspan: 1, textpath: 1,
    lineargradient: 1, radialgradient: 1, stop: 1,
    clippath: 1, mask: 1, marker: 1, pattern: 1, filter: 1,
    fegaussianblur: 1, feoffset: 1, feblend: 1, fecolormatrix: 1,
    femerge: 1, femergenode: 1, fecomponenttransfer: 1, fefuncr: 1,
    fefuncg: 1, fefuncb: 1, fefunca: 1, fecomposite: 1, fedropshadow: 1
  };
  function sanitizeSvgString(raw) {
    var root = null;
    var doc;
    try {
      doc = new DOMParser().parseFromString(raw, "image/svg+xml");
    } catch (e) { doc = null; }
    if (doc && doc.documentElement &&
        !doc.getElementsByTagName("parsererror").length &&
        String(doc.documentElement.nodeName).toLowerCase() === "svg") {
      root = doc.documentElement;
    } else {
      // Streaming case: the SVG is still being written, so it is NOT
      // well-formed XML yet. XML parsing rejects it outright, which used to
      // make a partial drawing vanish into a <pre> until the closing tag
      // landed. Fall back to a lenient HTML parse so the SVG renders
      // bit-by-bit as it streams. The allowlist prune below runs on the
      // result either way, so the security model is unchanged — a detached
      // div's innerHTML never runs scripts, and the prune strips them
      // regardless.
      try {
        var holder = document.createElement("div");
        holder.innerHTML = raw;
        root = holder.querySelector("svg");
      } catch (e2) { root = null; }
    }
    if (!root) return null;
    // External references a stylesheet can smuggle in. `url(...)` is the only
    // way CSS reaches the network, so every URL is dropped unless it is a
    // local `#fragment` (a filter/gradient) or an inline data: URI. @import is
    // killed outright — it is a stylesheet fetch in one line.
    function scrubCss(css) {
      var out = String(css || "");
      out = out.replace(/@import[^;]*;?/gi, "");
      out = out.replace(/url\(\s*(['"]?)(.*?)\1\s*\)/gi, function (m, q, u) {
        var t = String(u || "").trim();
        if (t.charAt(0) === "#" || /^data:/i.test(t)) return m;
        return "none";
      });
      // `javascript:` and expression() are legacy execution vectors in old
      // SVG/CSS parsers. Neither belongs in a generated diagram.
      out = out.replace(/expression\s*\(/gi, "none(");
      out = out.replace(/javascript:/gi, "");
      return out;
    }
    (function prune(node) {
      var kids = Array.prototype.slice.call(node.childNodes || []);
      for (var i = 0; i < kids.length; i++) {
        var ch = kids[i];
        if (ch.nodeType === 3 || ch.nodeType === 4) {
          // Text/CDATA is only meaningful under a raw-text element. The parent
          // was already vetted, so this is safe to leave in place.
          continue;
        }
        if (ch.nodeType !== 1) continue;
        var nm = String(ch.nodeName).toLowerCase();
        if (nm === "script") { ch.parentNode.removeChild(ch); continue; }
        if (!SVG_OK_ELEMS[nm]) { ch.parentNode.removeChild(ch); continue; }
        if (nm === "style") {
          // Content is the sheet itself — sanitize the CSS, then do NOT recurse
          // into it (its children are text nodes, not elements).
          ch.textContent = scrubCss(ch.textContent);
          continue;
        }
        var attrs = Array.prototype.slice.call(ch.attributes || []);
        for (var a = 0; a < attrs.length; a++) {
          var an = String(attrs[a].name).toLowerCase();
          var av = String(attrs[a].value || "");
          if (an.indexOf("on") === 0) { ch.removeAttribute(attrs[a].name); continue; }
          if (an === "href" || an === "xlink:href" || an === "src") {
            if (av.trim().charAt(0) !== "#") ch.removeAttribute(attrs[a].name);
            continue;
          }
          if (an === "style" && /url\s*\(/i.test(av) && !/url\s*\(\s*["']?data:/i.test(av)) {
            ch.removeAttribute(attrs[a].name);
          }
        }
        prune(ch);
      }
    })(root);
    return root;
  }

  // ── SVG healing ──────────────────────────────────────────────────────
  // A drawing that fails to parse used to degrade straight to a <pre> (or, at
  // the caller, get thrown away entirely because the container had already
  // been cleared). Before giving up, repair the breakages a streamed or
  // generated SVG actually hits: a wrapping code fence, a half-consumed
  // `markup="` prefix, double-encoded entities, a missing namespace, and
  // unbalanced tags left open by an interrupted stream.
  //
  // Best-effort: returns the input unchanged when it cannot improve it, so a
  // genuinely-fine drawing is never mangled by the healer.
  var _SVG_VOID_TAGS = {
    path: 1, rect: 1, circle: 1, ellipse: 1, line: 1, polyline: 1, polygon: 1,
    stop: 1, use: 1, image: 1, animate: 1, animatetransform: 1, animatemotion: 1
  };
  function _balanceSvgTags(s) {
    var stack = [];
    var out = s.replace(/<(\/?)([a-zA-Z][\w:-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(\/?)>/g,
      function (m, slash, name, attrs, selfclose) {
        var nm = String(name).toLowerCase();
        if (slash) {
          for (var i = stack.length - 1; i >= 0; i--) {
            if (stack[i] === nm) { stack.length = i; break; }
          }
          return m;
        }
        if (!selfclose && !_SVG_VOID_TAGS[nm]) stack.push(nm);
        return m;
      });
    if (stack.length) {
      for (var j = stack.length - 1; j >= 0; j--) out += "</" + stack[j] + ">";
    }
    return out;
  }
  function healSvgMarkup(raw) {
    var s = String(raw == null ? "" : raw);
    // A fence the model wrapped around the markup.
    s = s.replace(/^\s*```[a-zA-Z]*\s*/, "").replace(/\s*```\s*$/, "");
    // A truncated attribute split that left `markup="` (or a stray quote) on
    // the front / end of the payload.
    s = s.replace(/^\s*markup\s*=\s*["']?/i, "");
    s = s.replace(/["']\s*\/?>\s*$/, "");
    // Double-encoded entities — the tag was HTML-escaped somewhere in transit.
    s = s.replace(/&amp;lt;/g, "&lt;").replace(/&amp;gt;/g, "&gt;");
    s = s.replace(/&amp;quot;/g, "&quot;").replace(/&amp;amp;/g, "&amp;");
    // An XML prolog / doctype makes the lenient HTML fallback choke.
    s = s.replace(/^\s*<\?xml[^>]*\?>\s*/i, "");
    s = s.replace(/^\s*<!doctype[^>]*>\s*/i, "");
    s = s.trim();
    // A bare <svg> with no namespace renders in quirks (and some sanitizers
    // treat it as an unknown HTML element).
    if (/^<svg\b/i.test(s) && !/\sxmlns\s*=/.test(s)) {
      s = s.replace(/^<svg\b/i, '<svg xmlns="http://www.w3.org/2000/svg"');
    }
    // Close anything an interrupted stream left open.
    if (/^<svg\b/i.test(s)) s = _balanceSvgTags(s);
    return s;
  }

  R.svg = function (n, depth, ctx) {
    var raw = str(n.svg || n.content || n.text || n.markup);
    var d = el("div", "rc-svg");
    // Accept a fence / prolog / doctype in front of the real <svg> root.
    if (raw && /^\s*(?:```[a-zA-Z]*\s*)?(?:<\?xml[^>]*\?>\s*)?(?:<!doctype[^>]*>\s*)?<svg[\s>]/i.test(raw)) {
      var clean = sanitizeSvgString(raw);
      if (!clean) clean = sanitizeSvgString(healSvgMarkup(raw));
      if (clean) { d.appendChild(clean); return d; }
    }
    // No frozen markup but a child tree -> dynamic geometry. Build real SVG
    // DOM so `__expr__` attrs re-evaluate on every render pass.
    if (arr(n.children).length) {
      var live = buildSvgTree(n, ctx || noopCtx());
      if (live) { d.appendChild(live); return d; }
    }
    // Healing fallback: if there is ANY raw text, try one repaired pass before
    // degrading. A drawing that renders is always better than a <pre>.
    if (raw) {
      var healed = healSvgMarkup(raw);
      if (healed && healed !== raw) {
        var c2 = sanitizeSvgString(healed);
        if (c2) { d.appendChild(c2); return d; }
      }
      d.appendChild(el("pre", "rc-code", raw.slice(0, 4000)));
    } else return null;
    return d;
  };
  R.asyncimage = function (n, d, c) {
    var src = str(n.src || n.url || n.image || n.thumbnail);
    // No URL yet but a `query` present -> hand to R.image, which draws the
    // placeholder and resolves it asynchronously. Bailing here (the old
    // behaviour) dropped the whole node and left a hole in the card.
    if (!src) {
      if (str(n.query) || n.pending) return R.image(n, d, c);
      return null;
    }
    return R.image({ src: src, caption: n.caption || n.alt, alt: n.alt }, d, c);
  };
  R.asyncimagegroup = function (n, d, c) {
    var items = arr(n.images || n.items || n.srcs);
    // A group whose only payload is a query is a single pending image, not an
    // empty one — route it through R.image so it still renders + resolves.
    if (!items.length) {
      if (str(n.query) || n.pending) return R.image(n, d, c);
      return null;
    }
    // Normalise to plain src strings, then hand off to R.image so the
    // fallback chain + uniform sizing stay in ONE place.
    var srcs = items.map(function (it) {
      return typeof it === "object" ? str(it.src || it.url || it.image || it.thumbnail) : str(it);
    });
    return R.image({ images: srcs, caption: n.caption || n.alt, alt: n.alt }, d, c);
  };
  R.asyncvideo = function (n) {
    // Delegate to R.video — it owns the wrapper/player split and the error
    // handler. Setting `rc-video` on the <video> itself was wrong: after the
    // CSS split that class is the *wrapper*, so the player got no sizing rules
    // and spilled over the card title.
    return R.video({ src: n.src || n.url || n.video, poster: n.poster, caption: n.caption });
  };

  // ══ layout: collections, navigation, tables ════════════════════════════
  R.flowitem = function (n, d, c) {
    var row = el("div", "rc-flow-node");
    var idx = n.index != null ? n.index : (n.step != null ? n.step : null);
    if (idx != null) row.appendChild(el("span", "rc-flow-idx", String(idx)));
    var body = el("div", "rc-flow-body");
    var title = n.title != null ? n.title : null;
    var text = n.text != null ? n.text : (n.label != null ? n.label : null);
    if (title != null) {
      body.appendChild(richLine("rc-flow-title", title, c));
      if (text != null) body.appendChild(richLine("rc-flow-text", text, c));
    } else if (text != null) {
      body.appendChild(richLine("rc-flow-text", text, c));
    } else {
      renderChildren(n, d, body, c);
    }
    if (body.childNodes.length) row.appendChild(body);
    if (n.status) row.setAttribute("data-state", String(n.status));
    return row.childNodes.length ? row : null;
  };
  R.listitem = function (n, d, c) {
    var li = el("li", "rc-li");
    if (n.text != null) li.textContent = interp(String(n.text), c);
    else if (n.value != null) li.textContent = interp(String(n.value), c);
    else renderChildren(n, d, li, c);
    return li;
  };
  R.pressable = function (n, d, c) {
    var b = el("div", "rc-pressable");
    b.setAttribute("role", "button");
    b.tabIndex = 0;
    var binding = bindingOf(n);
    if (binding) bindClick(b, binding, c);
    var t = n.text != null ? n.text : (n.label != null ? n.label : null);
    if (t != null) b.appendChild(el("span", null, interp(String(t), c)));
    else renderChildren(n, d, b, c);
    return b.childNodes.length ? b : null;
  };
  R.carousel = function (n, d, c) {
    var wrap = el("div", "rc-carousel");
    renderChildren(n, d, wrap, c);
    return wrap.childNodes.length ? wrap : null;
  };
  R.carouselitem = function (n, d, c) {
    var it = el("div", "rc-carousel-item");
    if (n.title) it.appendChild(el("div", "rc-box-title", interp(String(n.title), c)));
    renderChildren(n, d, it, c);
    // A text-only slide (`<carousel-item><text>…</text></carousel-item>` hoists
    // to `text`) has no children, so fall back to the prop.
    if (!it.childNodes.length) {
      var t = n.text != null ? n.text : (n.value != null ? n.value : null);
      if (t != null) it.appendChild(richDiv("rc-text", interp(String(t), c)));
    }
    return it.childNodes.length ? it : null;
  };
  R.carouselmediaitem = function (n, d, c) {
    var it = el("div", "rc-carousel-item rc-carousel-media");
    var img = R.asyncimage(n, d, c);
    if (img) it.appendChild(img);
    renderChildren(n, d, it, c);
    return it.childNodes.length ? it : null;
  };
  // popover -> native <details>/<summary>, so it works with zero JS.
  R.popover = function (n, d, c) {
    var det = document.createElement("details");
    det.className = "rc-popover";
    var sum = document.createElement("summary");
    sum.className = "rc-popover-trigger";
    sum.textContent = interp(str(n.label || n.title || n.text, "Details"), c);
    det.appendChild(sum);
    var content = el("div", "rc-popover-content");
    renderChildren(n, d, content, c);
    det.appendChild(content);
    return det;
  };
  R.popovertrigger = function (n, d, c) {
    return el("span", "rc-popover-trigger", interp(str(n.text || n.label), c));
  };
  R.popovercontent = function (n, d, c) {
    var p = el("div", "rc-popover-content");
    renderChildren(n, d, p, c);
    return p.childNodes.length ? p : null;
  };
  R.tablesection = function (n, d, c) {
    var t = el("tbody", "rc-tbody");
    renderChildren(n, d, t, c);
    return t.childNodes.length ? t : null;
  };
  R.tablerow = function (n, d, c) {
    var tr = el("tr", "rc-tr");
    renderChildren(n, d, tr, c);
    return tr.childNodes.length ? tr : null;
  };
  R.tablecell = function (n, d, c) {
    var isHead = n.header === true || n.head === true || String(n.as || "") === "header";
    var td = el(isHead ? "th" : "td", "rc-td");
    var t = n.text != null ? n.text : (n.value != null ? n.value : null);
    if (t != null) td.textContent = interp(String(t), c);
    else renderChildren(n, d, td, c);
    return td;
  };

  // ══ inputs ═════════════════════════════════════════════════════════════
  function stateKeyOfNode(n) { return str(n.stateKey || n.state || n.bind || n.name, ""); }
  function fieldLabel(n, c) {
    return n.label ? el("span", "rc-field-label", interp(String(n.label), c)) : null;
  }
  R.input = function (n, d, c) {
    var key = stateKeyOfNode(n);
    var wrap = el("label", "rc-field");
    var lb = fieldLabel(n, c); if (lb) wrap.appendChild(lb);
    var i = document.createElement("input");
    i.className = "rc-input";
    i.type = str(n.inputType || n.htmlType || "text");
    if (n.placeholder) i.placeholder = interp(String(n.placeholder), c);
    if (n.value != null) i.value = String(n.value);
    else if (key && c && c.state && c.state[key] != null) i.value = String(c.state[key]);
    if (key && c) i.addEventListener("input", function () { c.set(key, i.value); });
    wrap.appendChild(i);
    return wrap;
  };
  R.textarea = function (n, d, c) {
    var key = stateKeyOfNode(n);
    var wrap = el("label", "rc-field");
    var lb = fieldLabel(n, c); if (lb) wrap.appendChild(lb);
    var t = document.createElement("textarea");
    t.className = "rc-textarea";
    t.rows = num(n.rows, 4, 1, 30);
    if (n.placeholder) t.placeholder = interp(String(n.placeholder), c);
    if (n.value != null) t.value = String(n.value);
    else if (key && c && c.state && c.state[key] != null) t.value = String(c.state[key]);
    if (key && c) t.addEventListener("input", function () { c.set(key, t.value); });
    wrap.appendChild(t);
    return wrap;
  };
  R.form = function (n, d, c) {
    var f = el("div", "rc-form");
    if (n.title) f.appendChild(el("div", "rc-form-title", interp(String(n.title), c)));
    renderChildren(n, d, f, c);
    return f.childNodes.length ? f : null;
  };
  R.radiogroup = function (n, d, c) {
    var key = stateKeyOfNode(n);
    var opts = arr(n.options || n.items);
    if (!opts.length) return null;
    var wrap = el("div", "rc-radiogroup");
    var lb = fieldLabel(n, c); if (lb) wrap.appendChild(lb);
    var cur = (key && c && c.state) ? c.state[key] : (n.value != null ? n.value : undefined);
    opts.forEach(function (o, i) {
      var val = typeof o === "object" ? (o.value != null ? o.value : i) : o;
      var lab = typeof o === "object" ? (o.label != null ? o.label : o.value) : o;
      var row = el("label", "rc-radio");
      var r = document.createElement("input");
      r.type = "radio"; r.name = key || "rc-radio"; r.value = String(val);
      if (String(cur) === String(val)) r.checked = true;
      if (key && c) r.addEventListener("change", function () { c.set(key, val); });
      row.appendChild(r);
      row.appendChild(el("span", null, interp(String(lab), c)));
      wrap.appendChild(row);
    });
    return wrap;
  };
  R.radio = function (n, d, c) {
    var key = stateKeyOfNode(n);
    var row = el("label", "rc-radio");
    var r = document.createElement("input");
    r.type = "radio";
    r.value = String(n.value != null ? n.value : (n.text || ""));
    if (key && c && c.state && String(c.state[key]) === r.value) r.checked = true;
    if (key && c) r.addEventListener("change", function () { c.set(key, n.value != null ? n.value : (n.text || "")); });
    row.appendChild(r);
    row.appendChild(el("span", null, interp(str(n.label || n.text), c)));
    return row;
  };
  R.slider = function (n, d, c) {
    var key = stateKeyOfNode(n);
    var wrap = el("label", "rc-field rc-slider");
    var head = el("div", "rc-slider-head");
    head.appendChild(el("span", null, interp(str(n.label, "Value"), c)));
    var out = el("span", "rc-slider-val", "");
    head.appendChild(out);
    wrap.appendChild(head);
    var s = document.createElement("input");
    s.type = "range";
    s.min = String(num(n.min, 0));
    s.max = String(num(n.max, 100, 0, 1e9));
    s.step = String(num(n.step, 1, 0.0001, 1e9));
    s.value = String(n.value != null ? n.value : num(n.min, 0));
    if (key && c && c.state && c.state[key] != null) s.value = String(c.state[key]);
    out.textContent = s.value;
    s.addEventListener("input", function () {
      out.textContent = s.value;
      if (key && c) c.set(key, Number(s.value));
    });
    wrap.appendChild(s);
    return wrap;
  };
  R.datepicker = function (n, d, c) {
    var key = stateKeyOfNode(n);
    var wrap = el("label", "rc-field");
    var lb = fieldLabel(n, c); if (lb) wrap.appendChild(lb);
    var i = document.createElement("input");
    i.type = "date"; i.className = "rc-input";
    if (n.value != null) i.value = String(n.value);
    else if (key && c && c.state && c.state[key] != null) i.value = String(c.state[key]);
    if (key && c) i.addEventListener("change", function () { c.set(key, i.value); });
    wrap.appendChild(i);
    return wrap;
  };

  // ══ data / citations / links ═══════════════════════════════════════════
  function citePill(node, ctx, cls, label) {
    var v = interp(str(label), ctx);
    if (!v) return null;
    var href = str(node.url || node.href || node.src);
    if (href && /^(https?:|mailto:)/i.test(href)) {
      var a = document.createElement("a");
      a.className = "rc-cite " + (cls || "");
      a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.textContent = v;
      return a;
    }
    return el("span", "rc-cite " + (cls || ""), v);
  }
  R.cite = function (n, d, c) { return citePill(n, c, "rc-cite-web", n.text || n.label || n.title || n.url); };
  R.codecite = function (n, d, c) { return citePill(n, c, "rc-cite-code", n.text || n.label || n.title); };
  R.filecite = function (n, d, c) { return citePill(n, c, "rc-cite-file", n.text || n.label || n.name || n.title); };
  R.memorycite = function (n, d, c) { return citePill(n, c, "rc-cite-mem", n.text || n.label || n.title); };
  R.entity = function (n, d, c) {
    var v = interp(str(n.text || n.label || n.name), c);
    if (!v) return null;
    var e = el("span", "rc-entity", v);
    var href = str(n.url || n.href);
    if (href && /^https?:/i.test(href)) {
      e = document.createElement("a");
      e.className = "rc-entity rc-link";
      e.href = href; e.target = "_blank"; e.rel = "noopener noreferrer";
      e.textContent = v;
    }
    return e;
  };
  R.filenavlist = function (n, d, c) {
    var items = arr(n.files || n.items || n.children);
    if (!items.length) return null;
    var wrap = el("div", "rc-filenav");
    items.forEach(function (it) {
      var name = typeof it === "object" ? str(it.name || it.text || it.label || it.path) : str(it);
      if (!name) return;
      var row = el("div", "rc-filenav-item");
      row.appendChild(el("span", "rc-filenav-dot"));
      row.appendChild(el("span", null, interp(name, c)));
      wrap.appendChild(row);
    });
    return wrap.childNodes.length ? wrap : null;
  };
  R.followup = function (n, d, c) {
    var items = arr(n.suggestions || n.items || n.options || n.children);
    if (!items.length) return null;
    var wrap = el("div", "rc-followups");
    items.forEach(function (it) {
      var label = typeof it === "object" ? str(it.text || it.label) : str(it);
      if (!label) return;
      var b = el("button", "rc-followup", interp(label, c));
      b.type = "button";
      b.addEventListener("click", function () {
        if (window.SableInteract && window.SableInteract.send) window.SableInteract.send(label);
      });
      wrap.appendChild(b);
    });
    return wrap.childNodes.length ? wrap : null;
  };

  // ══ raw-content blocks ═════════════════════════════════════════════════
  R.appblock = function (n, d, c) {
    var wrap = el("div", "rc-appblock");
    if (n.title) wrap.appendChild(el("div", "rc-appblock-title", interp(String(n.title), c)));
    renderChildren(n, d, wrap, c);
    return wrap.childNodes.length ? wrap : null;
  };
  R.writingblock = function (n, d, c) {
    var wrap = el("div", "rc-writing");
    if (n.title) wrap.appendChild(el("div", "rc-writing-title", interp(String(n.title), c)));
    if (n.text != null) wrap.appendChild(richDiv("rc-text", interp(String(n.text), c)));
    else renderChildren(n, d, wrap, c);
    return wrap.childNodes.length ? wrap : null;
  };

  // ══ visualization ══════════════════════════════════════════════════════
  R.mapwidgetv2 = function (n, d, c) {
    var wrap = el("div", "rc-map");
    wrap.appendChild(el("div", "rc-map-title", interp(str(n.label || n.title || n.name, "Map"), c)));
    var lat = n.lat != null ? n.lat : (n.latitude != null ? n.latitude : null);
    var lng = n.lng != null ? n.lng : (n.longitude != null ? n.longitude : null);
    if (lat != null && lng != null) {
      wrap.appendChild(el("div", "rc-map-coords", lat + ", " + lng));
      var a = document.createElement("a");
      a.className = "rc-link";
      a.href = "https://www.openstreetmap.org/?mlat=" + encodeURIComponent(lat) + "&mlon=" + encodeURIComponent(lng);
      a.target = "_blank"; a.rel = "noopener noreferrer";
      a.textContent = "Open map";
      wrap.appendChild(a);
    } else {
      wrap.appendChild(el("div", "rc-map-empty", "No coordinates provided."));
    }
    return wrap;
  };

  R.divider = function () { return el("hr", "rc-divider"); };
  R.spacer = function (node) { var d = el("div", "rc-spacer"); d.style.height = num(node.size, 12, 0, 120) + "px"; return d; };

  R.kv = function (node, depth, ctx) {
    var pairs = (node.pairs && typeof node.pairs === "object") ? node.pairs : null;
    if (!pairs) return null;
    var wrap = el("div", "rc-kv-wrap");
    if (node.title) wrap.appendChild(el("div", "rc-kv-title", interp(String(node.title), ctx)));
    var list = el("div", "rc-kv");
    Object.keys(pairs).forEach(function (k) {
      var row = el("div", "rc-kv-row");
      row.appendChild(el("div", "rc-kv-key", k));
      row.appendChild(el("div", "rc-kv-val", interp(String(pairs[k]), ctx)));
      list.appendChild(row);
    });
    wrap.appendChild(list);
    return wrap;
  };
  R.code = function (node, depth, ctx) {
    // `language` is optional; a fence-less <code> just omits the class.
    var lang = str(node.language || node.lang || node.syntax);
    var v = interp(str(node.text != null ? node.text : node.value), ctx);
    if (!v) {
      // Children form: <code><text>…</text></code> hoists into `text` upstream,
      // but a bare text child should still work.
      var cw = el("pre", "rc-code");
      var cd = el("code", null);
      renderChildren(node, depth, cd, ctx);
      if (cd.childNodes.length) { cw.appendChild(cd); return cw; }
      return null;
    }
    var pre = el("pre", "rc-code");
    var code = el("code", lang ? "language-" + lang : null, v);
    pre.appendChild(code);
    return pre;
  };
  R.quote = function (node, depth, ctx) {
    var v = interp(str(node.text), ctx);
    if (!v) return null;
    var d = el("blockquote", "rc-quote");
    d.appendChild(el("p", null, v));
    if (node.cite) d.appendChild(el("cite", "rc-quote-cite", interp(String(node.cite), ctx)));
    return d;
  };
  R.list = function (node, depth, ctx) {
    var d = el(node.ordered ? "ol" : "ul", "rc-list");
    var kids = arr(node.children);
    if (kids.length) {
      kids.forEach(function (c) {
        // The JSON schema allows bare strings as list items; renderNode() only
        // handles objects, so wrap them in an <li> here.
        if (c == null) return;
        if (typeof c !== "object") {
          var li = el("li");
          li.textContent = interp(String(c), ctx);
          d.appendChild(li);
          return;
        }
        var r = renderNode(c, depth + 1, ctx);
        if (r) d.appendChild(r.tagName === "LI" ? r : (function () { var w = el("li"); w.appendChild(r); return w; })());
      });
      return d.childNodes.length ? d : null;
    }
    arr(node.items).forEach(function (it) {
      var li = el("li");
      if (it && typeof it === "object") {
        if (it.text) li.appendChild(el("span", "rc-li-text", interp(String(it.text), ctx)));
        if (it.sub) li.appendChild(el("span", "rc-li-sub", interp(String(it.sub), ctx)));
      } else li.textContent = interp(str(it), ctx);
      if (li.childNodes.length) d.appendChild(li);
    });
    return d.childNodes.length ? d : null;
  };
  R.callout = function (node, depth, ctx) {
    var d = el("div", "rc-callout");
    d.dataset.tone = tone(node.tone, "note");
    if (node.title) d.appendChild(el("div", "rc-callout-title", interp(String(node.title), ctx)));
    var body = node.body != null ? node.body : (node.text != null ? node.text : node.content);
    if (body) d.appendChild(el("div", "rc-callout-body", interp(String(body), ctx)));
    return d;
  };
  R.table = function (node, depth, ctx) {
    var cols = arr(node.columns), rows = arr(node.rows);
    var kids = arr(node.children);
    // Children form: <table><table-row><table-cell>…</table-cell></table-row></table>.
    // Previously ONLY the columns/rows props were read, so a child-built table
    // hit `return null` and rendered as nothing at all.
    if (!cols.length && !rows.length && !kids.length) return null;
    var wrap = el("div", "rc-table-wrap");
    var t = el("table", "rc-table");
    if (cols.length) {
      var th = el("thead"), tr = el("tr");
      cols.forEach(function (c) { tr.appendChild(el("th", null, interp(String(c), ctx))); });
      th.appendChild(tr); t.appendChild(th);
    }
    var tb = el("tbody");
    if (kids.length) {
      kids.forEach(function (kid) {
        var k = kindOf(kid || {});
        var r = renderNode(kid, depth + 1, ctx);
        if (!r) return;
        // A <table-row> already renders to a <tr>; a bare <table-cell> row
        // gets wrapped so a headerless one-liner still forms a valid row.
        if (k === "tablerow") tb.appendChild(r);
        else {
          var tr2 = el("tr");
          tr2.appendChild(k === "tablecell" ? r : el("td", null, ""));
          tb.appendChild(tr2);
        }
      });
    } else {
      rows.forEach(function (r) { var tr = el("tr"); arr(r).forEach(function (c) { tr.appendChild(el("td", null, interp(String(c), ctx))); }); tb.appendChild(tr); });
    }
    t.appendChild(tb); wrap.appendChild(t);
    return wrap;
  };
  R.progress = function (node, depth, ctx) {
    var max = num(node.max, 100, 1);
    var val = num(typeof node.value === "string" ? interp(node.value, ctx) : node.value, 0, 0, max);
    var pct = Math.round((val / max) * 100);
    var wrap = el("div", "rc-progress");
    var head = el("div", "rc-progress-head");
    if (node.label) head.appendChild(el("span", "rc-progress-label", interp(String(node.label), ctx)));
    head.appendChild(el("span", "rc-progress-val", pct + "%"));
    wrap.appendChild(head);
    var track = el("div", "rc-progress-track"), fill = el("div", "rc-progress-fill");
    fill.style.width = pct + "%";
    if (node.tone) fill.dataset.tone = tone(node.tone, "accent");
    track.appendChild(fill); wrap.appendChild(track);
    return wrap;
  };
  R.timeline = function (node, depth, ctx) {
    var items = kidsOf(node, ["items"]);
    if (!items.length) return null;
    var wrap = el("div", "rc-timeline");
    items.forEach(function (it) {
      var row = el("div", "rc-tl-item");
      var dot = el("div", "rc-tl-dot"); dot.dataset.tone = tone(it && it.tone, "accent"); row.appendChild(dot);
      var body = el("div", "rc-tl-body");
      if (it && it.title) body.appendChild(el("div", "rc-tl-title", interp(String(it.title), ctx)));
      if (it && it.text) body.appendChild(el("div", "rc-tl-text", interp(String(it.text), ctx)));
      row.appendChild(body); wrap.appendChild(row);
    });
    return wrap;
  };
  R.flow = function (node, depth, ctx) {
    var wrap = el("div", "rc-flow");
    // Three shapes are accepted: child <flow-item> elements, a `steps` array,
    // or an `items` array. Each node renders as a rail-connected row — badge
    // column + body (optional title, then text).
    function buildNode(o, i) {
      var n = el("div", "rc-flow-node");
      var idx = o && o.index != null ? o.index : i + 1;
      n.appendChild(el("span", "rc-flow-idx", String(idx)));
      var body = el("div", "rc-flow-body");
      var title = o && o.title != null ? o.title : null;
      var text = o ? (o.text != null ? o.text : (o.label != null ? o.label : null)) : null;
      if (title != null) {
        body.appendChild(richLine("rc-flow-title", title, ctx));
        if (text != null) body.appendChild(richLine("rc-flow-text", text, ctx));
      } else if (text != null) {
        body.appendChild(richLine("rc-flow-text", text, ctx));
      }
      if (body.childNodes.length) n.appendChild(body);
      if (o && o.status) n.setAttribute("data-state", String(o.status));
      return n;
    }
    var kids = arr(node.children);
    if (kids.length) {
      kids.forEach(function (c, i) {
        if (c && typeof c === "object" && kindOf(c) === "flowitem") {
          if (c.index == null) c.index = i + 1;
          var r = renderNode(c, depth + 1, ctx);
          if (r) wrap.appendChild(r);
        } else if (c != null) {
          wrap.appendChild(buildNode({ text: c }, i));
        }
      });
    } else {
      var steps = arr(node.steps || node.items);
      steps.forEach(function (s, i) {
        wrap.appendChild(buildNode((s && typeof s === "object") ? s : { text: s }, i));
      });
    }
    // Optional progress: <flow current="3"> dims everything before step 3 and
    // accents it. A per-node status="done|current" overrides this.
    var cur = node.current != null ? num(node.current, -1, -1, 1e9) : -1;
    if (cur >= 0) {
      Array.prototype.forEach.call(wrap.children, function (child, i) {
        if (!child.classList || !child.classList.contains("rc-flow-node")) return;
        if (child.getAttribute("data-state")) return;
        if (i + 1 < cur) child.setAttribute("data-state", "done");
        else if (i + 1 === cur) child.setAttribute("data-state", "current");
      });
    }
    return wrap.childNodes.length ? wrap : null;
  };
  // ── pending image (async, non-blocking) ───────────────────────────────
  // A `query`-based image arrives unresolved (`pending: true`): the server
  // refuses to block the chat stream on a SearXNG round-trip, so it ships the
  // query and the browser fetches it here. ALL pending queries fire at once and
  // resolve independently — one slow search never holds up the card, let alone
  // the transcript. A placeholder occupies the slot until the URLs land.
  var _imgInflight = Object.create(null);   // query|count -> promise

  function _placeholder(node) {
    var wrap = el("div", "rc-image rc-image-pending");
    var n = num(node.count, 1, 1, 8);
    var holder = n > 1 ? el("div", "rc-imggrid") : wrap;
    if (n > 1) wrap.appendChild(holder);
    for (var i = 0; i < n; i++) {
      var ph = el("div", "rc-image-ph");
      ph.setAttribute("aria-busy", "true");
      ph.appendChild(el("span", "rc-image-ph-dot", ""));
      holder.appendChild(ph);
    }
    if (node.caption) wrap.appendChild(el("div", "rc-cap", String(node.caption)));
    return wrap;
  }

  function _fetchImages(query, count) {
    var key = query + "|" + count;
    if (_imgInflight[key]) return _imgInflight[key];
    var url = "/api/search/image?q=" + encodeURIComponent(query) + "&count=" + encodeURIComponent(count);
    var p = fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (data) {
        delete _imgInflight[key];   // let a later retry re-ask
        return data;
      });
    _imgInflight[key] = p;
    return p;
  }

  function _pendingImage(node) {
    var wrap = _placeholder(node);
    var query = str(node.query);
    var count = num(node.count, 1, 1, 8);
    if (!query) return wrap;
    _fetchImages(query, count).then(function (data) {
      if (!wrap.isConnected) return;   // card was torn down / re-rendered
      var imgs = data && (data.images || data.results);
      if (!imgs || !imgs.length) {
        // Nothing found — drop the placeholders so the grid closes up.
        wrap.remove();
        return;
      }
      var resolved = R.image({
        images: imgs.slice(0, count),
        pool: data.pool || imgs,
        caption: node.caption,
        alt: node.alt
      });
      if (resolved && wrap.parentNode) wrap.parentNode.replaceChild(resolved, wrap);
    });
    return wrap;
  }

  R.image = function (node) {
    // Two modes:
    //   pending — `query` set, no URLs yet. Draw a placeholder and resolve
    //             asynchronously (never blocks the stream).
    //   ready   — server-resolved `images` (+ `pool` of spares). Show `images`;
    //             when one dies, swap in the next unused entry from `pool`.
    //             Only when the pool is exhausted do we drop the tile, so the
    //             grid reflows and we never leave a hole or a broken-image box.
    if (node.pending && str(node.query)) return _pendingImage(node);

    var list = arr(node.images);
    var srcs = list.length ? list.map(function (i) { return str(i && (i.image || i.thumbnail)) || str(i); }) : [];
    var single = str(node.src);
    if (!srcs.length && single) srcs = [single];
    srcs = srcs.filter(Boolean);
    if (!srcs.length) {
      // Unresolved query that never got flagged — treat it as pending rather
      // than dropping the node, so the card keeps its slot and fills in.
      if (str(node.query)) return _pendingImage(node);
      return null;
    }

    // Ordered reserve: every candidate the server returned for this query.
    var pool = arr(node.pool).map(function (i) {
      return str(i && (i.image || i.thumbnail)) || str(i);
    }).filter(Boolean);
    if (!pool.length) pool = srcs.slice();

    var wrap = el("div", "rc-image");
    var multi = srcs.length > 1;
    // Caption MUST live outside the grid — otherwise `display: grid`
    // turns it into an extra cell (that was the phantom space on the right).
    var holder = multi ? el("div", "rc-imggrid") : wrap;
    if (multi) wrap.appendChild(holder);

    // Track which pool URLs are already claimed so two tiles never fight over
    // the same fallback.
    var used = Object.create(null);
    srcs.forEach(function (s) { used[s] = true; });

    srcs.forEach(function (src, _idx) {
      var img = document.createElement("img");
      img.className = "rc-image-el";
      img.alt = str(node.caption || node.alt, "");
      img.loading = "lazy";
      img.decoding = "async";
      // Click to open in Sable's in-app lightbox. Pass the whole live set +
      // this tile's index so multi-image grids let you arrow between them.
      img.style.cursor = "zoom-in";
      img.addEventListener("click", function () {
        if (typeof window.openLightbox !== "function") return;
        // Read the CURRENT src of every tile (fallback pool may have swapped
        // a dead URL for a fresh one) so the viewer shows what's on screen.
        var live = [].slice.call(holder.querySelectorAll(".rc-image-el"))
          .map(function (el) { return el.currentSrc || el.src; })
          .filter(Boolean);
        var start = live.indexOf(this.currentSrc || this.src);
        window.openLightbox(live.length ? live : [src], start < 0 ? _idx : start);
      });
      // Tag vector / animated assets so CSS can letterbox them instead of
      // cropping. An SVG with only a viewBox has no intrinsic size — cover-
      // fitting it into a fixed box is what collapsed the schematic to nothing.
      if (/^data:image\/svg\+xml/i.test(src) || /\.svg(\?|#|$)/i.test(src)) img.dataset.kind = "svg";
      else if (/^data:image\/gif/i.test(src) || /\.gif(\?|#|$)/i.test(src)) img.dataset.kind = "gif";
      img.addEventListener("error", function () {
        // Pull the next unused URL from the same query's pool.
        var next = null;
        for (var i = 0; i < pool.length; i++) {
          if (!used[pool[i]]) { next = pool[i]; break; }
        }
        if (next) {
          used[next] = true;
          img.src = next;
          return;
        }
        // Pool exhausted — drop this tile so the grid closes up cleanly.
        img.remove();
        if (holder !== wrap && !holder.childNodes.length) holder.remove();
      });
      img.src = src;
      holder.appendChild(img);
    });

    if (node.caption) wrap.appendChild(el("div", "rc-cap", String(node.caption)));
    return wrap;
  };
  R.video = function (node) {
    var src = str(node.src || node.url);
    if (!src) return null;
    var wrap = el("div", "rc-video");
    var v = document.createElement("video");
    v.className = "rc-video-el";
    v.controls = node.controls !== false;
    v.preload = str(node.preload, "metadata");   // don't pull the whole file
    v.playsInline = true;
    if (node.poster) v.poster = str(node.poster);
    // Dead source -> drop the player instead of leaving a black box behind.
    v.addEventListener("error", function () {
      if (wrap.parentNode) wrap.remove();
    }, { once: true });
    v.src = src;
    wrap.appendChild(v);
    if (node.caption) wrap.appendChild(el("div", "rc-cap", String(node.caption)));
    return wrap;
  };
  R.file = function (node) {
    var src = str(node.src || node.href);
    var name = str(node.name || node.label || node.text || node.path, "file");
    if (!src && !node.path) return null;
    var a = document.createElement("a");
    a.className = "rc-file";
    a.textContent = name;
    if (src) { a.href = src; a.download = name; }
    else a.href = "#";
    return a;
  };
  R.link = function (node, depth, ctx) {
    var href = str(node.href || node.src);
    var label = interp(str(node.label || node.text || href), ctx);
    if (!href || !/^(https?:|mailto:)/i.test(href)) return el("span", "rc-link rc-link-inert", label);
    var a = el("a", "rc-link", label);
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
    return a;
  };

  R.segmented = function (node, depth, ctx) {
    var wrap = el("div", "rc-segmented");
    if (node.block) wrap.dataset.block = "1";
    var opts = optArr(node.options || node.children);
    var key = node.state || node.bind || stateKeyOf(node.onChange) || stateKeyOf(node.onClick);
    var cur = node.value != null ? node.value : getPath(key || "", ctx);
    opts.forEach(function (o) {
      var val = typeof o === "object" ? o.value : o;
      var lab = typeof o === "object" ? (o.label != null ? o.label : o.value) : o;
      var b = el("button", "rc-seg-btn" + (val === cur ? " rc-seg-on" : ""), lab);
      b.type = "button";
      if (key && ctx) b.addEventListener("click", function () { ctx.set(key, val); });
      wrap.appendChild(b);
    });
    return wrap;
  };
  R.button = function (node, depth, ctx) {
    var v = interp(str(node.label || node.text), ctx);
    if (!v) return null;
    var binding = bindingOf(node);
    var hasAction = !!binding || node.action || node.send != null;
    var b = el("button", "rc-button", v);
    b.type = "button";
    b.dataset.tone = tone(node.tone || node.color, "accent");
    if (node.block) b.dataset.block = "1";
    // A run of buttons is usually a MODE SWITCHER, and a switcher has to show
    // where it currently is. `active` may be a literal or an expression
    // (`active={mode==="chaos"}`), which hydrate() has already resolved.
    if (node.active || node.selected || node.current) b.dataset.active = "1";
    if (node.active || node.selected) b.setAttribute("aria-pressed", "true");
    if (node.icon) b.insertBefore(renderIcon({ name: node.icon, size: "sm" }), b.firstChild);
    if (binding) {
      bindClick(b, binding, ctx);
    } else if (node.send != null) {
      b.classList.add("rc-clickable");
      b.addEventListener("click", function (e) {
        e.preventDefault();
        if (window.SableInteract && window.SableInteract.send) window.SableInteract.send(interp(String(node.send), ctx));
      });
    }
    return b;
  };
  R.check = function (node, depth, ctx) {
    var wrap = el("div", "rc-checklist");
    if (node.title) wrap.appendChild(el("div", "rc-choices-title", interp(String(node.title), ctx)));
    kidsOf(node, ["items"]).forEach(function (it) {
      var row = el("div", "rc-check-row" + (it && it.done ? " rc-done" : ""));
      row.appendChild(el("span", "rc-check-box"));
      row.appendChild(el("span", "rc-check-text", interp(str((it && (it.text || it.label)) || it), ctx)));
      wrap.appendChild(row);
    });
    return wrap.childNodes.length ? wrap : null;
  };
  R.toggle = function (node, depth, ctx) {
    var wrap = el("div", "rc-toggle");
    if (node.title) wrap.appendChild(el("div", "rc-toggle-title", interp(String(node.title), ctx)));
    var body = el("div", "rc-toggle-body");
    renderChildren(node, depth, body, ctx);
    wrap.appendChild(body);
    return wrap;
  };

  // ── quiz: a stepper over several <choices> ────────────────────────────
  // Without this, N <choices> children dump N cards into the transcript at
  // once. <quiz> shows one at a time, gates "Next" until the current step is
  // answered, and ends on a score card. The index and per-step results live in
  // ctx.state so they survive the full tree rebuild that ctx.set() triggers.
  R.quiz = function (node, depth, ctx) {
    var kids = arr(node.children).filter(function (c) {
      return c && typeof c === "object" && kindOf(c) === "choices";
    });
    if (!kids.length) return null;

    var sig = kids.map(function (c) {
      return String((c && (c.title || c.text)) || "").slice(0, 24);
    }).join("|");
    var key = node.state || node.id || ("__quiz__" + kids.length + "_" + sig);
    var total = kids.length;
    var idx = num(ctx.state[key + ":i"], 0, 0, total - 1);
    var done = ctx.state[key + ":done"] === true;
    var wrap = el("div", "rc-quiz");

    if (done) {
      var results = ctx.state[key + ":results"] || [];
      var scored = 0, score = 0;
      for (var r = 0; r < results.length; r++) {
        if (results[r] === 1 || results[r] === 0) scored++;
        if (results[r] === 1) score++;
      }
      var card = el("div", "rc-quiz-summary");
      var sc = el("div", "rc-quiz-score");
      sc.appendChild(el("span", null, String(score)));
      sc.appendChild(el("small", null, "/" + (scored || total)));
      card.appendChild(sc);
      card.appendChild(el("div", "rc-quiz-score-label",
        !scored ? "Thanks for answering."
          : score === scored ? "Full marks — you know this."
          : score >= Math.ceil(scored / 2) ? "Solid. Review the ones you missed."
          : "Worth another pass through the material."));
      var again = el("button", "rc-quiz-btn", "Try again");
      again.type = "button";
      again.addEventListener("click", function (e) {
        e.preventDefault();
        // Wipe every per-step answer slot too, or the questions rehydrate with
        // the previous run's picks still marked.
        Object.keys(ctx.state).forEach(function (k) {
          if (k.indexOf(key + ":c") === 0) delete ctx.state[k];
        });
        ctx.state[key + ":results"] = [];
        ctx.state[key + ":done"] = false;
        ctx.set(key + ":i", 0);
      });
      card.appendChild(again);
      wrap.appendChild(card);
      return wrap;
    }

    // Render ONLY the current step. Its own picked/correct state rehydrates
    // from ctx.state, so an answered step stays answered across rebuilds.
    var child = Object.assign({}, kids[idx]);
    if (child.step == null) child.step = idx + 1;
    if (child.of == null) child.of = total;
    // Scope this step's answer state to the quiz + step. Without it, R.choices
    // falls back to a key derived from the option labels -- so two questions
    // with the same labels shared one `saved` slot, and "Try again" had no
    // prefix to clear. That was the answer that wouldn't reset.
    if (child.state == null && child.id == null && child.bind == null) {
      child.state = key + ":c" + idx;
    }
    var stage = el("div", "rc-quiz-stage");
    var rendered = renderNode(child, depth + 1, ctx);
    if (rendered) stage.appendChild(rendered);
    wrap.appendChild(stage);

    // Answered = the step produced a verdict banner or a marked option.
    var answered = !!(rendered && rendered.querySelector(
      ".rc-quiz-result, .rc-choice-picked, .rc-choice-correct, .rc-choice-wrong"));

    var nav = el("div", "rc-quiz-nav");
    var next = el("button", "rc-quiz-btn", idx === total - 1 ? "See result" : "Next question");
    next.type = "button";
    if (!answered) { next.disabled = true; next.classList.add("rc-quiz-btn-off"); }
    next.addEventListener("click", function (e) {
      e.preventDefault();
      if (next.disabled) return;
      var hasVerdict = !!(rendered && rendered.querySelector(".rc-quiz-result"));
      var ok = !!(rendered && rendered.querySelector(".rc-quiz-result.rc-ok"));
      var results = ctx.state[key + ":results"] || [];
      results[idx] = hasVerdict ? (ok ? 1 : 0) : -1;  // -1 = poll, not scored
      ctx.state[key + ":results"] = results;
      if (idx === total - 1) ctx.set(key + ":done", true);
      else ctx.set(key + ":i", idx + 1);
    });
    nav.appendChild(next);
    wrap.appendChild(nav);
    return wrap;
  };

  R.choices = function (node, depth, ctx) {
    var opts = optArr(node.options || node.items || node.children);
    if (!opts.length) return null;
    function isRight(o) {
      if (!o || typeof o !== "object") return false;
      return o.correct === true || o.correct === "true" || o.__expr__correct === "true";
    }
    var hasCorrect = opts.some(isRight);
    var explain = node.explain != null ? node.explain : node.explanation;
    // Stable key so the picked/answered state survives a re-render.
    var key = node.state || node.bind || node.id || null;
    if (!key) {
      var sig = opts.map(function (o) {
        return typeof o === "object" ? String(o.value != null ? o.value : (o.label || o.text)) : String(o);
      }).join("|");
      key = "__choices__" + sig;
    }
    var saved = (ctx && ctx.state && ctx.state[key]) || null;

    var wrap = el("div", "rc-choices");
    // Header: an unmistakable "this is a quiz" label, then the question.
    var head = el("div", "rc-quiz-head");
    var kicker = el("div", "rc-quiz-kicker");
    kicker.appendChild(el("span", "rc-quiz-icon", "?"));
    kicker.appendChild(el("span", null, hasCorrect ? "Quick check" : "Poll"));
    // Optional progress chip: <choices step="1" of="3"> renders "1 / 3".
    var stepN = node.step != null ? num(node.step, 0, 0, 1e6) : null;
    var ofN = node.of != null ? num(node.of, 0, 0, 1e6) : null;
    if (stepN != null && ofN != null) {
      kicker.appendChild(el("span", "rc-quiz-progress", stepN + " / " + ofN));
    }
    head.appendChild(kicker);
    if (node.title) head.appendChild(el("div", "rc-choices-title", interp(String(node.title), ctx)));
    wrap.appendChild(head);
    var list = el("div", "rc-choices-list");
    var btns = [];
    var feedback = null;

    function applyState(picked) {
      btns.forEach(function (b) {
        b.disabled = true;
        b.classList.remove("rc-clickable");
        if (hasCorrect) {
          if (b.dataset.correct === "1") b.classList.add("rc-choice-correct");
          else if (b.dataset.value === String(picked)) b.classList.add("rc-choice-wrong");
        } else if (b.dataset.value === String(picked)) {
          b.classList.add("rc-choice-picked");
        }
      });
      if (feedback) { feedback.remove(); feedback = null; }
      if (hasCorrect) {
        var gotIt = btns.some(function (b) { return b.dataset.correct === "1" && b.dataset.value === String(picked); });
        // Result banner: big icon + verdict, then the explanation underneath.
        var banner = el("div", "rc-quiz-result " + (gotIt ? "rc-ok" : "rc-no"));
        var mark = el("span", "rc-quiz-mark", gotIt ? "✓" : "✕");
        var text = el("div", "rc-quiz-verdict");
        text.appendChild(el("div", "rc-quiz-verdict-main", gotIt ? "Correct" : "Not quite"));
        if (!gotIt) {
          var right = opts.filter(isRight)[0];
          var rl = right ? str(typeof right === "object" ? (right.text || right.label) : right) : null;
          if (rl) text.appendChild(el("div", "rc-quiz-verdict-sub", "Answer: " + rl));
        }
        banner.appendChild(mark);
        banner.appendChild(text);
        wrap.appendChild(banner);
        if (explain != null) wrap.appendChild(richLine("rc-choices-explain", explain, ctx));
        feedback = banner;
      }
    }

    opts.forEach(function (o, oi) {
      var isObj = o && typeof o === "object";
      var label = isObj ? str(o.text || o.label) : str(o);
      var val = isObj ? (o.value != null ? o.value : label) : o;
      var b = el("button", "rc-choice", interp(label, ctx));
      b.type = "button";
      b.dataset.value = String(val);
      b.dataset.letter = oi < 26 ? String.fromCharCode(65 + oi) : String(oi + 1);
      // Stagger index — the reveal animation delays each row by ~40ms so the
      // eye sweeps down the list instead of every row flashing at once.
      b.style.setProperty("--i", String(oi));
      if (isObj && isRight(o)) b.dataset.correct = "1";
      if (saved) {
        b.disabled = true;
      } else if (ctx && ctx.set) {
        b.classList.add("rc-clickable");
        b.addEventListener("click", function (e) {
          e.preventDefault();
          applyState(val);
          ctx.set(key, { picked: val, done: true });
        });
      }
      btns.push(b);
      list.appendChild(b);
    });
    wrap.appendChild(list);
    if (saved) applyState(saved.picked);
    return wrap;
  };
  // SVG bar chart. Geometry mirrors the reference exactly:
  //   viewBox 0 0 700 250 · plot box x[44..680] y[210..30] · bar rx=4
  //   gridlines + y-axis labels on nice ticks · value label above each bar
  //   day label below · bar fill via .rc-chart-bar (theme token, not hex)
  // "Nice" axis scale shared by both chart shapes. Returns a tick step and an
  // axis top derived from it, so gridlines land on readable numbers instead of
  // raw data fractions. No floor — a max of 3 must not become an axis of 20.
  function niceScale(max, ticks) {
    if (!(max > 0)) return { step: 1, max: ticks };
    var raw = max / ticks;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var n = raw / mag;
    var s = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    var step = s * mag;
    return { step: step, max: step * ticks };
  }
  // Cursor-following tooltip for chart bars. One element per chart card, kept
  // inside the card so it stays under .rc-root and inherits the theme tokens
  // (a body-level tooltip would fall outside the scoped var block). The label
  // is read from the bar's own <title>, so there is no second escaping path.
  function attachChartHover(wrap, holder) {
    var tip = el("div", "rc-chart-tip");
    wrap.appendChild(tip);
    holder.addEventListener("mousemove", function (e) {
      var bar = e.target && e.target.closest ? e.target.closest(".rc-chart-bar") : null;
      if (!bar || !bar.getBoundingClientRect().height) { tip.classList.remove("on"); return; }
      // Prefer data-tip. A native <title> would ALSO fire the browser's own
      // delayed tooltip, so chart bars carry data-tip + aria-label instead.
      var txt = bar.getAttribute("data-tip") || "";
      if (!txt) { var t = bar.querySelector("title"); txt = t ? t.textContent : ""; }
      if (!txt) { tip.classList.remove("on"); return; }
      tip.textContent = txt;
      tip.classList.add("on");
      var r = wrap.getBoundingClientRect();
      tip.style.left = (e.clientX - r.left) + "px";
      tip.style.top = (e.clientY - r.top) + "px";
    });
    holder.addEventListener("mouseleave", function () { tip.classList.remove("on"); });
  }
  R.chart = function (node, depth, ctx) {
    // `content` arrives two ways: as a real object when written
    // `content={{…}}` (a DIL expression), or as a raw JSON *string* when
    // written `content='{…}'` — which is the form the docs show. The string
    // form used to fall straight through to `node`, find no `.data`, and
    // silently render nothing. Parse it here.
    var raw = node.content;
    if (typeof raw === "string") {
      var t = raw.trim();
      if (t && (t[0] === "{" || t[0] === "[")) {
        try { raw = JSON.parse(t); } catch (e) { raw = null; }
      } else {
        raw = null;
      }
    }
    var c = (raw && typeof raw === "object") ? raw : node;
    var rows = arr(c.data), rawSeries = arr(c.series);
    if (!rows.length || !rawSeries.length || !c.xKey) return null;

    // Normalise the series list. Accepts ["water","sleep"] or
    // [{dataKey,name}, …], capped at 4 — past that the bars are unreadable.
    // Previously only series[0] was ever plotted, so a multi-series payload
    // silently dropped every column but the first.
    var defs = [];
    for (var di = 0; di < rawSeries.length && defs.length < 4; di++) {
      var s = rawSeries[di];
      var skey = typeof s === "string"
        ? s
        : (s && (s.dataKey != null ? s.dataKey : (s.key != null ? s.key : s.name)));
      if (!skey) continue;
      defs.push({
        key: skey,
        name: typeof s === "string" ? s : (s.name || s.label || skey)
      });
    }
    if (!defs.length) return null;

    var labels = rows.map(function (r) { return String(r[c.xKey]); });
    var allVals = [];
    var matrix = rows.map(function (r) {
      return defs.map(function (d) {
        var v = Number(r[d.key]) || 0;
        allVals.push(v);
        return v;
      });
    });

    // Axis scale. Pick a "nice" tick step from the data, then derive the top of
    // the axis as step*ticks. The old code hard-floored the max at 20, so a
    // dataset peaking at 3 was squashed into the bottom 15% of the plot.
    var TICKS = 4;
    var scale = niceScale(Math.max.apply(null, [0].concat(allVals)), TICKS);
    var niceMax = scale.max;
    var avg = allVals.length
      ? allVals.reduce(function (a, b) { return a + b; }, 0) / allVals.length
      : 0;

    // X1 stops short of the viewBox edge to leave a gutter for the avg label.
    var X0 = 44, X1 = 656, Y0 = 210, Y1 = 30;
    var plotW = X1 - X0, plotH = Y0 - Y1;
    var n = rows.length, slot = plotW / n;
    var multi = defs.length > 1;
    var gap = multi ? 4 : 0;
    var groupW = Math.min(60, slot * 0.64);
    var barW = multi
      ? Math.max(4, Math.min(22, (groupW - gap * (defs.length - 1)) / defs.length))
      : Math.min(48, slot * 0.55);

    // Quotes are escaped too: labels land inside data-tip="…" attributes, and
    // a stray " in a category name would otherwise break out of the attribute.
    function esc(s) {
      return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
    var p = [];
    p.push('<svg viewBox="0 0 700 250" role="img" aria-label="Bar chart">');
    for (var g = 0; g <= TICKS; g++) {
      var gy = Y0 - (plotH * g / TICKS);
      var gv = Math.round(scale.step * g * 1000) / 1000;
      p.push('<line class="rc-chart-grid" x1="' + X0 + '" y1="' + gy + '" x2="' + X1 + '" y2="' + gy + '"/>');
      p.push('<text class="rc-chart-axis" x="' + (X0 - 10) + '" y="' + (gy + 4) + '" text-anchor="end">' + gv + '</text>');
    }
    // Mean reference line — the one summary stat a bare bar chart can't show.
    if (allVals.length > 1) {
      var ay = Y0 - (avg / niceMax) * plotH;
      p.push('<line class="rc-chart-avg" x1="' + X0 + '" y1="' + ay.toFixed(1) + '" x2="' + X1 + '" y2="' + ay.toFixed(1) + '"/>');
      p.push('<text class="rc-chart-avg-label" x="' + (X1 + 7) + '" y="' + (ay + 4).toFixed(1) + '">avg ' + (Math.round(avg * 10) / 10) + '</text>');
    }
    rows.forEach(function (r, i) {
      var cx = X0 + slot * (i + 0.5);
      var gStart = cx - (barW * defs.length + gap * (defs.length - 1)) / 2;
      defs.forEach(function (d, si) {
        var v = matrix[i][si];
        var h = (v / niceMax) * plotH;
        var bx = gStart + si * (barW + gap);
        var by = Y0 - h;
        var tipTxt = labels[i] + (multi ? " · " + d.name : "") + ": " + v;
        p.push('<rect class="rc-chart-bar" data-s="' + si + '" data-tip="' + esc(tipTxt) + '" aria-label="' + esc(tipTxt) + '" style="--i:' + i + '" x="' + bx.toFixed(1) + '" y="' + by.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="4"/>');
        // Value labels only when there is room — a multi-series group would
        // stack them into each other. The hover tooltip covers that case.
        if (!multi) {
          p.push('<text class="rc-chart-val" x="' + cx.toFixed(1) + '" y="' + (by - 10).toFixed(1) + '" text-anchor="middle">' + v + '</text>');
        }
      });
      p.push('<text class="rc-chart-cat" x="' + cx.toFixed(1) + '" y="' + (Y0 + 22) + '" text-anchor="middle">' + esc(labels[i]) + '</text>');
    });
    p.push('</svg>');

    var wrap = el("div", "rc-chart");
    var head = el("div", "rc-chart-head");
    if (node.title) head.appendChild(el("div", "rc-chart-title", interp(String(node.title), ctx)));
    if (multi) {
      var legend = el("div", "rc-chart-legend");
      defs.forEach(function (d, si) {
        var item = el("span", "rc-chart-legend-item");
        var sw = el("i", "rc-chart-swatch");
        sw.dataset.s = String(si);
        item.appendChild(sw);
        item.appendChild(document.createTextNode(d.name));
        legend.appendChild(item);
      });
      head.appendChild(legend);
    }
    if (head.childNodes.length) wrap.appendChild(head);
    var holder = el("div", "rc-chart-svg");
    holder.innerHTML = p.join("");
    wrap.appendChild(holder);
    attachChartHover(wrap, holder);
    var foot = (c.meta && c.meta.footer) || c.caption;
    if (foot) wrap.appendChild(el("div", "rc-cap", String(foot)));
    return wrap;
  };

  function renderNodesInto(root, nodes, ctx) {
    arr(nodes).forEach(function (n) {
      var r = renderNode(n, 0, ctx);
      if (r) root.appendChild(r);
    });
  }
  // Resolve the non-pure hooks (`useBreakpoint`, `useTheme`, `useMemo`, …) on
  // the client. The parser eagerly seeds the pure ones and leaves a descriptor
  // under `state["__hook__<name>"]` for the rest.
  // Re-evaluate the derived hooks (`useMemo`) against current state. Called on
  // every render pass so a memo recomputes when its source state changes —
  // resolveHooks only runs once at seed.
  function recomputeMemos(state) {
    if (!state || typeof state !== "object") return;
    for (var k in state) {
      if (k.indexOf("__hook__") !== 0) continue;
      var desc = state[k], name = k.slice(8);
      if (!desc || desc.hook !== "Memo") continue;
      var m = String(desc.args || "").match(/=>\s*([\s\S]+?)(?:,\s*\[[^\]]*\])?\s*$/);
      if (m) state[name] = evalExpr(m[1].trim().replace(/[,)\s]+$/, ""), { state: state, scope: {} });
    }
  }

  // Bare `{@body const x = EXPR}` declarations. The server evaluates these once
  // and ships the source under `__decl__<name>`; we replay them in insertion
  // order on every render pass. Without this a derived value stays frozen at
  // its parse-time number while the state it reads keeps changing -- the slider
  // moved but "hours" never did.
  // Insertion order is declaration order (Python dicts + JSON preserve it), so
  // `hrs` is recomputed before `verdict`, which depends on it.
  function recomputeDecls(state) {
    if (!state || typeof state !== "object") return;
    for (var k in state) {
      if (k.indexOf("__decl__") !== 0) continue;
      var name = k.slice(8);
      var src = state[k];
      if (typeof src !== "string" || !src) continue;
      var v = evalExpr(src, { state: state, scope: {} });
      if (v !== undefined) state[name] = v;
    }
  }

  function resolveHooks(state) {
    if (!state || typeof state !== "object") return;
    for (var k in state) {
      if (k.indexOf("__hook__") !== 0) continue;
      var desc = state[k], name = k.slice(8);
      if (!desc || typeof desc !== "object") continue;
      var hook = desc.hook, args = desc.args || "";
      if (hook === "Breakpoint") {
        var w = window.innerWidth || 1024;
        state[name] = w < 640 ? "mobile" : (w < 1024 ? "tablet" : "desktop");
      } else if (hook === "Theme") {
        var dark = !window.matchMedia || window.matchMedia("(prefers-color-scheme: dark)").matches;
        state[name] = { dark: dark, mode: dark ? "dark" : "light" };
      } else if (hook === "Memo") {
        // `useMemo(()=>expr, deps)` -> evaluate expr once against state.
        var m = String(args).match(/=>\s*([\s\S]+?)(?:,\s*\[[^\]]*\])?\s*$/);
        if (m) state[name] = evalExpr(m[1].trim().replace(/[,)\s]+$/, ""), { state: state, scope: {} });
      } else if (hook === "Callback") {
        var c = String(args).match(/^\s*(\([^)]*\)|[A-Za-z_]\w*)?\s*=>\s*([\s\S]+)$/);
        state[name] = c ? c[2].trim() : null;
      } else if (hook === "Effect") {
        var e = String(args).match(/^\s*\(?\)?\s*=>\s*([\s\S]+)$/);
        if (e) { try { evalExpr(e[1].trim().replace(/[,)\s]+$/, ""), { state: state, scope: {} }); } catch (err) {} }
      }
    }
  }

  function renderLayout(data, container) {
    var root = el("div", "rc-root");
    var seeded = Object.assign({}, data.state || {});
    resolveHooks(seeded);
    var ctx = makeCtx(seeded, function () {
      recomputeMemos(ctx.state);
      // Derived `const` declarations must be replayed on every pass, BEFORE the
      // tree is rebuilt -- otherwise `{hrs.toFixed(1)}` re-evaluates against a
      // stale `hrs` and the readout never moves.
      recomputeDecls(ctx.state);
      var fresh = el("div", "rc-root");
      renderNodesInto(fresh, data.nodes, ctx);
      if (root.parentNode) root.parentNode.replaceChild(fresh, root);
      root = fresh;
      if (window.lucide) window.lucide.createIcons();
      // Re-typeset after EVERY interactive rebuild -- a slider drag or tab
      // switch that introduces new math would otherwise leave raw TeX on
      // screen, since typesetMath only ran once at initial mount.
      typesetMath(root);
    });
    renderNodesInto(root, data.nodes, ctx);
    container.appendChild(root);
    typesetMath(root);
    return root;
  }

  // Component trees never went through markdown's postRenderContent, so
  // <math> sat unrendered forever. Pull MathJax in on demand and typeset the
  // subtree -- but ONLY when a .rc-math element is actually present, so a card
  // with no math still costs nothing.
  // Multiple component trees can be on screen at once and each calls this on
  // every re-render. A single debounce timer would clobber itself -- the last
  // caller wins and every earlier scope silently never typesets. Collect scopes
  // in a set instead, flush them all in one pass.
  var _mathScopes = [];
  var _mathTimer = null;
  function typesetMath(scope) {
    if (!scope || !scope.querySelector) return;
    if (!scope.querySelector(".rc-math")) return;
    if (typeof window._lazyLoadMathJax !== "function") return;
    if (_mathScopes.indexOf(scope) === -1) _mathScopes.push(scope);
    if (_mathTimer) clearTimeout(_mathTimer);
    _mathTimer = setTimeout(function () {
      _mathTimer = null;
      var scopes = _mathScopes.filter(function (s) { return s && s.isConnected; });
      _mathScopes = [];
      if (!scopes.length) return;
      Promise.resolve(window._lazyLoadMathJax()).then(function () {
        if (window.MathJax && window.MathJax.typesetPromise) {
          window.MathJax.typesetPromise(scopes).catch(function () {});
        }
      }).catch(function () {});
    }, 30);
  }

  function render(payload, container) {
    if (!container) return null;
    var comps = arr(payload && payload.components);
    if (!comps.length) return null;

    // A single `layout`/`ui`/`render` component already owns its own .rc-root,
    // so hand it the container directly instead of nesting a second wrapper.
    if (comps.length === 1) {
      var only = comps[0];
      var t = String((only && only.type) || "").toLowerCase();
      if (t === "layout" || t === "ui" || t === "render") {
        return renderLayout((only && only.data) || only || {}, container);
      }
    }

    var root = el("div", "rc-root");
    comps.forEach(function (comp) {
      if (!comp || typeof comp !== "object") return;
      var ctype = String(comp.type || "").toLowerCase();
      var data = comp.data || comp;
      if (ctype === "layout" || ctype === "ui" || ctype === "render") {
        var sub = renderLayout(data, root);
        if (sub) root.appendChild(sub);
        return;
      }
      var probe = Object.assign({}, data);
      if (!probe.kind && !probe.type) probe.kind = ctype;
      var r = renderNode(probe, 0, noopCtx());
      root.appendChild(r || unknownNode(comp, noopCtx()));
    });
    if (!root.childNodes.length) return null;
    container.appendChild(root);
    if (window.lucide) window.lucide.createIcons();
    return root;
  }

  // ── Atomic render with last-good preservation ────────────────────────
  // The old contract appended straight into the live container, so every
  // caller had to clear it first (`innerHTML = ""`) — and if render() threw or
  // produced nothing, that left the container BLANK, destroying a drawing that
  // had already rendered correctly. This stages the new tree OFF-DOM and swaps
  // it in only when it is usable. Two levels of preservation:
  //
  //   1. Whole-block: nothing rendered -> the previous DOM is left untouched.
  //   2. Per-drawing: a .rc-svg node in the NEW tree failed to parse (it holds
  //      a <pre>/error box, no <svg>) while the OLD tree had a live <svg> at
  //      the same position -> the old drawing is carried over.
  //
  // Returns true when the container was updated, false when the last good
  // render was kept.
  function carryOverSvg(stage, live) {
    var fresh = stage.querySelectorAll(".rc-svg");
    if (!fresh.length) return;
    var old = live.querySelectorAll(".rc-svg");
    for (var i = 0; i < fresh.length; i++) {
      if (fresh[i].querySelector("svg")) continue;   // new one drew fine
      var prev = old[i];
      if (!prev) continue;
      var prevSvg = prev.querySelector("svg");
      if (!prevSvg) continue;
      // Carry the last working drawing into the new tree.
      fresh[i].innerHTML = "";
      fresh[i].appendChild(prevSvg.cloneNode(true));
    }
  }
  function renderInto(container, payload) {
    if (!container) return false;
    var stage = document.createElement("div");
    var ok = false;
    try {
      var root = render(payload, stage);
      ok = !!(root && stage.childNodes.length);
    } catch (e) {
      ok = false;
    }
    if (!ok) return false;                 // keep the last good render
    try { carryOverSvg(stage, container); } catch (e) {}
    container.innerHTML = "";
    while (stage.firstChild) container.appendChild(stage.firstChild);
    return true;
  }

  window.SableComponents = { render: render, renderInto: renderInto };
})();
