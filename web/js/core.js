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
    const phasePassword  = document.getElementById("phasePassword");
    const phaseSetup     = document.getElementById("phaseSetup");
    const loginOverlay   = document.getElementById("loginOverlay");

    const loginForm    = document.getElementById("loginForm");
    const loginTokenIn = document.getElementById("loginToken");
    const loginBtn     = document.getElementById("loginBtn");
    const loginError   = document.getElementById("loginError");

    const setupPasswordForm = document.getElementById("setupPasswordForm");
    const setupPasswordIn   = document.getElementById("setupPassword");
    const setupPasswordBtn  = document.getElementById("setupPasswordBtn");
    const setupPasswordErr  = document.getElementById("setupPasswordError");

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
    const setupBrowserBtn  = document.getElementById("setupBrowserBtn");
    const setupBrowserStatus = document.getElementById("setupBrowserStatus");
    const setupSkipBtn     = document.getElementById("setupSkipBtn");

    const getToken = () => localStorage.getItem(TOKEN_KEY);
    const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
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
      phasePassword.classList.add("hidden");
      phaseSetup.classList.add("hidden");
      loginOverlay.classList.add("hidden");
      if (phase) phase.classList.remove("hidden");
    }

    // Inject the bearer token into every API request; bounce to login on 401.
    let _authBounced = false;
    let _authReady = false; // Prevents 401 bounce during initial setup flow
    const _origFetch = window.fetch.bind(window);
    window.fetch = async (url, init = {}) => {
      const token = getToken();
      if (token) {
        init.headers = Object.assign({}, init.headers, { Authorization: "Bearer " + token });
      }
      const res = await _origFetch(url, init);
      if (res.status === 401 && typeof url === "string" && !url.includes("/api/login") && !url.includes("/api/setup/") && _authReady && !_authBounced) {
        _authBounced = true;
        clearToken();
        // Check if setup is needed before showing login screen
        try {
          const sRes = await _origFetch("/api/setup/status");
          if (sRes.ok) {
            const sData = await sRes.json();
            if (sData.needs_password) {
              showPhase(phasePassword);
              setupPasswordIn.focus();
              return res;
            }
          }
        } catch {}
        showPhase(loginOverlay);
        loginTokenIn.focus();
      }
      return res;
    };

    // Persistent login resolve — set when ensureAuth() needs to wait for login
    let _loginResolve = null;

    // Persistent login form handler (never removed, handles retries + 401 re-shows)
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
          _authBounced = false;
          showPhase(null);
          if (_loginResolve) { _loginResolve(); _loginResolve = null; }
          // Reload todo/tasks data if either panel is open in sidebar
          const _hosted = window.sidebarHost?.getCurrent?.();
          if ((_hosted === 'todo' || _hosted === 'tasks') && typeof loadAllPanels === "function") {
            loadAllPanels();
          }
        } else {
          // Check if server needs setup (no password set) — redirect to Phase 1
          try {
            const sRes = await _origFetch("/api/setup/status");
            if (sRes.ok) {
              const sData = await sRes.json();
              if (sData.needs_password) {
                clearToken();
                showPhase(phasePassword);
                setupPasswordIn.focus();
                return;
              }
            }
          } catch {}
          loginError.textContent = "Invalid token. Try again.";
          loginError.classList.remove("hidden");
          loginTokenIn.value = "";
          loginTokenIn.focus();
        }
      } catch {
        loginError.textContent = "Connection error. Try again.";
        loginError.classList.remove("hidden");
      } finally {
        loginBtn.disabled = false;
      }
    });

    function waitForLogin() {
      return new Promise((resolve) => { _loginResolve = resolve; });
    }

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
      // Check if first-run setup is needed
      try {
          const statusRes = await _origFetch("/api/setup/status");
          if (statusRes.ok) {
            const status = await statusRes.json();
            if (status.needs_password) {
              // === PHASE 1: Set Password ===
              _authReady = true;
              showPhase(phasePassword);
              setupPasswordIn.focus();

              await new Promise((resolve) => {
                setupPasswordForm.addEventListener("submit", async (e) => {
                  e.preventDefault();
                  const pw = setupPasswordIn.value.trim();
                  if (!pw) return;
                  setupPasswordBtn.disabled = true;
                  setupPasswordErr.classList.add("hidden");
                  try {
                    const res = await _origFetch("/api/setup/password", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ password: pw }),
                    });
                    if (res.ok) {
                      setToken(pw);
                      resolve();
                    } else {
                      const data = await res.json().catch(() => ({}));
                      setupPasswordErr.textContent = data.detail || "Failed to set password.";
                      setupPasswordErr.classList.remove("hidden");
                    }
                  } catch {
                    setupPasswordErr.textContent = "Connection error.";
                    setupPasswordErr.classList.remove("hidden");
                  } finally {
                    setupPasswordBtn.disabled = false;
                  }
                }, { once: true });
              });

              // === PHASE 2: Setup — API Key or Browser Login ===
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
                    setupBrowserBtn.disabled = true;
                    setupBrowserBtn.textContent = "Opening…";
                    if (setupBrowserStatus) { setupBrowserStatus.textContent = "Launching browser…"; setupBrowserStatus.style.color = "var(--text-dim)"; }
                    try {
                      const browserPath = setupBrowserSelect ? setupBrowserSelect.value : "";
                      const res = await _origFetch("/api/setup/browser-login", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ browser_path: browserPath }),
                      });
                      if (res.ok) {
                        if (setupBrowserStatus) { setupBrowserStatus.textContent = "✅ Browser opened! Sign in there, then close it to continue."; setupBrowserStatus.style.color = "#4ade80"; }
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

              // === PHASE 3: Auto-login after setup ===
              showPhase(loginOverlay);
              loginTokenIn.value = getToken();
              loginForm.dispatchEvent(new Event("submit"));
              return waitForLogin();
            }
          }
      } catch {
        // Setup status check failed — fall through to normal login
      }

      // === PHASE 3: Normal login flow ===
      _authReady = true;
      showPhase(loginOverlay);
      loginTokenIn.focus();
      return waitForLogin();
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
    let activeProjectId = null;
    let projectList = [];
    let parentId    = null;
    const activeStreams = new Map(); // chatId → AbortController
    const openTabs = new Map(); // chatId → { pane: HTMLElement, title: string }
    const contextCharsCache = new Map(); // chatId → total context chars
    let creating    = false;


