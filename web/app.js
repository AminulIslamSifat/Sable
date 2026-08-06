    window.MathJax = {
      tex: {
        inlineMath: [['$', '$'], ['\\(', '\\)']],
        displayMath: [['$$', '$$'], ['\\[', '\\]']],
        processEscapes: true
      },
      options: {
        skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
      },
      startup: {
        typeset: false
      }
    };

    const chatsEl  = document.getElementById("chats");
    const chatEl   = document.getElementById("chat");  // wrapper — contains .tab-pane divs
    const tabBarEl = null; // tab bar removed — switching via sidebar
    let activePane = null;  // the visible .tab-pane (messages live here)
    const inputEl  = document.getElementById("input");
    const sendBtn  = document.getElementById("send");
    const newChatBtn = document.getElementById("newChat");

    const modelSelectEl = document.getElementById("modelSelect");
    const thinkingSwitcherEl = document.getElementById("thinkingSwitcher");
    const toastEl  = document.getElementById("toast");

    /* ---------- Auth gate ---------- */
    const TOKEN_KEY = "sable_token";
    const loginOverlay = document.getElementById("loginOverlay");
    const loginForm    = document.getElementById("loginForm");
    const loginTokenIn = document.getElementById("loginToken");
    const loginBtn     = document.getElementById("loginBtn");
    const loginError   = document.getElementById("loginError");

    const getToken = () => localStorage.getItem(TOKEN_KEY);
    const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
    const clearToken = () => localStorage.removeItem(TOKEN_KEY);

    // Inject the bearer token into every API request; bounce to login on 401.
    const _origFetch = window.fetch.bind(window);
    window.fetch = async (url, init = {}) => {
      const token = getToken();
      if (token) {
        init.headers = Object.assign({}, init.headers, { Authorization: "Bearer " + token });
      }
      const res = await _origFetch(url, init);
      if (res.status === 401 && typeof url === "string" && !url.includes("/api/login")) {
        clearToken();
        // Show login overlay without reloading — reloading causes an infinite
        // loop because background API calls (models, chats, etc.) all fire
        // before auth is established and each 401 triggers another reload.
        loginOverlay.classList.remove("hidden");
        loginTokenIn.focus();
      }
      return res;
    };

    function ensureAuth() {
      if (getToken()) {
        loginOverlay.classList.add("hidden");
        return Promise.resolve();
      }
      loginOverlay.classList.remove("hidden");
      return new Promise((resolve) => {
        // { once: true } prevents stacking a new listener on every ensureAuth() call
        loginForm.addEventListener("submit", async (e) => {
          e.preventDefault();
          const token = loginTokenIn.value.trim();
          if (!token) return;
          loginBtn.disabled = true;
          loginError.classList.add("hidden");
          try {
            const res = await _origFetch("/api/login", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ token }),
            });
            if (res.ok) {
              setToken(token);
              loginOverlay.classList.add("hidden");
              resolve();
            } else {
              loginError.textContent = "Invalid token. Try again.";
              loginError.classList.remove("hidden");
              loginTokenIn.value = "";
              loginTokenIn.focus();
            }
          } catch (err) {
            loginError.textContent = "Connection error. Try again.";
            loginError.classList.remove("hidden");
          } finally {
            loginBtn.disabled = false;
          }
        }, { once: true });
      });
    }

    const ACTIVE_CHAT_KEY = "sable_active_chat";
    const PARENT_KEY = "sable_parent_id";
    const MODEL_KEY = "sable_selected_model";
    const THINKING_MODE_KEY = "sable_selected_thinking_mode";

    // Typewriter animation config (fetched from /api/config/ui on init)
    // Adaptive: low-memory devices get larger batches to reduce layout thrashing
    const _lowMem = (navigator.deviceMemory || 8) < 8;
    let TW_CHARS = _lowMem ? 12 : 3;
    let TW_MS = _lowMem ? 50 : 12;

    // Used only if /api/models isn't available yet — keep in sync with
    // engine/config.py's MODELS list so the dropdowns work either way.
    const FALLBACK_MODELS = [
      {
        id: "qwen3.8-max-preview", label: "Qwen3.8 Max Preview",
        capabilities: { image: true, video: false, document: false, audio: false },
        thinking_modes: [{ id: "thinking", label: "Thinking" }],
      },
      {
        id: "qwen3.7-max", label: "Qwen3.7 Max",
        capabilities: { image: true, video: false, document: false, audio: false },
        thinking_modes: [
          { id: "fast", label: "Fast" },
          { id: "thinking", label: "Thinking" },
        ],
      },
      {
        id: "qwen3.7-plus", label: "Qwen3.7 Plus",
        capabilities: { image: true, video: false, document: false, audio: false },
        thinking_modes: [
          { id: "fast", label: "Fast" },
          { id: "auto", label: "Auto" },
          { id: "thinking", label: "Thinking" },
        ],
      },
    ];

    let modelList = FALLBACK_MODELS;
    let selectedModel = null;
    let selectedThinkingMode = null;

    let chatList    = [];
    let chatSearchQuery = '';
    let chatSearchResults = null; // null = not searched, array = search results
    let activeChatId = null;
    let parentId    = null;
    const activeStreams = new Map(); // chatId → AbortController
    const openTabs = new Map(); // chatId → { pane: HTMLElement, title: string }
    let creating    = false;


    const attachBtn     = document.getElementById("attachBtn");
    const fileInput     = document.getElementById("fileInput");
    const attachPreview = document.getElementById("attachPreview");
    const inputArea     = document.getElementById("inputArea");
    let pendingFiles = []; // { file: File, path: string|null, chip: HTMLElement }

    /* ---- Model capabilities → attach button ---- */
    const CAP_ACCEPT = {
      image: "image/*",
      video: "video/*",
      audio: "audio/*",
      document: ".pdf,.doc,.docx,.txt,.md,.csv,.xlsx,.xls,.pptx,.ppt",
    };

    function getActiveCapabilities() {
      const entry = modelList.find(m => m.id === selectedModel);
      return entry?.capabilities || {};
    }

    function updateAttachUI() {
      const caps = getActiveCapabilities();
      const accepts = Object.entries(CAP_ACCEPT)
        .filter(([key]) => caps[key])
        .map(([, accept]) => accept);

      if (accepts.length === 0) {
        // Model supports no attachments — disable the button
        attachBtn.style.display = "";
        attachBtn.disabled = true;
        attachBtn.style.opacity = "0.35";
        attachBtn.style.cursor = "not-allowed";
        fileInput.accept = "";
      } else {
        attachBtn.disabled = false;
        attachBtn.style.opacity = "";
        attachBtn.style.cursor = "";
        attachBtn.style.display = "";
        fileInput.accept = accepts.join(",");
      }
    }

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
            out.push(`<div class="mermaid-wrap"><pre class="mermaid">${escHtml(codeLines.join("\n"))}</pre></div>`);
          } else if (/^svg$/i.test(lang)) {
            const rawSvg = codeLines.join("\n");
            const clean = window.DOMPurify ? DOMPurify.sanitize(rawSvg, { USE_PROFILES: { svg: true, svgFilters: true } }) : rawSvg;
            out.push(`<div class="svg-wrap">${clean}</div>`);
          } else if (/^(markdown|md|obsidian)$/i.test(lang) && mdUnwrapDepth < 2) {
            mdUnwrapDepth++;
            const inner = parseBlocks(codeLines);
            mdUnwrapDepth--;
            out.push(`<div class="md-content md-unwrap">${inner}</div>`);
          } else {
            out.push(`<div class="code-block"><button class="code-copy-btn" title="Copy code"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><pre><code${lang ? ` class="language-${escAttr(lang)}"` : ""}>${escHtml(codeLines.join("\n"))}</code></pre></div>`);
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
                return `<div class="mermaid-wrap"><pre class="mermaid">${escHtml(text)}</pre></div>`;
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
              return `<div class="code-block"><button class="code-copy-btn" title="Copy code"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><pre><code${langAttr}>${body}</code></pre></div>`;
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
      // DOMPurify don't swallow them silently. Code spans and fenced blocks are
      // shielded first: their angle brackets are literal and get escaped exactly
      // once by marked's code renderer — pre-escaping them here double-encodes
      // the "&" and a literal "&lt;" leaks into rendered code.
      const stash = [];
      const hide = (m) => { stash.push(m); return " N" + (stash.length - 1) + " "; };
      text = text.replace(/(^|\n)(```|~~~)[^\n]*\n[\s\S]*?(?:\n\2[ \t]*(?=\n|$)|$)/g, hide);
      text = text.replace(/(`+)([^`]*?)\1/g, hide);
      text = text.replace(/<(\/?)([a-zA-Z_][\w.-]*)(\s[^>]*)?>/g, (match, slash, tag, rest) => {
        if (_HTML_TAGS.has(tag.toLowerCase())) return match;
        return match.replace(/</g, "&lt;").replace(/>/g, "&gt;");
      });
      return text.replace(/ N(\d+) /g, (m, i) => stash[+i]);
    }

    // ── Emoji → Lucide mapping for chat messages ──
    const EMOJI_LUCIDE_MAP = {
      "⚡": "zap", "🔥": "flame", "✅": "check-circle", "❌": "x-circle",
      "⚠️": "alert-triangle", "💡": "lightbulb", "📝": "file-text", "🔧": "wrench",
      "🚀": "rocket", "💻": "monitor", "📁": "folder", "🔒": "lock",
      "🌐": "globe", "⭐": "star", "❤️": "heart", "🎯": "target",
      "📊": "bar-chart-2", "🐛": "bug", "✨": "sparkles", "🔄": "refresh-cw",
      "📦": "package", "🗂️": "archive", "⏱️": "clock", "🧠": "cpu",
      "💾": "hard-drive", "🛠️": "tool", "📌": "pin", "🔑": "key",
      "🎉": "party-popper", "💬": "message-circle", "📎": "paperclip", "🖥️": "monitor",
      "⬆️": "arrow-up", "⬇️": "arrow-down", "➡️": "arrow-right", "⬅️": "arrow-left",
      "🔍": "search", "📋": "clipboard", "🗑️": "trash-2", "🗑": "trash-2", "⚙️": "settings",
      "🏗️": "building", "🧪": "flask-conical", "📡": "radio", "🔗": "link",
      "❓": "help-circle", "⛔": "ban", "❗": "alert-circle", "📄": "file",
      "🔬": "microscope", "👁️": "eye", "✍️": "pen-tool", "🤖": "bot",
      "⏳": "hourglass", "🐋": "database", "⟳": "refresh-cw", "✕": "x",
      "✓": "check", "✗": "x", "⚙": "settings",
      "ℹ️": "info", "📂": "folder-open", "🗒️": "notebook-pen", "🎨": "palette",
    };
    const _EMOJI_RE = new RegExp(Object.keys(EMOJI_LUCIDE_MAP).map(e => e.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");

    function lucideReplaceEmoji(html) {
      if (document.documentElement.getAttribute("data-icon-style") !== "lucide") return html;
      return html.replace(_EMOJI_RE, (match) => {
        const icon = EMOJI_LUCIDE_MAP[match];
        return icon ? `<i data-lucide="${icon}" class="msg-lucide-icon"></i>` : match;
      });
    }

    function activateLucideIcons(container) {
      if (document.documentElement.getAttribute("data-icon-style") !== "lucide") return;
      if (window.lucide) lucide.createIcons({ nodes: (container || document).querySelectorAll("[data-lucide]") });
    }

    /** Returns emoji or lucide <i> tag depending on current icon style */
    function lucideIcon(emoji) {
      if (document.documentElement.getAttribute("data-icon-style") !== "lucide") return emoji;
      const icon = EMOJI_LUCIDE_MAP[emoji];
      return icon ? `<i data-lucide="${icon}" class="msg-lucide-icon"></i>` : emoji;
    }

    function closeUnclosedFences(text) {
      // Count fence openers/closers to detect unclosed code blocks.
      // A line is a fence opener/closer only if it matches ^(```|~~~) at start.
      // We track state properly so nested or mismatched fences don't confuse us.
      let inFence = false;
      let fenceChar = "";
      const lines = text.split("\n");
      for (let i = 0; i < lines.length; i++) {
        const m = lines[i].match(/^(```|~~~)\s*(\S*)\s*$/);
        if (m) {
          if (!inFence) {
            inFence = true;
            fenceChar = m[1];
          } else if (lines[i].trim() === fenceChar) {
            inFence = false;
            fenceChar = "";
          }
        }
      }
      if (inFence) {
        return text + "\n" + fenceChar;
      }
      return text;
    }

    function renderMarkdown(raw) {
      if (!raw) return "";
      // Strip agentic <action>...</action> blocks — metadata, not user-visible content
      raw = String(raw).replace(/<action>[\s\S]*?<\/action>/gi, "").trim();
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
    async function renderMermaidDiagrams(container) {
      const els = (container || document).querySelectorAll("pre.mermaid:not([data-processed])");
      if (!els.length) return;
      if (!window.mermaid) { await window._lazyLoadMermaid(); }
      if (!window.mermaid) return;
      if (!mermaidInited) {
        mermaid.initialize({
          startOnLoad: false,
          theme: "dark",
          securityLevel: "loose",
          fontFamily: "Maple Mono, ui-monospace, monospace",
          themeVariables: {
            primaryColor: "#c9a464",
            primaryTextColor: "#eaeaea",
            primaryBorderColor: "#26262a",
            lineColor: "#85858c",
            secondaryColor: "#1d1d20",
            tertiaryColor: "#17171a"
          }
        });
        mermaidInited = true;
      }
      for (const el of els) {
        const code = el.textContent.trim();
        const id = `mmd-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        try {
          const { svg } = await mermaid.render(id, code);
          const wrapper = document.createElement("div");
          wrapper.innerHTML = svg;
          const svgEl = wrapper.querySelector("svg");
          if (svgEl) {
            svgEl.removeAttribute("width");
            svgEl.removeAttribute("height");
            svgEl.style.width = "auto";
            svgEl.style.height = "auto";
          }
          el.innerHTML = wrapper.innerHTML;
          el.setAttribute("data-processed", "true");
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

    let toastTimer = null;
    function showToast(msg, type = "info") {
      toastEl.textContent = msg;
      toastEl.classList.remove("success", "info", "error");
      toastEl.classList.add(type, "show");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toastEl.classList.remove("show"), 3500);
    }
    window.showToast = showToast; // expose for filesystem.js

    // mobile: tap to dismiss toast immediately
    toastEl.addEventListener("click", () => {
      clearTimeout(toastTimer);
      toastEl.classList.remove("show");
    });

    // mobile browsers throttle setTimeout in background tabs —
    // dismiss any stale toast when the tab regains focus
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && toastEl.classList.contains("show")) {
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toastEl.classList.remove("show"), 800);
      }
    });

    function saveActiveChat() {
      try {
        if (activeChatId) localStorage.setItem(ACTIVE_CHAT_KEY, activeChatId);
        else localStorage.removeItem(ACTIVE_CHAT_KEY);

        if (parentId) localStorage.setItem(PARENT_KEY, parentId);
        else localStorage.removeItem(PARENT_KEY);
      } catch (err) {
        console.warn("Could not persist active chat:", err);
      }
    }

    /* ---------- Multi-tab infrastructure ---------- */

    function createTabPane(chatId) {
      // Remove the wrapper-level "Start a chat" placeholder once real panes exist
      const wrapperEmpty = chatEl.querySelector(":scope > .empty");
      if (wrapperEmpty) wrapperEmpty.remove();

      const pane = document.createElement("div");
      pane.className = "tab-pane";
      pane.dataset.chatId = chatId;
      pane.innerHTML = `<div class="empty"><h2>New conversation</h2><p>Send the first message.</p></div>`;
      chatEl.appendChild(pane);
      return pane;
    }

    function ensurePane(chatId) {
      if (openTabs.has(chatId)) return openTabs.get(chatId).pane;
      const pane = createTabPane(chatId);
      const meta = chatList.find(c => c.id === chatId);
      openTabs.set(chatId, { pane, title: meta?.title || "New chat" });
      return pane;
    }

    function switchToTab(chatId) {
      const pane = ensurePane(chatId);
      // Hide all panes, show target
      for (const [, tab] of openTabs) {
        tab.pane.classList.remove("active");
      }
      pane.classList.add("active");
      activePane = pane;
      activeChatId = chatId;
      updateSendBtn();
      renderTabBar();
      if (typeof window.updateCompactTitle === "function") {
        const tab = openTabs.get(chatId);
        window.updateCompactTitle(tab?.title || "New chat");
      }
    }

    function closeTab(chatId) {
      const tab = openTabs.get(chatId);
      if (!tab) return;
      tab.pane.remove();
      openTabs.delete(chatId);

      // If we closed the active tab, focus another
      if (activeChatId === chatId) {
        const remaining = [...openTabs.keys()];
        if (remaining.length > 0) {
          selectChat(remaining[remaining.length - 1]);
        } else {
          activeChatId = null;
          activePane = null;
          parentId = null;
          saveActiveChat();
          chatEl.innerHTML = `<div class="empty"><h2>Start a chat</h2><p>Create a new chat and talk to Sable.</p></div>`;
        }
      }
      renderTabBar();
    }

    function renderTabBar() { /* no-op: switching via sidebar */ }

    /* ---------- end multi-tab ---------- */

    function isNearBottom() {
      if (!activePane) return true;
      return activePane.scrollHeight - activePane.scrollTop - activePane.clientHeight < activePane.clientHeight * 0.25;
    }

    let _scrollLast = 0;
    let _scrollForChat = null;
    function scrollBottom(force) {
      if (!activePane) return;
      const now = performance.now();
      if (_scrollForChat !== activeChatId) _scrollLast = 0;
      _scrollForChat = activeChatId;
      if (!force && now - _scrollLast < 100) return;
      _scrollLast = now;
      if (force || isNearBottom()) {
        activePane.scrollTop = activePane.scrollHeight;
      }
    }

    function clearEmptyState() {
      if (!activePane) return;
      const empty = activePane.querySelector(".empty");
      if (empty) empty.remove();
    }

    function isStreaming(chatId) { return activeStreams.has(chatId ?? activeChatId); }

    function updateSendBtn() {
      // Always derive from the currently-viewed chat, never from a stale caller
      const streaming = activeStreams.has(activeChatId);
      sendBtn.classList.toggle("stop-mode", streaming);
      sendBtn.classList.remove("loading");
    }

    function startStream(chatId) {
      const controller = new AbortController();
      activeStreams.set(chatId, controller);
      if (chatId === activeChatId) updateSendBtn();
      renderChats();
      return controller;
    }

    function endStream(chatId) {
      activeStreams.delete(chatId);
      if (chatId === activeChatId) updateSendBtn();
      renderChats();
    }

    function setCreating(val) {
      creating = val;
      if (newChatBtn) {
        newChatBtn.disabled = val;
        newChatBtn.classList.toggle("loading", val);
      }
      const floatBtn = document.getElementById("newChatFloat");
      if (floatBtn) {
        floatBtn.disabled = val;
        floatBtn.classList.toggle("loading", val);
      }
      modelSelectEl.disabled = val;
      thinkingSwitcherEl.style.display = val ? "none" : "";
    }

    function autoResize() {
      inputEl.style.height = "auto";
      inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
    }

    inputEl.addEventListener("input", autoResize);

    // Shared skill-card DOM builders — used both for live streaming (inside
    // addBotStreaming) and for replaying persisted skill_events when a chat's
    // history is reloaded (addHistoryMessage). Keeping one implementation
    // means history looks identical to what was shown live.
    function createSkillCard(evt) {
      const card = document.createElement("div");
      card.className = "skill-card";
      const name = evt.name || "skill";
      let initial = evt.data && evt.data.content ? String(evt.data.content) : "";
      // For tags without content (view_file, insert_file, etc.), show the
      // key attributes so the card isn't just a blank "⚡ view_file" box.
      // Backend nests attrs under data.attrs — check both levels.
      if (!initial && evt.data) {
        const d = evt.data.attrs || evt.data;
        const parts = [];
        if (name === "spawn_agent") {
          if (d.task) parts.push(`task: ${d.task.slice(0, 80)}`);
          if (d.model) parts.push(`model: ${d.model}`);
          if (d.collect === "true") parts.push("collect: true");
          if (d.timeout) parts.push(`timeout: ${d.timeout}s`);
        } else {
          if (d.path) parts.push(d.path);
          if (d.start != null) parts.push(`L${d.start}`);
          if (d.end != null) parts.push(`–${d.end}`);
          if (d.at_line != null) parts.push(`@L${d.at_line}`);
          if (d.after_str) parts.push(`after "${d.after_str.slice(0, 40)}"`);
          if (d.full === "true" || d.full === true) parts.push("(full)");
        }
        if (parts.length) initial = parts.join("\n");
      }

      const header = document.createElement("div");
      header.className = "skill-header";
      header.onclick = () => card.classList.toggle("collapsed");

      const left = document.createElement("div");
      left.className = "skill-header-left";

      const arrow = document.createElement("span");
      arrow.className = "skill-arrow";
      arrow.innerHTML = '<i data-lucide="chevron-down"></i>';

      const nameEl = document.createElement("span");
      nameEl.className = "skill-name";
      // Specialized header for spawn_agent
      const _roleIcons = { researcher: "🔬", coder: "💻", reviewer: "👁️", writer: "✍️" };
      if (name === "spawn_agent") {
        const _r = (evt.data && (evt.data.attrs || evt.data).role) || "agent";
        nameEl.innerHTML = `${lucideIcon(_roleIcons[_r] || "🤖")} spawn · ${_r}`;
      } else {
        nameEl.innerHTML = lucideIcon("⚡") + " " + escHtml(name);
      }

      left.appendChild(arrow);
      left.appendChild(nameEl);

      const statusEl = document.createElement("span");
      statusEl.className = "skill-status";
      statusEl.textContent = "running…";

      const toggleBtn = document.createElement("button");
      toggleBtn.className = "skill-toggle-btn";
      toggleBtn.textContent = "Output";
      toggleBtn.onclick = (e) => {
        e.stopPropagation();
        const showing = card.classList.toggle("show-output");
        toggleBtn.textContent = showing ? "Command" : "Output";
      };

      const right = document.createElement("div");
      right.className = "skill-header-right";
      right.style.display = "flex";
      right.style.alignItems = "center";
      right.style.gap = "8px";
      right.appendChild(statusEl);
      right.appendChild(toggleBtn);

      header.appendChild(left);
      header.appendChild(right);

      const cmdPre = document.createElement("pre");
      cmdPre.className = "skill-command";
      if (initial) cmdPre.textContent = initial + "\n";

      const outPre = document.createElement("pre");
      outPre.className = "skill-output";

      card.appendChild(header);
      card.appendChild(cmdPre);
      card.appendChild(outPre);
      return card;
    }

    function appendSkillCardOutput(card, text) {
      card.querySelector(".skill-output").textContent += text || "";
    }

    function finishSkillCard(card, evt) {
      const status = card.querySelector(".skill-status");
      const pre = card.querySelector(".skill-output");
      status.textContent = (evt.ok ? "done · " : "failed · ") + (evt.duration_ms ?? 0) + "ms";
      status.style.color = evt.ok ? "var(--ok)" : "var(--danger)";
      // Show agent_id in the card name after spawn completes
      const result = evt.result || {};
      if (evt.name === "spawn_agent" && result.agent_id) {
        const nameEl = card.querySelector(".skill-name");
        if (nameEl) nameEl.textContent += `  #${result.agent_id.slice(0, 8)}`;
      }
      if (evt.error) pre.textContent += `\n[error] ${evt.error}`;
      if (!evt.ok) pre.classList.add("error");

      if (result.url && result.mime && String(result.mime).startsWith("image/")) {
        const img = document.createElement("img");
        img.src = result.url;
        img.className = "skill-image";
        card.appendChild(img);
      }
    }

    function addMessage(kind, text, images) {
      clearEmptyState();
      const div = document.createElement("div");
      div.className = `msg ${kind}`;
      if (kind === "user") {
        const now = new Date();
        const ts = `[${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')} ${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}]`;
        const tsEl = document.createElement("div");
        tsEl.className = "msg-timestamp";
        tsEl.textContent = ts;
        div.appendChild(tsEl);
        if (text) {
          const textEl = document.createElement("div");
          textEl.className = "user-text";
          textEl.textContent = text;
          div.appendChild(textEl);
          if (text.length > 300) {
            div.classList.add("collapsed");
            const expandBtn = document.createElement("button");
            expandBtn.className = "user-expand-btn";
            expandBtn.textContent = "Show more";
            expandBtn.addEventListener("click", () => {
              const isCollapsed = div.classList.toggle("collapsed");
              expandBtn.textContent = isCollapsed ? "Show more" : "Show less";
            });
            div.appendChild(expandBtn);
          }
        }
        if (Array.isArray(images) && images.length) {
          const imgWrap = document.createElement("div");
          imgWrap.className = "user-images";
          for (const src of images) {
            const img = document.createElement("img");
            img.src = src;
            img.addEventListener("click", () => window.open(src, "_blank"));
            imgWrap.appendChild(img);
          }
          div.appendChild(imgWrap);
        }
        // Copy toolbar for user messages
        if (text) {
          const toolbar = document.createElement("div");
          toolbar.className = "msg-toolbar";
          const copyBtn = document.createElement("button");
          copyBtn.innerHTML = '<i data-lucide="copy"></i>';
          copyBtn.title = "Copy";
          copyBtn.addEventListener("click", () => {
            // Read from DOM at click-time for reliability
            const userTextEl = div.querySelector(".user-text");
            const copyText = userTextEl ? userTextEl.textContent : text;
            const onSuccess = () => {
              copyBtn.innerHTML = '<i data-lucide="check"></i>';
              activateLucideIcons(copyBtn);
              setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; activateLucideIcons(copyBtn); }, 1500);
            };
            if (navigator.clipboard && window.isSecureContext) {
              navigator.clipboard.writeText(copyText).then(onSuccess).catch(() => {
                // Fallback for non-secure contexts
                const ta = document.createElement("textarea");
                ta.value = copyText;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.select();
                document.execCommand("copy");
                document.body.removeChild(ta);
                onSuccess();
              });
            } else {
              const ta = document.createElement("textarea");
              ta.value = copyText;
              ta.style.position = "fixed";
              ta.style.opacity = "0";
              document.body.appendChild(ta);
              ta.select();
              document.execCommand("copy");
              document.body.removeChild(ta);
              onSuccess();
            }
          });
          toolbar.appendChild(copyBtn);
          div.appendChild(toolbar);
          activateLucideIcons(toolbar);
        }
      } else {
        const content = document.createElement("div");
        content.className = "md-content";
        content.innerHTML = renderMarkdown(text);
        renderMermaidDiagrams(content);
        renderMathJax(content);
        activateLucideIcons(content);
        div.appendChild(content);
      }
      activePane.appendChild(div);
      scrollBottom(true);
      return div;
    }

    // ---------- memory-used chip + popup ----------
    function createMemoryChip(memories) {
      const chip = document.createElement("button");
      chip.className = "memory-chip";
      chip.innerHTML = `${lucideIcon("🧠")} Memory Used (${memories.length})`;
      chip.title = "Show the memories injected into this message";
      chip.addEventListener("click", () => openMemoryPopup(memories));
      return chip;
    }

    function attachMemoryChip(userMsgDiv, memories) {
      if (!userMsgDiv || !Array.isArray(memories) || !memories.length) return;
      if (userMsgDiv.querySelector(".memory-chip")) return;  // no duplicates
      const chip = createMemoryChip(memories);
      const toolbar = userMsgDiv.querySelector(".msg-toolbar");
      if (toolbar) userMsgDiv.insertBefore(chip, toolbar);
      else userMsgDiv.appendChild(chip);
    }

    function openMemoryPopup(memories) {
      document.querySelectorAll(".memory-overlay").forEach((el) => el.remove());
      const overlay = document.createElement("div");
      overlay.className = "memory-overlay";

      const panel = document.createElement("div");
      panel.className = "memory-panel";

      const header = document.createElement("div");
      header.className = "memory-header";
      const h = document.createElement("h2");
      h.innerHTML = `${lucideIcon("🧠")} Memory Used (${memories.length})`;
      const closeBtn = document.createElement("button");
      closeBtn.className = "memory-close";
      closeBtn.textContent = "✕";
      closeBtn.addEventListener("click", () => overlay.remove());
      header.appendChild(h);
      header.appendChild(closeBtn);
      panel.appendChild(header);

      const body = document.createElement("div");
      body.className = "memory-body";
      for (const m of memories) {
        const item = document.createElement("div");
        item.className = "memory-item";

        const top = document.createElement("div");
        top.className = "memory-item-top";
        const key = document.createElement("span");
        key.className = "memory-item-key";
        key.textContent = m.key || "(untitled)";
        const cat = document.createElement("span");
        cat.className = "memory-cat memory-cat-" + (m.category || "general");
        cat.textContent = m.category || "general";
        const score = document.createElement("span");
        score.className = "memory-score";
        score.textContent = m.score != null ? `score ${m.score}` : "";
        top.appendChild(key);
        top.appendChild(cat);
        top.appendChild(score);

        const val = document.createElement("div");
        val.className = "memory-item-val";
        val.textContent = m.value || "";

        item.appendChild(top);
        item.appendChild(val);
        body.appendChild(item);
      }
      panel.appendChild(body);
      overlay.appendChild(panel);

      overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
      document.body.appendChild(overlay);

      const onEsc = (e) => {
        if (e.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", onEsc); }
      };
      document.addEventListener("keydown", onEsc);
    }

    // ---------- file edit sidebar ----------
    const diffSidebarEl = document.getElementById("diffSidebar");
    const diffCardsEl = document.getElementById("diffCards");
    const diffCloseBtn = document.getElementById("diffClose");
    const diffClearBtn = document.getElementById("diffClear");
    const diffToggleBtn = document.getElementById("diffToggleBtn");
    const MAX_DIFF_CARDS = 12;

    if (diffCloseBtn) diffCloseBtn.addEventListener("click", () => document.body.classList.remove("diff-open"));
    if (diffClearBtn) diffClearBtn.addEventListener("click", () => { if (diffCardsEl) diffCardsEl.innerHTML = ""; });
    if (diffToggleBtn) diffToggleBtn.addEventListener("click", () => {
      const opening = !document.body.classList.contains("diff-open");
      document.body.classList.toggle("diff-open");
      if (opening && typeof AgentPanel !== "undefined") AgentPanel.close();
    });

    function diffLineEl(cls, text) {
      const d = document.createElement("div");
      d.className = `diff-line ${cls}`;
      d.textContent = text ?? "";
      return d;
    }

    function handleFileEdit(evt, autoOpen) {
      if (!diffCardsEl || !evt) return;
      const item = document.createElement("div");
      item.className = "diff-item";

      const name = document.createElement("div");
      name.className = "diff-item-name";
      name.textContent = evt.name || evt.path || "file";
      name.title = evt.path || "";
      name.addEventListener("click", () => {
        if (evt.backup_path && typeof window.openDiffEditor === "function") {
          window.openDiffEditor(evt.path, evt.backup_path, evt.name || evt.path);
        }
      });

      const stats = document.createElement("div");
      stats.className = "diff-item-stats";
      const added = evt.added || 0;
      const removed = evt.removed || 0;
      stats.innerHTML = `<span class="diff-add-stat">+${added}</span><span class="diff-sep"> / </span><span class="diff-del-stat">-${removed}</span>`;

      const revertBtn = document.createElement("button");
      revertBtn.className = "diff-revert-btn";
      revertBtn.textContent = "\u21a9 Revert";
      if (!evt.backup_path) revertBtn.disabled = true;
      revertBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!evt.backup_path || revertBtn.disabled) return;
        revertBtn.disabled = true;
        revertBtn.textContent = "Reverting\u2026";
        try {
          const res = await fetch("/api/file/revert", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: evt.path, backup_path: evt.backup_path }),
          });
          const data = await res.json().catch(() => ({}));
          if (res.ok && data.status === "ok") {
            revertBtn.textContent = "\u2713 Reverted";
            showToast("File reverted successfully", "success");
          } else {
            revertBtn.textContent = "\u2717 Failed";
            showToast(data.detail || "Revert failed", "error");
          }
        } catch (err) {
          revertBtn.textContent = "\u2717 Error";
          showToast("Revert error: " + err.message, "error");
        }
        setTimeout(() => { revertBtn.textContent = "\u21a9 Revert"; revertBtn.disabled = false; }, 2000);
      });

      item.append(name, stats, revertBtn);
      diffCardsEl.prepend(item);
      while (diffCardsEl.children.length > MAX_DIFF_CARDS) {
        diffCardsEl.lastElementChild.remove();
      }
      if (autoOpen) {
        document.body.classList.add("diff-open");
        if (typeof AgentPanel !== "undefined") AgentPanel.close();
      }
    }

    function addHistoryMessage(message) {
      clearEmptyState();
      const events = Array.isArray(message.skill_events) ? message.skill_events : [];
      // New messages store per-round thinking inside skill_events as
      // "round_thinking" entries so we can rebuild the t1,c1,t2,c2 layout.
      // Legacy messages only have the flat `thinking` blob — fall back to a
      // single block at the top for those.
      const hasRoundThinking = events.some((e) => e.type === "round_thinking");
      if (message.thinking && !hasRoundThinking) {
        const wrap = document.createElement("div");
        wrap.className = "thinking-wrap";
        wrap.innerHTML = `
          <details class="thinking">
            <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking</summary>
            <div class="thinking-body">${escHtml(message.thinking)}</div>
          </details>`;
        activePane.appendChild(wrap);
        activateLucideIcons(wrap);
      }

      let hasRoundText = false;
      if (events.length > 0) {
        const cards = {};
        let group = null;
        for (const evt of events) {
          if (evt.type === "round_thinking") {
            // This round's thought — render it, then force the next commands
            // into a brand-new stack placed right after it.
            group = null;
            const wrap = document.createElement("div");
            wrap.className = "thinking-wrap";
            wrap.innerHTML = `
              <details class="thinking">
                <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking</summary>
                <div class="thinking-body">${escHtml(evt.text || "")}</div>
              </details>`;
            activePane.appendChild(wrap);
            activateLucideIcons(wrap);
          } else if (evt.type === "round_text") {
            // Per-round text — render inline within the chat flow so it
            // interleaves with tool cards instead of grouping at the bottom.
            hasRoundText = true;
            if (evt.text && evt.text.trim()) {
              const textDiv = document.createElement("div");
              textDiv.className = "msg bot";
              const content = document.createElement("div");
              content.className = "md-content";
              content.innerHTML = renderMarkdown(evt.text);
              renderMermaidDiagrams(content);
              renderMathJax(content);
              activateLucideIcons(content);
              textDiv.appendChild(content);
              activePane.appendChild(textDiv);
            }
          } else if (evt.type === "skill_start") {
            if (evt.name === "ask_user") continue; // MCQ rendered on skill_output
            if (!group) {
              group = document.createElement("div");
              group.className = "skill-stack";
              group.style.display = "flex";
              activePane.appendChild(group);
            }
            const card = createSkillCard(evt);
            group.appendChild(card);
            activateLucideIcons(card);
            cards[evt.id] = card;
          } else if (evt.type === "skill_output") {
            if (evt.name === "ask_user") {
              try {
                const card = renderAskUserCard(JSON.parse(evt.text), activePane);
                card.classList.add("answered");
                card.querySelectorAll("button").forEach(b => b.disabled = true);
              } catch(e) { /* skip malformed */ }
              continue;
            }
            const card = cards[evt.id];
            if (card) appendSkillCardOutput(card, evt.text);
          } else if (evt.type === "skill_end") {
            if (evt.name === "ask_user") continue;
            const card = cards[evt.id];
            if (card) finishSkillCard(card, evt);
          } else if (evt.type === "agent_result") {
            // Render persisted agent completion card (same as live SSE)
            if (typeof addAgentResultCard === "function") {
              addAgentResultCard({
                type: evt.ok ? "agent_completed" : "agent_failed",
                agent_id: evt.agent_id,
                data: evt.data || {},
              });
            }
          } else if (evt.type === "file_edit") {
            handleFileEdit(evt, false);
          } else if (evt.type === "memory_used") {
            // Tool-round memory chip — inject inline into the last skill card header
            if (Array.isArray(evt.memories) && evt.memories.length) {
              const chip = createMemoryChip(evt.memories);
              chip.classList.add("memory-chip-tool");
              const allCards = activePane.querySelectorAll(".skill-card");
              const target = allCards.length ? allCards[allCards.length - 1] : null;
              if (target) {
                const right = target.querySelector(".skill-header-right");
                if (right) right.insertBefore(chip, right.firstChild);
                else target.querySelector(".skill-header")?.appendChild(chip);
              } else {
                activePane.appendChild(chip);
              }
            }
          }
        }
      }

      let displayContent = message.content || "";
      let realTs = null;
      if (message.role === "user") {
        // Strip the injected memory block + timestamp prefix — the bubble shows
        // what the user typed; the memories live behind the chip instead.
        const memMatch = displayContent.match(/^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n/);
        if (memMatch) displayContent = displayContent.slice(memMatch[0].length);
        const tsMatch = displayContent.match(/^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\n?/);
        if (tsMatch) {
          realTs = tsMatch[1];
          displayContent = displayContent.slice(tsMatch[0].length);
        }
      }
      // If round_text events already rendered bot text inline, skip the final blob
      const msgDiv = (message.role !== "user" && hasRoundText)
        ? null
        : addMessage(message.role === "user" ? "user" : "bot", displayContent);
      if (message.role === "user" && msgDiv) {
        if (realTs) {
          const tsEl = msgDiv.querySelector(".msg-timestamp");
          if (tsEl) tsEl.textContent = `[${realTs}]`;
        }
        if (Array.isArray(message.memory_used) && message.memory_used.length) {
          attachMemoryChip(msgDiv, message.memory_used);
        }
      }
      // Attach toolbar to historical bot messages
      if (message.role !== "user" && msgDiv) {
        const toolbar = document.createElement("div");
        toolbar.className = "msg-toolbar";
        const copyBtn = document.createElement("button");
        copyBtn.innerHTML = '<i data-lucide="copy"></i>';
        copyBtn.title = "Copy";
        copyBtn.addEventListener("click", () => {
          const md = msgDiv.querySelector(".md-content");
          const text = md ? md.innerText : "";
          navigator.clipboard.writeText(text).then(() => {
            copyBtn.innerHTML = '<i data-lucide="check"></i>';
            activateLucideIcons(copyBtn);
            setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; activateLucideIcons(copyBtn); }, 1500);
          });
        });
        toolbar.appendChild(copyBtn);
        msgDiv.appendChild(toolbar);
        activateLucideIcons(toolbar);
      }
    }

    // ── Ask User MCQ Card ──
    function renderAskUserCard(payload, container) {
      const { question, options, multi, default: def } = payload;
      const card = document.createElement("div");
      card.className = "ask-user-card";

      const qEl = document.createElement("div");
      qEl.className = "ask-user-question";
      qEl.textContent = question;
      card.appendChild(qEl);

      const optWrap = document.createElement("div");
      optWrap.className = "ask-user-options";
      card.appendChild(optWrap);

      const manualWrap = document.createElement("div");
      manualWrap.className = "ask-user-manual";
      manualWrap.style.display = "none";
      manualWrap.innerHTML = `<input type="text" placeholder="Type your answer…" /><button class="ask-user-submit">Send</button>`;
      card.appendChild(manualWrap);

      function submitAnswer(answer) {
        card.classList.add("answered");
        card.querySelectorAll("button").forEach(b => b.disabled = true);
        const chosen = document.createElement("div");
        chosen.className = "ask-user-chosen";
        chosen.textContent = "→ " + answer;
        card.appendChild(chosen);
        // Send as normal user message
        inputEl.value = answer;
        sendMessage();
      }

      options.forEach((opt, i) => {
        const btn = document.createElement("button");
        btn.className = "ask-user-opt" + (i === def ? " default" : "");
        btn.textContent = opt;
        btn.addEventListener("click", () => {
          if (i === options.length - 1) {
            // Last option = manual escape hatch
            manualWrap.style.display = "flex";
            manualWrap.querySelector("input").focus();
            return;
          }
          submitAnswer(opt);
        });
        optWrap.appendChild(btn);
      });

      manualWrap.querySelector(".ask-user-submit").addEventListener("click", () => {
        const val = manualWrap.querySelector("input").value.trim();
        if (val) submitAnswer(val);
      });
      manualWrap.querySelector("input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          const val = e.target.value.trim();
          if (val) submitAnswer(val);
        }
      });

      container.appendChild(card);
      scrollBottom();
      return card;
    }

    // one "turn" holds everything for a single response: thinking, then any
    // skill/tool runs it made, then the final answer — all stacked in order,
    // scoped to just this response (not shared globally).
    function addBotStreaming() {
      clearEmptyState();

      // Capture the chat this turn belongs to — typewriter ticks and scroll
      // calls will bail if the user has switched away before they fire.
      const turnChatId = activeChatId;

      const turn = document.createElement("div");
      turn.className = "turn";
      activePane.appendChild(turn);

      // Immediate feedback that the message was sent and a response is on
      // its way — removed as soon as any real content (thinking, a skill
      // event, or an answer token) actually arrives.
      const pending = document.createElement("div");
      pending.className = "pending-indicator";
      pending.innerHTML = `<span class="processing-text">processing…</span>`;
      turn.appendChild(pending);
      let pendingShown = true;
      function hidePending() {
        if (!pendingShown) return;
        pendingShown = false;
        pending.remove();
        ensureAnswer();
      }

      // Per-round thinking: each agentic command gets its own thinking block
      // inserted right before its skill card, instead of one global bucket.
      let currentThinkWrap = null;
      let currentThinkBody = null;
      let currentThinkSummary = null;

      // ── Typewriter animation for thinking reveal ──
      let _thinkQueue = "";
      let _thinkTimer = null;
      function _thinkTick() {
        // Bail if user switched to a different chat while timer was pending
        if (turnChatId !== activeChatId) { _thinkTimer = null; return; }
        if (!_thinkQueue || !currentThinkBody) { _thinkTimer = null; return; }
        const chunk = _thinkQueue.slice(0, TW_CHARS);
        _thinkQueue = _thinkQueue.slice(TW_CHARS);
        currentThinkBody.textContent += chunk;
        scrollBottom();
        _thinkTimer = _thinkQueue ? setTimeout(_thinkTick, TW_MS) : null;
      }
      function _enqueueThink(text) {
        _thinkQueue += text;
        if (!_thinkTimer) _thinkTimer = setTimeout(_thinkTick, TW_MS);
      }
      function _flushThinkQueue() {
        if (_thinkTimer) { clearTimeout(_thinkTimer); _thinkTimer = null; }
        if (_thinkQueue && currentThinkBody) {
          currentThinkBody.textContent += _thinkQueue;
          _thinkQueue = "";
        }
      }

      function ensureThinkingBlock() {
        // Create a fresh thinking block for the current round.
        // It will be placed just before the next skill command group or answer.
        if (currentThinkWrap) return;
        // A new thinking block means a new round — the commands that follow it
        // must land in a fresh stack placed right after this block, not piled
        // into a previous round's stack. Gives the t1,c1,t2,c2 layout.
        lastCommandGroup = null;
        currentThinkWrap = document.createElement("div");
        currentThinkWrap.className = "thinking-wrap";
        currentThinkWrap.innerHTML = `
          <details class="thinking" open>
            <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking…</summary>
            <div class="thinking-body"></div>
          </details>`;
        currentThinkBody = currentThinkWrap.querySelector(".thinking-body");
        currentThinkSummary = currentThinkWrap.querySelector("summary");
        turn.appendChild(currentThinkWrap);
        activateLucideIcons(currentThinkWrap);
      }

      function closeCurrentThinking() {
        if (!currentThinkWrap) return;
        _flushThinkQueue();
        if (currentThinkSummary) { currentThinkSummary.innerHTML = '<i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking'; activateLucideIcons(currentThinkSummary); }
        const det = currentThinkWrap.querySelector("details");
        if (det) det.open = false;
        currentThinkWrap = null;
        currentThinkBody = null;
        currentThinkSummary = null;
      }

      const skillCards = {};
      let answerEl = null;
      let answerContent = null;
      let raw = "";
      let lastCommandGroup = null;
      let skillRounds = [[]];
      let sawNormalAnswer = false;
      let fileEditSummary = { count: 0, added: 0, removed: 0, card: null };
      let _tacExitTimer = null;

      function trackSkillEvent(evt) {
        skillRounds[skillRounds.length - 1].push(evt);
      }

      function ensureAnswer() {
        if (answerEl) return;
        answerEl = document.createElement("div");
        answerEl.className = "msg bot streaming msg-enter";
        const content = document.createElement("div");
        content.className = "md-content";
        answerEl.appendChild(content);
        answerContent = content;
        raw = "";
        turn.appendChild(answerEl);
        lastCommandGroup = null;
      }

      // ── Typewriter animation for answer reveal ──
      let _ansQueue = "";
      let _ansTimer = null;
      let _ansInFence = false;
      const _ANS_STRUCTURAL_RE = /[\n`|<>#*_\[~=~-]/;

      function _ansTick() {
        // Bail if user switched to a different chat while timer was pending
        if (turnChatId !== activeChatId) { _ansTimer = null; return; }
        if (!_ansQueue || !answerContent) { _ansTimer = null; return; }
        const chunk = _ansQueue.slice(0, TW_CHARS);
        _ansQueue = _ansQueue.slice(TW_CHARS);
        raw += chunk;

        // Fast path: plain text append — skip full markdown pipeline
        let fast = false;
        if (!_ansInFence && !_ANS_STRUCTURAL_RE.test(chunk)) {
          const lastP = answerContent.lastElementChild;
          if (lastP && lastP.tagName === "P" && lastP.lastChild && lastP.lastChild.nodeType === 3) {
            lastP.lastChild.textContent += chunk;
            fast = true;
          }
        }
        // Fast path: inside code fence — append to <code> directly until fence closes
        if (!fast && _ansInFence && !chunk.includes("```")) {
          const codeEls = answerContent.querySelectorAll(".code-block pre code");
          const codeEl = codeEls[codeEls.length - 1];
          if (codeEl) {
            codeEl.textContent += chunk;
            fast = true;
          }
        }
        if (!fast) {
          answerContent.innerHTML = renderMarkdown(raw);
          _ansInFence = (raw.match(/^```/gm) || []).length % 2 === 1;
        }

        scrollBottom();
        _ansTimer = _ansQueue ? setTimeout(_ansTick, TW_MS) : null;
        if (!_ansTimer) { renderMermaidDiagrams(answerContent); renderMathJax(answerContent); activateLucideIcons(answerContent); }
      }
      function _enqueueAnswer(text) {
        _ansQueue += text;
        if (!_ansTimer) _ansTimer = setTimeout(_ansTick, TW_MS);
      }
      function _flushAnswerQueue() {
        if (_ansTimer) { clearTimeout(_ansTimer); _ansTimer = null; }
        if (_ansQueue && answerContent) {
          raw += _ansQueue;
          _ansQueue = "";
          answerContent.innerHTML = renderMarkdown(raw);
          renderMermaidDiagrams(answerContent);
          renderMathJax(answerContent);
          activateLucideIcons(answerContent);
          scrollBottom();
        }
      }

      function closeAnswer() {
        _flushAnswerQueue();
        if (!answerEl) return;
        answerEl.classList.remove("streaming");
        if (!raw.trim()) answerEl.remove();
        answerEl = null;
        answerContent = null;
        raw = "";
        _ansInFence = false;
      }

      function ensureCommandGroup() {
        closeAnswer();
        if (!lastCommandGroup || !turn.contains(lastCommandGroup)) {
          lastCommandGroup = document.createElement("div");
          lastCommandGroup.className = "skill-stack";
          turn.appendChild(lastCommandGroup);
        }
        return lastCommandGroup;
      }

      scrollBottom();
      return {
        appendThinking(text) {
          hidePending();
          ensureThinkingBlock();
          _enqueueThink(text);
        },
        closeThinking() {
          closeCurrentThinking();
        },
        showRoundThinking(text) {
          if (!text) return;
          hidePending();
          closeCurrentThinking();
          lastCommandGroup = null;
          // Build an open thinking block and animate its content via typewriter
          currentThinkWrap = document.createElement("div");
          currentThinkWrap.className = "thinking-wrap";
          currentThinkWrap.innerHTML = `
            <details class="thinking" open>
              <summary><i data-lucide="chevron-right" class="thinking-chevron"></i>Thinking…</summary>
              <div class="thinking-body"></div>
            </details>`;
          currentThinkBody = currentThinkWrap.querySelector(".thinking-body");
          currentThinkSummary = currentThinkWrap.querySelector("summary");
          turn.appendChild(currentThinkWrap);
          activateLucideIcons(currentThinkWrap);
          _enqueueThink(text);
        },
        addSkillStart(evt) {
          hidePending();
          closeCurrentThinking();
          const group = ensureCommandGroup();
          group.style.display = "flex";
          const card = createSkillCard(evt);
          const placeholder = skillCards[evt.id];
          if (placeholder && placeholder.classList.contains("pending")) {
            placeholder.replaceWith(card);
          } else {
            group.appendChild(card);
          }
          skillCards[evt.id] = card;
          activateLucideIcons(card);
          trackSkillEvent(evt);
          // Keep activity card as last child so its exit never causes a layout jump
          const tac = turn.querySelector(".tool-activity-card");
          if (tac) turn.appendChild(tac);
          scrollBottom();
        },
        appendSkillOutput(evt) {
          const card = skillCards[evt.id];
          if (!card) return;
          trackSkillEvent(evt);
          appendSkillCardOutput(card, evt.text);
          scrollBottom();
        },
        finishSkill(evt) {
          const card = skillCards[evt.id];
          if (!card) return;
          trackSkillEvent(evt);
          finishSkillCard(card, evt);
          delete skillCards[evt.id];
        },
        addAskUser(payload) {
          hidePending();
          closeCurrentThinking();
          renderAskUserCard(payload, turn);
        },
        addEvent(text) {
          hidePending();
          const group = ensureCommandGroup();
          group.style.display = "flex";
          const div = document.createElement("div");
          div.className = "event";
          div.textContent = text;
          group.appendChild(div);
          scrollBottom();
        },
        nextSkillRound() {
          if (skillRounds[skillRounds.length - 1].length) skillRounds.push([]);
        },
        attachToolMemory(memories) {
          // Memories injected from a tool result — pin the chip inline
          // inside the last skill card's header-right.
          hidePending();
          const cards = turn.querySelectorAll(".skill-card");
          const target = cards.length ? cards[cards.length - 1] : null;
          const chip = createMemoryChip(memories);
          chip.classList.add("memory-chip-tool");
          if (target) {
            const right = target.querySelector(".skill-header-right");
            if (right) right.insertBefore(chip, right.firstChild);
            else target.querySelector(".skill-header")?.appendChild(chip);
          } else {
            turn.appendChild(chip);
          }
          scrollBottom();
        },
        appendAnswer(text) {
          hidePending();
          ensureAnswer();
          answerEl.style.display = "";
          if (text && text.trim() && !/\[(error|stopped|client error)\]/.test(text)) sawNormalAnswer = true;
          // Errors/stop messages render instantly; normal text gets typewriter
          if (/\[(error|stopped|client error)\]/.test(text)) {
            _flushAnswerQueue();
            raw += text;
            answerContent.innerHTML = renderMarkdown(raw);
            activateLucideIcons(answerContent);
            scrollBottom();
          } else {
            _enqueueAnswer(text);
          }
        },
        replaceWithRateLimit(message, hours) {
          hidePending();
          // Kill typewriter queues immediately — don't flush partial content
          if (_thinkTimer) { clearTimeout(_thinkTimer); _thinkTimer = null; }
          _thinkQueue = "";
          if (_ansTimer) { clearTimeout(_ansTimer); _ansTimer = null; }
          _ansQueue = "";
          currentThinkWrap = null;
          currentThinkBody = null;
          currentThinkSummary = null;
          // Remove only the currently-streaming partial answer — keep completed skill work
          if (answerEl) {
            answerEl.remove();
            answerEl = null;
            answerContent = null;
            raw = "";
          }
          // Build persistent rate-limit card
          ensureAnswer();
          answerEl.classList.remove('streaming');
          const h = hours || '?';
          answerContent.innerHTML = `
            <div class="rate-limit-card">
              <span class="rl-icon">⏳</span>
              <span class="rl-title">Daily Usage Limit Reached</span>
              <span class="rl-detail">${message || 'You have reached the upper limit for today\'s usage.'}</span>
              <span class="rl-timer">Try again in ~${h} hour${h === 1 ? '' : 's'}. This message will stay visible so you don't miss it.</span>
            </div>`;
          raw = "\u200B"; // non-empty so closeAnswer() won't remove the card
          scrollBottom();
        },
        trackFileEdit(evt) {
          fileEditSummary.count++;
          fileEditSummary.added += evt.added || 0;
          fileEditSummary.removed += evt.removed || 0;
          // Ensure answer container exists so we have somewhere to append the card
          ensureAnswer();
          if (!fileEditSummary.card) {
            const card = document.createElement("div");
            card.className = "file-edit-summary-card";
            card.addEventListener("click", () => {
            document.body.classList.add("diff-open");
            if (typeof AgentPanel !== "undefined") AgentPanel.close();
            // Switch sidebar tab to Diff mode
            document.querySelectorAll(".fs-sidebar-tab").forEach((t) => t.classList.remove("active"));
            const diffTab = document.querySelector('.fs-sidebar-tab[data-panel="diff"]');
            if (diffTab) diffTab.classList.add("active");
            const filesPanel = document.getElementById("sidebarFilesPanel");
            const diffPanel = document.getElementById("sidebarDiffPanel");
            if (filesPanel) filesPanel.classList.remove("active");
            if (diffPanel) diffPanel.classList.add("active");
          });
            fileEditSummary.card = card;
          }
          // Always re-parent to the current answerEl so the card follows the
          // latest agent round instead of staying stuck on the first one.
          if (fileEditSummary.card.parentNode !== answerEl) {
            answerEl.appendChild(fileEditSummary.card);
          }
          const c = fileEditSummary.card;
          const f = fileEditSummary.count;
          const a = fileEditSummary.added;
          const r = fileEditSummary.removed;
          c.innerHTML = `<span class="fes-icon">📝</span>` +
            `<span class="fes-text"><strong>${f}</strong> file${f === 1 ? "" : "s"} edited · ` +
            `<span class="fes-add">+${a}</span> / <span class="fes-del">-${r}</span></span>` +
            `<span class="fes-arrow">▶</span>`;
          scrollBottom();
        },
        showToolPending(evt) {
          hidePending();
          const tag = evt.tag || "tool";
          const attrs = evt.attrs || {};
          const meta = {
            create_file:  { icon: "📝", label: "Creating file", detail: attrs.path || "", progress: true },
            edit_file:    { icon: "✏️", label: "Editing file", detail: attrs.path || "", progress: true },
            insert_file:  { icon: "✏️", label: "Inserting into file", detail: attrs.path || "" },
            view_file:    { icon: "👁️", label: "Reading file", detail: attrs.path || (attrs.full ? "full file" : "") },
            execute_command: { icon: "⚡", label: "Running command", detail: "" },
            execute_background_command: { icon: "⚡", label: "Running background task", detail: "" },
            get_file:     { icon: "📂", label: "Loading file", detail: "" },
            create_note:  { icon: "🗒️", label: "Creating note", detail: attrs.path || "" },
            save_svg:     { icon: "🎨", label: "Saving SVG", detail: attrs.path || "" },
            spawn_agent:  { icon: "🤖", label: `Spawning ${attrs.role || "agent"}`, detail: (attrs.task || "").slice(0, 60), progress: true },
          };
          const info = meta[tag] || { icon: "⚙️", label: tag, detail: "" };
          // Reuse existing card in-place to avoid layout shift
          if (_tacExitTimer) { clearTimeout(_tacExitTimer); _tacExitTimer = null; }
          let card = turn.querySelector(".tool-activity-card");
          if (!card) {
            card = document.createElement("div");
            turn.appendChild(card);
          }
          card.className = "tool-activity-card";
          const detailHtml = info.progress
            ? `<div class="tac-detail tac-detail-split"><span class="tac-path">${info.detail || ""}</span><span class="tac-count">writing…</span></div>`
            : (info.detail ? `<div class="tac-detail">${info.detail}</div>` : "");
          card.innerHTML =
            `<div class="tac-icon">${lucideIcon(info.icon)}</div>` +
            `<div class="tac-info"><div class="tac-title">${info.label}</div>` +
            detailHtml +
            (info.progress ? `<div class="tac-progress-track"><div class="tac-progress-fill"></div></div>` : "") +
            `</div><div class="tac-status">${info.progress ? `<div class="tac-pulse-dot"></div>` : `<div class="tac-spinner"></div>`}</div>`;
          // Always keep it as the last element
          turn.appendChild(card);
          activateLucideIcons(card);
          scrollBottom();
        },
        showToolProgress(evt) {
          const card = turn.querySelector(".tool-activity-card");
          if (!card) return;
          const count = card.querySelector(".tac-count");
          if (!count) return;
          const bytes = evt.bytes || 0;
          const size = bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
          count.textContent = `${evt.lines || 0} lines · ${size}`;
        },
        showToolDone() {
          const card = turn.querySelector(".tool-activity-card");
          if (!card) return;
          const status = card.querySelector(".tac-status");
          if (status) { status.innerHTML = `<span class="tac-check">${lucideIcon("✓")}</span>`; activateLucideIcons(status); }
          card.classList.add("tac-done");
          // Fade out only if no next tool reuses it (cancelled in showToolPending)
          _tacExitTimer = setTimeout(() => {
            card.classList.add("tac-exit");
            setTimeout(() => card.remove(), 350);
          }, 600);
        },
        finalize() {
          hidePending();
          closeCurrentThinking();
          closeAnswer();
          // Clean up any lingering tool activity card
          const tac = turn.querySelector(".tool-activity-card");
          if (tac) tac.remove();
          // Any placeholder still spinning means the stream died mid-tag.
          turn.querySelectorAll(".skill-card.pending").forEach(card => {
            const status = card.querySelector(".pending-status");
            if (status) {
              status.textContent = "interrupted";
              status.style.color = "var(--danger)";
            }
            card.classList.remove("pending");
          });
          if (!turn.querySelector(".msg.bot") && !turn.querySelector(".skill-card")) {
            ensureAnswer();
            answerEl.classList.remove("streaming");
            answerContent.textContent = "⚠ Empty response from upstream — check server terminal for WAF/auth details.";
          }
          // Attach toolbar to every bot message in this turn
          turn.querySelectorAll(".msg.bot").forEach(botEl => {
            if (botEl.querySelector(".msg-toolbar")) return;
            const toolbar = document.createElement("div");
            toolbar.className = "msg-toolbar";

            const copyBtn = document.createElement("button");
            copyBtn.innerHTML = '<i data-lucide="copy"></i>';
            copyBtn.title = "Copy";
            copyBtn.addEventListener("click", () => {
              const md = botEl.querySelector(".md-content");
              const text = md ? md.innerText : "";
              navigator.clipboard.writeText(text).then(() => {
                copyBtn.innerHTML = '<i data-lucide="check"></i>';
                activateLucideIcons(copyBtn);
                setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i>'; activateLucideIcons(copyBtn); }, 1500);
              });
            });

            const regenBtn = document.createElement("button");
            regenBtn.innerHTML = '<i data-lucide="refresh-cw"></i>';
            regenBtn.title = "Regenerate";
            regenBtn.addEventListener("click", () => {
              if (isStreaming()) return;
              // Find the preceding user message in this chat
              const pane = botEl.closest(".tab-pane") || activePane;
              const allMsgs = Array.from(pane.querySelectorAll(".msg.user"));
              const thisTurn = botEl.closest(".turn");
              let prevUser = null;
              for (const u of allMsgs) {
                if (u.compareDocumentPosition(thisTurn) & Node.DOCUMENT_POSITION_FOLLOWING) {
                  prevUser = u;
                }
              }
              if (!prevUser) { showToast("No user message to regenerate from", "error"); return; }
              const userText = prevUser.querySelector(".user-text")?.textContent || "";
              if (!userText) return;
              // Remove this turn from UI
              turn.remove();
              // Re-send with current parentId (server will branch)
              inputEl.value = userText;
              sendMessage();
            });

            toolbar.appendChild(copyBtn);
            toolbar.appendChild(regenBtn);
            botEl.appendChild(toolbar);
            activateLucideIcons(toolbar);
          });

          // Commands ran but no normal answer ever arrived — pin a retry bar
          // under the last command group so the tool results can be resent.
          const hasCommands = turn.querySelectorAll(".skill-card").length > 0;
          if (hasCommands && !sawNormalAnswer && !turn.querySelector(".retry-command-bar")) {
            const lastRound = skillRounds.filter(r => r.length).pop() || [];
            if (lastRound.length) {
              const stacks = turn.querySelectorAll(".skill-stack");
              const bar = document.createElement("div");
              bar.className = "retry-command-bar";
              const retryBtn = document.createElement("button");
              retryBtn.textContent = "↻ Resend tool results";
              retryBtn.addEventListener("click", () => retryLastCommand(lastRound, bar, retryBtn));
              bar.appendChild(retryBtn);
              if (stacks.length) stacks[stacks.length - 1].after(bar);
              else turn.appendChild(bar);
            }
          }
        }
      };
    }

    function attachResendBar(targetDiv, messageText) {
      if (targetDiv.querySelector('.resend-bar')) return;
      const resendBar = document.createElement("div");
      resendBar.className = "msg-toolbar resend-bar";
      const resendBtn = document.createElement("button");
      resendBtn.textContent = "↻ Resend";
      resendBtn.addEventListener("click", () => {
        resendBar.remove();
        inputEl.value = messageText;
        sendMessage();
      });
      resendBar.appendChild(resendBtn);
      targetDiv.appendChild(resendBar);
    }

    async function consumeChatStream(res, ui, userMsgDiv, streamChatId) {
      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let gotAnswer = false;
      let gotDone = false;
      let gotError = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data: ")) continue;

          let evt;
          try { evt = JSON.parse(line.slice(6)); }
          catch { continue; }

          if (evt.type === "meta") {
            // Only adopt parent_id if the user is still viewing this stream's
            // chat — prevents a background stream from hijacking state.
            if (activeChatId === streamChatId) {
              parentId = evt.parent_id || parentId;
              saveActiveChat();
            }
          } else if (evt.type === "status") {
            if (evt.message === "feeding_skill_results") ui.nextSkillRound();
          } else if (evt.type === "memory_used") {
            if (Array.isArray(evt.memories) && evt.memories.length) {
              if (evt.source === "tool") ui.attachToolMemory(evt.memories);
              else if (userMsgDiv) attachMemoryChip(userMsgDiv, evt.memories);
            }
          } else if (evt.type === "round_thinking") {
            ui.showRoundThinking(evt.text || "");
          } else if (evt.type === "thinking") {
            // Legacy fallback — backend no longer sends raw thinking tokens
            ui.appendThinking(evt.text || "");
          } else if (evt.type === "answer") {
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            ui.appendAnswer(evt.text || "");
          } else if (evt.type === "done") {
            gotDone = true;
            if (activeChatId === streamChatId) {
              parentId = evt.parent_id || parentId;
              saveActiveChat();
            }
          } else if (evt.type === "rate_limited") {
            gotError = true;
            ui.replaceWithRateLimit(evt.message, evt.hours);
            break;
          } else if (evt.type === "error") {
            gotError = true;
            const msg = evt.message || "Unknown error";
            showToast(msg, "error");
            ui.appendAnswer(`\n[error] ${msg}`);
          } else if (evt.type === "tool_call") {
            ui.addEvent(`⚙ tool: ${JSON.stringify(evt.data).slice(0, 300)}`);
          } else if (evt.type === "tool_result") {
            ui.addEvent(`✓ result: ${JSON.stringify(evt.data).slice(0, 300)}`);
          } else if (evt.type === "tool_pending") {
            ui.showToolPending(evt);
          } else if (evt.type === "tool_progress") {
            ui.showToolProgress(evt);
          } else if (evt.type === "skill_start") {
            if (evt.name === "ask_user") continue; // MCQ card rendered on skill_output
            if (!gotAnswer) { ui.closeThinking(); gotAnswer = true; }
            ui.showToolDone();
            ui.addSkillStart(evt);
          } else if (evt.type === "skill_output") {
            if (evt.name === "ask_user") {
              try { ui.addAskUser(JSON.parse(evt.text)); } catch(e) { ui.appendSkillOutput(evt); }
              continue;
            }
            ui.appendSkillOutput(evt);
          } else if (evt.type === "skill_end") {
            if (evt.name === "ask_user") continue;
            ui.finishSkill(evt);
          } else if (evt.type === "chat_title") {
            const newTitle = (evt.title || "").trim();
            if (newTitle && activeChatId === streamChatId) {
              // Update sidebar
              const chatMeta = chatList.find(c => c.id === activeChatId);
              if (chatMeta) chatMeta.title = newTitle;
              renderChats();
              // Update open tab
              const tab = openTabs.get(activeChatId);
              if (tab) { tab.title = newTitle; renderTabBar(); }
              if (typeof window.updateCompactTitle === "function") window.updateCompactTitle(newTitle);
            }
          } else if (evt.type === "file_edit") {
            handleFileEdit(evt, false);
            ui.trackFileEdit(evt);
            // Live-refresh the Monaco editor if the edited file is currently open
            if (typeof window.refreshIdeFile === "function" && evt.path) {
              window.refreshIdeFile(evt.path);
            }
          }
        }
        if (gotError) break;
      }
      return { gotAnswer, gotDone, gotError };
    }

    async function retryLastCommand(skillEvents, bar, btn) {
      if (isStreaming()) return;
      if (!activeChatId) { showToast("No active chat", "error"); return; }

      bar.remove();
      const streamChatId = activeChatId;
      const ui = addBotStreaming();
      startStream(streamChatId);

      try {
        const res = await fetch("/api/retry-command", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            chat_id: activeChatId,
            skill_events: skillEvents,
            model: selectedModel,
            thinking_mode: selectedThinkingMode
          })
        });

        if (!res.ok) {
          let detail = "";
          try { detail = await res.text(); } catch (_) {}
          const msg = `Retry failed ${res.status}${detail ? ": " + detail.slice(0, 300) : ""}`;
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
          return;
        }

        const { gotAnswer, gotDone, gotError } = await consumeChatStream(res, ui, null, streamChatId);
        if (!gotAnswer && !gotError && !gotDone) {
          const msg = "Stream ended without a response";
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
        }
      } catch (err) {
        showToast("Connection lost: " + err.message, "error");
        ui.appendAnswer(`\n[client error] ${err.message}`);
      } finally {
        ui.finalize();
        endStream(streamChatId);
      }
    }

    let scraperChatsCollapsed = false;

    function renderChats() {
      chatsEl.innerHTML = '';

      // Split chats: scraper (browser-*) vs API
      const q = chatSearchQuery.toLowerCase().trim();
      // If we have server-side search results, show those instead of title filtering
      if (q && chatSearchResults !== null) {
        chatsEl.innerHTML = '';
        // Group results by chat_id
        const byChat = new Map();
        for (const r of chatSearchResults) {
          if (!byChat.has(r.chat_id)) byChat.set(r.chat_id, { title: r.title, messages: [] });
          byChat.get(r.chat_id).messages.push(r);
        }
        for (const [chatId, group] of byChat) {
          const lbl = document.createElement('div');
          lbl.className = 'chat-group-label';
          lbl.textContent = group.title || 'Untitled';
          lbl.style.cursor = 'pointer';
          lbl.onclick = () => selectChat(chatId);
          chatsEl.appendChild(lbl);
          for (const msg of group.messages.slice(0, 5)) {
            const row = document.createElement('div');
            row.className = 'chat-row';
            const btn = document.createElement('button');
            btn.className = 'chat-item';
            const snippet = (msg.content || '').replace(/\n/g, ' ').slice(0, 120);
            btn.textContent = `${msg.role === 'user' ? '👤' : '🤖'} ${snippet}`;
            btn.title = msg.created_at || '';
            btn.onclick = () => selectChat(chatId);
            row.appendChild(btn);
            chatsEl.appendChild(row);
          }
        }
        if (byChat.size === 0) {
          const empty = document.createElement('div');
          empty.className = 'chat-group-label';
          empty.textContent = 'No matches found';
          chatsEl.appendChild(empty);
        }
        return;
      }
      const filtered = q ? chatList.filter(c => (c.title || '').toLowerCase().includes(q)) : chatList;
      const apiChats = filtered.filter(c => !c.id.startsWith('browser-'));
      const scraperChats = filtered.filter(c => c.id.startsWith('browser-'));

      const __groupOf = (c) => {
        const raw = c.updated_at || c.created_at || c.last_message_at || null;
        if (!raw) return 'Chats';
        const d = new Date(typeof raw === 'number' ? (raw < 1e12 ? raw * 1000 : raw) : raw);
        if (isNaN(d.getTime())) return 'Chats';
        const now = new Date();
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const yesterday = new Date(today.getTime() - 86400000);
        const ds = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        if (ds >= today) return 'Today';
        if (ds >= yesterday) return 'Yesterday';
        return 'Earlier';
      };

      const renderChatRow = (chat) => {
        const row = document.createElement("div");
        row.className = "chat-row";
        const btn = document.createElement("button");
        btn.className = "chat-item" + (chat.id === activeChatId ? " active" : "");
        btn.textContent = chat.title || "New chat";
        btn.onclick = () => selectChat(chat.id);
        if (activeStreams.has(chat.id)) row.classList.add("streaming");
        const del = document.createElement("button");
        del.className = "chat-delete";
        del.textContent = "×";
        del.title = "Delete chat";
        del.onclick = (e) => { e.stopPropagation(); deleteChat(chat.id); };
        row.appendChild(btn);
        row.appendChild(del);
        return row;
      };

      // API chats with date groups
      let __lastGroup = null;
      for (const chat of apiChats) {
        const __g = __groupOf(chat);
        if (__g !== __lastGroup) {
          __lastGroup = __g;
          const lbl = document.createElement('div');
          lbl.className = 'chat-group-label';
          lbl.textContent = __g;
          chatsEl.appendChild(lbl);
        }
        chatsEl.appendChild(renderChatRow(chat));
      }

      // Scraper chats at bottom in collapsible section
      if (scraperChats.length > 0) {
        const header = document.createElement('div');
        header.className = 'scraper-chats-header' + (scraperChatsCollapsed ? ' collapsed' : '');
        header.innerHTML = '<span class="arrow">▼</span> 🌐 Scraper Chats (' + scraperChats.length + ')';
        header.onclick = () => {
          scraperChatsCollapsed = !scraperChatsCollapsed;
          renderChats();
        };
        chatsEl.appendChild(header);

        const body = document.createElement('div');
        body.className = 'scraper-chats-body' + (scraperChatsCollapsed ? ' collapsed' : '');
        for (const chat of scraperChats) {
          body.appendChild(renderChatRow(chat));
        }
        chatsEl.appendChild(body);
      }

      // Sync tab titles from chatList (backend auto-titles after first msg)
      for (const [id, tab] of openTabs) {
        const meta = chatList.find(c => c.id === id);
        if (meta && meta.title && meta.title !== tab.title) {
          tab.title = meta.title;
        }
      }
      renderTabBar();
    }

    function currentModelEntry() {
      return modelList.find(m => m.id === selectedModel) || modelList[0];
    }

    // Rebuilds the sidebar thinking-mode dropdown for whichever model is
    // currently selected — each model supports a different set of modes
    // (e.g. qwen3.8-max-preview only has "Thinking", qwen3.7-plus has
    // Fast/Auto/Thinking), so this runs on load and on every model change.
    function populateThinkingModes(preferredModeId) {
      const entry = currentModelEntry();
      const modes = (entry && entry.thinking_modes && entry.thinking_modes.length > 0)
        ? entry.thinking_modes
        : [{ id: "thinking", label: "Thinking" }];

      selectedThinkingMode = (preferredModeId && modes.some(m => m.id === preferredModeId))
        ? preferredModeId
        : modes[0].id;

      thinkingSwitcherEl.innerHTML = "";
      thinkingSwitcherEl.style.setProperty('--n', modes.length);
      for (let idx = 0; idx < modes.length; idx++) {
        const m = modes[idx];
        const btn = document.createElement("button");
        btn.textContent = m.label || m.id;
        btn.dataset.modeId = m.id;
        if (m.id === selectedThinkingMode) {
          btn.classList.add('active');
          thinkingSwitcherEl.style.setProperty('--i', idx);
        }
        btn.addEventListener('click', () => {
          if (btn.classList.contains('active')) return;
          selectedThinkingMode = m.id;
          thinkingSwitcherEl.querySelectorAll('button').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          thinkingSwitcherEl.style.setProperty('--i', idx);
          try { localStorage.setItem(THINKING_MODE_KEY, selectedThinkingMode); } catch (err) {}
        });
        thinkingSwitcherEl.appendChild(btn);
      }

      // Only one available mode — hide the switcher entirely.
      thinkingSwitcherEl.style.display = modes.length <= 1 ? 'none' : '';

      try { localStorage.setItem(THINKING_MODE_KEY, selectedThinkingMode); } catch (err) {}
    }

    /* ---------- Glass dropdown (custom model selector) ---------- */
    const glassDropdown = document.getElementById("modelDropdown");
    const glassTrigger = document.getElementById("modelTrigger");
    const glassMenu = document.getElementById("modelMenu");
    const glassLabel = glassTrigger.querySelector(".glass-dropdown-label");

    function syncGlassDropdown() {
      glassMenu.innerHTML = "";
      for (const opt of modelSelectEl.options) {
        const item = document.createElement("div");
        item.className = "glass-dropdown-item" + (opt.selected ? " active" : "");
        item.textContent = opt.textContent;
        item.dataset.value = opt.value;
        item.addEventListener("click", () => {
          modelSelectEl.value = opt.value;
          modelSelectEl.dispatchEvent(new Event("change"));
          glassLabel.textContent = opt.textContent;
          glassDropdown.classList.remove("open");
          syncGlassDropdown();
        });
        glassMenu.appendChild(item);
      }
      const sel = modelSelectEl.options[modelSelectEl.selectedIndex];
      if (sel) glassLabel.textContent = sel.textContent;
    }

    glassTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      glassDropdown.classList.toggle("open");
    });

    document.addEventListener("click", (e) => {
      if (!glassDropdown.contains(e.target)) {
        glassDropdown.classList.remove("open");
      }
    });

    async function loadModels() {
      let models = FALLBACK_MODELS;
      try {
        const data = await fetch("/api/models").then(r => r.json());
        if (Array.isArray(data.models) && data.models.length > 0) {
          models = data.models;
        }
      } catch (err) {
        console.warn("Could not load /api/models, using fallback list:", err);
      }

      modelList = models;

      let savedModel = null;
      let savedMode = null;
      try {
        savedModel = localStorage.getItem(MODEL_KEY);
        savedMode = localStorage.getItem(THINKING_MODE_KEY);
      } catch (err) {}

      selectedModel = (savedModel && models.some(m => m.id === savedModel)) ? savedModel : models[0].id;

      modelSelectEl.innerHTML = "";
      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.label || m.id;
        if (m.id === selectedModel) opt.selected = true;
        modelSelectEl.appendChild(opt);
      }
      syncGlassDropdown();

      populateThinkingModes(savedMode);
      updateAttachUI();
    }

    modelSelectEl.addEventListener("change", async () => {
      // Block cross-provider switches on locked chats (within-group is fine)
      const activeMeta = chatList.find(c => c.id === activeChatId);
      if (activeMeta?.provider) {
        const newEntry = modelList.find(m => m.id === modelSelectEl.value);
        const newProvider = newEntry?.api_backend || "qwen";
        const chatProvider = activeMeta.provider === "scraping" ? "deepseek" : activeMeta.provider;
        if (newProvider !== chatProvider) {
          modelSelectEl.value = selectedModel; // revert
          showToast("This chat is locked to " + activeMeta.provider + " — start a new chat to switch providers.", "error");
          return;
        }
      }
      selectedModel = modelSelectEl.value;
      try { localStorage.setItem(MODEL_KEY, selectedModel); } catch (err) {}
      // Switching models resets the thinking mode to that model's default,
      // since the previous mode may not exist on the newly selected model.
      populateThinkingModes(null);
      updateAttachUI();

      // In scraper mode the model selector maps to browser model buttons
      // (e.g. DeepSeek Instant/Expert/Vision) — switch immediately and
      // open a fresh chat so the new model starts clean.
      if (scraperMode) {
        try {
          const res = await fetch("/api/scraper/model", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model_type: selectedModel })
          });
          const data = await res.json().catch(() => ({}));
          if (data.status === "ok") {
            showToast("Switched to " + (modelSelectEl.options[modelSelectEl.selectedIndex]?.textContent || selectedModel), "success");
            await createChat();
          } else {
            showToast(data.message || "Model switch failed", "error");
          }
        } catch (e) {
          showToast("Model switch error: " + e.message, "error");
        }
      }
    });

    // thinking mode change handled inline per-button above

    async function loadChats(mode) {
      try {
        // skeleton loading placeholders
        chatsEl.innerHTML = '';
        for (let i = 0; i < 3; i++) {
          const skel = document.createElement('div');
          skel.className = 'skeleton-chat';
          chatsEl.appendChild(skel);
        }
        const url = mode ? `/api/chats?mode=${encodeURIComponent(mode)}` : "/api/chats";
        const data = await fetch(url).then(r => r.json());
        chatList = data.chats || [];
        renderChats();
      } catch (err) {
        console.error("Failed to load chats:", err);
      }
    }

    async function loadMessages(chatId) {
      try {
        const data = await fetch(`/api/chats/${chatId}/messages`).then(r => r.json());
        const pane = ensurePane(chatId);
        pane.innerHTML = "";
        const messages = data.messages || [];
        if (messages.length === 0) {
          pane.innerHTML = `<div class="empty"><h2>New conversation</h2><p>Send the first message.</p></div>`;
          return [];
        }
        // Temporarily point activePane at target so addHistoryMessage appends correctly
        const prevPane = activePane;
        activePane = pane;
        for (const msg of messages) addHistoryMessage(msg);
        activePane = prevPane;
        renderMathJax(pane);
        if (chatId === activeChatId) scrollBottom(true);
        return messages;
      } catch (err) {
        console.error("Failed to load messages:", err);
        return [];
      }
    }

    /**
     * Rebuild the model dropdown filtered to the provider's model group.
     * - null (new/unlocked chat): show all models, enabled
     * - "qwen": show only qwen models, enabled (free switching within group)
     * - "deepseek": show only deepseek models, enabled (free switching within group)
     * - "scraping": show deepseek models, DISABLED (locked tight)
     */
    function lockModelDropdown(provider) {
      const allowed = provider
        ? modelList.filter(m => {
            if (provider === "deepseek" || provider === "scraping") return m.api_backend === "deepseek";
            if (provider === "gemini") return m.api_backend === "gemini";
            if (provider === "groq") return m.api_backend === "groq";
            if (provider === "mistral") return m.api_backend === "mistral";
            return m.api_backend === "qwen" || !m.api_backend; // qwen fallback
          })
        : modelList;

      modelSelectEl.innerHTML = "";
      for (const m of allowed) {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.label || m.id;
        modelSelectEl.appendChild(opt);
      }

      // If current selection isn't in the allowed set, switch to first allowed
      if (!allowed.some(m => m.id === selectedModel)) {
        selectedModel = allowed[0]?.id || modelList[0].id;
        modelSelectEl.value = selectedModel;
        try { localStorage.setItem(MODEL_KEY, selectedModel); } catch(e) {}
        populateThinkingModes(null);
      } else {
        modelSelectEl.value = selectedModel;
      }

      // Only scraping gets hard-disabled; qwen/deepseek allow within-group switching
      modelSelectEl.disabled = provider === "scraping";
      glassTrigger.disabled = provider === "scraping";
      glassTrigger.style.opacity = provider === "scraping" ? "0.45" : "";
      syncGlassDropdown();
    }

    async function selectChat(chatId) {
      const meta = chatList.find(c => c.id === chatId);
      const alreadyOpen = openTabs.has(chatId);

      // Switch the visible tab (creates pane if needed)
      switchToTab(chatId);

      // Cancel any stale scroll rAF from the previous chat
      _scrollPending = false;
      _scrollForChat = null;

      // Lock model dropdown to the chat's provider (or unlock for new chats)
      lockModelDropdown(meta?.provider || null);

      // Update send button: show stop-mode only if THIS chat is streaming
      updateSendBtn();

      saveActiveChat();
      renderChats();

      // Only load messages from API if this tab hasn't been loaded yet
      if (!alreadyOpen) {
        const msgs = await loadMessages(chatId);
        // Derive parentId from the actual message chain
        if (Array.isArray(msgs) && msgs.length) {
          const last = msgs[msgs.length - 1];
          parentId = last?.parent_id ? String(last.parent_id) : last?.id ? String(last.id) : null;
        } else {
          parentId = meta?.parent_id ? String(meta.parent_id) : null;
        }
      } else {
        // Already loaded — just derive parentId from cached meta
        parentId = meta?.parent_id ? String(meta.parent_id) : null;
      }

      // Connect agent SSE for this chat
      if (typeof onChatOpened === "function") onChatOpened(chatId);

      inputEl.focus();
    }

    async function deleteChat(chatId) {
      if (!confirm("Delete this chat?")) return;
      try {
        const res = await fetch(`/api/chats/${chatId}`, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.deleted) {
          showToast(data.detail || "Could not delete chat", "error");
          return;
        }
        chatList = chatList.filter(c => c.id !== chatId);
        closeTab(chatId); // handles activeChatId reassignment + empty state
        renderChats();
        showToast("Chat deleted", "success");
      } catch (err) {
        showToast("Delete failed: " + err.message, "error");
      }
    }

    document.getElementById('deleteAllChatsBtn').addEventListener('click', async () => {
      if (!confirm('Delete ALL chats? This cannot be undone.')) return;
      try {
        const res = await fetch('/api/chats', { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.deleted) {
          showToast(data.detail || 'Could not delete chats', 'error');
          return;
        }
        chatList = [];
        // Close all tabs
        for (const [id] of openTabs) {
          const tab = openTabs.get(id);
          if (tab) tab.pane.remove();
        }
        openTabs.clear();
        activePane = null;
        activeChatId = null;
        parentId = null;
        saveActiveChat();
        renderChats();
        renderTabBar();
        chatEl.innerHTML = `<div class="empty"><h2>Start a chat</h2><p>Create a new chat and talk to Sable.</p></div>`;
        showToast(`Deleted ${data.chats_removed} chat(s)`, 'success');
      } catch (err) {
        showToast('Delete failed: ' + err.message, 'error');
      }
    });

    // ── Strip Browser Profiles ──────────────────────────────────
    document.getElementById('stripProfilesBtn').addEventListener('click', async () => {
      if (!confirm('Strip all browser profiles down to bare session data? Caches will be removed.')) return;
      const btn = document.getElementById('stripProfilesBtn');
      const status = document.getElementById('stripProfilesStatus');
      btn.disabled = true;
      btn.textContent = '⏳ Stripping…';
      status.textContent = 'Stripping profiles…';
      status.style.color = 'var(--text-dim)';
      try {
        const res = await fetch('/api/settings/browser/strip-profiles', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          status.textContent = data.detail || 'Strip failed';
          status.style.color = 'var(--danger)';
          showToast(data.detail || 'Strip failed', 'error');
          return;
        }
        const lastLine = (data.output || '').trim().split('\n').pop() || '';
        status.textContent = '✅ ' + lastLine;
        status.style.color = 'var(--success, #4caf50)';
        showToast('Browser profiles stripped', 'success');
      } catch (err) {
        status.textContent = 'Error: ' + err.message;
        status.style.color = 'var(--danger)';
        showToast('Strip failed: ' + err.message, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = '🧹 Strip Browser Profiles';
      }
    });
    // ── /Strip Browser Profiles ─────────────────────────────────


    // ── Data Export / Import ─────────────────────────────────────
    async function _streamDataOp(url, btnId, statusEl, confirmMsg, busyLabel, doneFn) {
      if (!confirm(confirmMsg)) return;
      const btn = document.getElementById(btnId);
      btn.disabled = true;
      btn.textContent = '⏳ ' + busyLabel + '…';
      statusEl.textContent = busyLabel + '…';
      statusEl.style.color = 'var(--text-dim)';
      try {
        const res = await fetch(url, { method: 'POST' });
        if (!res.ok || !res.body) {
          const err = await res.json().catch(() => ({}));
          statusEl.textContent = err.detail || 'Failed';
          statusEl.style.color = 'var(--danger)';
          showToast(err.detail || 'Operation failed', 'error');
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            try {
              const ev = JSON.parse(line);
              if (ev.type === 'progress') {
                statusEl.textContent = `⏳ [${ev.step}/${ev.total}] ${ev.dir} — ${ev.status}`;
              } else if (ev.type === 'done') {
                doneFn(ev);
              } else if (ev.type === 'error') {
                statusEl.textContent = '❌ ' + (ev.detail || 'Unknown error');
                statusEl.style.color = 'var(--danger)';
                showToast(ev.detail || 'Operation failed', 'error');
              }
            } catch {}
          }
        }
      } catch (err) {
        statusEl.textContent = 'Error: ' + err.message;
        statusEl.style.color = 'var(--danger)';
        showToast('Failed: ' + err.message, 'error');
      } finally {
        btn.disabled = false;
      }
    }

    document.getElementById('exportDataBtn').addEventListener('click', () => {
      const status = document.getElementById('dataExportStatus');
      const btn = document.getElementById('exportDataBtn');
      _streamDataOp(
        '/api/settings/data/export', 'exportDataBtn', status,
        'Export all data to ~/.sable/backup/? This overwrites any existing backup.',
        'Exporting',
        (ev) => {
          const dirs = Object.keys(ev.exported || {});
          status.textContent = `✅ Exported ${dirs.length} dirs → ~/.sable/backup/`;
          status.style.color = 'var(--success, #4caf50)';
          showToast('Data exported successfully', 'success');
          btn.textContent = '⬆ Export Data';
        }
      );
    });

    document.getElementById('importDataBtn').addEventListener('click', () => {
      const status = document.getElementById('dataExportStatus');
      const btn = document.getElementById('importDataBtn');
      _streamDataOp(
        '/api/settings/data/import', 'importDataBtn', status,
        'Import data from ~/.sable/backup/? This will overwrite current files.',
        'Importing',
        (ev) => {
          const dirs = ev.imported || [];
          status.textContent = `✅ Imported ${dirs.length} dirs from backup`;
          status.style.color = 'var(--success, #4caf50)';
          showToast('Data imported successfully', 'success');
          btn.textContent = '⬇ Import Data';
        }
      );
    });
    // ── /Data Export / Import ────────────────────────────────────


    // --- Service control buttons ---
    const stopServiceBtn = document.getElementById('stopServiceBtn');
    const restartServiceBtn = document.getElementById('restartServiceBtn');
    if (stopServiceBtn) {
      stopServiceBtn.addEventListener('click', async () => {
        if (!confirm('Stop the Sable service? The UI will go offline.')) return;
        stopServiceBtn.textContent = 'Stopping…';
        try { await fetch('/api/settings/service/stop', { method: 'POST' }); } catch {}
        showToast('Service stopping — UI will go offline', 'info');
      });
    }
    if (restartServiceBtn) {
      restartServiceBtn.addEventListener('click', async () => {
        if (!confirm('Restart the Sable service? Brief downtime (~20s).')) return;
        restartServiceBtn.textContent = 'Restarting…';
        try { await fetch('/api/settings/service/restart', { method: 'POST' }); } catch {}
        showToast('Restarting — back in ~20s', 'info');
        setTimeout(() => { restartServiceBtn.textContent = '↻ Restart Service'; }, 25000);
      });
    }




    // --- Consolidation queue: messages sent while consolidation is pending get queued ---
    let _consolidationPromise = null;
    let _messageQueue = [];

    function consolidateMemory(chatId, model, useTimeout = false) {
      const cid = chatId || activeChatId;
      if (!cid) return Promise.resolve();
      const mode = scraperMode ? 'scraper' : 'api';
      showToast("🧠 Consolidating memory...", "info");

      const controller = new AbortController();
      const timeout = useTimeout ? setTimeout(() => controller.abort(), 30000) : null;

      _consolidationPromise = fetch("/api/memory/consolidate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: cid, model: model || selectedModel, mode: mode }),
        signal: useTimeout ? controller.signal : undefined
      })
        .then(async (res) => {
          clearTimeout(timeout);
          if (!res.ok) {
            const text = await res.text().catch(() => "");
            showToast(`🧠 Consolidation failed (${res.status}): ${text.slice(0, 200)}`, "error");
            return;
          }
          const data = await res.json();
          if (data.status === "ok") {
            if (data.added > 0 || data.deleted > 0) {
              const parts = [];
              if (data.added) parts.push(`${data.added} added`);
              if (data.deleted) parts.push(`${data.deleted} deleted`);
              showToast(`🧠 ${parts.join(", ")}`, "success");
            } else {
              showToast("🧠 Nothing new worth remembering", "info");
            }
          } else if (data.status === "skipped") {
            showToast("🧠 Skipped — too few messages", "info");
          } else {
            showToast(`🧠 Consolidation failed: ${data.detail || "unknown error"}`, "error");
          }
        })
        .catch((e) => {
          clearTimeout(timeout);
          if (e.name === "AbortError") {
            showToast("🧠 Consolidation timed out (30s)", "error");
          } else {
            showToast("🧠 Consolidation error: " + e.message, "error");
          }
        })
        .finally(() => {
          _consolidationPromise = null;
          // Flush queued messages
          const queued = _messageQueue.splice(0);
          if (queued.length) {
            inputEl.value = queued[0];
            sendMessage();
          }
        });

      return _consolidationPromise;
    }

    async function createChat() {
      if (creating) return null;
      setCreating(true);
      const oldChatId = activeChatId;
      if (oldChatId && scraperMode) {
        // Scraper mode: send consolidation prompt into the active browser tab,
        // wait for the model to respond with memory JSON, then create new chat.
        showToast("🧠 Consolidating memory in browser...", "info");
        try {
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 120000);
          const res = await fetch("/api/memory/consolidate-scraper", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_id: oldChatId, model: selectedModel }),
            signal: controller.signal,
          });
          clearTimeout(timeout);
          if (res.ok) {
            const data = await res.json();
            if (data.status === "ok") {
              const parts = [];
              if (data.added) parts.push(`${data.added} added`);
              if (data.deleted) parts.push(`${data.deleted} deleted`);
              showToast(parts.length ? `🧠 ${parts.join(", ")}` : "🧠 Nothing new worth remembering", "success");
            } else if (data.status === "skipped") {
              showToast("🧠 Skipped — too few messages", "info");
            } else {
              showToast(`🧠 Consolidation failed: ${data.detail || "unknown"}`, "error");
            }
          } else {
            const text = await res.text().catch(() => "");
            showToast(`🧠 Consolidation failed (${res.status}): ${text.slice(0, 200)}`, "error");
          }
        } catch (e) {
          if (e.name === "AbortError") {
            showToast("🧠 Consolidation timed out (2min)", "error");
          } else {
            showToast("🧠 Consolidation error: " + e.message, "error");
          }
        }
      } else if (oldChatId) {
        // API mode: fire-and-forget background consolidation
        consolidateMemory(oldChatId, selectedModel);
      }
      try {
        const res  = await fetch("/api/chat/new", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: selectedModel })
        });
        const text = await res.text();
        let data = {};
        try { data = JSON.parse(text); } catch (_) { data = { error: `Server ${res.status}: ${text.slice(0, 300)}` }; }
        if (!data.chat_id) {
          showToast(data.error || "Could not create chat", "error");
          return null;
        }
        // Open as a new tab
        switchToTab(data.chat_id);
        parentId = null;
        lockModelDropdown(null); // unlock dropdown for fresh chat
        if (typeof onChatOpened === "function") onChatOpened(activeChatId);
        saveActiveChat();
        await loadChats();
        // Pane already has empty state from createTabPane
        inputEl.focus();
        return activeChatId;
      } catch (err) {
        showToast("Network error: " + err.message, "error");
        return null;
      } finally {
        setCreating(false);
      }
    }

    /* ============================= attachments ============================= */

    function addAttachmentChip(file) {
      const chip = document.createElement("div");
      chip.className = "attach-chip uploading";
      const img = document.createElement("img");
      img.src = URL.createObjectURL(file);
      chip.appendChild(img);
      attachPreview.appendChild(chip);
      return chip;
    }

    function removePendingByChip(chip) {
      const idx = pendingFiles.findIndex(p => p.chip === chip);
      if (idx !== -1) {
        URL.revokeObjectURL(pendingFiles[idx].chip.querySelector("img").src);
        pendingFiles[idx].chip.remove();
        pendingFiles.splice(idx, 1);
      }
    }

    function clearPending() {
      while (pendingFiles.length) removePendingByChip(pendingFiles[0].chip);
    }

    async function uploadFile(file) {
      const chip = addAttachmentChip(file);
      const idx = pendingFiles.length;
      pendingFiles.push({ file, path: null, chip });

      const form = new FormData();
      form.append("file", file);
      try {
        const res = await fetch("/api/upload", { method: "POST", body: form });
        const data = await res.json();
        if (!res.ok || !data.uploaded) {
          showToast(data.detail || "Upload failed", "error");
          removePendingByChip(chip);
          return;
        }
        pendingFiles[idx].path = data.path;
        pendingFiles[idx].meta = data.meta || null;
        chip.classList.remove("uploading");
        const rm = document.createElement("button");
        rm.className = "remove";
        rm.textContent = "\u00d7";
        rm.onclick = () => removePendingByChip(chip);
        chip.appendChild(rm);
      } catch (err) {
        showToast("Upload error: " + err.message, "error");
        removePendingByChip(chip);
      }
    }

    function handleFiles(files) {
      const caps = getActiveCapabilities();
      for (const f of files) {
        const kind = f.type.startsWith("image/") ? "image"
          : f.type.startsWith("video/") ? "video"
          : f.type.startsWith("audio/") ? "audio"
          : "document";
        if (caps[kind]) uploadFile(f);
        else showToast(`${kind} files not supported by this model`, "error");
      }
    }

    attachBtn.addEventListener("click", () => fileInput.click());
    // Make the whole glass pill clickable (forwards to inner button)
    document.querySelector(".attach-cell").addEventListener("click", (e) => {
      if (e.target === e.currentTarget) attachBtn.click();
    });
    document.querySelector(".send-cell").addEventListener("click", (e) => {
      if (e.target === e.currentTarget) sendBtn.click();
    });

    // Header pill click forwarding
    document.querySelectorAll(".header-icon-cell").forEach((cell) => {
      cell.style.cursor = "pointer";
      cell.addEventListener("click", (e) => {
        if (e.target === e.currentTarget) cell.querySelector("button")?.click();
      });
    });


    fileInput.addEventListener("change", () => {
      if (fileInput.files.length) handleFiles(fileInput.files);
      fileInput.value = "";
    });

    inputEl.addEventListener("paste", (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const caps = getActiveCapabilities();
      for (const item of items) {
        const kind = item.type.startsWith("image/") ? "image"
          : item.type.startsWith("video/") ? "video"
          : item.type.startsWith("audio/") ? "audio"
          : null;
        if (kind && caps[kind]) {
          e.preventDefault();
          handleFiles([item.getAsFile()]);
        }
      }
    });

    let dragCounter = 0;
    inputArea.addEventListener("dragenter", (e) => {
      e.preventDefault();
      dragCounter++;
      inputArea.classList.add("drag-over");
    });
    inputArea.addEventListener("dragleave", (e) => {
      e.preventDefault();
      dragCounter--;
      if (dragCounter <= 0) { dragCounter = 0; inputArea.classList.remove("drag-over"); }
    });
    inputArea.addEventListener("dragover", (e) => e.preventDefault());
    inputArea.addEventListener("drop", (e) => {
      e.preventDefault();
      dragCounter = 0;
      inputArea.classList.remove("drag-over");
      if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
    });

    /* =========================== end attachments =========================== */

    async function sendMessage() {
      if (isStreaming()) {
        const ctrl = activeStreams.get(activeChatId);
        if (ctrl) ctrl.abort();
        return;
      }

      const message = inputEl.value.trim();
      if (!message) return;

      // @ mention → spawn agent instead of sending chat message
      if (typeof parseAgentMention === "function") {
        const mention = parseAgentMention(message);
        if (mention) {
          if (!activeChatId) {
            const created = await createChat();
            if (!created) return;
          }
          inputEl.value = "";
          autoResize();
          hideMentionPopup();
          showToast(`${mention.role === "researcher" ? "🔍" : mention.role === "coder" ? "💻" : mention.role === "reviewer" ? "📋" : mention.role === "writer" ? "✍️" : "⚙️"} Spawning ${mention.role}…`, "info");
          try {
            const result = await spawnAgentFromMention(mention.role, mention.task, activeChatId);
            if (result.error) {
              showToast(`Agent spawn failed: ${result.error}`, "error");
            } else {
              showToast(`✅ ${mention.role} spawned (${result.model})`, "success");
            }
          } catch (e) {
            showToast(`Agent spawn error: ${e.message}`, "error");
          }
          return;
        }
      }

      // Queue message if consolidation is still running in SCRAPER mode only
      // API mode consolidation runs independently without blocking user input
      if (_consolidationPromise && scraperMode) {
        _messageQueue.push(message);
        inputEl.value = "";
        autoResize();
        showToast("🧠 Message queued — waiting for memory consolidation...", "info");
        return;
      }

      if (!activeChatId) {
        const created = await createChat();
        if (!created) return;
      }

      // Mode cross-guard: block sending from mismatched provider chats
      const activeMeta = chatList.find(c => c.id === activeChatId);
      if (activeMeta?.provider) {
        if (scraperMode && activeMeta.provider !== "scraping") {
          showToast("This chat is locked to " + activeMeta.provider + " — switch off scraper mode or start a new chat.", "error");
          return;
        }
        if (!scraperMode && activeMeta.provider === "scraping") {
          showToast("This is a scraping chat — enable scraper mode or start a new chat.", "error");
          return;
        }
      }

      const streamChatId = activeChatId;
      const controller = startStream(streamChatId);
      inputEl.value = "";
      autoResize();

      // Collect image URLs for chat display BEFORE clearing pending chips
      const imageUrls = pendingFiles
        .filter(p => p.path)
        .map(p => "/system/uploads/" + p.path.split("/").pop());

      // Remove previous turn's file-edit summary card
      if (activePane) activePane.querySelectorAll(".file-edit-summary-card").forEach(el => el.remove());

      const userMsgDiv = addMessage("user", message, imageUrls);
      const lastSentMessage = message;
      const ui = addBotStreaming();

      const filesPayload = pendingFiles
        .filter(p => p.path)
        .map(p => p.meta || { path: p.path });
      clearPending();

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            chat_id: streamChatId,
            parent_id: parentId,
            files: filesPayload.length ? filesPayload : undefined,
            model: selectedModel,
            thinking_mode: selectedThinkingMode,
            stream: true
          }),
          signal: controller.signal
        });

        if (!res.ok) {
          let detail = "";
          try { detail = await res.text(); } catch (_) {}
          const message = `Server error ${res.status}${detail ? ": " + detail.slice(0, 500) : ""}`;
          showToast(message, "error");
          ui.appendAnswer(`\n[error] ${message}`);
          ui.finalize();
          return;
        }

        const contentType = res.headers.get("content-type") || "";
        if (contentType.includes("application/json")) {
          const data = await res.json();
          const msg = data.error || "Unexpected JSON response";
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
          ui.finalize();
          return;
        }

        const { gotAnswer, gotDone, gotError } = await consumeChatStream(res, ui, userMsgDiv, streamChatId);

        // Detect empty response: stream ended with no answer tokens,
        // or stream "done" but answer content is whitespace-only.
        const emptyResponse = (!gotAnswer && !gotError && !gotDone)
          || (gotDone && !gotAnswer && !gotError);
        if (emptyResponse) {
          const msg = gotDone ? "Response finished with no content" : "Stream ended without a response";
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
        }
        // Show resend button when response was empty or errored
        if ((emptyResponse || gotError) && userMsgDiv && lastSentMessage) {
          attachResendBar(userMsgDiv, lastSentMessage);
        }
      } catch (err) {
        if (err.name === "AbortError") {
          showToast("Generation stopped", "info");
          ui.appendAnswer("\n[stopped]");
        } else {
          showToast("Connection lost: " + err.message, "error");
          ui.appendAnswer(`\n[client error] ${err.message}`);
          if (userMsgDiv && lastSentMessage) {
            attachResendBar(userMsgDiv, lastSentMessage);
          }
        }
      } finally {
        ui.finalize();
        endStream(streamChatId);

        // After first message, provider is now locked in DB — lock the dropdown
        const meta = chatList.find(c => c.id === streamChatId);
        if (meta && !meta.provider) {
          const prov = scraperMode ? "scraping"
            : (modelList.find(m => m.id === selectedModel)?.api_backend || "qwen");
          meta.provider = prov; // update local cache
          lockModelDropdown(prov);
        }
      }
    }

    sendBtn.addEventListener("click", sendMessage);


    // Programmatic message send for auto-turn (agent completion notifications).
    // Goes through the exact same /api/chat pipeline as a user-typed message,
    // so skill cards, stop button, markdown, and history replay all work normally.
    async function sendAutoTurnMessage(message) {
      if (!message || !activeChatId) return;
      if (isStreaming()) {
        // Queue: retry after current stream finishes
        setTimeout(() => sendAutoTurnMessage(message), 1500);
        return;
      }

      const streamChatId = activeChatId;
      const controller = startStream(streamChatId);

      // Remove previous turn's file-edit summary card
      if (activePane) activePane.querySelectorAll(".file-edit-summary-card").forEach(el => el.remove());

      const userMsgDiv = addMessage("user", message);
      const ui = addBotStreaming();

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            chat_id: streamChatId,
            parent_id: parentId,
            model: selectedModel,
            thinking_mode: selectedThinkingMode,
            stream: true
          }),
          signal: controller.signal
        });

        if (!res.ok) {
          let detail = "";
          try { detail = await res.text(); } catch (_) {}
          const msg = `Auto-turn error ${res.status}${detail ? ": " + detail.slice(0, 300) : ""}`;
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
          ui.finalize();
          return;
        }

        const contentType = res.headers.get("content-type") || "";
        if (contentType.includes("application/json")) {
          const data = await res.json();
          const msg = data.error || "Unexpected JSON response";
          showToast(msg, "error");
          ui.appendAnswer(`\n[error] ${msg}`);
          ui.finalize();
          return;
        }

        await consumeChatStream(res, ui, userMsgDiv, streamChatId);
      } catch (err) {
        if (err.name === "AbortError") {
          showToast("Auto-turn stopped", "info");
          ui.appendAnswer("\n[stopped]");
        } else {
          showToast("Auto-turn connection lost: " + err.message, "error");
          ui.appendAnswer(`\n[client error] ${err.message}`);
        }
      } finally {
        ui.finalize();
        endStream(streamChatId);
      }
    }



    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        if (isStreaming()) return; // let Enter insert newline while model responds
        // On touch devices Enter inserts a newline; the send button sends.
        if (window.matchMedia("(pointer: coarse)").matches) return;
        e.preventDefault();
        sendMessage();
      }
    });

    if (newChatBtn) newChatBtn.addEventListener("click", createChat);
    const newChatSidebarBtn = document.getElementById("newChatSidebar");
    if (newChatSidebarBtn) {
        newChatSidebarBtn.addEventListener("click", createChat);
    }
    const newChatFloatBtn = document.getElementById("newChatFloat");
    if (newChatFloatBtn) {
        newChatFloatBtn.addEventListener("click", createChat);
    }

    // Chat search toggle + filter
    const chatSearchBtn = document.getElementById('chatSearchBtn');
    const chatSearchInput = document.getElementById('chatSearch');
    if (chatSearchBtn && chatSearchInput) {
      chatSearchBtn.addEventListener('click', () => {
        const isVisible = chatSearchInput.classList.toggle('visible');
        chatsEl.style.marginTop = isVisible ? '36px' : '';
        if (isVisible) {
          chatSearchInput.focus();
        } else {
          chatSearchInput.value = '';
          chatSearchQuery = '';
          chatSearchResults = null;
          renderChats();
        }
      });
      let _searchDebounce = null;
      chatSearchInput.addEventListener('input', () => {
        chatSearchQuery = chatSearchInput.value;
        clearTimeout(_searchDebounce);
        if (!chatSearchQuery.trim()) {
          chatSearchResults = null;
          renderChats();
          return;
        }
        _searchDebounce = setTimeout(async () => {
          try {
            const data = await fetch(`/api/chats/search?q=${encodeURIComponent(chatSearchQuery.trim())}`).then(r => r.json());
            chatSearchResults = data.results || [];
          } catch { chatSearchResults = []; }
          renderChats();
        }, 300);
      });
      chatSearchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          chatSearchInput.value = '';
          chatSearchQuery = '';
          chatSearchResults = null;
          chatSearchInput.classList.remove('visible');
          chatsEl.style.marginTop = '';
          renderChats();
        }
      });
    }

    const sidebarToggleBtn = document.getElementById('sidebarToggle');
    if (sidebarToggleBtn) {
      sidebarToggleBtn.addEventListener('click', () => {
        const isMobile = window.matchMedia('(max-width: 860px)').matches;
        if (isMobile) {
          document.body.classList.toggle('sidebar-open');
        } else {
          document.body.classList.toggle('sidebar-collapsed');
        }
      });
    }
    const sidebarOverlay = document.querySelector('.sidebar-overlay');

    const brandRow = document.querySelector('.brand-row');
    if (brandRow) {
      brandRow.addEventListener('click', (e) => {
        // Don't toggle if they clicked the new-chat button itself
        if (e.target.closest('#newChat')) return;
        document.querySelector('.sidebar-top-content')?.classList.toggle('collapsed');
      });
    }

    if (sidebarOverlay) {
      sidebarOverlay.addEventListener('click', () => {
        document.body.classList.remove('sidebar-open');
      });
    }



    (async () => {
    await ensureAuth();
    await loadModels();
    // Fetch typewriter animation speed from server config
    fetch("/api/config/ui").then(r => r.json()).then(cfg => {
      if (cfg.typewriter_chars_per_tick) TW_CHARS = cfg.typewriter_chars_per_tick;
      if (cfg.typewriter_tick_ms) TW_MS = cfg.typewriter_tick_ms;
    }).catch(() => {});

    // Load chats filtered by current mode so sidebar matches active mode
    const initialMode = scraperMode ? 'scraper' : 'api';
    loadChats(initialMode).then(async () => {
      let savedChatId = null;
      let savedParentId = null;
      try {
        savedChatId = localStorage.getItem(ACTIVE_CHAT_KEY);
        savedParentId = localStorage.getItem(PARENT_KEY);
      } catch (err) {
        console.warn("Could not read persisted chat:", err);
      }

      if (savedChatId && chatList.some(c => c.id === savedChatId)) {
        await selectChat(savedChatId);
      } else if (chatList.length > 0) {
        await selectChat(chatList[0].id);
      } else {
        chatEl.innerHTML = `<div class="empty"><h2>Start a chat</h2><p>Create a new chat and talk to Sable.</p></div>`;
      }
    });
    })();

    /* ---------- Settings & Live Logs ---------- */
    const settingsOverlay = document.getElementById("settingsOverlay");
    const settingsBtn = document.getElementById("settingsBtn");
    const settingsClose = document.getElementById("settingsClose");
    const logViewer = document.getElementById("logViewer");
    const logAutoScroll = document.getElementById("logAutoScroll");
    const logClear = document.getElementById("logClear");
    let logSource = null;

    function openSettings() {
      settingsOverlay.classList.remove("hidden");
      if (!logSource) connectLogs();
    }

    function closeSettings() {
      settingsOverlay.classList.add("hidden");
    }

    settingsBtn.addEventListener("click", openSettings);
    settingsClose.addEventListener("click", closeSettings);
    settingsOverlay.addEventListener("click", (e) => {
      if (e.target === settingsOverlay) closeSettings();
    });

    // Tab switching (lazy-load per tab)
    document.querySelectorAll(".settings-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".settings-tab").forEach((t) => t.classList.remove("active"));
        document.querySelectorAll(".settings-tab-content").forEach((c) => c.classList.remove("active"));
        tab.classList.add("active");
        const target = document.getElementById("tab-" + tab.dataset.tab);
        if (target) target.classList.add("active");
        const tabName = tab.dataset.tab;
        if (tabName === 'general') loadBrowserSettings();
        else if (tabName === 'account') loadAccountProfiles();
        else if (tabName === 'mcp') loadMcpServers();
      });
    });


    // Horizontal scroll on mouse wheel for settings tab bars
    document.querySelectorAll(".settings-tabs").forEach((bar) => {
      bar.addEventListener("wheel", (e) => {
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
          e.preventDefault();
          bar.scrollLeft += e.deltaY;
        }
      }, { passive: false });
    });


    logClear.addEventListener("click", () => { logViewer.textContent = ""; });

    /* ---------- Library Panel ---------- */
    const libraryOverlay = document.getElementById("libraryOverlay");
    const libraryBtn = document.getElementById("libraryBtn");
    const libraryClose = document.getElementById("libraryClose");
    const libraryTabs = document.getElementById("libraryTabs");
    const libraryBody = document.getElementById("libraryBody");
    let _libLoaded = { agents: false, research: false, notes: false, gallery: false, skills: false };

    function openLibrary() {
      libraryOverlay.classList.remove("hidden");
      // Load active tab if not yet loaded
      const activeTab = libraryTabs.querySelector(".settings-tab.active");
      if (activeTab) loadLibraryTab(activeTab.dataset.tab);
    }

    function closeLibrary() {
      libraryOverlay.classList.add("hidden");
    }

    libraryBtn.addEventListener("click", openLibrary);
    libraryClose.addEventListener("click", closeLibrary);
    libraryOverlay.addEventListener("click", (e) => {
      if (e.target === libraryOverlay) closeLibrary();
    });

    // Library tab switching
    libraryTabs.querySelectorAll(".settings-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        libraryTabs.querySelectorAll(".settings-tab").forEach((t) => t.classList.remove("active"));
        libraryBody.querySelectorAll(".settings-tab-content").forEach((c) => c.classList.remove("active"));
        tab.classList.add("active");
        const target = document.getElementById("tab-" + tab.dataset.tab);
        if (target) target.classList.add("active");
        loadLibraryTab(tab.dataset.tab);
      });
    });

    async function loadLibraryTab(tabId) {
      const section = tabId.replace("lib-", "");
      const container = document.getElementById("tab-" + tabId);
      if (!container) return;
      // Email: skip reload if already cached
      if (section === "email" && _emailState.loaded) return;
      container.innerHTML = '<div class="library-loading">Loading…</div>';
      try {
        if (section === "gallery") {
          const res = await fetch("/api/library/gallery");
          const items = await res.json();
          renderGallery(container, items);
        } else if (section === "skills") {
          const res = await fetch("/api/library/skills");
          const items = await res.json();
          renderSkills(container, items);
        } else if (section === "email") {
          renderEmailPanel(container);
        } else {
          const res = await fetch(`/api/library/${section}`);
          const items = await res.json();
          renderMdCards(container, items, section);
        }
      } catch (e) {
        container.innerHTML = '<div class="library-empty">Failed to load.</div>';
      }
    }

    function renderMdCards(container, items, section) {
      if (!items.length) {
        container.innerHTML = '<div class="library-empty">Nothing here yet.</div>';
        return;
      }
      container.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "library-card-grid";
      items.forEach((item) => {
        const card = document.createElement("div");
        card.className = "library-card";
        card.innerHTML = `
          <div class="library-card-title">${escHtml(item.title)}</div>
          <div class="library-card-date">${item.date || ""}</div>
          <div class="library-card-preview">${escHtml(item.preview || "")}</div>
        `;
        card.addEventListener("click", () => openLibraryReader(section, item.filename, item.title));
        grid.appendChild(card);
      });
      container.appendChild(grid);
    }

    function renderGallery(container, items) {
      if (!items.length) {
        container.innerHTML = '<div class="library-empty">No images uploaded yet.</div>';
        return;
      }
      container.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "library-gallery-grid";
      items.forEach((item) => {
        const cell = document.createElement("div");
        cell.className = "library-gallery-item";
        cell.innerHTML = `<img src="${item.url}" alt="${escHtml(item.filename)}" loading="lazy">`;
        cell.title = item.filename;
        cell.addEventListener("click", () => window.open(item.url, "_blank"));
        grid.appendChild(cell);
      });
      container.appendChild(grid);
    }

    function renderSkills(container, items) {
      if (!items.length) {
        container.innerHTML = '<div class="library-empty">No user-created skills yet. They\'ll appear here once consolidated from conversations.</div>';
        return;
      }
      container.innerHTML = "";
      const list = document.createElement("div");
      list.className = "library-skill-list";
      items.forEach((skill) => {
        const card = document.createElement("div");
        card.className = "library-card library-skill-card";
        card.innerHTML = `
          <div class="library-card-title">${escHtml(skill.name || "unnamed")}</div>
          <div class="library-card-preview">${escHtml(skill.description || "")}</div>
          <div class="library-card-date">Trigger: ${escHtml(skill.trigger || "—")}</div>
        `;
        list.appendChild(card);
      });
      container.appendChild(list);
    }

    /* ---------- Email Panel ---------- */

    let _emailState = { folder: 'INBOX', messages: [], configured: false, loaded: false };

    async function renderEmailPanel(container, force) {
      if (_emailState.loaded && !force) return;
      container.innerHTML = '<div class="library-loading">Loading…</div>';
      try {
        const res = await fetch('/api/email/configured');
        const cfg = await res.json();
        _emailState.configured = cfg.configured;
        if (!cfg.configured) {
          renderEmailSetup(container);
        } else {
          renderEmailInbox(container, cfg);
        }
        _emailState.loaded = true;
      } catch {
        container.innerHTML = '<div class="library-empty">Failed to connect to email service.</div>';
      }
    }

    function refreshEmailPanel() {
      const container = document.getElementById('tab-lib-email');
      if (!container) return;
      _emailState.loaded = false;
      renderEmailPanel(container, true);
    }

    function renderEmailSetup(container) {
      container.innerHTML = `
        <div class="email-setup">
          <h3 style="margin-bottom:12px;font-size:15px;"><span class="icon-emoji">📧</span><i data-lucide="mail" class="icon-lucide" style="width:16px;height:16px;margin-right:4px;"></i> Configure Email</h3>
          <p style="font-size:12px;color:var(--muted);margin-bottom:16px;">Connect via IMAP/SMTP. Use an app-specific password for Gmail/Outlook.</p>
          <div class="email-form-grid">
            <label>IMAP Host<input id="em-imap-host" placeholder="imap.gmail.com" /></label>
            <label>IMAP Port<input id="em-imap-port" type="number" value="993" /></label>
            <label>SMTP Host<input id="em-smtp-host" placeholder="smtp.gmail.com" /></label>
            <label>SMTP Port<input id="em-smtp-port" type="number" value="587" /></label>
            <label>Email / Username<input id="em-username" placeholder="you@gmail.com" /></label>
            <label>App Password<input id="em-password" type="password" placeholder="••••••••" /></label>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;">
            <button class="icon-btn" id="em-save-cfg" style="padding:6px 16px;">Save & Connect</button>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);">
              <input type="checkbox" id="em-use-ssl" checked /> Use SSL
            </label>
          </div>
          <div id="em-setup-error" style="color:var(--danger,#ff5050);font-size:12px;margin-top:8px;"></div>
        </div>
      `;
      if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });
      container.querySelector('#em-save-cfg').addEventListener('click', async () => {
        const body = {
          imap_host: container.querySelector('#em-imap-host').value.trim(),
          imap_port: parseInt(container.querySelector('#em-imap-port').value) || 993,
          smtp_host: container.querySelector('#em-smtp-host').value.trim(),
          smtp_port: parseInt(container.querySelector('#em-smtp-port').value) || 587,
          username: container.querySelector('#em-username').value.trim(),
          password: container.querySelector('#em-password').value,
          use_ssl: container.querySelector('#em-use-ssl').checked,
        };
        if (!body.imap_host || !body.smtp_host || !body.username || !body.password) {
          container.querySelector('#em-setup-error').textContent = 'All fields are required.';
          return;
        }
        try {
          const res = await fetch('/api/email/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const data = await res.json();
          if (!res.ok) {
            container.querySelector('#em-setup-error').textContent = data.detail || 'Connection failed';
            return;
          }
          showToast('Email connected ✓');
          renderEmailPanel(container);
        } catch (e) {
          container.querySelector('#em-setup-error').textContent = 'Network error';
        }
      });
    }

    async function renderEmailInbox(container, cfg) {
      container.innerHTML = `
        <div class="email-toolbar">
          <select id="em-folder-select" class="email-folder-select">
            <option value="INBOX">Inbox</option>
          </select>
          <input id="em-search" class="email-search" placeholder="Search…" />
          <button class="icon-btn email-compose-btn" id="em-compose-btn" title="Compose"><span class="icon-emoji">✉</span><i data-lucide="pen-line" class="icon-lucide"></i> New</button>
          <button class="icon-btn" id="em-refresh-btn" title="Refresh"><span class="icon-emoji">↻</span><i data-lucide="refresh-cw" class="icon-lucide"></i></button>
          <button class="icon-btn" id="em-disconnect-btn" title="Disconnect" style="margin-left:auto;"><span class="icon-emoji">⚙</span><i data-lucide="settings" class="icon-lucide"></i></button>
        </div>
        <div id="em-message-list" class="email-message-list"><div class="library-loading">Loading…</div></div>
        <div id="em-compose-area" class="email-compose-area" style="display:none;"></div>
      `;

      // Load folders
      try {
        const fRes = await fetch('/api/email/folders');
        if (fRes.ok) {
          const folders = await fRes.json();
          const sel = container.querySelector('#em-folder-select');
          sel.innerHTML = '';
          folders.forEach(f => {
            const opt = document.createElement('option');
            opt.value = f;
            opt.textContent = f;
            if (f === _emailState.folder) opt.selected = true;
            sel.appendChild(opt);
          });
        }
      } catch {}

      // Events
      container.querySelector('#em-folder-select').addEventListener('change', (e) => {
        _emailState.folder = e.target.value;
        loadEmailMessages(container);
      });
      container.querySelector('#em-refresh-btn').addEventListener('click', () => refreshEmailPanel());
      container.querySelector('#em-compose-btn').addEventListener('click', () => showComposeForm(container));
      container.querySelector('#em-disconnect-btn').addEventListener('click', async () => {
        if (confirm('Disconnect email?')) {
          await fetch('/api/email/config', { method: 'DELETE' });
          renderEmailPanel(container);
        }
      });
      let searchTimeout;
      container.querySelector('#em-search').addEventListener('input', (e) => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => loadEmailMessages(container, e.target.value.trim()), 400);
      });

      if (window.lucide) lucide.createIcons({ nodes: container.querySelectorAll('[data-lucide]') });
      loadEmailMessages(container);
    }

    async function loadEmailMessages(container, search) {
      const listEl = container.querySelector('#em-message-list');
      if (!listEl) return;
      listEl.innerHTML = '<div class="library-loading">Loading…</div>';
      try {
        let url = '/api/email/messages?folder=' + encodeURIComponent(_emailState.folder) + '&limit=50';
        if (search) url += '&search=' + encodeURIComponent(search);
        const res = await fetch(url);
        if (!res.ok) { const d = await res.json(); listEl.innerHTML = '<div class="library-empty">' + escHtml(d.detail || 'Error') + '</div>'; return; }
        const data = await res.json();
        _emailState.messages = data.messages;
        if (!data.messages.length) {
          listEl.innerHTML = '<div class="library-empty">No messages.</div>';
          return;
        }
        listEl.innerHTML = '';
        data.messages.forEach(msg => {
          const row = document.createElement('div');
          row.className = 'email-msg-row';
          row.innerHTML = `
            <div class="email-msg-from">${escHtml(msg.from || 'Unknown')}</div>
            <div class="email-msg-subject">${msg.has_attachments ? '<i data-lucide="paperclip" style="width:12px;height:12px;display:inline;vertical-align:middle;margin-right:2px;"></i> ' : ''}${escHtml(msg.subject || '(no subject)')}</div>
            <div class="email-msg-date">${formatEmailDate(msg.date)}</div>
          `;
          row.addEventListener('click', () => openEmailMessage(container, msg.uid));
          listEl.appendChild(row);
        });
        if (window.lucide) lucide.createIcons({ nodes: listEl.querySelectorAll('[data-lucide]') });
      } catch {
        listEl.innerHTML = '<div class="library-empty">Failed to load messages.</div>';
      }
    }

    async function openEmailMessage(container, uid) {
      const listEl = container.querySelector('#em-message-list');
      if (!listEl) return;
      listEl.innerHTML = '<div class="library-loading">Loading message…</div>';
      try {
        const res = await fetch('/api/email/message/' + uid + '?folder=' + encodeURIComponent(_emailState.folder));
        if (!res.ok) { const d = await res.json(); listEl.innerHTML = '<div class="library-empty">' + escHtml(d.detail || 'Not found') + '</div>'; return; }
        const msg = await res.json();
        listEl.innerHTML = `
          <div class="email-reader">
            <div class="email-reader-header">
              <button class="icon-btn email-back-btn" id="em-back"><span class="icon-emoji">←</span><i data-lucide="arrow-left" class="icon-lucide"></i> Back</button>
              <div class="email-reader-meta">
                <div class="email-reader-subject">${escHtml(msg.subject)}</div>
                <div class="email-reader-from">From: ${escHtml(msg.from)}</div>
                <div class="email-reader-to">To: ${escHtml(msg.to)}${msg.cc ? ' | Cc: ' + escHtml(msg.cc) : ''}</div>
                <div class="email-reader-date">${msg.date}</div>
              </div>
            </div>
            <div class="email-reader-body">${escHtml(msg.body).replace(/\n/g, '<br>')}</div>
            ${msg.attachments && msg.attachments.length ? '<div class="email-attachments"><strong>Attachments:</strong> ' + msg.attachments.map(a => escHtml(a.filename) + ' (' + (a.size/1024).toFixed(1) + ' KB)').join(', ') + '</div>' : ''}
          </div>
        `;
        if (window.lucide) lucide.createIcons({ nodes: listEl.querySelectorAll('[data-lucide]') });
        listEl.querySelector('#em-back').addEventListener('click', () => loadEmailMessages(container));
      } catch {
        listEl.innerHTML = '<div class="library-empty">Failed to load message.</div>';
      }
    }

    function showComposeForm(container) {
      const area = container.querySelector('#em-compose-area');
      if (!area) return;
      area.style.display = 'block';
      area.innerHTML = `
        <div class="email-compose-form">
          <h4 style="margin-bottom:10px;font-size:13px;"><span class="icon-emoji">✉</span><i data-lucide="send" class="icon-lucide" style="width:14px;height:14px;margin-right:4px;"></i> Compose</h4>
          <input id="em-to" class="email-input" placeholder="To (email)" />
          <input id="em-cc" class="email-input" placeholder="Cc (optional)" />
          <input id="em-subject" class="email-input" placeholder="Subject" />
          <textarea id="em-body" class="email-textarea" rows="8" placeholder="Message body…"></textarea>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <button class="icon-btn" id="em-send" style="padding:6px 16px;">Send</button>
            <button class="icon-btn" id="em-cancel-compose" style="padding:6px 12px;">Cancel</button>
          </div>
          <div id="em-compose-status" style="font-size:12px;margin-top:6px;"></div>
        </div>
      `;
      if (window.lucide) lucide.createIcons({ nodes: area.querySelectorAll('[data-lucide]') });
      area.querySelector('#em-cancel-compose').addEventListener('click', () => { area.style.display = 'none'; });
      area.querySelector('#em-send').addEventListener('click', async () => {
        const to = area.querySelector('#em-to').value.trim();
        const subject = area.querySelector('#em-subject').value.trim();
        const body = area.querySelector('#em-body').value;
        const cc = area.querySelector('#em-cc').value.trim();
        const statusEl = area.querySelector('#em-compose-status');
        if (!to || !subject) { statusEl.textContent = 'To and Subject required.'; statusEl.style.color = 'var(--danger,#ff5050)'; return; }
        statusEl.textContent = 'Sending…'; statusEl.style.color = 'var(--muted)';
        try {
          const res = await fetch('/api/email/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to, subject, body, cc: cc || null }),
          });
          const data = await res.json();
          if (!res.ok) { statusEl.textContent = data.detail || 'Send failed'; statusEl.style.color = 'var(--danger,#ff5050)'; return; }
          statusEl.textContent = '✓ Sent!'; statusEl.style.color = 'var(--success,#4caf50)';
          setTimeout(() => { area.style.display = 'none'; }, 1200);
        } catch { statusEl.textContent = 'Network error'; statusEl.style.color = 'var(--danger,#ff5050)'; }
      });
    }

    function formatEmailDate(dateStr) {
      if (!dateStr) return '';
      try {
        const d = new Date(dateStr);
        const now = new Date();
        if (d.toDateString() === now.toDateString()) {
          return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
      } catch { return dateStr.slice(0, 16); }
    }

    async function openLibraryReader(section, filename, title) {
      try {
        const res = await fetch(`/api/library/read/${section}/${encodeURIComponent(filename)}`);
        const data = await res.json();
        if (data.error) { showToast(data.error, "error"); return; }
        // Show in a simple modal overlay
        const existing = document.getElementById("libraryReaderOverlay");
        if (existing) existing.remove();
        const overlay = document.createElement("div");
        overlay.id = "libraryReaderOverlay";
        overlay.className = "settings-overlay";
        overlay.innerHTML = `
          <div class="settings-panel library-reader-panel">
            <div class="settings-header">
              <h2>${escHtml(title)}</h2>
              <button class="icon-btn" id="libraryReaderClose"><span class="icon-emoji">✕</span><i data-lucide="x" class="icon-lucide"></i></button>
            </div>
            <div class="library-reader-content">${renderMarkdownSimple(data.content)}</div>
          </div>
        `;
        document.body.appendChild(overlay);
        overlay.querySelector("#libraryReaderClose").addEventListener("click", () => overlay.remove());
        overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
        lucide.createIcons({ nodes: overlay.querySelectorAll("[data-lucide]") });
      } catch { showToast("Failed to load file", "error"); }
    }

    function renderMarkdownSimple(md) {
      // Minimal markdown → HTML for library reader
      let html = escHtml(md);
      html = html.replace(/^### (.+)$/gm, "<h4>$1</h4>");
      html = html.replace(/^## (.+)$/gm, "<h3>$1</h3>");
      html = html.replace(/^# (.+)$/gm, "<h2>$1</h2>");
      html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
      html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
      html = html.replace(/^- (.+)$/gm, "<li>$1</li>");
      html = html.replace(/(<li>.*<\/li>\n?)+/g, "<ul>$&</ul>");
      html = html.replace(/^---$/gm, "<hr>");
      html = html.replace(/\n{2,}/g, "</p><p>");
      return "<p>" + html + "</p>";
    }

    // === Brain / Memory Panel ===
    const CATEGORIES = ["semantic", "episodic", "procedural", "ephemeral"];
    const CAT_LABELS = { semantic: "Facts & Knowledge", episodic: "Events & Experiences", procedural: "Skills & Processes", ephemeral: "⏳ Temporary" };
    let _memoryCache = { semantic: [], episodic: [], procedural: [], ephemeral: [] };
    let _activeCat = "semantic";
    const memList = document.getElementById("memoryList");
    const memKeyInput = document.getElementById("memKeyInput");
    const memValInput = document.getElementById("memValInput");
    const memAddBtn = document.getElementById("memAddBtn");
    const memSaveBtn = document.getElementById("memSaveBtn");
    const memStatus = document.getElementById("memStatus");
    const memExpiryRow = document.getElementById("memExpiryRow");
    const memExpiryInput = document.getElementById("memExpiryInput");
    const protectedList = document.getElementById("protectedList");
    const protKeyInput = document.getElementById("protKeyInput");
    const protValInput = document.getElementById("protValInput");
    const protAddBtn = document.getElementById("protAddBtn");
    const protSaveBtn = document.getElementById("protSaveBtn");
    const protStatus = document.getElementById("protStatus");
    let _protectedCache = [];

    function renderMemory() {
      memList.innerHTML = "";
      memExpiryRow.style.display = _activeCat === "ephemeral" ? "" : "none";
      // Category tabs
      const tabBar = document.createElement("div");
      tabBar.className = "mem-tab-bar";
      CATEGORIES.forEach(cat => {
        const btn = document.createElement("button");
        btn.textContent = CAT_LABELS[cat];
        btn.className = "icon-btn mem-tab-btn" + (cat === _activeCat ? " active" : "");
        btn.addEventListener("click", () => { _activeCat = cat; renderMemory(); });
        tabBar.appendChild(btn);
      });
      memList.appendChild(tabBar);
      // Entries for active category
      const entries = _memoryCache[_activeCat] || [];
      if (entries.length === 0) {
        const empty = document.createElement("p");
        empty.className = "muted mem-empty";
        empty.textContent = "No entries in this category yet.";
        memList.appendChild(empty);
        return;
      }
      entries.forEach((entry, idx) => {
        const wrapper = document.createElement("div");
        wrapper.className = "mem-entry";

        const header = document.createElement("div");
        header.className = "mem-entry-header";
        const keySpan = document.createElement("span");
        keySpan.className = "mem-entry-key";
        keySpan.textContent = entry.key || "(no key)";
        const valPreview = document.createElement("span");
        valPreview.className = "mem-entry-val";
        valPreview.textContent = entry.value || "";
        const delBtn = document.createElement("button");
        delBtn.textContent = "✕";
        delBtn.className = "icon-btn mem-del-btn";
        delBtn.addEventListener("click", (e) => { e.stopPropagation(); _memoryCache[_activeCat].splice(idx, 1); renderMemory(); });
        header.append(keySpan, valPreview);
        if (entry.expires_at) {
          const badge = document.createElement("span");
          badge.className = "mem-expiry-badge";
          badge.textContent = "⏳ " + String(entry.expires_at).replace("T", " ").slice(0, 16);
          header.appendChild(badge);
        }
        header.appendChild(delBtn);
        wrapper.appendChild(header);

        // Expandable full value on click
        const fullVal = document.createElement("div");
        fullVal.className = "mem-full-val";
        fullVal.textContent = entry.value || "";
        wrapper.appendChild(fullVal);

        let expanded = false;
        wrapper.addEventListener("click", () => {
          expanded = !expanded;
          fullVal.classList.toggle("show", expanded);
          valPreview.classList.toggle("hidden", expanded);
        });

        memList.appendChild(wrapper);
      });
    }

    async function loadMemory() {
      try {
        const res = await fetch("/api/settings/memory");
        const data = await res.json();
        const raw = data.memory;
        if (raw && typeof raw === "object" && !Array.isArray(raw)) {
          _memoryCache = { semantic: raw.semantic || [], episodic: raw.episodic || [], procedural: raw.procedural || [], ephemeral: raw.ephemeral || [] };
        } else {
          _memoryCache = { semantic: Array.isArray(raw) ? raw : [], episodic: [], procedural: [], ephemeral: [] };
        }
        renderMemory();
      } catch (e) { console.error("loadMemory failed", e); }
    }

    memAddBtn.addEventListener("click", () => {
      const key = memKeyInput.value.trim();
      const value = memValInput.value.trim();
      if (!key && !value) return;
      if (!_memoryCache[_activeCat]) _memoryCache[_activeCat] = [];
      const newEntry = { key, value };
      if (_activeCat === "ephemeral" && memExpiryInput.value) {
        newEntry.expires_at = memExpiryInput.value;
      }
      _memoryCache[_activeCat].push(newEntry);
      memKeyInput.value = "";
      memValInput.value = "";
      memExpiryInput.value = "";
      renderMemory();
    });

    memSaveBtn.addEventListener("click", async () => {
      memStatus.textContent = "Saving...";
      try {
        const res = await fetch("/api/settings/memory", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ memory: _memoryCache })
        });
        memStatus.innerHTML = res.ok ? `${lucideIcon("✓")} Saved` : `${lucideIcon("✕")} Failed`;
        activateLucideIcons(memStatus);
      } catch (e) { memStatus.innerHTML = `${lucideIcon("✕")} Error`; activateLucideIcons(memStatus); }
      setTimeout(() => { memStatus.textContent = ""; }, 2000);
    });

    // === Protected Memory ===
    function renderProtected() {
      protectedList.innerHTML = "";
      if (_protectedCache.length === 0) {
        const empty = document.createElement("p");
        empty.className = "muted mem-empty";
        empty.textContent = "No protected entries yet.";
        protectedList.appendChild(empty);
        return;
      }
      _protectedCache.forEach((entry, idx) => {
        const wrapper = document.createElement("div");
        wrapper.className = "mem-entry mem-entry-protected";
        const header = document.createElement("div");
        header.className = "mem-entry-header";
        const keySpan = document.createElement("span");
        keySpan.className = "mem-entry-key";
        keySpan.textContent = entry.key || "(no key)";
        const valPreview = document.createElement("span");
        valPreview.className = "mem-entry-val";
        valPreview.textContent = entry.value || "";
        const badge = document.createElement("span");
        badge.className = "mem-protected-badge";
        badge.textContent = "🔒";
        const delBtn = document.createElement("button");
        delBtn.textContent = "✕";
        delBtn.className = "icon-btn mem-del-btn";
        delBtn.addEventListener("click", (e) => { e.stopPropagation(); _protectedCache.splice(idx, 1); renderProtected(); });
        header.append(keySpan, valPreview, badge, delBtn);
        wrapper.appendChild(header);
        const fullVal = document.createElement("div");
        fullVal.className = "mem-full-val";
        fullVal.textContent = entry.value || "";
        wrapper.appendChild(fullVal);
        let expanded = false;
        wrapper.addEventListener("click", () => {
          expanded = !expanded;
          fullVal.classList.toggle("show", expanded);
          valPreview.classList.toggle("hidden", expanded);
        });
        protectedList.appendChild(wrapper);
      });
    }

    async function loadProtected() {
      try {
        const res = await fetch("/api/settings/memory/protected");
        const data = await res.json();
        _protectedCache = Array.isArray(data.protected) ? data.protected : [];
        renderProtected();
      } catch (e) { console.error("loadProtected failed", e); }
    }

    protAddBtn.addEventListener("click", () => {
      const key = protKeyInput.value.trim();
      const value = protValInput.value.trim();
      if (!key && !value) return;
      _protectedCache.push({ key, value });
      protKeyInput.value = "";
      protValInput.value = "";
      renderProtected();
    });

    protSaveBtn.addEventListener("click", async () => {
      protStatus.textContent = "Saving...";
      try {
        const res = await fetch("/api/settings/memory/protected", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ protected: _protectedCache })
        });
        protStatus.innerHTML = res.ok ? `${lucideIcon("✓")} Saved` : `${lucideIcon("✕")} Failed`;
        activateLucideIcons(protStatus);
      } catch (e) { protStatus.innerHTML = `${lucideIcon("✕")} Error`; activateLucideIcons(protStatus); }
      setTimeout(() => { protStatus.textContent = ""; }, 2000);
    });

    // === Memory Search Settings ===
    const msModelSelect = document.getElementById("msModelSelect");
    const msTopK = document.getElementById("msTopK");
    const msThresholdEditor = document.getElementById("msThresholdEditor");
    const msEnabled = document.getElementById("msEnabled");
    const msSaveBtn = document.getElementById("msSaveBtn");
    const msInfo = document.getElementById("msInfo");
    let _msLoaded = false;

    function buildThresholdEditor(models, customThresholds) {
      msThresholdEditor.innerHTML = "";
      const header = document.createElement("p");
      header.className = "muted";
      header.style.cssText = "font-size:11px;margin:0 0 2px;text-transform:uppercase;letter-spacing:0.5px;";
      header.textContent = "Per-model thresholds (blank = calibrated default)";
      msThresholdEditor.appendChild(header);
      (models || []).forEach((m) => {
        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:8px;";
        const label = document.createElement("span");
        label.className = "muted";
        label.style.cssText = "font-size:12px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        label.textContent = m.id.split("/").pop();
        label.title = m.id;
        const input = document.createElement("input");
        input.type = "number";
        input.step = "0.001";
        input.min = "0";
        input.max = "1";
        input.className = "mem-input";
        input.style.cssText = "width:80px;";
        input.placeholder = String(m.threshold);
        input.dataset.model = m.id;
        const custom = customThresholds?.[m.id];
        if (custom !== undefined && custom !== null) input.value = custom;
        row.appendChild(label);
        row.appendChild(input);
        msThresholdEditor.appendChild(row);
      });
    }

    async function loadMemorySearchSettings() {
      if (_msLoaded) return;
      try {
        const res = await fetch("/api/settings/memory-search");
        if (!res.ok) return;
        const data = await res.json();
        msModelSelect.innerHTML = "";
        (data.available_models || []).forEach((m) => {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = `${m.id.split("/").pop()} (θ=${m.threshold})`;
          if (m.id === data.current_model) opt.selected = true;
          msModelSelect.appendChild(opt);
        });
        msTopK.value = data.top_k || 10;
        document.getElementById("msMaxChars").value = data.max_prompt_chars || 20000;
        buildThresholdEditor(data.available_models, data.model_thresholds);
        msEnabled.checked = data.enabled !== false;
        msInfo.textContent = `Active: ${data.current_model} | Threshold: ${data.current_threshold}`;
        _msLoaded = true;
      } catch (e) { console.error("loadMemorySearchSettings failed", e); }
    }

    msSaveBtn.addEventListener("click", async () => {
      try {
        const modelThresholds = {};
        msThresholdEditor.querySelectorAll("input[data-model]").forEach((inp) => {
          if (inp.value.trim() !== "") modelThresholds[inp.dataset.model] = parseFloat(inp.value);
        });
        const res = await fetch("/api/settings/memory-search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: msModelSelect.value,
            top_k: parseInt(msTopK.value) || 10,
            max_prompt_chars: parseInt(document.getElementById("msMaxChars").value) || 20000,
            model_thresholds: modelThresholds,
            enabled: msEnabled.checked,
          }),
        });
        if (res.ok) {
          const data = await res.json();
          msInfo.textContent = `Active: ${data.current_model} | Threshold: ${data.current_threshold}`;
          showToast("✅ Memory search settings saved", "success");
        } else {
          showToast("✕ Failed to save", "error");
        }
      } catch (e) { showToast("✕ Error saving", "error"); }
    });

    document.getElementById("msRefreshCache").addEventListener("click", async () => {
      const btn = document.getElementById("msRefreshCache");
      const status = document.getElementById("msCacheStatus");
      btn.disabled = true;
      btn.textContent = "⏳ Rebuilding…";
      status.textContent = "";
      try {
        const res = await fetch("/api/settings/memory-search/refresh-cache", { method: "POST" });
        if (res.ok) {
          const data = await res.json();
          status.textContent = data.detail || "Cache rebuilt.";
          showToast("🔄 Memory cache rebuilt", "success");
        } else {
          status.textContent = "Failed to rebuild cache.";
          showToast("✕ Cache refresh failed", "error");
        }
      } catch (e) {
        status.textContent = "Error.";
        showToast("✕ Error refreshing cache", "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "🔄 Refresh Cache";
      }
    });

    // === Personal Context (instruction/personal.md) ===
    const personalArea = document.getElementById("personalContextArea");
    const personalSaveBtn = document.getElementById("personalSaveBtn");
    const personalStatus = document.getElementById("personalStatus");

    async function loadPersonal() {
      try {
        const res = await fetch("/api/settings/personal");
        const data = await res.json();
        personalArea.value = data.content || "";
      } catch (e) { console.error("loadPersonal failed", e); }
    }

    personalSaveBtn.addEventListener("click", async () => {
      personalSaveBtn.disabled = true;
      personalStatus.textContent = "Saving...";
      try {
        const res = await fetch("/api/settings/personal", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: personalArea.value }),
        });
        if (res.ok) {
          personalStatus.textContent = "✓ Saved";
          showToast("👤 Personal context saved", "success");
        } else {
          personalStatus.textContent = "Failed to save.";
          showToast("✕ Failed to save personal context", "error");
        }
      } catch (e) {
        personalStatus.textContent = "Error.";
        showToast("✕ Error saving personal context", "error");
      } finally {
        personalSaveBtn.disabled = false;
        setTimeout(() => { personalStatus.textContent = ""; }, 3000);
      }
    });

    // Load memory + search settings when Brain tab is clicked
    document.querySelector('[data-tab="brain"]').addEventListener("click", () => {
      loadPersonal();
      loadMemory();
      loadProtected();
      loadMemorySearchSettings();
    });

    // === Skills Panel ===
    const skillsGrid = document.getElementById("skillsGrid");
    const skillDetailOverlay = document.getElementById("skillDetailOverlay");
    const skillDetailClose = document.getElementById("skillDetailClose");
    const skillDisableToggle = document.getElementById("skillDisableToggle");
    let skillsLoaded = false;
    let _currentSkillKey = null;

    const DISABLED_SKILLS_KEY = "sable_disabled_skills";
    function getDisabledSkills() {
      try { return JSON.parse(localStorage.getItem(DISABLED_SKILLS_KEY)) || []; } catch { return []; }
    }
    function setDisabledSkills(arr) {
      try { localStorage.setItem(DISABLED_SKILLS_KEY, JSON.stringify(arr)); } catch (e) {}
    }

    async function loadSkills() {
      if (skillsLoaded) return;
      try {
        const res = await fetch("/api/skills/browse");
        const data = await res.json();
        const skills = data.skills || [];
        const disabled = getDisabledSkills();
        skillsGrid.innerHTML = "";
        skills.forEach((sk) => {
          const chip = document.createElement("div");
          const isDisabled = disabled.includes(sk.path);
          chip.className = "skill-chip" + (isDisabled ? " skill-chip-disabled" : "");
          chip.innerHTML = `<div class="skill-chip-name">${sk.name}${isDisabled ? ' <span class="skill-disabled-badge">off</span>' : ""}</div><div class="skill-chip-cat">${sk.category}</div>`;
          chip.addEventListener("click", () => showSkillDetail(sk));
          skillsGrid.appendChild(chip);
        });
        skillsLoaded = true;
      } catch (e) { console.error("loadSkills failed", e); }
    }

    function showSkillDetail(sk) {
      _currentSkillKey = sk.path;
      document.getElementById("skillDetailName").textContent = sk.name;
      document.getElementById("skillDetailCat").textContent = sk.category || "—";
      document.getElementById("skillDetailPath").textContent = "skills/" + sk.path;

      // Scripts
      const scriptsRow = document.getElementById("skillScriptsRow");
      const scriptsEl = document.getElementById("skillDetailScripts");
      scriptsEl.innerHTML = "";
      if (sk.scripts && sk.scripts.length > 0) {
        scriptsRow.style.display = "";
        sk.scripts.forEach((s) => {
          const span = document.createElement("span");
          span.textContent = s;
          scriptsEl.appendChild(span);
        });
      } else {
        scriptsRow.style.display = "none";
      }

      // Render instruction.md as markdown
      const instrEl = document.getElementById("skillInstruction");
      instrEl.innerHTML = sk.instruction_content ? renderMarkdown(sk.instruction_content) : "<em>No instruction file.</em>";

      // Disable toggle state
      skillDisableToggle.checked = getDisabledSkills().includes(sk.path);

      skillDetailOverlay.classList.add("show");
    }

    skillDisableToggle.addEventListener("change", () => {
      if (!_currentSkillKey) return;
      let disabled = getDisabledSkills();
      if (skillDisableToggle.checked) {
        if (!disabled.includes(_currentSkillKey)) disabled.push(_currentSkillKey);
      } else {
        disabled = disabled.filter((k) => k !== _currentSkillKey);
      }
      setDisabledSkills(disabled);
      // Update chip in grid
      skillsLoaded = false;
      loadSkills();
    });

    skillDetailClose.addEventListener("click", () => skillDetailOverlay.classList.remove("show"));
    skillDetailOverlay.addEventListener("click", (e) => {
      if (e.target === skillDetailOverlay) skillDetailOverlay.classList.remove("show");
    });

    document.querySelector('[data-tab="skills"]').addEventListener("click", loadSkills);
    document.querySelector('[data-tab="account"]').addEventListener("click", loadAccountProfiles);

    // --- Providers tab: Unified API key manager ---
    const _keyProviderMeta = {
      gemini:  { apiBase: "/api/settings/gemini",  name: "Gemini",  placeholder: "Paste API key (AIza…)" },
      groq:    { apiBase: "/api/settings/groq",    name: "Groq",    placeholder: "Paste API key (gsk_…)" },
      mistral: { apiBase: "/api/settings/mistral", name: "Mistral", placeholder: "Paste API key (key: …)" },
    };
    const _keyEls = {
      select: document.getElementById("keyProviderSelect"),
      input:  document.getElementById("apiKeyInput"),
      btn:    document.getElementById("addApiKeyBtn"),
      list:   document.getElementById("apiKeyList"),
      status: document.getElementById("apiKeyStatus"),
    };
    let _currentKeyProvider = "gemini";

    async function _loadKeysFor(provider) {
      const meta = _keyProviderMeta[provider];
      if (!meta || !_keyEls.list) return;
      try {
        const res = await fetch(`${meta.apiBase}/keys`);
        const data = await res.json();
        const keys = data.keys || [];
        _keyEls.list.innerHTML = "";
        if (keys.length === 0) {
          _keyEls.list.innerHTML = '<div style="font-size:12px;color:var(--text);padding:8px 0;">No keys configured yet.</div>';
          _keyEls.status.textContent = "";
          return;
        }
        keys.forEach((k) => {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-radius:8px;background:color-mix(in srgb, var(--panel) 60%, transparent);border:1px solid var(--border);";
          const label = document.createElement("span");
          label.style.cssText = "font-size:12px;font-family:monospace;color:var(--text);";
          label.textContent = k.masked + (k.active ? " ●" : "");
          const delBtn = document.createElement("button");
          delBtn.textContent = "✕";
          delBtn.style.cssText = "background:transparent;border:none;color:var(--danger);cursor:pointer;font-size:13px;padding:2px 6px;border-radius:4px;";
          delBtn.title = "Remove key";
          delBtn.addEventListener("click", async () => {
            if (!confirm(`Remove this ${meta.name} API key?`)) return;
            try {
              await fetch(`${meta.apiBase}/api-key/${k.index}`, { method: "DELETE" });
              _loadKeysFor(_currentKeyProvider);
            } catch (e) { showToast("Failed to remove key", "error"); }
          });
          row.appendChild(label);
          row.appendChild(delBtn);
          _keyEls.list.appendChild(row);
        });
        _keyEls.status.textContent = `${keys.length} key${keys.length !== 1 ? "s" : ""} configured · auto-rotation enabled`;
      } catch (e) {
        _keyEls.status.textContent = "Failed to load keys";
      }
    }

    function _switchKeyProvider(provider) {
      _currentKeyProvider = provider;
      const meta = _keyProviderMeta[provider];
      if (_keyEls.input && meta) _keyEls.input.placeholder = meta.placeholder;
      if (_keyEls.input) _keyEls.input.value = "";
      _loadKeysFor(provider);
    }

    if (_keyEls.select) {
      _keyEls.select.addEventListener("change", () => _switchKeyProvider(_keyEls.select.value));
    }
    if (_keyEls.btn) {
      _keyEls.btn.addEventListener("click", async () => {
        const key = _keyEls.input?.value?.trim();
        if (!key) { showToast("Paste an API key first", "error"); return; }
        const meta = _keyProviderMeta[_currentKeyProvider];
        if (!meta) return;
        try {
          const res = await fetch(`${meta.apiBase}/api-key`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: key }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            const detail = typeof err.detail === "string" ? err.detail : (Array.isArray(err.detail) ? err.detail[0]?.msg : JSON.stringify(err.detail));
            showToast(detail || "Failed to add key", "error");
            return;
          }
          if (_keyEls.input) _keyEls.input.value = "";
          showToast(`${meta.name} key added ✓`, "success");
          _loadKeysFor(_currentKeyProvider);
        } catch (e) { showToast("Failed to add key", "error"); }
      });
    }
    if (_keyEls.input) {
      _keyEls.input.addEventListener("keydown", (e) => { if (e.key === "Enter") _keyEls.btn?.click(); });
    }


    // --- Provider model fetching ---
    const providerSelect = document.getElementById("customModelBackend");
    const modelSelect = document.getElementById("customModelId");
    const modelLabelInput = document.getElementById("customModelLabel");
    let _fetchedModels = []; // cache of {id, label} from last fetch

    async function fetchProviderModels(provider) {
      if (!provider) {
        modelSelect.disabled = true;
        modelSelect.innerHTML = '<option value="">Select provider first…</option>';
        return;
      }
      modelSelect.disabled = true;
      modelSelect.innerHTML = '<option value="">Loading models…</option>';
      modelLabelInput.value = "";
      try {
        const res = await fetch(`/api/settings/providers/${provider}/models`);
        const data = await res.json();
        _fetchedModels = data.models || [];
        if (!data.available) {
          modelSelect.innerHTML = '<option value="">⚠️ No API key configured for this provider</option>';
          modelSelect.disabled = true;
          return;
        }
        if (_fetchedModels.length === 0) {
          modelSelect.innerHTML = '<option value="">No models found</option>';
          modelSelect.disabled = true;
          return;
        }
        modelSelect.innerHTML = '<option value="">— Choose a model —</option>';
        const provLabel = provider.charAt(0).toUpperCase() + provider.slice(1);
        _fetchedModels.forEach((m) => {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = `${provLabel}: ${m.label}`;
          modelSelect.appendChild(opt);
        });
        modelSelect.disabled = false;
      } catch (e) {
        modelSelect.innerHTML = '<option value="">Failed to fetch models</option>';
        modelSelect.disabled = true;
      }
    }

    if (providerSelect) {
      providerSelect.addEventListener("change", () => fetchProviderModels(providerSelect.value));
    }
    if (modelSelect) {
      modelSelect.addEventListener("change", () => {
        const selected = _fetchedModels.find((m) => m.id === modelSelect.value);
        modelLabelInput.value = selected ? selected.label : "";
      });
    }

    document.querySelector('[data-tab="providers"]')?.addEventListener("click", () => {
      _switchKeyProvider(_keyEls.select?.value || "gemini");
      loadCustomModels();
    });

    // --- Model management (all models: static + custom) ---
    async function loadCustomModels() {
      const listEl = document.getElementById("customModelList");
      const statusEl = document.getElementById("customModelStatus");
      if (!listEl) return;
      try {
        const res = await fetch("/api/models");
        const data = await res.json();
        const allModels = data.models || [];
        listEl.innerHTML = "";
        if (allModels.length === 0) {
          listEl.innerHTML = '<div style="font-size:12px;color:var(--text);padding:8px 0;">No models available.</div>';
          statusEl.textContent = "";
          return;
        }
        allModels.forEach((m) => {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;flex-direction:column;border-radius:8px;background:color-mix(in srgb, var(--panel) 60%, transparent);border:1px solid var(--border);overflow:hidden;";
          // Top bar (clickable)
          const topBar = document.createElement("div");
          topBar.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 10px;cursor:pointer;";
          topBar.addEventListener("mouseenter", () => topBar.style.background = "color-mix(in srgb, var(--accent) 6%, transparent)");
          topBar.addEventListener("mouseleave", () => topBar.style.background = "transparent");
          const info = document.createElement("span");
          info.style.cssText = "font-size:12px;color:var(--text);";
          const badge = m.custom ? '<span style="color:var(--accent);font-size:9px;background:color-mix(in srgb, var(--accent) 15%, transparent);padding:1px 5px;border-radius:4px;margin-left:6px;">CUSTOM</span>' : '';
          info.innerHTML = `<b>${m.label}</b> <span style="color:var(--text);font-family:monospace;font-size:11px;">${m.id}</span> <span style="color:var(--text);font-size:10px;text-transform:uppercase;">${m.api_backend || 'local'}</span>${badge}`;
          topBar.appendChild(info);
          // Expandable detail panel
          const detail = document.createElement("div");
          detail.style.cssText = "display:none;padding:6px 12px 10px;font-size:11px;color:var(--text);border-top:1px solid var(--border);";
          const caps = m.capabilities || {};
          const capIcons = [];
          if (caps.image) capIcons.push("🖼️ Image");
          if (caps.video) capIcons.push("🎬 Video");
          if (caps.document) capIcons.push("📄 Document");
          if (caps.audio) capIcons.push("🎧 Audio");
          const hasThinking = (m.thinking_modes || []).some(tm => tm.thinking_enabled);
          const thinkingBadge = hasThinking ? ' · 🧠 Thinking' : '';
          detail.innerHTML = `<span style="color:var(--text);font-weight:500;">Capabilities:</span> ${capIcons.length ? capIcons.join(" · ") : '<span style="opacity:0.6;">None</span>'}${thinkingBadge}`;
          topBar.addEventListener("click", () => {
            const open = detail.style.display !== "none";
            detail.style.display = open ? "none" : "block";
          });
          row.appendChild(topBar);
          row.appendChild(detail);
          // Delete button (stop propagation so it doesn't toggle detail)
          const delBtn = document.createElement("button");
          delBtn.textContent = "✕";
          delBtn.style.cssText = "background:transparent;border:none;color:var(--danger);cursor:pointer;font-size:13px;padding:2px 6px;border-radius:4px;position:absolute;right:8px;top:50%;transform:translateY(-50%);";
          delBtn.title = "Remove model";
          delBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            if (!confirm(`Remove "${m.label}" from your model list?`)) return;
            try {
              await fetch(`/api/settings/models/${encodeURIComponent(m.id)}`, { method: "DELETE" });
              showToast("Model removed", "success");
              loadCustomModels();
              loadModels();
            } catch (e2) { showToast("Failed to remove model", "error"); }
          });
          topBar.style.position = "relative";
          topBar.appendChild(delBtn);
          listEl.appendChild(row);
        });
        const customCount = allModels.filter((m) => m.custom).length;
        statusEl.textContent = `${allModels.length} model${allModels.length !== 1 ? "s" : ""} active${customCount ? ` · ${customCount} custom` : ""}`;
      } catch (e) {
        statusEl.textContent = "Failed to load models";
        setTimeout(() => { if (statusEl.textContent === "Failed to load models") statusEl.textContent = ""; }, 4000);
      }
    }

    const addCustomModelBtn = document.getElementById("addCustomModelBtn");
    if (addCustomModelBtn) {
      addCustomModelBtn.addEventListener("click", async () => {
        const mid = document.getElementById("customModelId")?.value;
        const label = document.getElementById("customModelLabel")?.value.trim();
        const backend = document.getElementById("customModelBackend")?.value;
        const capabilities = {
          image: document.getElementById("capImage")?.checked || false,
          video: document.getElementById("capVideo")?.checked || false,
          document: document.getElementById("capDocument")?.checked || false,
          audio: document.getElementById("capAudio")?.checked || false,
        };
        const supportsThinking = document.getElementById("capThinking")?.checked || false;
        if (!backend) { showToast("Select a provider first", "error"); return; }
        if (!mid) { showToast("Select a model from the dropdown", "error"); return; }
        if (!label) { showToast("Enter a display name", "error"); return; }
        try {
          const res = await fetch("/api/settings/models", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: mid, label, api_backend: backend, capabilities, supports_thinking: supportsThinking }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            const detail = typeof err.detail === "string" ? err.detail : (Array.isArray(err.detail) ? err.detail[0]?.msg : JSON.stringify(err.detail));
            showToast(detail || "Failed to add model", "error");
            return;
          }
          // Reset form
          modelSelect.value = "";
          modelLabelInput.value = "";
          document.getElementById("capImage").checked = false;
          document.getElementById("capVideo").checked = false;
          document.getElementById("capDocument").checked = false;
          document.getElementById("capAudio").checked = false;
          document.getElementById("capThinking").checked = false;
          showToast("Model added ✓", "success");
          loadCustomModels();
          loadModels();
        } catch (e) { showToast("Failed to add model", "error"); }
      });
    }

    function appendLogLine(msg) {
      const span = document.createElement("span");
      let cls = "log-info";
      if (/\[WARN(ING)?\]/i.test(msg)) cls = "log-warn";
      else if (/\[ERROR\]/i.test(msg)) cls = "log-error";
      else if (/\[DEBUG\]/i.test(msg)) cls = "log-debug";
      span.className = cls;
      span.textContent = msg + "\n";
      logViewer.appendChild(span);

      // Keep buffer manageable (max ~2000 lines)
      while (logViewer.childElementCount > 2000) {
        logViewer.removeChild(logViewer.firstChild);
      }

      if (logAutoScroll.checked) {
        logViewer.scrollTop = logViewer.scrollHeight;
      }
    }

    function connectLogs() {
      if (logSource) logSource.close();
      logSource = new EventSource("/api/logs?token=" + encodeURIComponent(getToken() || ""));
      logSource.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data.type === "log" && data.message) {
            appendLogLine(data.message);
          }
        } catch {}
      };
      logSource.onerror = () => {
        // Auto-reconnect handled by EventSource
      };
    }

    // Keyboard shortcut: Escape closes settings / library / skill detail
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const readerOverlay = document.getElementById("libraryReaderOverlay");
        if (readerOverlay) {
          readerOverlay.remove();
        } else if (skillDetailOverlay.classList.contains("show")) {
          skillDetailOverlay.classList.remove("show");
        } else if (!libraryOverlay.classList.contains("hidden")) {
          closeLibrary();
        } else if (!settingsOverlay.classList.contains("hidden")) {
          closeSettings();
        }
      }
    });

    // Browser headless toggle
    const headlessToggle = document.getElementById("headlessToggle");
    const refreshWafBtn = document.getElementById("refreshWafBtn");

    async function loadBrowserSettings() {
      try {
        const res = await fetch("/api/settings/browser");
        if (res.ok) {
          const data = await res.json();
          headlessToggle.checked = data.headless;
        }
      } catch {}
      // Also load context pass settings
      loadContextPassSettings();
    }

    // ── Context Pass Settings ──
    const ctxPassModel = document.getElementById("ctxPassModel");
    const ctxPassBrowserAcc = document.getElementById("ctxPassBrowserAcc");

    function populateCtxPassModels() {
      if (!ctxPassModel) return;
      const current = ctxPassModel.value;
      ctxPassModel.innerHTML = '<option value="">Default (current model)</option>';
      for (const m of modelList) {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.name || m.id;
        ctxPassModel.appendChild(opt);
      }
      ctxPassModel.value = current;
    }

    async function populateCtxPassProfiles() {
      if (!ctxPassBrowserAcc) return;
      const current = ctxPassBrowserAcc.value;
      ctxPassBrowserAcc.innerHTML = '<option value="">Default (current)</option>';
      try {
        const res = await fetch("/api/settings/accounts");
        if (res.ok) {
          const data = await res.json();
          for (const acc of (data.accounts || [])) {
            const opt = document.createElement("option");
            opt.value = acc.name;
            opt.textContent = acc.email ? `${acc.name} (${acc.email})` : acc.name;
            ctxPassBrowserAcc.appendChild(opt);
          }
        }
      } catch {}
      ctxPassBrowserAcc.value = current;
    }

    async function loadContextPassSettings() {
      populateCtxPassModels();
      await populateCtxPassProfiles();
      try {
        const res = await fetch("/api/settings/context-pass");
        if (res.ok) {
          const d = await res.json();
          if (ctxPassModel) ctxPassModel.value = d.summarizer_model || "";
          if (ctxPassBrowserAcc) ctxPassBrowserAcc.value = d.browser_data_acc || "";
        }
      } catch {}
    }

    async function saveContextPassSettings() {
      try {
        await fetch("/api/settings/context-pass", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            summarizer_model: ctxPassModel ? ctxPassModel.value : "",
            browser_data_acc: ctxPassBrowserAcc ? ctxPassBrowserAcc.value : "",
          }),
        });
      } catch {}
    }

    if (ctxPassModel) ctxPassModel.addEventListener("change", saveContextPassSettings);
    if (ctxPassBrowserAcc) ctxPassBrowserAcc.addEventListener("change", saveContextPassSettings);
    // ── /Context Pass Settings ──

    refreshWafBtn.addEventListener("click", async () => {
      refreshWafBtn.disabled = true;
      refreshWafBtn.textContent = "🛡️ Refreshing…";
      try {
        const res = await fetch("/api/settings/browser/refresh-waf", { method: "POST" });
        if (res.ok) {
          showToast("WAF token refreshed!", "success");
        } else {
          const err = await res.json();
          showToast(err.detail || "Refresh failed", "error");
        }
      } catch (e) {
        showToast("Refresh error: " + e.message, "error");
      } finally {
        refreshWafBtn.disabled = false;
        refreshWafBtn.textContent = "🛡️ Refresh WAF";
      }
    });

    const refreshDeepseekTokenBtn = document.getElementById("refreshDeepseekTokenBtn");
    const deepseekTokenStatus = document.getElementById("deepseekTokenStatus");
    if (refreshDeepseekTokenBtn) {
      const setDsStatus = (msg, color) => {
        if (!deepseekTokenStatus) return;
        deepseekTokenStatus.textContent = msg;
        deepseekTokenStatus.style.color = color || "var(--text-dim)";
      };
      refreshDeepseekTokenBtn.addEventListener("click", async () => {
        refreshDeepseekTokenBtn.disabled = true;
        refreshDeepseekTokenBtn.textContent = "↻ Refreshing...";
        setDsStatus("Refreshing DeepSeek token from browser profile…", "var(--text-dim)");
        try {
          const res = await fetch("/api/settings/deepseek/refresh-token", { method: "POST" });
          const data = await res.json().catch(() => ({}));
          if (res.ok) {
            const preview = data.token_preview || "none";
            setDsStatus("✅ Token refreshed: " + preview, "var(--success, #3daa5c)");
            showToast("DeepSeek token: " + preview, "success");
            await loadModels();
          } else {
            const msg = data.detail || data.error || "DeepSeek token refresh failed";
            setDsStatus("✕ " + msg, "var(--danger, #cf3b52)");
            showToast(msg, "error");
          }
        } catch (e) {
          const msg = "DeepSeek refresh error: " + e.message;
          setDsStatus("✕ " + msg, "var(--danger, #cf3b52)");
          showToast(msg, "error");
        } finally {
          refreshDeepseekTokenBtn.disabled = false;
          refreshDeepseekTokenBtn.textContent = "↻ Refresh Token";
        }
      });
    }

    // ---------- Account Profile Switcher ----------
    const accountProfileCards = document.getElementById("accountProfileCards");
    const refreshAccountsBtn = document.getElementById("refreshAccountsBtn");

    async function loadAccountProfiles() {
      if (!accountProfileCards) return;
      accountProfileCards.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">Loading accounts…</p>';
      try {
        const res = await fetch("/api/settings/accounts");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const accounts = data.accounts || [];
        const active = data.active;

        if (!accounts.length) {
          accountProfileCards.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">No account profiles found. Create dirs like <code>system/browser-data-acc1</code>, <code>system/browser-data-acc2</code>…</p>';
          return;
        }

        accountProfileCards.innerHTML = accounts.map((acc) => {
          const isActive = acc.name === active;
          const email = acc.email || "unknown account";
          const size = acc.size_mb ? acc.size_mb + " MB" : "";
          return `<div style="display:flex;align-items:center;justify-content:space-between;background:var(--panel);border:1px solid ${isActive ? 'var(--accent)' : 'var(--border)'};border-radius:10px;padding:10px 14px;">
            <div style="min-width:0;">
              <div style="font-size:12px;font-weight:600;color:var(--text);">${email}</div>
              <div style="font-size:11px;color:var(--text-dim);margin-top:2px;">${acc.name}${size ? ' · ' + size : ''}${isActive ? ' · <span style="color:var(--accent);">active</span>' : ''}</div>
            </div>
            <div style="display:flex;gap:6px;align-items:center;flex-shrink:0;">
              <button class="icon-btn account-open-btn" data-profile="${acc.name}" style="width:auto;padding:5px 12px;font-size:11px;white-space:nowrap;">Open</button>
              ${isActive ? '' : `<button class="icon-btn account-switch-btn" data-profile="${acc.name}" style="width:auto;padding:5px 12px;font-size:11px;white-space:nowrap;">Switch</button>`}
              ${isActive ? '' : `<button class="icon-btn account-delete-btn" data-profile="${acc.name}" style="width:auto;padding:5px 10px;font-size:11px;white-space:nowrap;color:var(--danger);border-color:var(--danger);">Delete</button>`}
            </div>
          </div>`;
        }).join("");

        accountProfileCards.querySelectorAll(".account-switch-btn").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const profile = btn.dataset.profile;
            btn.disabled = true;
            btn.textContent = "Switching…";
            showToast("🔄 Switching account profile…", "info");
            try {
              const res = await fetch("/api/settings/accounts/switch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile }),
              });
              const data = await res.json().catch(() => ({}));
              if (res.ok) {
                showToast(`✅ Switched to ${data.email || profile}`, "success");
                await loadAccountProfiles();
                await loadModels();
              } else {
                showToast("Switch failed: " + (data.detail || "unknown"), "error");
                btn.disabled = false;
                btn.textContent = "Switch";
              }
            } catch (e) {
              showToast("Switch error: " + e.message, "error");
              btn.disabled = false;
              btn.textContent = "Switch";
            }
          });
        });
        accountProfileCards.querySelectorAll(".account-delete-btn").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const profile = btn.dataset.profile;
            if (!confirm(`Delete ${profile}?\n\nThis permanently removes the browser data directory.`)) return;
            btn.disabled = true;
            btn.textContent = "Deleting…";
            try {
              const res = await fetch("/api/settings/accounts/delete", {
                method: "DELETE",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile }),
              });
              const data = await res.json().catch(() => ({}));
              if (res.ok) {
                showToast(`🗑️ Deleted ${profile}`, "success");
                await loadAccountProfiles();
              } else {
                showToast("Delete failed: " + (data.detail || "unknown"), "error");
                btn.disabled = false;
                btn.textContent = "Delete";
              }
            } catch (e) {
              showToast("Delete error: " + e.message, "error");
              btn.disabled = false;
              btn.textContent = "Delete";
            }
          });
        });
        accountProfileCards.querySelectorAll(".account-open-btn").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const profile = btn.dataset.profile;
            btn.disabled = true;
            btn.textContent = "Opening…";
            try {
              const res = await fetch("/api/settings/accounts/open", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile }),
              });
              const data = await res.json().catch(() => ({}));
              if (res.ok) {
                showToast(`🌐 Opened browser for ${profile}`, "success");
              } else {
                showToast("Open failed: " + (data.detail || "unknown"), "error");
              }
            } catch (e) {
              showToast("Open error: " + e.message, "error");
            }
            btn.disabled = false;
            btn.textContent = "Open";
          });
        });
      } catch (e) {
        accountProfileCards.innerHTML = `<p class="muted" style="font-size:12px;margin:0;color:var(--danger);">Failed to load: ${e.message}</p>`;
      }
    }

    if (refreshAccountsBtn) {
      refreshAccountsBtn.addEventListener("click", loadAccountProfiles);
    }


    const addAccountBtn = document.getElementById("addAccountBtn");
    if (addAccountBtn) {
      addAccountBtn.addEventListener("click", async () => {
        addAccountBtn.disabled = true;
        addAccountBtn.textContent = "Opening…";
        try {
          const res = await fetch("/api/settings/accounts/create", { method: "POST" });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Failed");
          addAccountBtn.textContent = "Opening…";
          setTimeout(() => { addAccountBtn.textContent = "Add Account"; addAccountBtn.disabled = false; }, 3000);
        } catch (e) {
          addAccountBtn.textContent = e.message?.includes("401") ? "Not logged in" : "Failed";
          setTimeout(() => { addAccountBtn.textContent = "Add Account"; addAccountBtn.disabled = false; }, 2500);
        }
      });
    }



    // Load browser settings when settings panel opens
    const origOpenSettings = openSettings;
    openSettings = function() {
      origOpenSettings();
      // Only load the active tab's data, not everything upfront
      const activeTab = document.querySelector('.settings-tab.active');
      if (activeTab) {
        const tabName = activeTab.dataset.tab;
        if (tabName === 'general') loadBrowserSettings();
        else if (tabName === 'account') loadAccountProfiles();
      }
    };

    // ---------- Font Size ----------
    const FONT_SIZE_KEY = "sable_font_size";
    const fontSizeSelect = document.getElementById("fontSizeSelect");

    function applyFontSize(size) {
      document.documentElement.style.setProperty("--font-size-response", size);
    }

    fontSizeSelect.addEventListener("change", () => {
      const size = fontSizeSelect.value;
      applyFontSize(size);
      try { localStorage.setItem(FONT_SIZE_KEY, size); } catch (e) {}
    });

    (function loadFontSize() {
      let saved = null;
      try { saved = localStorage.getItem(FONT_SIZE_KEY); } catch (e) {}
      if (saved) {
        fontSizeSelect.value = saved;
        applyFontSize(saved);
      }
    })();

    // ---------- System Font Size ----------
    const SYS_FONT_KEY = "sable_system_font_size";
    const systemFontSizeSelect = document.getElementById("systemFontSizeSelect");

    function applySystemFontSize(size) {
      document.documentElement.style.setProperty("--font-size-system", size);
    }

    systemFontSizeSelect.addEventListener("change", () => {
      const size = systemFontSizeSelect.value;
      applySystemFontSize(size);
      try { localStorage.setItem(SYS_FONT_KEY, size); } catch (e) {}
    });

    (function loadSystemFontSize() {
      let saved = null;
      try { saved = localStorage.getItem(SYS_FONT_KEY); } catch (e) {}
      if (saved) { systemFontSizeSelect.value = saved; applySystemFontSize(saved); }
    })();

    // ---------- Editor Font Size ----------
    const EDITOR_FONT_KEY = "sable_editor_font_size";
    const editorFontSizeSelect = document.getElementById("editorFontSizeSelect");

    function applyEditorFontSize(size) {
      const px = parseInt(size, 10);
      document.documentElement.style.setProperty("--editor-font-size", px + "px");
      // Update all active Monaco editors (instances are local to filesystem.js IIFE)
      if (typeof monaco !== "undefined" && monaco.editor && monaco.editor.getEditors) {
        monaco.editor.getEditors().forEach(ed => ed.updateOptions({ fontSize: px }));
      }
    }

    editorFontSizeSelect.addEventListener("change", () => {
      const size = editorFontSizeSelect.value;
      applyEditorFontSize(size);
      try { localStorage.setItem(EDITOR_FONT_KEY, size); } catch (e) {}
    });

    (function loadEditorFontSize() {
      let saved = null;
      try { saved = localStorage.getItem(EDITOR_FONT_KEY); } catch (e) {}
      if (saved) { editorFontSizeSelect.value = saved; applyEditorFontSize(saved); }
    })();

    // ---------- IDE Chat Font Size ----------
    const IDE_CHAT_FONT_KEY = "sable_ide_chat_font_size";
    const ideChatFontSizeSelect = document.getElementById("ideChatFontSizeSelect");

    function applyIdeChatFontSize(size) {
      document.documentElement.style.setProperty("--ide-chat-font-size", size);
    }

    ideChatFontSizeSelect.addEventListener("change", () => {
      const size = ideChatFontSizeSelect.value;
      applyIdeChatFontSize(size);
      try { localStorage.setItem(IDE_CHAT_FONT_KEY, size); } catch (e) {}
    });

    (function loadIdeChatFontSize() {
      let saved = null;
      try { saved = localStorage.getItem(IDE_CHAT_FONT_KEY); } catch (e) {}
      if (saved) { ideChatFontSizeSelect.value = saved; applyIdeChatFontSize(saved); }
    })();

    // ---------- Font Family ----------
    const FONT_FAMILY_KEY = "sable_font_family";
    const fontFamilySelect = document.getElementById("fontFamilySelect");
    const FONT_STACKS = {
      maple: "'Maple Mono', ui-monospace, monospace",
      inter: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
      system: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    };

    function applyFontFamily(key) {
      const stack = FONT_STACKS[key] || FONT_STACKS.maple;
      document.documentElement.style.setProperty("--font-body", stack);
      document.documentElement.style.setProperty("--font-mono", stack);
      document.documentElement.style.setProperty("--font-serif", stack);
    }

    fontFamilySelect.addEventListener("change", () => {
      applyFontFamily(fontFamilySelect.value);
      try { localStorage.setItem(FONT_FAMILY_KEY, fontFamilySelect.value); } catch (e) {}
    });

    (function loadFontFamily() {
      let saved = null;
      try { saved = localStorage.getItem(FONT_FAMILY_KEY); } catch (e) {}
      if (saved && FONT_STACKS[saved]) {
        fontFamilySelect.value = saved;
        applyFontFamily(saved);
      }
    })();


    // ---------- MCP Server Management ----------
    async function loadMcpServers() {
      const listEl = document.getElementById("mcpServerList");
      const statusEl = document.getElementById("mcpStatus");
      if (!listEl) return;
      try {
        const res = await fetch("/api/settings/mcp");
        const data = await res.json();
        const servers = data.servers || [];
        if (servers.length === 0) {
          listEl.innerHTML = '<p style="font-size:12px;color:var(--text-dim);padding:8px 0;">No MCP servers configured yet. Add one above to get started.</p>';
          return;
        }
        listEl.innerHTML = servers.map(s => `
          <div style="border:1px solid var(--border);border-radius:10px;padding:12px;background:var(--panel);">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
              <div style="display:flex;align-items:center;gap:8px;">
                <span style="width:8px;height:8px;border-radius:50%;background:${s.connected ? '#4ade80' : '#f87171'};display:inline-block;"></span>
                <span style="font-size:13px;font-weight:600;color:var(--text);">${s.name}</span>
                <span style="font-size:11px;color:var(--text-dim);">${s.command} ${(s.args||[]).join(' ')}</span>
              </div>
              <div style="display:flex;gap:4px;">
                ${s.connected
                  ? `<button onclick="mcpDisconnect('${s.name}')" class="icon-btn" style="width:auto;padding:4px 10px;font-size:11px;">Disconnect</button>`
                  : `<button onclick="mcpConnect('${s.name}')" class="icon-btn" style="width:auto;padding:4px 10px;font-size:11px;">Connect</button>`
                }
                <button onclick="mcpRemove('${s.name}')" class="icon-btn" style="width:auto;padding:4px 10px;font-size:11px;color:#f87171;">Remove</button>
              </div>
            </div>
            <div style="margin-top:6px;display:flex;gap:4px;align-items:center;">
              <input type="password" id="mcpEnv_${s.name}" placeholder="GITHUB_PERSONAL_ACCESS_TOKEN" value="${(s.env && Object.values(s.env)[0]) || ''}" style="flex:1;padding:4px 8px;font-size:11px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);" />
              <button onclick="mcpUpdateEnv('${s.name}')" class="icon-btn" style="width:auto;padding:4px 10px;font-size:11px;">Save Env</button>
            </div>
            ${s.error ? `<p style="font-size:11px;color:#f87171;margin:4px 0 0 0;">⚠️ ${s.error}</p>` : ''}
            ${s.tools && s.tools.length > 0 ? `
              <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);">
                <p style="font-size:11px;color:var(--text-dim);margin:0 0 4px 0;">Tools (${s.tools.length}):</p>
                <div style="display:flex;flex-wrap:wrap;gap:4px;">
                  ${s.tools.map(t => `<span style="font-size:10px;padding:2px 8px;border-radius:6px;background:var(--bg);border:1px solid var(--border);color:var(--text);" title="${t.description || ''}">${t.name}</span>`).join('')}
                </div>
              </div>
            ` : ''}
          </div>
        `).join('');
        statusEl.textContent = `${servers.length} server(s) configured, ${servers.filter(s=>s.connected).length} connected`;
      } catch (e) {
        statusEl.textContent = "Failed to load MCP servers: " + e.message;
      }
    }

    async function mcpAddServer() {
      const name = document.getElementById("mcpName").value.trim();
      const command = document.getElementById("mcpCommand").value.trim();
      const argsRaw = document.getElementById("mcpArgs").value.trim();
      const envRaw = document.getElementById("mcpEnv").value.trim();
      const statusEl = document.getElementById("mcpStatus");

      if (!name || !command) {
        statusEl.textContent = "❌ Name and command are required.";
        return;
      }

      const args = argsRaw ? argsRaw.split(',').map(a => a.trim()).filter(Boolean) : [];
      let env = {};
      if (envRaw) {
        try { env = JSON.parse(envRaw); } catch (e) {
          statusEl.textContent = "❌ Invalid env JSON.";
          return;
        }
      }

      try {
        const res = await fetch("/api/settings/mcp", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, command, args, env}),
        });
        const data = await res.json();
        if (res.ok) {
          statusEl.textContent = `✅ Server '${name}' added.`;
          document.getElementById("mcpName").value = "";
          document.getElementById("mcpCommand").value = "";
          document.getElementById("mcpArgs").value = "";
          document.getElementById("mcpEnv").value = "";
          loadMcpServers();
        } else {
          statusEl.textContent = "❌ " + (data.detail || "Failed to add server");
        }
      } catch (e) {
        statusEl.textContent = "❌ " + e.message;
      }
    }

    async function mcpConnect(name) {
      const statusEl = document.getElementById("mcpStatus");
      statusEl.textContent = `Connecting to '${name}'…`;
      try {
        const res = await fetch(`/api/settings/mcp/${name}/connect`, {method: "POST"});
        const data = await res.json();
        if (data.connected) {
          statusEl.textContent = `✅ '${name}' connected — ${data.tools.length} tools discovered.`;
        } else {
          statusEl.textContent = `❌ '${name}' failed: ${data.error || 'unknown error'}`;
        }
        loadMcpServers();
      } catch (e) {
        statusEl.textContent = "❌ " + e.message;
      }
    }

    async function mcpDisconnect(name) {
      try {
        await fetch(`/api/settings/mcp/${name}/disconnect`, {method: "POST"});
        loadMcpServers();
      } catch (e) {
        document.getElementById("mcpStatus").textContent = "❌ " + e.message;
      }
    }

    async function mcpRemove(name) {
      if (!confirm(`Remove MCP server '${name}'?`)) return;
      try {
        const res = await fetch(`/api/settings/mcp/${name}`, {method: "DELETE"});
        if (res.ok) {
          document.getElementById("mcpStatus").textContent = `Server '${name}' removed.`;
          loadMcpServers();
        }
      } catch (e) {
        document.getElementById("mcpStatus").textContent = "❌ " + e.message;
      }
    }

    async function mcpUpdateEnv(name) {
      const input = document.getElementById(`mcpEnv_${name}`);
      const statusEl = document.getElementById("mcpStatus");
      if (!input || !statusEl) return;
      const val = input.value.trim();
      try {
        const res = await fetch(`/api/settings/mcp/${name}`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({env: {GITHUB_PERSONAL_ACCESS_TOKEN: val}}),
        });
        if (res.ok) {
          statusEl.textContent = `✅ Env updated for '${name}'. Reconnect to apply.`;
        } else {
          const err = await res.json().catch(() => ({detail: res.statusText}));
          statusEl.textContent = "❌ " + (err.detail || "Failed to update env");
        }
      } catch (e) {
        statusEl.textContent = "❌ " + e.message;
      }
    }

    // Wire up the Add button
    const mcpAddBtn = document.getElementById("mcpAddBtn");
    if (mcpAddBtn) mcpAddBtn.addEventListener("click", mcpAddServer);


    // ---------- Icon Style ----------
    const ICON_STYLE_KEY = "sable_icon_style";
    const iconStyleSelect = document.getElementById("iconStyleSelect");
    function applyIconStyle(style) {
      document.documentElement.setAttribute("data-icon-style", style);
      if (style === "lucide" && window.lucide) {
        lucide.createIcons();
      }
    }

    iconStyleSelect.addEventListener("change", () => {
      applyIconStyle(iconStyleSelect.value);
      try { localStorage.setItem(ICON_STYLE_KEY, iconStyleSelect.value); } catch (e) {}
    });

    (function loadIconStyle() {
      let saved = null;
      try { saved = localStorage.getItem(ICON_STYLE_KEY); } catch (e) {}
      const style = saved || "lucide";
      iconStyleSelect.value = style;
      applyIconStyle(style);
    })();



    // ---------- Theme ----------

    const THEME_KEY = "sable_theme";
    const themePicker = document.getElementById("themePicker");

    function applyTheme(name) {
      if (name && name !== "sable") {
        document.documentElement.setAttribute("data-theme", name);
      } else {
        document.documentElement.removeAttribute("data-theme");
      }
      updateFavicon();
    }

    function updateFavicon() {
      const cs = getComputedStyle(document.documentElement);
      const accent = (cs.getPropertyValue("--accent-text") || "#e8cd97").trim();
      const svg = `<svg viewBox="0 0 32 32" width="32" height="32" xmlns="http://www.w3.org/2000/svg">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0%" stop-color="${accent}"/>
<stop offset="100%" stop-color="${accent}" stop-opacity="0.7"/>
</linearGradient></defs>
<polygon fill="#181825" stroke="url(#g)" stroke-width="3" stroke-linejoin="round" points="16,2 26,8 26,24 16,30 6,24 6,8"/>
<circle cx="16" cy="2" r="3" fill="${accent}"/>
<circle cx="26" cy="8" r="3" fill="${accent}"/>
<circle cx="26" cy="24" r="3" fill="${accent}"/>
<circle cx="16" cy="30" r="3" fill="${accent}"/>
<circle cx="6" cy="24" r="3" fill="${accent}"/>
<circle cx="6" cy="8" r="3" fill="${accent}"/>
<circle cx="16" cy="16" r="4" fill="${accent}"/>
</svg>`;
      let link = document.querySelector("link[rel='icon']");
      if (!link) { link = document.createElement("link"); link.rel = "icon"; document.head.appendChild(link); }
      link.type = "image/svg+xml";
      link.href = "data:image/svg+xml," + encodeURIComponent(svg);

      // --- Sidebar / login logo ---
      const logoSvg = `<svg viewBox="0 0 64 64" width="64" height="64" xmlns="http://www.w3.org/2000/svg">
<defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0%" stop-color="${accent}"/>
<stop offset="100%" stop-color="${accent}" stop-opacity="0.7"/>
</linearGradient>
<filter id="glow"><feGaussianBlur stdDeviation="1.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<polygon fill="#181825" stroke="url(#lg)" stroke-width="4" stroke-linejoin="round" points="32,4 52,16 52,48 32,60 12,48 12,16" filter="url(#glow)"/>
<circle cx="32" cy="4" r="4" fill="${accent}"/>
<circle cx="52" cy="16" r="4" fill="${accent}"/>
<circle cx="52" cy="48" r="4" fill="${accent}"/>
<circle cx="32" cy="60" r="4" fill="${accent}"/>
<circle cx="12" cy="48" r="4" fill="${accent}"/>
<circle cx="12" cy="16" r="4" fill="${accent}"/>
<circle cx="32" cy="32" r="6" fill="${accent}"/>
</svg>`;
      document.querySelectorAll('img[src*="sable_icon"]').forEach(img => {
        img.src = "data:image/svg+xml," + encodeURIComponent(logoSvg);
      });
    }

    themePicker.addEventListener("click", (e) => {
      const btn = e.target.closest(".theme-swatch");
      if (!btn) return;
      const name = btn.dataset.theme;
      applyTheme(name);
      themePicker.querySelectorAll(".theme-swatch").forEach((b) => b.classList.toggle("active", b === btn));
      try { localStorage.setItem(THEME_KEY, name); } catch (err) {}
    });

    (function loadTheme() {
      let saved = null;
      try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
      if (saved) {
        applyTheme(saved);
        const match = themePicker.querySelector('.theme-swatch[data-theme="' + saved + '"]');
        if (match) {
          themePicker.querySelectorAll(".theme-swatch").forEach((b) => b.classList.toggle("active", b === match));
        }
      } else {
        updateFavicon();
      }
    })();

    // ---------- Mode Switcher (API / Scraper) ----------
    const modeApiBtn = document.getElementById('modeApi');
    const modeScraperBtn = document.getElementById('modeScraper');
    const scraperEngineWrap = document.getElementById('scraperEngineWrap');
    const scraperEngineSelect = document.getElementById('scraperEngineSelect');
    const scraperStatusEl = document.getElementById('scraperStatus');
    let scraperMode = false;
    let scraperEngines = [];

    function setScraperStatus(msg, type) {
      scraperStatusEl.textContent = msg;
      scraperStatusEl.className = 'scraper-status visible ' + (type || '');
    }

    function updateModeUI() {
      modeApiBtn.classList.toggle('active', !scraperMode);
      modeScraperBtn.classList.toggle('active', scraperMode);
      scraperEngineWrap.classList.toggle('visible', scraperMode);
      if (!scraperMode) {
        scraperStatusEl.className = 'scraper-status';
      }
    }

    async function loadScraperEngines() {
      try {
        const res = await fetch('/api/settings/scraper/engines');
        if (res.ok) {
          const data = await res.json();
          scraperEngines = data.engines || [];
          scraperEngineSelect.innerHTML = '';
          for (const eng of scraperEngines) {
            const opt = document.createElement('option');
            opt.value = eng.id;
            opt.textContent = eng.label;
            scraperEngineSelect.appendChild(opt);
          }
        }
      } catch {}
    }

    async function loadScraperSettings() {
      try {
        const res = await fetch('/api/settings/scraper');
        if (res.ok) {
          const data = await res.json();
          scraperMode = !!data.enabled;
          if (data.engine_type && scraperEngineSelect) {
            scraperEngineSelect.value = data.engine_type;
          }
          updateModeUI();
          if (scraperMode) {
            setScraperStatus('● Browser connected — ' + (data.engine_label || 'Scraper'), 'ok');
          }
        }
      } catch {}
    }

    async function setScraperMode(enabled) {
      scraperMode = enabled;
      updateModeUI();
      if (enabled) {
        setScraperStatus('Launching browser…', '');
      }
      try {
        const payload = { enabled, headless: false };
        if (enabled && scraperEngineSelect.value) {
          payload.engine_type = scraperEngineSelect.value;
        }
        const res = await fetch('/api/settings/scraper', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          const data = await res.json();
          scraperMode = !!data.enabled;
          updateModeUI();
          if (data.enabled) {
            const pl = data.prelaunch;
            if (pl && pl.status === 'ok') {
              setScraperStatus('● Browser launched — ' + (data.engine_label || 'Scraper'), 'ok');
            } else if (pl && pl.status === 'error') {
              setScraperStatus('✗ ' + pl.message, 'err');
            } else {
              setScraperStatus('● Scraper enabled — ' + (data.engine_label || ''), 'ok');
            }
            showToast('Scraper mode ON — headed browser', 'success');
            await loadModels();
            // Navigate to latest scraper chat
            await loadChats('scraper');
            if (chatList.length > 0) await selectChat(chatList[0].id);
          } else {
            setScraperStatus('', '');
            showToast('Scraper mode OFF — API chat', 'success');
            await loadModels();
            // Navigate to latest API chat
            await loadChats('api');
            if (chatList.length > 0) await selectChat(chatList[0].id);
          }
        } else {
          const err = await res.json().catch(() => ({}));
          showToast(err.detail || 'Could not update scraper mode', 'error');
          scraperMode = !enabled;
          updateModeUI();
        }
      } catch (e) {
        showToast('Scraper mode error: ' + e.message, 'error');
        scraperMode = !enabled;
        updateModeUI();
      }
    }

    modeApiBtn.addEventListener('click', () => {
      if (!scraperMode) return;
      setScraperMode(false);
    });

    modeScraperBtn.addEventListener('click', () => {
      if (scraperMode) return;
      setScraperMode(true);
    });

    scraperEngineSelect.addEventListener('change', async () => {
      if (!scraperMode) return;
      try {
        const res = await fetch('/api/settings/scraper', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ engine_type: scraperEngineSelect.value, enabled: true, headless: false })
        });
        if (res.ok) {
          const data = await res.json();
          const pl = data.prelaunch;
          if (pl && pl.status === 'ok') {
            setScraperStatus('● Browser relaunched — ' + (data.engine_label || ''), 'ok');
          } else if (pl && pl.status === 'error') {
            setScraperStatus('✗ ' + pl.message, 'err');
          }
          showToast('Engine switched to ' + (data.engine_label || scraperEngineSelect.value), 'success');
          await loadModels();
        }
      } catch {}
    });

    loadScraperEngines().then(async () => {
      await loadScraperSettings();
      // Refresh model list now that scraper state is known — if scraper is
      // active with DeepSeek the dropdown must show DS model types, not Qwen.
      await loadModels();
    });

  // ── Browser Session Monitor ──────────────────────────────────
  async function loadBrowserSession() {
    const card = document.getElementById('browserSessionCard');
    if (!card) return;
    try {
      const res = await fetch('/api/scraper/sessions');
      const d = await res.json();
      if (!d.active) {
        card.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">No active browser session.</p>';
        return;
      }
      const alive = d.alive;
      const dot = alive ? '\u{1F7E2}' : '\u{1F534}';
      const statusTxt = alive ? 'Running' : 'Dead / Zombie';
      card.innerHTML =
        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">' +
          '<span style="font-size:13px;font-weight:600;color:var(--text);">' + dot + ' ' + statusTxt + '</span>' +
          '<button onclick="killBrowserSession()" style="background:var(--danger);color:#fff;border:none;border-radius:6px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600;">\u2715 Kill</button>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:12px;color:var(--text-dim);">' +
          '<span>Engine</span><span style="color:var(--text);">' + (d.engine_type || '\u2014') + '</span>' +
          '<span>Chat ID</span><span style="color:var(--text);">' + (d.chat_id || '\u2014') + '</span>' +
          '<span>PID</span><span style="color:var(--text);">' + (d.chrome_pid || '\u2014') + '</span>' +
          '<span>CDP Port</span><span style="color:var(--text);">' + (d.cdp_port || '\u2014') + '</span>' +
          '<span>Headless</span><span style="color:var(--text);">' + (d.headless ? 'Yes' : 'No') + '</span>' +
          '<span>URL</span><span style="color:var(--text);word-break:break-all;font-size:11px;">' + (d.page_url || '\u2014') + '</span>' +
        '</div>';
    } catch {
      card.innerHTML = '<p class="muted" style="font-size:12px;margin:0;color:var(--danger);">Failed to fetch session info.</p>';
    }
  }

  async function killBrowserSession() {
    try {
      const res = await fetch('/api/scraper/sessions/kill', { method: 'POST' });
      const d = await res.json();
      showToast(d.killed_pid ? 'Killed PID ' + d.killed_pid : 'Session reset (no PID found)', 'success');
    } catch {
      showToast('Failed to kill session', 'error');
    }
    await loadBrowserSession();
  }

  document.getElementById('refreshSessionBtn')?.addEventListener('click', loadBrowserSession);
  loadBrowserSession();
  setInterval(loadBrowserSession, 15000);
  // ── /Browser Session Monitor ─────────────────────────────────

  // ── Browser Profile Restore ─────────────────────────────────
  async function loadBrowserProfiles() {
    const container = document.getElementById('browserProfileCards');
    if (!container) return;
    try {
      const res = await fetch('/api/settings/accounts/backups');
      const d = await res.json();
      const accounts = d.accounts || [];
      if (!accounts.length) {
        container.innerHTML = '<p class="muted" style="font-size:12px;margin:0;">No account profiles found.</p>';
        return;
      }
      let html = '';
      for (const acc of accounts) {
        const bakDot = acc.has_backup ? '\u{1F7E2}' : '\u{1F534}';
        html +=
          '<div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px;">' +
              '<span style="font-size:13px;font-weight:600;color:var(--text);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escHtml(acc.name) + (acc.email ? ' <span style="font-weight:400;font-size:11px;color:var(--text-dim);">' + escHtml(acc.email) + '</span>' : '') + '</span>' +
              '<span style="display:flex;gap:6px;flex-shrink:0;">' +
                '<button data-profile="' + escAttr(acc.name) + '" class="accBackupBtn" style="background:var(--panel);color:var(--accent);border:1px solid var(--accent);border-radius:6px;padding:5px 12px;font-size:11px;cursor:pointer;font-weight:600;">Backup</button>' +
                '<button data-profile="' + escAttr(acc.name) + '" class="accRestoreBtn" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:11px;cursor:pointer;font-weight:600;' + (acc.has_backup ? '' : 'opacity:0.4;pointer-events:none;') + '">Restore</button>' +
              '</span>' +
            '</div>' +
            '<div style="display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:12px;color:var(--text-dim);">' +
              '<span>Size</span><span style="color:var(--text);">' + acc.size_mb + ' MB</span>' +
              '<span>Backup</span><span style="color:var(--text);">' + bakDot + ' ' + (acc.has_backup ? acc.backup_size_mb + ' MB' : 'None') + '</span>' +
            '</div>' +
          '</div>';
      }
      container.innerHTML = html;
      container.querySelectorAll('.accRestoreBtn').forEach(btn => {
        btn.addEventListener('click', () => restoreAccountProfile(btn.dataset.profile, btn));
      });
      container.querySelectorAll('.accBackupBtn').forEach(btn => {
        btn.addEventListener('click', () => backupAccountProfile(btn.dataset.profile, btn));
      });
    } catch {
      container.innerHTML = '<p class="muted" style="font-size:12px;margin:0;color:var(--danger);">Failed to load profiles.</p>';
    }
  }

  async function restoreAccountProfile(profile, btn) {
    if (!confirm('Restore ' + profile + ' from backup?\n\nThis DELETES the current profile and replaces it with the .bak snapshot.')) return;
    btn.disabled = true;
    btn.textContent = 'Restoring…';
    try {
      const res = await fetch('/api/settings/accounts/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile })
      });
      const d = await res.json();
      if (res.ok) showToast(profile + ' restored', 'success');
      else showToast('Restore failed: ' + (d.detail || 'Unknown'), 'error');
    } catch { showToast('Restore failed — network error', 'error'); }
    btn.disabled = false;
    btn.textContent = 'Restore';
    await loadBrowserProfiles();
  }

  async function backupAccountProfile(profile, btn) {
    btn.disabled = true;
    btn.textContent = 'Backing up…';
    try {
      const res = await fetch('/api/settings/accounts/backup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile })
      });
      const d = await res.json();
      if (res.ok) showToast(profile + ' backed up (' + d.size_mb + ' MB)', 'success');
      else showToast('Backup failed: ' + (d.detail || 'Unknown'), 'error');
    } catch { showToast('Backup failed — network error', 'error'); }
    btn.disabled = false;
    btn.textContent = 'Backup';
    await loadBrowserProfiles();
  }

  document.getElementById('refreshProfilesBtn')?.addEventListener('click', loadBrowserProfiles);

  document.getElementById('backupAllBtn')?.addEventListener('click', async () => {
    const btn = document.getElementById('backupAllBtn');
    btn.disabled = true; btn.textContent = '⬆ Backing up…';
    try {
      const res = await fetch('/api/settings/accounts/backups');
      const d = await res.json();
      const accounts = d.accounts || [];
      let ok = 0, fail = 0;
      for (const acc of accounts) {
        try {
          const r = await fetch('/api/settings/accounts/backup', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile: acc.name })
          });
          if (r.ok) ok++; else fail++;
        } catch { fail++; }
      }
      showToast('Backup All: ' + ok + ' done' + (fail ? ', ' + fail + ' failed' : ''), fail ? 'error' : 'success');
    } catch { showToast('Backup All failed — network error', 'error'); }
    btn.disabled = false; btn.textContent = '⬆ Backup All';
    await loadBrowserProfiles();
  });

  document.getElementById('restoreAllBtn')?.addEventListener('click', async () => {
    if (!confirm('Restore ALL profiles from .bak snapshots?\nThis DELETES current data and replaces with backups.')) return;
    const btn = document.getElementById('restoreAllBtn');
    btn.disabled = true; btn.textContent = '⬇ Restoring…';
    try {
      const res = await fetch('/api/settings/accounts/backups');
      const d = await res.json();
      const accounts = (d.accounts || []).filter(a => a.has_backup);
      if (!accounts.length) { showToast('No backups found to restore', 'error'); btn.disabled = false; btn.textContent = '⬇ Restore All'; return; }
      let ok = 0, fail = 0;
      for (const acc of accounts) {
        try {
          const r = await fetch('/api/settings/accounts/restore', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile: acc.name })
          });
          if (r.ok) ok++; else fail++;
        } catch { fail++; }
      }
      showToast('Restore All: ' + ok + ' done' + (fail ? ', ' + fail + ' failed' : ''), fail ? 'error' : 'success');
    } catch { showToast('Restore All failed — network error', 'error'); }
    btn.disabled = false; btn.textContent = '⬇ Restore All';
    await loadBrowserProfiles();
  });

  loadBrowserProfiles();
  // ── /Browser Profile Backup ────────────────────────────────


  // ── Context Menu ──────────────────────────────────────────
  const ctxMenu = document.getElementById('contextMenu');

  document.addEventListener('contextmenu', (e) => {
    // Only on main area / sidebar, not on inputs or textareas
    if (e.target.closest('textarea, input, select, .ctx-menu, #fsOverlay')) return;
    e.preventDefault();

    const x = Math.min(e.clientX, window.innerWidth - ctxMenu.offsetWidth - 12);
    const y = Math.min(e.clientY, window.innerHeight - ctxMenu.offsetHeight - 12);
    ctxMenu.style.left = x + 'px';
    ctxMenu.style.top = y + 'px';
    ctxMenu.classList.add('open');
  });

  function closeCtx() { ctxMenu.classList.remove('open'); }

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.ctx-menu')) closeCtx();
  });

  // Code block copy button (delegated)
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.code-copy-btn');
    if (!btn) return;
    const block = btn.closest('.code-block');
    const codeEl = block?.querySelector('pre code');
    if (!codeEl) return;
    navigator.clipboard.writeText(codeEl.textContent).then(() => {
      btn.classList.add('copied');
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
      }, 1500);
    });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeCtx();
  });
  window.addEventListener('resize', closeCtx);
  window.addEventListener('scroll', closeCtx, true);

  ctxMenu.addEventListener('click', async (e) => {
    const item = e.target.closest('.ctx-item');
    if (!item) return;
    closeCtx();
    const action = item.dataset.action;

    if (action === 'new-chat') {
      document.getElementById('newChat')?.click();
    } else if (action === 'settings') {
      document.getElementById('settingsBtn')?.click();
    } else if (action === 'context-pass') {
      if (!activeChatId) { showToast('No active chat to pass context from', 'error'); return; }
      showToast('Summarizing context…', 'info');
      try {
        const res = await fetch('/api/context/pass', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: activeChatId, model: selectedModel }),
        });
        const d = await res.json();
        if (!res.ok || d.error) { showToast(d.error || 'Context pass failed', 'error'); return; }
        const summary = d.summary;
        if (!summary) { showToast('Empty summary returned', 'error'); return; }
        // Create new chat and auto-send the summary as first message
        const created = await createChat();
        if (!created) { showToast('Failed to create new chat', 'error'); return; }
        inputEl.value = summary;
        autoResize();
        await sendMessage();
        showToast('Context passed to new chat', 'success');
      } catch (e) { showToast('Context pass error: ' + e.message, 'error'); }
    } else if (action === 'sync-context') {
      showToast('Syncing context…', 'info');
      try {
        const res = await fetch('/api/sync-context', { method: 'POST' });
        const d = await res.json();
        showToast(res.ok ? (d.message || 'Context synced') : (d.error || 'Sync failed'), res.ok ? 'success' : 'error');
      } catch { showToast('Sync failed', 'error'); }
    } else if (action === 'refresh-deepseek') {
      showToast('Refreshing DeepSeek token…', 'info');
      try {
        const res = await fetch('/api/settings/deepseek/refresh-token', { method: 'POST' });
        const d = await res.json();
        showToast(res.ok ? (d.message || 'Token refreshed') : (d.error || 'Refresh failed'), res.ok ? 'success' : 'error');
      } catch { showToast('Refresh failed', 'error'); }
    } else if (action === 'refresh-waf') {
      showToast('Refreshing WAF token…', 'info');
      try {
        const res = await fetch('/api/settings/browser/refresh-waf', { method: 'POST' });
        const d = await res.json();
        showToast(res.ok ? (d.message || 'WAF token refreshed') : (d.detail || 'Refresh failed'), res.ok ? 'success' : 'error');
      } catch { showToast('Refresh failed', 'error'); }
    } else if (action === 'clear-browser-cache') {
      if (!confirm('Strip all browser profile caches? This keeps session data but removes cache/junk.')) return;
      showToast('Stripping browser profiles…', 'info');
      try {
        const res = await fetch('/api/settings/browser/strip-profiles', { method: 'POST' });
        const d = await res.json();
        showToast(res.ok ? 'Profiles stripped' : (d.error || 'Strip failed'), res.ok ? 'success' : 'error');
      } catch { showToast('Strip failed', 'error'); }
    }
  });
  // ── /Context Menu ─────────────────────────────────────────



