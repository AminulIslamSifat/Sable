    /* =========================================================================
       Self-contained markdown renderer (no CDN dependency — works fully offline)
       Supports: headers, bold/italic/strikethrough, ==highlight==, inline & fenced
       code, tables, ordered/unordered/task lists (with basic nesting), blockquotes
       + Obsidian-style callouts ([!note] etc.), links, images, hr.
       All raw text is HTML-escaped before any tag is emitted, so this is safe
       against injected markup by construction — no separate sanitizer needed.
       ========================================================================= */

    const CALLOUT_META = {
      note:     { icon: "📝", color: "#6ea8fe" },
      info:     { icon: "ℹ️", color: "#6ea8fe" },
      abstract: { icon: "📋", color: "#4fd1c5" },
      summary:  { icon: "📋", color: "#4fd1c5" },
      tldr:     { icon: "📋", color: "#4fd1c5" },
      tip:      { icon: "💡", color: "#4fd18a" },
      hint:     { icon: "💡", color: "#4fd18a" },
      success:  { icon: "✅", color: "#4fd18a" },
      check:    { icon: "✅", color: "#4fd18a" },
      done:     { icon: "✅", color: "#4fd18a" },
      question: { icon: "❓", color: "#c9a464" },
      help:     { icon: "❓", color: "#c9a464" },
      faq:      { icon: "❓", color: "#c9a464" },
      warning:  { icon: "⚠️", color: "#e8b45a" },
      caution:  { icon: "⚠️", color: "#e8b45a" },
      attention:{ icon: "⚠️", color: "#e8b45a" },
      danger:   { icon: "⛔", color: "#e5646a" },
      error:    { icon: "⛔", color: "#e5646a" },
      bug:      { icon: "🐛", color: "#e5646a" },
      important:{ icon: "❗", color: "#e5646a" },
      example:  { icon: "📄", color: "#b48ce8" },
      quote:    { icon: "💬", color: "#9a9aa2" },
      cite:     { icon: "💬", color: "#9a9aa2" },
      default:  { icon: "📌", color: "#c9a464" }
    };

    function escHtml(str) {
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }

    function escAttr(str) {
      return escHtml(str).replace(/"/g, "&quot;");
    }

    function inlineMd(raw) {
      const codeStore = [];
      let text = escHtml(raw);

      // protect inline code spans first so formatting inside isn't touched
      text = text.replace(/`([^`]+?)`/g, (m, code) => {
        codeStore.push(code);
        return "\u0000C" + (codeStore.length - 1) + "\u0000";
      });

      // images ![alt](url)
      text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, (m, alt, url, title) => {
        if (!/^https?:|^data:image\//i.test(url)) return m;
        return `<img src="${escAttr(url)}" alt="${escAttr(alt)}"${title ? ` title="${escAttr(title)}"` : ""}>`;
      });

      // links [text](url)
      text = text.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, (m, label, url, title) => {
        if (!/^https?:|^mailto:|^#/i.test(url)) return m;
        return `<a href="${escAttr(url)}" target="_blank" rel="noopener noreferrer"${title ? ` title="${escAttr(title)}"` : ""}>${label}</a>`;
      });

      text = text.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
      text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
      text = text.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
      text = text.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
      text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");
      text = text.replace(/==([^=]+)==/g, "<mark>$1</mark>");

      text = text.replace(/\u0000C(\d+)\u0000/g, (m, i) => `<code>${codeStore[i]}</code>`);

      return text;
    }

    function splitTableRow(line) {
      let t = line.trim();
      if (t.startsWith("|")) t = t.slice(1);
      if (t.endsWith("|")) t = t.slice(0, -1);
      return t.split("|").map(c => c.trim());
    }

    function isTableSep(line) {
      return /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/.test(line) && line.includes("-");
    }

    function renderTable(headerCells, aligns, rows) {
      const th = headerCells.map((h, idx) =>
        `<th${aligns[idx] ? ` style="text-align:${aligns[idx]}"` : ""}>${inlineMd(h)}</th>`
      ).join("");
      const trs = rows.map(r =>
        `<tr>${r.map((c, idx) => `<td${aligns[idx] ? ` style="text-align:${aligns[idx]}"` : ""}>${inlineMd(c || "")}</td>`).join("")}</tr>`
      ).join("");
      return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></div>`;
    }

    function renderBlockquote(lines) {
      const first = lines[0] || "";
      const calloutMatch = first.match(/^\[!(\w+)\]([+-]?)\s*(.*)$/);
      if (calloutMatch) {
        const type = calloutMatch[1].toLowerCase();
        const title = calloutMatch[3].trim() || (calloutMatch[1].charAt(0).toUpperCase() + calloutMatch[1].slice(1));
        const rest = lines.slice(1).join("\n");
        const meta = CALLOUT_META[type] || CALLOUT_META.default;
        const bodyHtml = rest.trim() ? parseBlocks(rest.split("\n")) : "";
        return `<div class="callout callout-${type}" style="--callout-color:${meta.color}">`
          + `<div class="callout-title"><span class="callout-icon">${lucideIcon(meta.icon)}</span>${inlineMd(title)}</div>`
          + (bodyHtml ? `<div class="callout-content">${bodyHtml}</div>` : "")
          + `</div>`;
      }
      return `<blockquote>${parseBlocks(lines)}</blockquote>`;
    }

    function getIndent(line) {
      return (line.match(/^(\s*)/) || ["", ""])[1].length;
    }

    function parseList(lines, i, baseIndent) {
      const orderedRe = /^\s*\d+\.\s+/;
      const unorderedRe = /^\s*[-*+]\s+/;
      const isOrdered = orderedRe.test(lines[i]);
      const marker = isOrdered ? orderedRe : unorderedRe;
      const checkboxRe = /^\[( |x|X)\]\s+/;
      const items = [];

      while (i < lines.length) {
        const line = lines[i];
        if (/^\s*$/.test(line)) { i++; continue; }
        const indent = getIndent(line);
        if (indent < baseIndent) break;
        if (indent > baseIndent) {
          if (!items.length) break;
          const nested = parseList(lines, i, indent);
          if (nested.next === i) {
            // indented non-list text — treat as continuation of current item
            items[items.length - 1].content += " " + line.trim();
            i++;
            continue;
          }
          items[items.length - 1].sub += nested.html;
          i = nested.next;
          continue;
        }
        if (!marker.test(line)) break;
        let content = line.replace(marker, "");
        let checked = null;
        const cb = content.match(checkboxRe);
        if (cb) {
          checked = cb[1].toLowerCase() === "x";
          content = content.replace(checkboxRe, "");
        }
        items.push({ content, checked, sub: "" });
        i++;
      }

      const tag = isOrdered ? "ol" : "ul";
      const hasTasks = items.some(it => it.checked !== null);
      const itemsHtml = items.map(it => {
        const cbHtml = it.checked !== null
          ? `<input type="checkbox" disabled ${it.checked ? "checked" : ""}> `
          : "";
        return `<li>${cbHtml}${inlineMd(it.content)}${it.sub}</li>`;
      }).join("");
      return { html: `<${tag}${hasTasks ? ' class="contains-task-list"' : ""}>${itemsHtml}</${tag}>`, next: i };
    }

    function parseBlocks(lines) {
      const out = [];
      let i = 0;

      while (i < lines.length) {
        const line = lines[i];

        if (/^\s*$/.test(line)) { i++; continue; }

        const fence = line.match(/^(```|~~~)\s*(\S*)\s*$/);
        if (fence) {
          const fenceChar = fence[1];
          const lang = fence[2] || "";
          const codeLines = [];
          i++;
          while (i < lines.length && lines[i].trim() !== fenceChar) {
            codeLines.push(lines[i]);
            i++;
          }
          i++; // closing fence
          if (/^mermaid$/i.test(lang)) {
            out.push(`<div class="mermaid-wrap"><div class="mermaid-scroll"><pre class="mermaid">${escHtml(codeLines.join("\n"))}</pre></div></div>`);
          } else if (/^svg$/i.test(lang)) {
            const rawSvg = codeLines.join("\n");
            const clean = window.DOMPurify ? DOMPurify.sanitize(rawSvg, { USE_PROFILES: { svg: true, svgFilters: true } }) : rawSvg;
            const _svgPopBtn = `<button class="svg-popout-btn" title="Open in new tab"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><line x1="21" y1="3" x2="14" y2="10"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg></button>`;
            out.push(`<div class="svg-wrap">${_svgPopBtn}${clean}</div>`);
          } else if (/^(markdown|md|obsidian)$/i.test(lang) && mdUnwrapDepth < 2) {
            mdUnwrapDepth++;
            const inner = parseBlocks(codeLines);
            mdUnwrapDepth--;
            out.push(`<div class="md-content md-unwrap">${inner}</div>`);
          } else {
            const _previewLangs = /^(html|htm|svg|threejs|three\.js|p5js|p5)$/i;
            const _canPreview = _previewLangs.test(lang);
            const _runBtn = _canPreview ? `<button class="code-run-btn" title="Run preview" data-lang="${escAttr(lang)}"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3"/></svg></button>` : "";
            out.push(`<div class="code-block">${_runBtn}<button class="code-copy-btn" title="Copy code"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><pre><code${lang ? ` class="language-${escAttr(lang)}"` : ""}>${escHtml(codeLines.join("\n"))}</code></pre></div>`);
          }
          continue;
        }

        const h = line.match(/^(#{1,6})\s*(.*)$/);
        if (h) {
          const level = h[1].length;
          out.push(`<h${level}>${inlineMd(h[2].trim())}</h${level}>`);
          i++;
          continue;
        }

        if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
          out.push("<hr>");
          i++;
          continue;
        }

        if (/^\s{0,3}>/.test(line)) {
          const quoteLines = [];
          while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
            quoteLines.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
            i++;
          }
          out.push(renderBlockquote(quoteLines));
          continue;
        }

        if (line.includes("|") && lines[i + 1] !== undefined && isTableSep(lines[i + 1])) {
          const headerCells = splitTableRow(line);
          const aligns = splitTableRow(lines[i + 1]).map(c => {
            const t = c.trim();
            if (/^:-+:$/.test(t)) return "center";
            if (/^-+:$/.test(t)) return "right";
            if (/^:-+$/.test(t)) return "left";
            return "";
          });
          i += 2;
          const bodyRows = [];
          while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
            bodyRows.push(splitTableRow(lines[i]));
            i++;
          }
          out.push(renderTable(headerCells, aligns, bodyRows));
          continue;
        }

        if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
          const { html, next } = parseList(lines, i, getIndent(line));
          out.push(html);
          i = next;
          continue;
        }

        const paraLines = [];
        while (
          i < lines.length &&
          !/^\s*$/.test(lines[i]) &&
          !/^(#{1,6})\s+/.test(lines[i]) &&
          !/^\s{0,3}>/.test(lines[i]) &&
          !/^(```|~~~)/.test(lines[i]) &&
          !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
          !/^\s*([-*_])\s*(\1\s*){2,}$/.test(lines[i]) &&
          !(lines[i].includes("|") && lines[i + 1] !== undefined && isTableSep(lines[i + 1]))
        ) {
          paraLines.push(lines[i]);
          i++;
        }
        if (paraLines.length) {
          out.push(`<p>${inlineMd(paraLines.join("\n"))}</p>`);
        } else {
          i++;
        }
      }

      return out.join("\n");
    }

    let markedReady = false;
    let mdUnwrapDepth = 0;

    function ensureMarked() {
      if (!markedReady && window.marked) {
        try {
          const mdRenderer = {
            table(header, body) {
              const h = typeof header === "object" ? header.header : header;
              const b = typeof header === "object" ? header.body : body;
              return `<div class="table-wrap"><table><thead>${h}</thead><tbody>${b}</tbody></table></div>`;
            },
            code(arg, langArg, escapedArg) {
              let text, lang, escaped;
              if (arg && typeof arg === "object") {
                text = arg.text ?? "";
                lang = arg.lang ?? "";
                escaped = arg.escaped ?? false;
              } else {
                text = arg ?? "";
                lang = langArg ?? "";
                escaped = escapedArg ?? false;
              }
              lang = String(lang || "").trim();
              if (/^mermaid$/i.test(lang)) {
                return `<div class="mermaid-wrap"><div class="mermaid-scroll"><pre class="mermaid">${escHtml(text)}</pre></div></div>`;
              }
              if (/^svg$/i.test(lang)) {
                const clean = DOMPurify ? DOMPurify.sanitize(text, { USE_PROFILES: { svg: true, svgFilters: true } }) : text;
                return `<div class="svg-wrap">${clean}</div>`;
              }
              if (/^(markdown|md|obsidian)$/i.test(lang) && mdUnwrapDepth < 2) {
                mdUnwrapDepth++;
                let inner;
                try { inner = marked.parse(text); }
                finally { mdUnwrapDepth--; }
                return `<div class="md-content md-unwrap">${inner}</div>`;
              }
              const langAttr = lang ? ` class="language-${escAttr(lang)}"` : "";
              const body = escaped ? text : escHtml(text);
              const _mdPreviewLangs = /^(html|htm|svg|threejs|three\.js|p5js|p5)$/i;
              const _mdCanPreview = _mdPreviewLangs.test(lang);
              const _mdRunBtn = _mdCanPreview ? `<button class="code-run-btn" title="Run preview" data-lang="${escAttr(lang)}"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3"/></svg></button>` : "";
              return `<div class="code-block">${_mdRunBtn}<button class="code-copy-btn" title="Copy code"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><pre><code${langAttr}>${body}</code></pre></div>`;
            }
          };
          marked.use({ gfm: true, breaks: true, renderer: mdRenderer });

          if (window.DOMPurify && !DOMPurify.__sableHook) {
            DOMPurify.addHook("afterSanitizeAttributes", (node) => {
              if (node.tagName === "A") {
                node.setAttribute("target", "_blank");
                node.setAttribute("rel", "noopener noreferrer");
              }
            });
            DOMPurify.__sableHook = true;
          }

          markedReady = true;
        } catch (err) {
          console.warn("marked init failed:", err);
        }
      }
    }

    function normalizeMd(text) {
      const lines = text.split("\n");
      let inFence = false;
      let fenceChar = "";

      for (let i = 0; i < lines.length; i++) {
        const fence = lines[i].match(/^(```|~~~)/);
        if (fence) {
          if (!inFence) {
            inFence = true;
            fenceChar = fence[1];
          } else if (lines[i].trim() === fenceChar) {
            inFence = false;
            fenceChar = "";
          }
          continue;
        }

        if (!inFence) {
          lines[i] = lines[i].replace(/^(#{1,6})([^\s#])/, "$1 $2");
        }
      }

      return lines.join("\n");
    }

    function usesLegacyExtras(text) {
      return /^>\s*\[!\w+\]/m.test(text) || /==[^=]+==/.test(text);
    }

    const _HTML_TAGS = new Set("a,abbr,address,area,article,aside,audio,b,base,bdi,bdo,blockquote,body,br,button,canvas,caption,cite,code,col,colgroup,data,datalist,dd,del,details,dfn,dialog,div,dl,dt,em,embed,fieldset,figcaption,figure,footer,form,h1,h2,h3,h4,h5,h6,head,header,hr,html,i,iframe,img,input,ins,kbd,label,legend,li,link,main,map,mark,meta,meter,nav,noscript,object,ol,optgroup,option,output,p,param,picture,pre,progress,q,rp,rt,ruby,s,samp,script,section,select,slot,small,source,span,strong,style,sub,summary,sup,table,tbody,td,template,textarea,tfoot,th,thead,time,title,tr,track,u,ul,var,video,wbr,svg,path,rect,circle,ellipse,line,polyline,polygon,text,g,defs,use,symbol,linearGradient,radialGradient,stop,clipPath,mask,pattern,image,foreignObject,animate,animateTransform,animateMotion,desc,title,metadata,marker,solidColor,solidColorRef,switch,unknown".split(","));

    function escapeNonHtmlTags(text) {
      // Escape angle brackets for tags that aren't valid HTML/SVG so marked +
      // DOMPurify don't swallow them silently. Fenced blocks and inline code
      // spans are skipped entirely via line scan (no regex stash) to avoid
      // catastrophic backtracking on large inputs.
      const lines = text.split("\n");
      let inFence = false;
      let fenceChar = "";

      for (let i = 0; i < lines.length; i++) {
        const fm = lines[i].match(/^(```|~~~)/);
        if (fm) {
          if (!inFence) { inFence = true; fenceChar = fm[1]; }
          else if (lines[i].trim() === fenceChar) { inFence = false; fenceChar = ""; }
          continue;
        }
        if (inFence) continue;

        // Escape non-HTML tags outside fenced blocks
        lines[i] = lines[i].replace(/<(\/?)([a-zA-Z_][\w.-]*)(\s[^>]*)?>/g, (match, slash, tag) => {
          if (_HTML_TAGS.has(tag.toLowerCase())) return match;
          return match.replace(/</g, "&lt;").replace(/>/g, "&gt;");
        });
      }

      return lines.join("\n");
    }


    // ── Emoji → Lucide mapping for chat messages ──
    // Lean map: only high-frequency tech/dev emojis worth converting.
    // All icon names verified against bundled lucide v1.28.0.
    const EMOJI_LUCIDE_MAP = {
      "⚡": "zap", "🔥": "flame", "✅": "circle-check", "❌": "circle-x",
      "⚠️": "triangle-alert", "💡": "lightbulb", "📝": "file-pen", "🔧": "wrench",
      "🚀": "rocket", "💻": "laptop", "📁": "folder", "🔒": "lock",
      "🌐": "globe", "⭐": "star", "❤️": "heart", "🎯": "target",
      "📊": "chart-bar", "🐛": "bug", "✨": "sparkles", "🔄": "refresh-cw",
      "📦": "package", "🗂️": "folder-archive", "⏱️": "timer", "🧠": "brain",
      "💾": "save", "🛠️": "hammer", "📌": "pin", "🔑": "key-round",
      "🎉": "party-popper", "💬": "message-square", "📎": "paperclip", "🖥️": "monitor",
      "⬆️": "arrow-up", "⬇️": "arrow-down", "➡️": "arrow-right", "⬅️": "arrow-left",
      "🔍": "search", "📋": "clipboard-list", "🗑️": "trash2", "🗑": "trash2", "⚙️": "settings",
      "🏗️": "building", "🧪": "flask-conical", "📡": "satellite-dish", "🔗": "link",
      "❓": "circle-help", "⛔": "octagon-x", "❗": "circle-alert", "📄": "file-text",
      "🔬": "microscope", "👁️": "eye", "✍️": "signature", "🤖": "bot",
      "⏳": "hourglass", "🐋": "whale", "⟳": "refresh-cw", "✕": "x-circle",
      "✓": "check", "✗": "x-circle", "⚙": "settings",
      "ℹ️": "info", "📂": "folder-open", "🗒️": "notepad-text", "🎨": "palette",
      "👀": "eye", "💀": "skull", "👻": "ghost", "⏰": "alarm-clock",
    };
    const _EMOJI_RE = new RegExp(Object.keys(EMOJI_LUCIDE_MAP).map(e => e.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");

    function lucideReplaceEmoji(html) {
      if (document.documentElement.getAttribute("data-icon-style") !== "lucide") return html;
      if (!window.lucide || !window.lucide.icons) return html;
      return html.replace(_EMOJI_RE, (match) => {
        const iconName = EMOJI_LUCIDE_MAP[match];
        if (!iconName) return match;
        const iconDef = window.lucide.icons[iconName];
        if (!iconDef) return `<i data-lucide="${iconName}" class="msg-lucide-icon"></i>`;
        // Build inline SVG directly — no createIcons() needed
        const [tag, attrs, children] = iconDef;
        let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="msg-lucide-icon lucide-${iconName}">`;
        if (children) {
          for (const child of children) {
            if (Array.isArray(child)) {
              const [cTag, cAttrs] = child;
              svg += `<${cTag}`;
              for (const [k, v] of Object.entries(cAttrs)) svg += ` ${k}="${v}"`;
              svg += "/>";
            }
          }
        }
        svg += "</svg>";
        return svg;
      });
    }

    function activateLucideIcons(container) {
      if (document.documentElement.getAttribute("data-icon-style") !== "lucide") return;
      if (window.lucide) lucide.createIcons({ nodes: (container || document).querySelectorAll("[data-lucide]") });
    }

    /** Returns emoji or inline lucide SVG depending on current icon style */
    function lucideIcon(emoji) {
      if (document.documentElement.getAttribute("data-icon-style") !== "lucide") return emoji;
      const iconName = EMOJI_LUCIDE_MAP[emoji];
      if (!iconName || !window.lucide || !window.lucide.icons) return emoji;
      const iconDef = window.lucide.icons[iconName];
      if (!iconDef) return `<i data-lucide="${iconName}" class="msg-lucide-icon"></i>`;
      const [tag, attrs, children] = iconDef;
      let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="msg-lucide-icon lucide-${iconName}">`;
      if (children) {
        for (const child of children) {
          if (Array.isArray(child)) {
            const [cTag, cAttrs] = child;
            svg += `<${cTag}`;
            for (const [k, v] of Object.entries(cAttrs)) svg += ` ${k}="${v}"`;
            svg += "/>";
          }
        }
      }
      svg += "</svg>";
      return svg;
    }

    function countOpenFences(text) {
      // Returns { inFence: bool, fenceChar: string } — single source of truth
      // for fence state. Used by both closeUnclosedFences and the typewriter.
      let inFence = false;
      let fenceChar = "";
      const lines = text.split("\n");
      for (let i = 0; i < lines.length; i++) {
        const m = lines[i].match(/^(```|~~~)/);
        if (m) {
          if (!inFence) {
            inFence = true;
            fenceChar = m[1];
          } else if (lines[i].trim().startsWith(fenceChar)) {
            inFence = false;
            fenceChar = "";
          }
        }
      }
      return { inFence, fenceChar };
    }

    function closeUnclosedFences(text) {
      const { inFence, fenceChar } = countOpenFences(text);
      if (inFence) {
        return text + "\n" + fenceChar;
      }
      return text;
    }

    function renderMarkdown(raw) {
      if (!raw) return "";
      // Backend parser already strips <tool_call> and <action> tags from the
      // answer stream. No frontend stripping needed.
      var _s = String(raw);
      raw = _s.trim();
      const normalized = normalizeMd(raw.replace(/\r\n/g, "\n"));
      const safe = closeUnclosedFences(normalized);

      ensureMarked();
      if (window.marked && window.DOMPurify && !usesLegacyExtras(safe)) {
        try {
          // escapeNonHtmlTags only feeds the marked+DOMPurify path — it stops
          // unknown prose tags from being swallowed. The legacy parser below
          // escapes everything itself, so pre-escaping here would double-encode.
          const html = marked.parse(escapeNonHtmlTags(safe));
          return lucideReplaceEmoji(DOMPurify.sanitize(html, { ADD_ATTR: ["target", "data-lucide"] }));
        } catch (err) {
          console.error("marked render failed:", err);
        }
      }

      try {
        return lucideReplaceEmoji(parseBlocks(safe.split("\n")));
      } catch (err) {
        console.error("Markdown render error:", err);
        return `<p>${escHtml(raw)}</p>`;
      }
    }

    /* ---------- mermaid post-render ---------- */
    let mermaidInited = false;
    function _initMermaid() {
      mermaid.initialize({
        startOnLoad: false,
        theme: "dark",
        securityLevel: "loose",
        fontFamily: "Maple Mono, ui-monospace, monospace",
        fontSize: 12,
        flowchart: { nodeSpacing: 20, rankSpacing: 30, useMaxWidth: false },
        themeVariables: {
          primaryColor: "#c9a464",
          primaryTextColor: "#eaeaea",
          primaryBorderColor: "#26262a",
          lineColor: "#85858c",
          secondaryColor: "#1d1d20",
          tertiaryColor: "#17171a",
          fontSize: "12px"
        }
      });
      mermaidInited = true;
    }
    /* ---------- mermaid viewer controls (github-style) ---------- */
    function _mmIcon(d) {
      return `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
    }
    function attachMermaidControls(mwrap, svgEl, code) {
      const sc = mwrap.querySelector(".mermaid-scroll") || mwrap;
      const baseW = parseFloat(svgEl.getAttribute("width")) || 800;
      const baseH = parseFloat(svgEl.getAttribute("height")) || 600;
      let scale = 1;
      const mk = (title, icon, fn) => {
        const b = document.createElement("button");
        b.className = "mm-btn";
        b.title = title;
        b.innerHTML = icon;
        b.addEventListener("click", e => { e.stopPropagation(); fn(b); });
        return b;
      };
      const setZoom = f => {
        scale = Math.min(4, Math.max(0.25, scale * f));
        svgEl.style.width = baseW * scale + "px";
        svgEl.style.height = baseH * scale + "px";
      };
      const pan = (dx, dy) => sc.scrollBy({ left: dx, top: dy, behavior: "smooth" });
      const I = {
        up: '<path d="m6 14 6-6 6 6"/>', down: '<path d="m6 10 6 6 6-6"/>',
        left: '<path d="m14 6-6 6 6 6"/>', right: '<path d="m10 6 6 6-6 6"/>',
        zin: '<path d="M12 5v14M5 12h14"/>', zout: '<path d="M5 12h14"/>',
        reset: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
        full: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
        copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
        fit: '<path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="m14 10 7-7"/><path d="m3 21 7-7"/>'
      };
      const top = document.createElement("div");
      top.className = "mm-top";
      const fit = mk("Fit to width", _mmIcon(I.fit), b => {
        const on = mwrap.classList.toggle("mm-fit");
        b.classList.toggle("mm-on", on);
        if (on) { scale = 1; svgEl.style.width = ""; svgEl.style.height = ""; }
      });
      top.append(
        fit,
        mk("Toggle fullscreen", _mmIcon(I.full), () => mwrap.classList.toggle("mm-full")),
        mk("Copy source", _mmIcon(I.copy), b => {
          (navigator.clipboard ? navigator.clipboard.writeText(code) : Promise.reject())
            .catch(() => {})
            .finally(() => { b.classList.add("mm-ok"); setTimeout(() => b.classList.remove("mm-ok"), 900); });
        })
      );
      const pad = document.createElement("div");
      pad.className = "mm-pad";
      pad.append(
        mk("Pan up", _mmIcon(I.up), () => pan(0, -120)),
        mk("Zoom in", _mmIcon(I.zin), () => setZoom(1.25)),
        mk("Pan left", _mmIcon(I.left), () => pan(-120, 0)),
        mk("Reset view", _mmIcon(I.reset), () => {
          scale = 1;
          svgEl.style.width = ""; svgEl.style.height = "";
          mwrap.classList.remove("mm-fit");
          fit.classList.remove("mm-on");
          sc.scrollTo({ top: 0, left: 0, behavior: "smooth" });
        }),
        mk("Pan right", _mmIcon(I.right), () => pan(120, 0)),
        mk("Pan down", _mmIcon(I.down), () => pan(0, 120)),
        mk("Zoom out", _mmIcon(I.zout), () => setZoom(0.8))
      );
      const root = document.createElement("div");
      root.className = "mm-controls";
      root.append(top, pad);
      mwrap.appendChild(root);
      // drag to pan
      sc.addEventListener("pointerdown", e => {
        if (e.button !== 0 || e.target.closest(".mm-btn")) return;
        const sx = e.clientX, sy = e.clientY, sl = sc.scrollLeft, st = sc.scrollTop;
        sc.classList.add("mm-drag");
        const move = ev => { sc.scrollLeft = sl - (ev.clientX - sx); sc.scrollTop = st - (ev.clientY - sy); };
        const done = () => {
          sc.classList.remove("mm-drag");
          removeEventListener("pointermove", move);
          removeEventListener("pointerup", done);
        };
        addEventListener("pointermove", move);
        addEventListener("pointerup", done);
      });
      // ctrl/cmd + wheel = zoom
      sc.addEventListener("wheel", e => {
        if (!e.ctrlKey && !e.metaKey) return;
        e.preventDefault();
        setZoom(e.deltaY < 0 ? 1.15 : 1 / 1.15);
      }, { passive: false });
      // esc exits fullscreen
      document.addEventListener("keydown", e => {
        if (e.key === "Escape") mwrap.classList.remove("mm-full");
      });
    }
    async function renderMermaidDiagrams(container) {
      const els = (container || document).querySelectorAll("pre.mermaid:not([data-processed])");
      if (!els.length) return;
      if (!window.mermaid) { await window._lazyLoadMermaid(); }
      if (!window.mermaid) return;
      if (!mermaidInited) _initMermaid();
      for (const el of els) {
        const code = el.textContent.trim();
        const id = `mmd-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        try {
          const { svg } = await mermaid.render(id, code);
          const wrapper = document.createElement("div");
          wrapper.innerHTML = svg;
          const svgEl = wrapper.querySelector("svg");
          if (svgEl) {
            // Keep mermaid's fixed width/height (useMaxWidth:false) so 12px text
            // renders at true size. Only strip inline style (may force width:100%).
            svgEl.removeAttribute("style");
          }
          el.replaceChildren(svgEl);
          el.setAttribute("data-processed", "true");
          // GitHub-style viewer controls (zoom / pan / reset / fullscreen / copy)
          const mwrap = el.closest(".mermaid-wrap");
          if (mwrap && svgEl && !mwrap.querySelector(".mm-controls")) {
            attachMermaidControls(mwrap, svgEl, code);
          }
        } catch (err) {
          el.innerHTML = `<div class="mermaid-error">Mermaid error: ${escHtml(err.message || String(err))}</div>`;
          el.setAttribute("data-processed", "true");
          const errEl = document.getElementById("d" + id);
          if (errEl) errEl.remove();
        }
      }
    }

    /* ---------- mathjax post-render ---------- */
    async function renderMathJax(container) {
      if (!window.MathJax || !MathJax.typesetPromise) {
        await window._lazyLoadMathJax();
        if (!window.MathJax || !MathJax.typesetPromise) return;
      }
      const target = container || document.body;
      MathJax.typesetPromise([target]).catch(err => console.warn("MathJax typeset error:", err));
    }

    /* ============================= end markdown ============================= */
