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
  function arr(v) { return Array.isArray(v) ? v : (v == null ? [] : [v]); }
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
    hstack: "row", vstack: "col", column: "col", columns: "col",
    heading: "title", h1: "title", h2: "title", h3: "title", h4: "title",
    paragraph: "text", label: "text", span: "text", p: "text", subtitle: "text",
    metric: "stat", kpi: "stat", bar: "progress", meter: "progress",
    img: "image", pic: "image", avatar: "image",
    pre: "code", codeblock: "code", quote: "quote", blockquote: "quote",
    "grid-item": "griditem", griditem: "griditem", "grid-cell": "griditem",
    segmented: "segmented", "segmented-control": "segmented", tabbar: "segmented",
    btn: "button", cta: "button", hr: "divider", separator: "divider",
    keyvalue: "kv", "key-value": "kv", checkbox: "check", switch: "toggle",
    collapse: "toggle", accordion: "toggle", radio: "choices", options: "choices",
    chip: "chips", steps: "flow"
  };
  function kindOf(node) {
    var raw = String((node && (node.kind || node.type)) || "").toLowerCase();
    return KIND[raw] || raw;
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

  function interp(s, ctx) {
    if (typeof s !== "string" || s.indexOf("{") === -1) return s;
    return s.replace(/\{([a-zA-Z0-9_.]+)\}/g, function (m, path) {
      var v = getPath(path, ctx);
      return v == null ? m : String(v);
    });
  }
  function richText(parent, s) {
    var src = String(s == null ? "" : s);
    var re = /\*\*([^*]+)\*\*|(?<!\*)\*([^*]+)\*(?!\*)|`([^`]+)`/g;
    var last = 0, m;
    while ((m = re.exec(src)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(src.slice(last, m.index)));
      if (m[1] != null) parent.appendChild(el("strong", "rc-strong", m[1]));
      else if (m[2] != null) parent.appendChild(el("em", "rc-italic", m[2]));
      else if (m[3] != null) parent.appendChild(el("code", "rc-inline-code", m[3]));
      last = re.lastIndex;
    }
    if (last < src.length) parent.appendChild(document.createTextNode(src.slice(last)));
  }
  function richDiv(cls, s) { var d = el("div", cls); richText(d, s); return d; }

  function unknownNode(node) { return el("div", "rc-unknown", "unsupported: " + (kindOf(node) || "?")); }

  var ICON_PX = { xs: 13, sm: 15, md: 18, lg: 22, xl: 26, "2xl": 30, "3xl": 38 };
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

  var R = {};
  var MAX_DEPTH = 20, MAX_CHILDREN = 150;

  function renderNode(node, depth, ctx) {
    if (!node || typeof node !== "object" || depth > MAX_DEPTH) return null;
    var k = kindOf(node);
    var fn = R[k];
    if (!fn) return unknownNode(node);
    try { return fn(node, depth, ctx); }
    catch (e) { return el("div", "rc-error", "render error: " + k + " — " + e.message); }
  }
  function renderChildren(node, depth, container, ctx) {
    var kids = arr(node.children).length ? node.children : (Array.isArray(node.content) ? node.content : []);
    var n = 0;
    for (var i = 0; i < kids.length && n < MAX_CHILDREN; i++) {
      var r = renderNode(kids[i], depth + 1, ctx);
      if (r) { container.appendChild(r); n++; }
    }
    return container.childNodes.length ? container : null;
  }

  R.box = function (node, depth, ctx) {
    var isCard = node.border === true || (node.background && node.background !== "transparent");
    var d = el("div", "rc-box");
    d.dataset.variant = isCard ? "card" : "plain";
    if (node.tone) d.dataset.tone = tone(node.tone);
    if (node.flex) d.dataset.flex = "1";
    if (node.size && /px$/.test(String(node.size))) d.dataset.square = String(node.size);
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
    if (raw == null && !arr(node.children).length) return null;
    var d = richDiv("rc-text", interp(String(raw == null ? "" : raw), ctx));
    if (node.size) d.dataset.size = node.size;
    if (node.tone) d.dataset.tone = tone(node.tone);
    if (node.weight) d.dataset.weight = String(node.weight);
    if (node.color) d.dataset.color = String(node.color);
    if (node.align) d.style.textAlign = node.align;
    return d;
  };
  R.title = function (node, depth, ctx) {
    var v = interp(str(node.text != null ? node.text : node.value), ctx);
    if (!v) return null;
    var lvl = num(node.level, ({ xl: 2, lg: 3, md: 4, sm: 4, xs: 4 })[node.size] || 4, 1, 4);
    return el("div", "rc-heading rc-h" + lvl, v);
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
    arr(node.items).forEach(function (it) {
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
    arr(node.items).forEach(function (it) { if (it && typeof it === "object") { var r = R.stat(it, depth, ctx); if (r) wrap.appendChild(r); } });
    return wrap.childNodes.length ? wrap : null;
  };
  R.icon = function (node) { return renderIcon(node); };
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
    var v = interp(str(node.text != null ? node.text : node.value), ctx);
    if (!v) return null;
    var pre = el("pre", "rc-code");
    pre.appendChild(el("code", null, v));
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
    if (kids.length) { kids.forEach(function (c) { var r = renderNode(c, depth + 1, ctx); if (r) d.appendChild(r); }); return d.childNodes.length ? d : null; }
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
    if (!cols.length && !rows.length) return null;
    var wrap = el("div", "rc-table-wrap");
    var t = el("table", "rc-table");
    if (cols.length) { var th = el("thead"), tr = el("tr"); cols.forEach(function (c) { tr.appendChild(el("th", null, interp(String(c), ctx))); }); th.appendChild(tr); t.appendChild(th); }
    var tb = el("tbody");
    rows.forEach(function (r) { var tr = el("tr"); arr(r).forEach(function (c) { tr.appendChild(el("td", null, interp(String(c), ctx))); }); tb.appendChild(tr); });
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
    var items = arr(node.items);
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
    var steps = arr(node.steps || node.items);
    if (!steps.length) return null;
    var wrap = el("div", "rc-flow");
    steps.forEach(function (s, i) {
      var n = el("div", "rc-flow-node");
      n.appendChild(el("span", "rc-flow-idx", String(i + 1)));
      n.appendChild(el("span", null, typeof s === "object" ? str(s.label || s.text) : s));
      wrap.appendChild(n);
    });
    return wrap;
  };
  R.image = function (node) {
    var src = str(node.src);
    if (!src) return null;
    var wrap = el("div", "rc-image");
    var img = document.createElement("img");
    img.className = "rc-image-el";
    img.alt = str(node.caption || node.alt, "");
    img.loading = "lazy";
    img.src = src;
    wrap.appendChild(img);
    if (node.caption) wrap.appendChild(el("div", "rc-cap", String(node.caption)));
    return wrap;
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
    var opts = arr(node.options);
    var cur = node.value != null ? node.value : getPath(node.state || "", ctx);
    opts.forEach(function (o) {
      var val = typeof o === "object" ? o.value : o;
      var lab = typeof o === "object" ? (o.label != null ? o.label : o.value) : o;
      var b = el("button", "rc-seg-btn" + (val === cur ? " rc-seg-on" : ""), lab);
      b.type = "button";
      if (node.state && ctx) b.addEventListener("click", function () { ctx.set(node.state, val); });
      wrap.appendChild(b);
    });
    return wrap;
  };
  R.button = function (node, depth, ctx) {
    var v = interp(str(node.label || node.text), ctx);
    if (!v) return null;
    var hasAction = node.action || node.send != null || node.set != null || node.toggle != null;
    var b = el(hasAction ? "button" : "span", "rc-button", v);
    b.dataset.tone = tone(node.tone || node.color, "accent");
    if (node.block) b.dataset.block = "1";
    if (node.icon) b.insertBefore(renderIcon({ name: node.icon, size: "sm" }), b.firstChild);
    if (hasAction) {
      b.type = "button";
      b.classList.add("rc-clickable");
      b.addEventListener("click", function (e) {
        e.preventDefault();
        if (node.set && typeof node.set === "object" && ctx) Object.keys(node.set).forEach(function (k) { ctx.set(k, node.set[k]); });
        else if (node.toggle && ctx) ctx.toggle(node.toggle);
        else if (node.send != null && window.SableInteract && window.SableInteract.send) window.SableInteract.send(interp(String(node.send), ctx));
      });
    }
    return b;
  };
  R.check = function (node, depth, ctx) {
    var wrap = el("div", "rc-checklist");
    if (node.title) wrap.appendChild(el("div", "rc-choices-title", interp(String(node.title), ctx)));
    arr(node.items).forEach(function (it) {
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
  R.choices = function (node, depth, ctx) {
    var opts = arr(node.options || node.items);
    if (!opts.length) return null;
    var wrap = el("div", "rc-choices");
    if (node.title) wrap.appendChild(el("div", "rc-choices-title", interp(String(node.title), ctx)));
    var list = el("div", "rc-choices-list");
    opts.forEach(function (o) {
      var label = typeof o === "object" ? str(o.text || o.label) : str(o);
      var b = el("button", "rc-choice", interp(label, ctx));
      b.type = "button";
      list.appendChild(b);
    });
    wrap.appendChild(list);
    return wrap;
  };
  R.chart = function (node, depth, ctx) {
    var c = (node.content && typeof node.content === "object") ? node.content : node;
    var rows = arr(c.data), series = arr(c.series);
    if (!rows.length || !series.length || !c.xKey) return null;
    var labels = rows.map(function (r) { return String(r[c.xKey]); });
    var vals = rows.map(function (r) { return Number(r[series[0].dataKey]) || 0; });
    var max = Math.max.apply(null, [1].concat(vals));
    var wrap = el("div", "rc-chart");
    var plot = el("div", "rc-chart-plot");
    vals.forEach(function (v, i) {
      var col = el("div", "rc-chart-col");
      var bar = el("div", "rc-chart-bar");
      bar.style.height = Math.round((v / max) * 100) + "%";
      bar.title = labels[i] + ": " + v;
      col.appendChild(bar);
      col.appendChild(el("div", "rc-chart-label", labels[i]));
      plot.appendChild(col);
    });
    wrap.appendChild(plot);
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
  function renderLayout(data, container) {
    var root = el("div", "rc-root");
    var ctx = makeCtx(data.state || {}, function () {
      var fresh = el("div", "rc-root");
      renderNodesInto(fresh, data.nodes, ctx);
      if (root.parentNode) root.parentNode.replaceChild(fresh, root);
      root = fresh;
      if (window.lucide) window.lucide.createIcons();
    });
    renderNodesInto(root, data.nodes, ctx);
    container.appendChild(root);
    return root;
  }

  function render(payload, container) {
    var comps = arr(payload && payload.components);
    if (!comps.length) return null;
    var root = el("div", "rc-root");
    comps.forEach(function (comp) {
      if (!comp || typeof comp !== "object") return;
      var ctype = String(comp.type || "").toLowerCase();
      var data = comp.data || comp;
      if (ctype === "layout" || ctype === "ui" || ctype === "render") { renderLayout(data, root); return; }
      var probe = Object.assign({}, data);
      if (!probe.kind && !probe.type) probe.kind = ctype;
      var r = renderNode(probe, 0, noopCtx());
      root.appendChild(r || unknownNode(comp));
    });
    if (!root.childNodes.length) return null;
    container.appendChild(root);
    if (window.lucide) window.lucide.createIcons();
    return root;
  }

  window.SableComponents = { render: render };
})();
