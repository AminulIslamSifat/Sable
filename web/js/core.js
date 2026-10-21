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

    // Phase elements
    const phaseAccount   = document.getElementById("phaseAccount");
    const phaseSetup     = document.getElementById("phaseSetup");

    // Setup phase 2 elements
    const setupTabApiKey   = document.getElementById("setupTabApiKey");
    const setupTabBrowser  = document.getElementById("setupTabBrowser");
    const setupPanelApiKey = document.getElementById("setupPanelApiKey");
    const setupPanelBrowser= document.getElementById("setupPanelBrowser");
    const setupProviderSelect = document.getElementById("setupProviderSelect");
    const setupApiKeyInput = document.getElementById("setupApiKeyInput");
    const setupAddKeyBtn   = document.getElementById("setupAddKeyBtn");
    const setupApiKeyStatus= document.getElementById("setupApiKeyStatus");
    const setupBrowserSelect = document.getElementById("setupBrowserSelect");
    const setupProviderChecks = document.getElementById("setupProviderChecks");
    const setupBrowserBtn  = document.getElementById("setupBrowserBtn");
    const setupBrowserStatus = document.getElementById("setupBrowserStatus");
    const setupSkipBtn     = document.getElementById("setupSkipBtn");

    const getToken = () => localStorage.getItem(TOKEN_KEY);
    const clearToken = () => localStorage.removeItem(TOKEN_KEY);

    // Provider API base map for setup key flow
    const _setupProviderMeta = {
      gemini:     { apiBase: "/api/settings/gemini",     placeholder: "Paste API key (AIza…)" },
      groq:       { apiBase: "/api/settings/groq",       placeholder: "Paste API key (gsk_…)" },
      mistral:    { apiBase: "/api/settings/mistral",    placeholder: "Paste API key (key: …)" },
      openai:     { apiBase: "/api/settings/openai",     placeholder: "Paste API key (sk-…)" },
      deepseek:   { apiBase: "/api/settings/deepseek",   placeholder: "Paste API key (sk-…)" },
      puter:      { apiBase: "/api/settings/puter",      placeholder: "Paste Puter token (eyJ…)" },
      cloudflare: { apiBase: "/api/settings/cloudflare", placeholder: "Paste API token (cfat_…)", singleToken: true },
    };

    // Hide all phases, then show one
    function showPhase(phase) {
      phaseAccount.classList.add("hidden");
      phaseSetup.classList.add("hidden");
      if (phase) phase.classList.remove("hidden");
    }

    // Inject the bearer token into every API request; bounce to login on 401.
    let _authBounced = false;
    let _authReady = false; // Set only AFTER auth flow fully completes
    const _origFetch = window.fetch.bind(window);
    window.fetch = async (url, init = {}) => {
      const token = getToken();
      if (token) {
        init.headers = Object.assign({}, init.headers, { Authorization: "Bearer " + token });
      }
      const res = await _origFetch(url, init);
      // Only bounce on 401 if:
      // 1. We actually sent a token (not just missing during init)
      // 2. Auth flow has fully completed (_authReady)
      // 3. Haven't already bounced this session
      // 4. Not an auth/setup endpoint (those handle their own errors)
      if (res.status === 401 && token && _authReady && !_authBounced && typeof url === "string"
          && !url.includes("/api/login") && !url.includes("/api/setup/") && !url.includes("/api/account/")) {
        _authBounced = true;
        clearToken();
        // Check if setup is needed before showing login screen
        try {
          const sRes = await _origFetch("/api/setup/status");
          if (sRes.ok) {
            const sData = await sRes.json();
            if (sData.needs_password) {
              showPhase(phaseAccount);
              return res;
            }
          }
        } catch {}
        showPhase(phaseAccount);
      }
      return res;
    };


    function ensureAuth() {
      if (getToken()) {
        // Validate cached token against server before trusting it
        return (async () => {
          try {
            const checkRes = await _origFetch("/api/login", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ token: getToken() }),
            });
            if (checkRes.ok) {
              _authReady = true;
              showPhase(null);
              return;
            }
          } catch {}
          // Token invalid — clear it and fall through to setup/login flow
          clearToken();
        })().then(() => {
          if (getToken()) return; // validated above, already resolved
          return _runSetupOrLogin();
        });
      }
      return _runSetupOrLogin();
    }

    async function _runSetupOrLogin() {
      // Did the account gate run during THIS page load? A token is not proof —
      // it survives from a previous session — so track the gate explicitly.
      // PHASE 1 runs it when the server says needs_password; PHASE 3 runs it
      // otherwise; the fallback at the bottom runs it when status is unknown.
      let _gateRan = false;
      // Check if first-run setup is needed
      try {
          const statusRes = await _origFetch("/api/setup/status");
          if (statusRes.ok) {
            let status = await statusRes.json();

            // === PHASE 1: Account sign in / sign up ===
            if (status.needs_password) {
              // Do NOT set _authReady here — it causes the fetch interceptor
              // to bounce on every API call fired by other scripts during setup,
              // creating an infinite reload loop.
              showPhase(phaseAccount);
              if (window.AccountAuth) {
                await window.AccountAuth.run();
                _gateRan = true;
                try {
                  const s2 = await _origFetch("/api/setup/status");
                  if (s2.ok) status = await s2.json();
                } catch {}
              } else {
                // account-auth.js hasn't loaded yet — wait for it
                await new Promise((resolve) => {
                  const check = setInterval(() => {
                    if (window.AccountAuth) {
                      clearInterval(check);
                      resolve();
                    }
                  }, 50);
                });
                await window.AccountAuth.run();
                _gateRan = true;
                try {
                  const s2 = await _origFetch("/api/setup/status");
                  if (s2.ok) status = await s2.json();
                } catch {}
              }
            }

            // === PHASE 2: Setup — API Key or Browser Login ===
            if (status.needs_setup) {
              showPhase(phaseSetup);

              // Load available browsers for the dropdown
              try {
                const bRes = await _origFetch("/api/setup/available-browsers");
                if (bRes.ok) {
                  const bData = await bRes.json();
                  const browsers = bData.browsers || [];
                  if (browsers.length && setupBrowserSelect) {
                    setupBrowserSelect.innerHTML = browsers.map(b =>
                      `<option value="${b.path.replace(/"/g, '&quot;')}">${b.name} (${b.type})</option>`
                    ).join("");
                  }
                }
              } catch {}

              // Update placeholder when provider changes
              if (setupProviderSelect) {
                setupProviderSelect.addEventListener("change", () => {
                  const meta = _setupProviderMeta[setupProviderSelect.value];
                  if (meta && setupApiKeyInput) setupApiKeyInput.placeholder = meta.placeholder;
                });
              }

              // Tab switching
              function switchSetupTab(tab) {
                if (tab === "apikey") {
                  setupTabApiKey.style.background = "var(--accent)";
                  setupTabApiKey.style.color = "#fff";
                  setupTabBrowser.style.background = "transparent";
                  setupTabBrowser.style.color = "var(--text)";
                  setupPanelApiKey.style.display = "";
                  setupPanelBrowser.style.display = "none";
                } else {
                  setupTabBrowser.style.background = "var(--accent)";
                  setupTabBrowser.style.color = "#fff";
                  setupTabApiKey.style.background = "transparent";
                  setupTabApiKey.style.color = "var(--text)";
                  setupPanelBrowser.style.display = "";
                  setupPanelApiKey.style.display = "none";
                }
              }
              if (setupTabApiKey) setupTabApiKey.addEventListener("click", () => switchSetupTab("apikey"));
              if (setupTabBrowser) setupTabBrowser.addEventListener("click", () => switchSetupTab("browser"));

              await new Promise((resolve) => {
                // --- API Key flow ---
                if (setupAddKeyBtn) {
                  setupAddKeyBtn.addEventListener("click", async () => {
                    const provider = setupProviderSelect?.value;
                    const key = setupApiKeyInput?.value?.trim();
                    if (!key) {
                      if (setupApiKeyStatus) { setupApiKeyStatus.textContent = "⚠️ Paste an API key first"; setupApiKeyStatus.style.color = "#ff6b6b"; }
                      return;
                    }
                    if (!provider) {
                      if (setupApiKeyStatus) { setupApiKeyStatus.textContent = "⚠️ Select a provider first"; setupApiKeyStatus.style.color = "#ff6b6b"; }
                      return;
                    }
                    setupAddKeyBtn.disabled = true;
                    setupAddKeyBtn.textContent = "Saving & fetching models…";
                    try {
                      // Use the unified setup endpoint that saves key + auto-registers models
                      const res = await _origFetch("/api/setup/api-key", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ provider, api_key: key }),
                      });
                      if (res.ok) {
                        const data = await res.json();
                        const count = data.models_registered || 0;
                        const msg = count > 0
                          ? `✅ Key saved! Auto-registered ${count} model${count > 1 ? 's' : ''}.`
                          : "✅ Key saved! Models are ready.";
                        if (setupApiKeyStatus) { setupApiKeyStatus.textContent = msg; setupApiKeyStatus.style.color = "#4ade80"; }
                        setupApiKeyInput.value = "";
                        // Reload models so the dropdown updates immediately
                        if (window.loadModels) await window.loadModels();
                        setTimeout(resolve, 2000);
                      } else {
                        const err = await res.json().catch(() => ({}));
                        if (setupApiKeyStatus) { setupApiKeyStatus.textContent = "❌ " + (err.detail || "Failed to save key"); setupApiKeyStatus.style.color = "#ff6b6b"; }
                      }
                    } catch (e) {
                      if (setupApiKeyStatus) { setupApiKeyStatus.textContent = "❌ Connection error"; setupApiKeyStatus.style.color = "#ff6b6b"; }
                    } finally {
                      setupAddKeyBtn.disabled = false;
                      setupAddKeyBtn.textContent = "Add API Key";
                    }
                  });
                }

                // --- Browser login flow ---
                if (setupBrowserBtn) {
                  setupBrowserBtn.addEventListener("click", async () => {
                    const providers = setupProviderChecks
                      ? Array.from(setupProviderChecks.querySelectorAll("input[type=checkbox]:checked")).map(c => c.value)
                      : [];
                    if (!providers.length) {
                      if (setupBrowserStatus) { setupBrowserStatus.textContent = "⚠️ Select at least one provider"; setupBrowserStatus.style.color = "#ff6b6b"; }
                      return;
                    }
                    setupBrowserBtn.disabled = true;
                    setupBrowserBtn.textContent = "Opening…";
                    if (setupBrowserStatus) { setupBrowserStatus.textContent = `Launching browser (${providers.length} tab${providers.length > 1 ? "s" : ""})…`; setupBrowserStatus.style.color = "var(--text-dim)"; }
                    try {
                      const browserPath = setupBrowserSelect ? setupBrowserSelect.value : "";
                      const res = await _origFetch("/api/setup/browser-login", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ browser_path: browserPath, providers }),
                      });
                      if (res.ok) {
                        const body = await res.json().catch(() => ({}));
                        const n = body.count || providers.length;
                        if (setupBrowserStatus) { setupBrowserStatus.textContent = `✅ Browser opened with ${n} tab${n > 1 ? "s" : ""}! Sign in on each, then close it to continue.`; setupBrowserStatus.style.color = "#4ade80"; }
                        setTimeout(resolve, 3000);
                      } else {
                        const body = await res.json().catch(() => ({}));
                        if (setupBrowserStatus) { setupBrowserStatus.textContent = "❌ " + (body.detail || "Failed to open browser").split("\n")[0]; setupBrowserStatus.style.color = "#ff6b6b"; }
                      }
                    } catch (e) {
                      if (setupBrowserStatus) { setupBrowserStatus.textContent = "❌ Connection error"; setupBrowserStatus.style.color = "#ff6b6b"; }
                    } finally {
                      setupBrowserBtn.disabled = false;
                      setupBrowserBtn.textContent = "Open Browser";
                    }
                  });
                }

                // --- Skip button ---
                if (setupSkipBtn) {
                  setupSkipBtn.addEventListener("click", () => resolve(), { once: true });
                }
              });
            }

            // === PHASE 3: Login ===
            // Account (email/password) gate — the only front door.
            // Run it ONLY if PHASE 1 didn't already. PHASE 1 fires whenever the
            // server reports needs_password, and it awaits the very same gate;
            // calling run() again here rebuilt the panel, re-registered every
            // listener and orphaned the first run's promise — so the first
            // Unlock click was swallowed and the button had to be pressed twice.
            if (!_gateRan) {
              showPhase(phaseAccount);
              if (!window.AccountAuth) {
                await new Promise((resolve) => {
                  const _wait = setInterval(() => {
                    if (window.AccountAuth) { clearInterval(_wait); resolve(); }
                  }, 50);
                });
              }
              // Blocks until sign-in succeeds (or the tab is abandoned).
              await window.AccountAuth.run();
              _gateRan = true;
            }
            if (getToken()) {
              _authReady = true;
              showPhase(null);
              // Token acquired during the account gate — let the boot sequence
              // (loadModels + loadChats) continue without a page reload.
            }
            // MUST return here. Without it, control falls through to the
            // "status check failed" fallback below, which re-shows the account
            // phase and calls AccountAuth.run() a SECOND time on the happy path
            // — rebuilding the panel, re-registering every listener and
            // orphaning the first run's promise (the two-click Unlock bug).
            return;
          }
      } catch {
        // Setup status check failed — fall through to normal login
      }

      // === Status check failed (or reported nothing usable) — fall back ===
      // Only reachable when the gate genuinely hasn't run yet.
      if (!_gateRan) {
        _authReady = true;
        showPhase(phaseAccount);
        if (!window.AccountAuth) {
          await new Promise((resolve) => {
            const _wait = setInterval(() => {
              if (window.AccountAuth) { clearInterval(_wait); resolve(); }
            }, 50);
          });
        }
        await window.AccountAuth.run();
        _gateRan = true;
        if (getToken()) { _authReady = true; showPhase(null); }
      }
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

    // ── Adaptive typewriter speed ─────────────────────────────────────
    // Per-model median tok/s (last 30 responses, from /api/config/ui).
    // TW_CHARS is scaled so the reveal matches the model's real speed.
    // Above FLUSH_TPS we skip animation entirely — at that speed the reply
    // lands in under a second and animating it is pure CPU theatre.
    const TW_FLUSH_TPS = 300;      // skip animation above this
    const TW_MAX_CHARS = 60;       // hard ceiling per tick
    const TW_MIN_MS    = 33;       // ~30 renders/sec — the practical CPU ceiling
    let TW_MODEL_SPEEDS = {};      // model id → {median_tps, avg_tps, samples}

    function twSpeedFor(model) {
      const s = TW_MODEL_SPEEDS[model];
      if (!s) return 0;
      return s.median_tps || s.avg_tps || 0;
    }

    // Returns {chars, ms, flush} tuned to the model's measured speed.
    function twTuneFor(model) {
      const s = TW_MODEL_SPEEDS[model];
      const tps = twSpeedFor(model);
      if (!tps) return { chars: TW_CHARS, ms: TW_MS, flush: false };

      // Flush if EITHER the median or the single most recent reading is fast.
      // Median alone is sluggish to react (a stale disk seed outvotes it), but
      // the last reading is direct evidence that *this* model can outrun the
      // animation — and flushing is always safe, it just renders in one pass.
      if (tps >= TW_FLUSH_TPS || (s && s.last_tps >= TW_FLUSH_TPS)) {
        return { chars: 0, ms: TW_MS, flush: true };
      }

      // chars/tick = tok/s × 4 chars/tok × (tick_ms / 1000)
      const ms = Math.max(TW_MIN_MS, TW_MS);
      let chars = Math.round(tps * 4 * (ms / 1000));
      // Respect a low-memory override as the floor
      chars = Math.max(_lowMem ? 12 : 1, Math.min(chars, TW_MAX_CHARS));
      return { chars, ms, flush: false };
    }

    // ── Local ring buffer ─────────────────────────────────────────────
    // The server DB is the durable store; this is the session-local top-up so
    // the meter adapts live without refetching. Seeded once from disk at init,
    // then fed by every completed response. No further network calls.
    const TW_RING = {};        // model → [tps, ...]
    const TW_RING_MAX = 30;

    // Seeded prior weight. Deliberately small: the disk value is a *summary*
    // of past responses, not N independent observations of the model's speed
    // right now. If we seed all 30, a stale median outvotes 15 fresh readings
    // and the meter takes ~15 turns to correct — exactly the DeepSeek problem.
    // At 6, live data takes over after ~7 responses while the disk value still
    // damps the first few noisy readings. Best of both.
    const TW_SEED_WEIGHT = 6;

    function twSeedFromServer(speeds) {
      for (const model in speeds) {
        const s = speeds[model];
        const n = Math.min(s.samples || 0, TW_SEED_WEIGHT);
        const m = s.median_tps || 0;
        if (!n || !m) continue;
        TW_RING[model] = new Array(n).fill(m);
      }
    }

    function _twMedian(arr) {
      const s = arr.slice().sort((a, b) => a - b);
      const mid = s.length >> 1;
      return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
    }

    // Record one live reading and recompute the model's median in place.
    function twRecord(model, tps) {
      if (!model || !isFinite(tps) || tps <= 0) return;
      const r = TW_RING[model] || (TW_RING[model] = []);
      r.push(tps);
      while (r.length > TW_RING_MAX) r.shift();
      const median = _twMedian(r);
      let sum = 0;
      for (let i = 0; i < r.length; i++) sum += r[i];
      TW_MODEL_SPEEDS[model] = {
        median_tps: Math.round(median * 100) / 100,
        avg_tps: Math.round((sum / r.length) * 100) / 100,
        last_tps: Math.round(tps * 100) / 100,
        samples: r.length,
      };
    }

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
    let activeProjectId = null;
    let projectList = [];
    let parentId    = null;
    const activeStreams = new Map(); // chatId → AbortController
    const openTabs = new Map(); // chatId → { pane: HTMLElement, title: string }
    const contextCharsCache = new Map(); // chatId → total context chars
    let creating    = false;


