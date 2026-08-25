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
    const phaseBrowser   = document.getElementById("phaseBrowser");
    const loginOverlay   = document.getElementById("loginOverlay");

    const loginForm    = document.getElementById("loginForm");
    const loginTokenIn = document.getElementById("loginToken");
    const loginBtn     = document.getElementById("loginBtn");
    const loginError   = document.getElementById("loginError");

    const setupPasswordForm = document.getElementById("setupPasswordForm");
    const setupPasswordIn   = document.getElementById("setupPassword");
    const setupPasswordBtn  = document.getElementById("setupPasswordBtn");
    const setupPasswordErr  = document.getElementById("setupPasswordError");
    const setupBrowserBtn   = document.getElementById("setupBrowserBtn");
    const setupBrowserStatus = document.getElementById("setupBrowserStatus");

    const getToken = () => localStorage.getItem(TOKEN_KEY);
    const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
    const clearToken = () => localStorage.removeItem(TOKEN_KEY);

    // Hide all phases, then show one
    function showPhase(phase) {
      phasePassword.classList.add("hidden");
      phaseBrowser.classList.add("hidden");
      loginOverlay.classList.add("hidden");
      if (phase) phase.classList.remove("hidden");
    }

    // Inject the bearer token into every API request; bounce to login on 401.
    let _authBounced = false;
    const _origFetch = window.fetch.bind(window);
    window.fetch = async (url, init = {}) => {
      const token = getToken();
      if (token) {
        init.headers = Object.assign({}, init.headers, { Authorization: "Bearer " + token });
      }
      const res = await _origFetch(url, init);
      if (res.status === 401 && typeof url === "string" && !url.includes("/api/login") && !_authBounced) {
        _authBounced = true;
        clearToken();
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
        showPhase(null);
        return Promise.resolve();
      }

      // Check if first-run setup is needed
      return (async () => {
        try {
          const statusRes = await _origFetch("/api/setup/status");
          if (statusRes.ok) {
            const status = await statusRes.json();
            if (status.needs_password) {
              // === PHASE 1: Set Password ===
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

              // === PHASE 2: Browser Login ===
              showPhase(phaseBrowser);

              await new Promise((resolve) => {
                setupBrowserBtn.addEventListener("click", async () => {
                  setupBrowserBtn.disabled = true;
                  setupBrowserStatus.textContent = "Opening browser...";
                  setupBrowserStatus.classList.remove("hidden");
                  try {
                    const res = await _origFetch("/api/setup/browser-login", {
                      method: "POST",
                      headers: { Authorization: "Bearer " + getToken() },
                    });
                    if (res.ok) {
                      setupBrowserStatus.textContent = "Browser opened! Complete login there, then close it.";
                      setupBrowserStatus.style.color = "";
                      setTimeout(resolve, 3000);
                    } else {
                      let errMsg = "Failed to open browser.";
                      try {
                        const body = await res.json();
                        if (body.detail) errMsg = body.detail.split("\n")[0];
                      } catch {}
                      setupBrowserStatus.textContent = "❌ " + errMsg;
                      setupBrowserStatus.style.color = "#ff6b6b";
                      console.error("Browser login failed:", errMsg);
                    }
                  } catch (e) {
                    setupBrowserStatus.textContent = "❌ Connection error: " + (e.message || e);
                    setupBrowserStatus.style.color = "#ff6b6b";
                    console.error("Browser login error:", e);
                  } finally {
                    setupBrowserBtn.disabled = false;
                  }
                }, { once: true });
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
        showPhase(loginOverlay);
        loginTokenIn.focus();
        return waitForLogin();
      })();
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


