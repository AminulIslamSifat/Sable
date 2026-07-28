"""
Comprehensive embedding model stress test — pushes models to their limits.
58 memory entries (real Memory.json + synthetic diverse), 40 complex queries
(real verbatim prompts + big ambiguous multi-topic messes).

Run: cd /home/sifat/hdd/projects/Sable && uv run python test/test_embedding.py
     cd /home/sifat/hdd/projects/Sable && uv run python test/test_embedding.py --model sentence-transformers/all-MiniLM-L6-v2
     cd /home/sifat/hdd/projects/Sable && uv run python test/test_embedding.py --all
     cd /home/sifat/hdd/projects/Sable && uv run python test/test_embedding.py --live
     cd /home/sifat/hdd/projects/Sable && uv run python test/test_embedding.py --live --top-k 10
"""

import argparse
import builtins
import io
import json
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding


class TeeWriter(io.TextIOBase):
    """Duplicates writes to an original stream and an in-memory buffer."""

    def __init__(self, original: io.TextIOBase) -> None:
        super().__init__()
        self._original = original
        self._buffer = io.StringIO()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self._buffer.write(s)
        return self._original.write(s)

    def flush(self) -> None:
        self._buffer.flush()
        self._original.flush()

    def getvalue(self) -> str:
        return self._buffer.getvalue()


_original_print = builtins.print


def _tee_print(*args, **kwargs) -> None:
    """Print that respects the active tee when one is installed."""
    _original_print(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# MEMORY ENTRIES — 58 total (28 real from Brain/Memory.json + 30 synthetic diverse)
# ═══════════════════════════════════════════════════════════════════════════════
MEMORY_ENTRIES = [
    # ── REAL: Semantic (17) ──
    "Sable — an agentic chat platform with multi-engine support and autonomous agent capabilities",
    "Qwen chat completions API requires upstream response UUID as parentId, not local SQLite message IDs. Using local IDs causes silent 400 errors (wrapped in HTTP 200 with x-actual-status-code: 400 header).",
    "Memory consolidation must pass upstream UUID from chats.parent_id (via get_parent_id()) as parent_id to service.chat(), not local SQLite message IDs, to avoid silent Qwen API 400 errors.",
    "search-online/search_online tags fully removed from KNOWN_TAGS, HANDLERS, and SKILL_REGISTRY in engine/skills.py. Online Search now routes exclusively through execute_command calling skills/data/search_online/web_search_batch.py with --json --search-only and --json --fetch-urls flags.",
    "partial_open_re regex removed from SkillParser in engine/skills.py (only existed for search-online attr streaming). _nesting_depth_at guard kept — protects execute_command content from tag misinterpretation.",
    "web_search_batch.py default max_chars is 10000 (was 5000). --max-chars CLI flag controls per-page truncation for both fetch_specific_urls() and comprehensive_web_search().",
    "Sable runs as a user systemd service at ~/.config/systemd/user/sable.service executing 'uv run uvicorn server:app --host 0.0.0.0 --port 6001' (LAN-accessible since 2026-07-25).",
    "Full Android SDK installed at /opt/android-sdk (owned by sifat:sifat, user-writable): build-tools 37.0.0, platforms;android-36, cmdline-tools/latest (sdkmanager), NDK 28.2.13676358.",
    "Sifat's system default JDK is java-26-openjdk. Java 26 incompatible with Gradle 9.1.0 + AGP 9.0.1 used by Phantom. jdk21-openjdk pinned per-project via org.gradle.java.home.",
    "MathJax v3 (tex-chtml) loaded in web/index.html via CDN with startup.typeset:false. renderMathJax(container) helper invoked at streaming chat render, answer panel render, and loadMessages.",
    "Sable is exposed over HTTPS via 'tailscale serve --bg --https=443 http://127.0.0.1:6001', reachable tailnet-only at https://archie.tail91f613.ts.net.",
    "Sable web/ has PWA install-prompt prerequisites: manifest.json (name 'Sable', display standalone, theme_color #7c5cbf), and sw.js (cache 'sable-v1', network-first with cache-fallback).",
    "Tailscale tailnet owner aminulislamsifat5@: Arch machine is 'archie' (IP 100.88.199.105, net.ipv4.ip_forward=1); Android phone is 'It's Shadow'/its-shadow (IP 100.101.29.71).",
    "Token auth added to Sable. Access token auto-generated on first run, persisted to .auth_token. FastAPI middleware auth_guard gates all /api/* routes EXCEPT /api/login and /api/health.",
    "Mobile UX rules: post-response inputEl.focus() removed; Enter inserts newline on touch devices; isNearBottom() auto-scroll threshold 40% desktop, 15% touch.",
    "Sifat's laptop: 128GB SSD (system), 500GB Toshiba HDD (data, mounted ~/hdd), 16GB RAM, 3.4GHz Intel Core i5 8th gen (i5-8250U class). No dedicated GPU.",
    "fastembed and numpy added as Sable dependencies. Supported FastEmbed models: all-MiniLM-L6-v2, bge-small-en-v1.5, snowflake-arctic-embed-xs, nomic-embed-text-v1.5, jina-embeddings-v2-small-en, bge-base-en-v1.5, gte-base, mxbai-embed-large-v1.",
    # ── REAL: Episodic (4) ──
    "2026-07-24: Brain panel with categorized memory (semantic/episodic/procedural) integrated into settings and context sync",
    "2026-07-26: Added Skills tab to Sable settings with card grid + detail modal. Removed File Organizer skill. SKILL_REGISTRY reduced from 17 to 15 entries.",
    "2026-07-26: MathJax v3 integrated into Sable web/index.html. Initial fix missed DB-loaded messages; resolved by adding renderMathJax(chatEl) call in loadMessages().",
    "2026-07-27: Fixed ChatService global asyncio.Lock blocking concurrent requests. Added lock-free fast-path in _ensure_headers(); lock only held on first-time fetch or refresh.",
    # ── REAL: Procedural (7) ──
    "Use POST /api/settings/memory with {memory: {...}} payload containing semantic/episodic/procedural arrays. Frontend Brain tab provides category tabs, add/delete/save UI.",
    "Qwen API wraps errors in HTTP 200 responses; always check x-actual-status-code header for real status. Empty answer often means hidden 400/422 from invalid parentId.",
    "web_search_batch.py: --max-chars must come BEFORE --fetch-urls in CLI invocation. --fetch-urls uses nargs='+' and greedily consumes following positional args.",
    "noctalia-shell Quickshell (qs -c noctalia-shell) is NOT managed by systemd. Killing it breaks shell overlay until full Hyprland session restart. No reliable IPC reload.",
    "Sable's MathJax uses startup.typeset:false, so any new render path injecting markdown/HTML must explicitly call renderMathJax(container) after innerHTML is set.",
    "Android Private DNS (strict DoT) conflicts with Tailscale MagicDNS: public domain resolution blackholes while tailnet names work. Fix: disable Private DNS or set AdGuard IPs in Tailscale admin.",
    "Sable's deepseek_engine.py Chrome launch must use --ozone-platform=wayland (not --ozone-platform-hint=auto). Hint flag fails on Hyprland because no $DISPLAY is set.",
    # ── SYNTHETIC: Diverse technical knowledge (30) ──
    "Git rebase interactive mode (git rebase -i HEAD~5) allows squashing, reordering, and editing commits before pushing; force-push with --force-with-lease to avoid overwriting teammates' work.",
    "PostgreSQL connection pooling via PgBouncer: set pool_mode=transaction, max_client_conn=1000, default_pool_size=25; avoids 'too many connections' errors under concurrent FastAPI load.",
    "Docker multi-stage builds reduce image size: first stage compiles with full toolchain, second stage copies only artifacts into slim base (python:3.12-slim). .dockerignore excludes .git, __pycache__, .venv.",
    "Nginx reverse proxy config for WebSocket: proxy_http_version 1.1, proxy_set_header Upgrade $http_upgrade, proxy_set_header Connection 'upgrade', proxy_read_timeout 86400s for long-lived connections.",
    "Redis pub/sub for real-time event broadcasting: PUBLISH channel message from backend workers, SUBSCRIBE in async Python via redis.asyncio; no persistence, fire-and-forget semantics.",
    "JWT token refresh flow: short-lived access token (15min) + long-lived refresh token (7d) stored httpOnly; /api/auth/refresh endpoint validates refresh token and issues new pair; revoke on logout.",
    "SQLite WAL mode (PRAGMA journal_mode=WAL) enables concurrent readers with single writer; checkpoint with PRAGMA wal_checkpoint(TRUNCATE); busy_timeout=5000 prevents 'database is locked' under load.",
    "CSS Grid vs Flexbox: Grid for 2D layout (rows AND columns simultaneously), Flexbox for 1D distribution; grid-template-areas for named regions; minmax() + auto-fill for responsive without media queries.",
    "Python asyncio.gather vs TaskGroup: gather cancels siblings on first exception only if return_exceptions=False; TaskGroup (3.11+) always cancels all on first failure, structured concurrency guaranteed.",
    "Linux cgroups v2 memory limiting: systemd-run --scope -p MemoryMax=2G -- command; prevents OOM killer from targeting unrelated processes; check with systemd-cgtop or cat /sys/fs/cgroup/memory.current.",
    "Rust ownership model: each value has exactly one owner; borrowing via &T (shared) or &mut T (exclusive); lifetimes 'a tie reference validity to scope; eliminates data races at compile time.",
    "Kubernetes pod scheduling: nodeSelector for hard affinity, preferredDuringSchedulingIgnoredDuringExecution for soft; taints/tolerations repel pods; resource requests vs limits determine QoS class.",
    "OAuth2 PKCE flow for SPAs: generate code_verifier (random 43-128 chars), hash to code_challenge (S256), send challenge in /authorize, send verifier in /token; prevents authorization code interception.",
    "Webpack tree-shaking requires ESM (import/export), sideEffects:false in package.json, and production mode; CommonJS require() defeats static analysis; check bundle with webpack-bundle-analyzer.",
    "Go channel patterns: fan-out (multiple goroutines read from one channel), fan-in (merge multiple channels into one), pipeline (chain stages), context.Context for cancellation propagation.",
    "TLS 1.3 handshake: single round-trip (vs 2 in TLS 1.2), removes RSA key exchange, only ECDHE/DHE for forward secrecy, 0-RTT resumption possible but vulnerable to replay attacks.",
    "React Server Components: render on server, stream HTML to client, zero JS bundle for server-only components; 'use client' directive marks boundary; Next.js App Router implements this pattern.",
    "PostgreSQL EXPLAIN ANALYZE: actual time vs total time, rows vs loops, Seq Scan vs Index Scan; enable enable_seqscan=off to test index usage; pg_stat_statements for slow query identification.",
    "SSH tunneling: local forward (-L 8080:localhost:5432 user@host) exposes remote DB locally; dynamic SOCKS proxy (-D 1080) for arbitrary traffic; ControlMaster for connection multiplexing.",
    "Python GIL implications: CPU-bound threads don't parallelize; use multiprocessing.Pool or ProcessPoolExecutor; asyncio handles I/O-bound concurrency single-threaded; free-threaded build (3.13t) experimental.",
    "Hyprland window rules: windowrulev2 = float,class:^(pavucontrol)$; workspace rules via workspace = name, monitor:HDMI-A-1; layerrule for blur/opacity on specific layers like waybar or rofi.",
    "systemd user service vs system service: user services in ~/.config/systemd/user/, start with systemctl --user, no root needed; system services in /etc/systemd/system/, need sudo, start at boot.",
    "FastAPI middleware ordering: middleware executes in reverse registration order (last added = outermost); CORSMiddleware should be added last to wrap everything; custom auth middleware before CORS.",
    "fish shell universal variables: set -Ux VAR value persists across all fish sessions without config file; fish_add_path for PATH manipulation; abbr for abbreviations (not aliases); functions for complex logic.",
    "SQLite FTS5 full-text search: CREATE VIRTUAL TABLE docs USING fts5(title, body); MATCH operator for queries; bm25() ranking function; prefix indexes for autocomplete; tokenize='porter unicode61'.",
    "Bluetooth audio on Linux: PipeWire replaces PulseAudio; wireplumber session manager; A2DP for high-quality output, HSP/HFP for mic; codec negotiation (LDAC > aptX > SBC); pactl list sinks for status.",
    "Python type narrowing: isinstance() narrows in if blocks; TypeGuard for custom predicates; assert isinstance(x, T) narrows for rest of scope; match/case with structural patterns (3.10+).",
    "Git worktrees: git worktree add ../feature-branch feature allows parallel checkouts without stashing; each worktree has own HEAD and index; git worktree prune cleans stale references.",
    "Linux inotify limits: fs.inotify.max_user_watches=524288 for IDE file watchers; fs.inotify.max_user_instances=8192; check with cat /proc/sys/fs/inotify/max_user_watches; ENOSPC error when exceeded.",
    "WebSocket reconnection strategy: exponential backoff (1s, 2s, 4s, 8s, max 30s) with jitter; ping/pong keepalive every 30s; queue messages during disconnect; reconnect on visibilitychange event.",
]

# ═══════════════════════════════════════════════════════════════════════════════
# TEST QUERIES — 40 total (15 real + 25 synthetic big/ambiguous)
# "expected" = variable-length set of relevant memory indices (0 to 20+)
# Scoring: recall@K where K = min(len(expected), 10) — how many of the expected
# entries appear in the model's top-K results. Also tracks precision.
# ═══════════════════════════════════════════════════════════════════════════════
TEST_QUERIES = [
    # ── REAL VERBATIM PROMPTS (15) ──
    {
        "query": "right now sable cant handle multiple user at the same time, or multiple chat. Or even streaming and api running simulatiously.\n\nwhat is the problem?",
        "expected": [20, 6, 0, 36, 47],
        "description": "Concurrency bug report (real verbatim)",
    },
    {
        "query": "Then cant the api, and scraping at least work parallely they use diferent browser",
        "expected": [20, 6, 0, 27],
        "description": "Parallel execution follow-up (real verbatim)",
    },
    {
        "query": "08:44:57 [ERROR] sable.scraper: Browser prelaunch failed\nTraceback (most recent call last):\n  File \"engine/scraper.py\", line 317, in _ensure_engine\n    await launch()\n  File \"engine/scraper_engines/deepseek/deepseek_engine.py\", line 219, in launch_chrome\n    await self._wait_for_cdp_ready()\n  File \"engine/scraper_engines/deepseek/deepseek_engine.py\", line 253, in _wait_for_cdp_ready\n    sys.exit(1)\nSystemExit: 1\nRuntimeError: Browser engine exited during startup: 1\n\nwhy is it failing?",
        "expected": [27, 6, 0],
        "description": "Full traceback paste + diagnostic (real verbatim)",
    },
    {
        "query": "check out /home/sifat/odysseus and how they are handling memory of the model, and waht model are theyusign to make vector and if is there better model like around 80mb",
        "expected": [16, 0, 15, 2, 17],
        "description": "Cross-project memory research (real verbatim)",
    },
    {
        "query": "i want to check all the model you listed. So add all of them, make them check and save the result in a txt file with each text avg time and total time. with model name for me to analyze",
        "expected": [16],
        "description": "Benchmark automation request (real verbatim)",
    },
    {
        "query": "which result are accurate, increase the difficulty of test, add more versatile data like around 30 data to check and more complex prompt to match",
        "expected": [16],
        "description": "Benchmark quality demand (real verbatim)",
    },
    {
        "query": "Increase the memory versatility and use broder propmt. Like the prompt i use normally (you can see them in db, from db check 20 of my prompt and then test it with out current memory.json)",
        "expected": [16, 17, 21, 0],
        "description": "Memory test redesign (real verbatim)",
    },
    {
        "query": "❌ Failed to load Alibaba-NLP/gte-small-en-v1.5: Model Alibaba-NLP/gte-small-en-v1.5 is not supported in TextEmbedding. Please check the supported models using `TextEmbedding.list_supported_models()`",
        "expected": [16],
        "description": "Raw error paste (real verbatim)",
    },
    {
        "query": "uv run python test/test_embedding.py\n🔄 Loading bge-small-en-v1.5 (first run downloads ~130MB)...\nTraceback (most recent call last):\n  File \"test/test_embedding.py\", line 57, in <module>\n    main()\n  File \"test/test_embedding.py\", line 14, in main\n    from fastembed import TextEmbedding\nModuleNotFoundError: No module named 'fastembed'",
        "expected": [16],
        "description": "Missing dependency error (real verbatim)",
    },
    {
        "query": "SABLE EMBEDDING MODEL BENCHMARK RESULTS\nGenerated: 2026-07-27 09:28:21\nMemory entries: 30 | Test queries: 8\nmxbai-embed-large-v1 55.0 Enc ms 54.2% Acc\nbge-small-en-v1.5 10.3 Enc ms 50.0% Acc\nbge-base-en-v1.5 22.3 Enc ms 45.8% Acc\nall-MiniLM-L6-v2 19.0 Enc ms 37.5% Acc\n\nthe result are surprisingly weird and underwhelming",
        "expected": [16],
        "description": "Results paste + evaluation (real verbatim)",
    },
    {
        "query": "how do i access sable from my phone over tailscale when im not on the same wifi network",
        "expected": [10, 12, 6, 11, 13],
        "description": "Remote access question (realistic)",
    },
    {
        "query": "why is qwen returning completely empty responses with no exception thrown and HTTP status shows 200 but the answer field is just blank nothing at all",
        "expected": [1, 22, 2, 3],
        "description": "Silent API failure (realistic verbose)",
    },
    {
        "query": "mathjax latex formulas render fine when streaming new messages but when i reload the page or scroll up to older messages loaded from the database the math shows as raw dollar sign text instead of rendered equations",
        "expected": [9, 19, 25],
        "description": "MathJax DB rendering bug (realistic verbose)",
    },
    {
        "query": "my android phone connects to tailscale fine and i can ping archie.tail91f613.ts.net and access sable over https but chrome and firefox cant resolve any public websites like google.com or github.com while tailscale is active disconnecting tailscale fixes internet immediately",
        "expected": [26, 12, 10],
        "description": "Android DNS conflict (realistic verbose)",
    },
    {
        "query": "the deepseek scraper chrome process keeps crashing on hyprland wayland session with error Missing X server or DISPLAY even though everything else runs fine on wayland and i have no xwayland issues with other apps",
        "expected": [27, 6, 0, 48],
        "description": "Wayland scraper crash (realistic verbose)",
    },
    # ── SYNTHETIC: Big ambiguous multi-topic prompts (25) ──
    {
        "query": "so i was trying to deploy this new microservice and the docker build keeps failing at the pip install step saying no matching distribution found but it works locally and i think its something about the multi-stage build copying the wrong requirements file or maybe the .dockerignore is excluding something it shouldnt also the nginx proxy in front keeps returning 502 bad gateway when i try to hit the websocket endpoint but regular http works fine",
        "expected": [30, 31, 57, 6],
        "description": "Docker + Nginx + WebSocket multi-failure (ambiguous)",
    },
    {
        "query": "my fastapi app is getting database is locked errors randomly under load and i think its because multiple coroutines are hitting sqlite at the same time but i already set busy_timeout and journal_mode=WAL in the connection string and also the connection pool settings might be wrong because im using asyncio and each request creates a new connection instead of reusing them and sometimes the whole thing just hangs for 5 seconds then works again",
        "expected": [34, 36, 20, 47],
        "description": "SQLite concurrency + connection pooling chaos (ambiguous)",
    },
    {
        "query": "ok so the auth flow is completely broken after i added the refresh token rotation and now sometimes the frontend gets a 401 even though the refresh token should still be valid and i think the issue is either the httpOnly cookie not being sent on cross-origin requests or the CORS middleware ordering in fastapi is wrong because i added my custom auth middleware after CORS and now preflight OPTIONS requests are hitting the auth check and failing",
        "expected": [35, 50, 13, 33],
        "description": "Auth + CORS + middleware ordering mess (ambiguous)",
    },
    {
        "query": "i need to set up a real-time notification system where multiple backend workers publish events and the frontend subscribes via websocket but i dont know whether to use redis pub/sub or just postgresql LISTEN/NOTIFY and also the websocket keeps disconnecting after exactly 60 seconds which i think is nginx proxy_read_timeout but i already set it to 86400 and also the reconnection logic in the frontend keeps creating duplicate subscriptions",
        "expected": [32, 31, 57, 29],
        "description": "Real-time architecture + timeout + reconnect (ambiguous)",
    },
    {
        "query": "the git history is an absolute disaster and i need to squash like 15 commits into 3 logical ones but some of them touch the same files and i keep getting conflicts during interactive rebase and also i accidentally pushed to main instead of my feature branch and now i need to undo that without losing my teammates changes and also i have uncommitted work in another directory that i think is a stale worktree",
        "expected": [28, 55],
        "description": "Git rebase + force-push + worktree chaos (ambiguous)",
    },
    {
        "query": "my python script that processes images is taking forever and i tried using threading but it doesnt seem to help at all and someone said its the GIL but i dont understand why because the image processing library releases the GIL for C extensions and also i tried multiprocessing but now the memory usage is insane because each process loads the full model and i only have 16GB RAM and the system starts swapping",
        "expected": [47, 15, 37],
        "description": "GIL + multiprocessing + memory pressure (ambiguous)",
    },
    {
        "query": "so i set up a kubernetes cluster on 3 nodes and my pods keep getting evicted with OOMKilled status even though i set memory limits to 512Mi and the app only uses like 200MB normally but during peak traffic it spikes and also the scheduling is weird because all pods end up on one node and the other two are idle and i think its something about resource requests vs limits and also the HPA isnt scaling properly",
        "expected": [39, 37],
        "description": "K8s OOM + scheduling + HPA (ambiguous)",
    },
    {
        "query": "the bluetooth headphones keep disconnecting every 10 minutes on my arch laptop and when i check journalctl it says a2dp sink not available and also the microphone quality is terrible on calls because it switches to HSP profile automatically and i think pipewire is fighting with pulseaudio because i have both installed and wireplumber keeps restarting and also the volume resets to 100% every reboot",
        "expected": [53, 49],
        "description": "Bluetooth audio + PipeWire + profile switching (ambiguous)",
    },
    {
        "query": "i want to add full text search to my sqlite database but the LIKE queries are taking 3 seconds on 50k rows and i read about FTS5 but i dont know how to keep it in sync with the main table and also the tokenizer doesnt handle unicode properly for bengali text and also i want autocomplete suggestions as the user types but prefix queries are slow and also the ranking is wrong because bm25 gives too much weight to common words",
        "expected": [52, 34],
        "description": "SQLite FTS5 + unicode + autocomplete + ranking (ambiguous)",
    },
    {
        "query": "my hyprland config keeps breaking after updates and now the window rules for floating pavucontrol stopped working and also my waybar disappeared and i think its because i killed quickshell earlier trying to reload the theme and now the bar and dock and lockscreen are all gone and restarting hyprland doesnt help and also the workspace rules for my second monitor got reset and everything opens on workspace 1 now",
        "expected": [24, 48, 49],
        "description": "Hyprland + Quickshell + window rules disaster (ambiguous)",
    },
    {
        "query": "the TLS certificate for my tailscale HTTPS endpoint keeps expiring every 90 days and i have to manually renew it and also when i try to access it from my phone the browser says connection not private and i think its because the certificate chain is incomplete and also i want to add HSTS headers but nginx keeps overriding them and also the 0-RTT resumption seems to cause replay attacks on my login endpoint",
        "expected": [10, 43, 31, 12],
        "description": "TLS + Tailscale HTTPS + HSTS + 0-RTT (ambiguous)",
    },
    {
        "query": "i need to implement OAuth2 login for my SPA but the authorization code keeps getting intercepted and i read about PKCE but i dont understand the difference between code_verifier and code_challenge and also my redirect URI keeps mismatching because the dev server is on localhost:3000 but production is on a different domain and also the refresh token rotation is causing race conditions when multiple tabs try to refresh simultaneously",
        "expected": [40, 35, 33],
        "description": "OAuth2 PKCE + redirect + token race (ambiguous)",
    },
    {
        "query": "the webpack bundle is 4.2MB and i have no idea whats in it and tree shaking doesnt seem to work because i have a library that uses CommonJS require and also the code splitting creates 47 chunks and the initial load is still slow and also the CSS is duplicated across chunks and i think sideEffects:false in package.json is breaking my global styles and also source maps are leaking to production",
        "expected": [41],
        "description": "Webpack bundle + tree-shaking + code-splitting (ambiguous)",
    },
    {
        "query": "my go service has a goroutine leak and pprof shows 50000 goroutines stuck in chan receive and i think its because the context cancellation isnt propagating to the worker pool and also the fan-in pattern i used has a deadlock when one of the input channels closes early and also the HTTP handler keeps writing to the response after the client disconnects and panics with broken pipe",
        "expected": [42],
        "description": "Go goroutine leak + context + channel deadlock (ambiguous)",
    },
    {
        "query": "so i was reading about rust and i dont understand why the borrow checker keeps rejecting my code when i try to push to a vector while iterating over it and also the lifetime annotations on my struct are getting insane with like 5 different lifetimes and also i tried using Rc<RefCell<T>> but now i have runtime panics instead of compile errors and also the async runtime keeps deadlocking when i hold a mutex guard across an await point",
        "expected": [38],
        "description": "Rust ownership + lifetimes + async deadlock (ambiguous)",
    },
    {
        "query": "the systemd user service for sable keeps failing to start after reboot with error code 1 and journalctl shows ModuleNotFoundError for uvicorn even though it works fine when i run it manually in the terminal and i think its something about the PATH not being set correctly in the service file and also the working directory is wrong and also it starts before the network is up so the tailscale proxy fails to connect",
        "expected": [6, 49, 10, 12],
        "description": "systemd service + PATH + network ordering (ambiguous)",
    },
    {
        "query": "my fish shell config is broken and every new terminal takes 3 seconds to load because i have like 200 universal variables set and also fish_add_path keeps duplicating entries in PATH and also my abbreviations conflict with actual commands and also the prompt takes forever because it calls git status on a repo with 500k files and also the inotify watcher limit is hit so my IDE keeps showing file changed on disk warnings",
        "expected": [51, 56],
        "description": "fish shell + universal vars + inotify limits (ambiguous)",
    },
    {
        "query": "i want to switch from REST to GraphQL for my API but the N+1 query problem is making it slower than REST and also the subscription endpoint for real-time data keeps dropping connections and also the caching layer is completely different and my CDN doesnt understand GraphQL POST requests and also the error handling is weird because partial failures return 200 with errors array and the frontend doesnt know what to do",
        "expected": [34, 57, 31, 32],
        "description": "GraphQL + N+1 + subscriptions + caching (ambiguous)",
    },
    {
        "query": "the android build keeps failing with AGP 9.0.1 requiring JDK 21 but my system has java 26 and gradle wrapper downloads a different version every time and also the NDK version mismatch between what flutter expects and what sdkmanager installed and also the build cache is corrupted and every clean build takes 12 minutes on this i5 with no GPU acceleration and also the R8 shrinking keeps removing classes that are only referenced via reflection",
        "expected": [8, 7, 15],
        "description": "Android JDK + NDK + Gradle + R8 chaos (ambiguous)",
    },
    {
        "query": "so the PWA install prompt never shows up on my phone even though i have the manifest and service worker and also the service worker caches stale data and users see old versions after deploy and also the offline fallback page doesnt work because the cache-first strategy returns the cached 404 page and also the maskable icon looks cropped on samsung phones and also the theme_color doesnt match the status bar on iOS",
        "expected": [11, 57, 14],
        "description": "PWA + service worker + caching + icons (ambiguous)",
    },
    {
        "query": "my postgresql database is running out of connections and pgbouncer keeps throwing errors about pool exhaustion and also the slow query log shows the same query taking 8 seconds sometimes and 50ms other times and i think its because the query planner switches between seq scan and index scan depending on the table statistics and also autovacuum hasnt run in 3 days and the dead tuples are piling up and also the WAL files are filling up the disk",
        "expected": [29, 45, 34],
        "description": "PostgreSQL pooling + query planner + vacuum + WAL (ambiguous)",
    },
    {
        "query": "the react app re-renders 47 times on a single state update and i profiled it with react devtools and the server components are being fetched on every navigation even though they should be cached and also the use client boundary is in the wrong place so the entire page tree is client-rendered and also the streaming SSR shows a flash of loading skeleton and also the hydration mismatch warning appears randomly in production",
        "expected": [44],
        "description": "React RSC + re-renders + hydration + streaming (ambiguous)",
    },
    {
        "query": "i set up an SSH tunnel to access my home database from work but it keeps dropping after 5 minutes of inactivity and also the ControlMaster socket gets stale and subsequent connections hang and also when i try to use it as a SOCKS proxy for my browser the DNS resolution fails because it tries to resolve locally instead of through the tunnel and also the port forwarding conflicts with a local service on 8080",
        "expected": [46, 31],
        "description": "SSH tunnel + ControlMaster + SOCKS + DNS (ambiguous)",
    },
    {
        "query": "the type checker keeps complaining about my union types and i have this function that accepts str | int | None and after the isinstance check for str the type still shows as str | int in the else branch and also my custom TypeGuard function doesnt narrow properly in list comprehensions and also the match statement with structural patterns doesnt infer the type of nested dataclasses and also pyright and mypy disagree on whether a Protocol is satisfied",
        "expected": [54],
        "description": "Python type narrowing + TypeGuard + match + Protocol (ambiguous)",
    },
    {
        "query": "so the memory consolidation in sable is creating duplicate entries and i think its because the embedding similarity threshold is too low and also the parent_id being passed to qwen is the local sqlite id instead of the upstream uuid and also the context window keeps overflowing because we're stuffing too many memories into the system prompt and also the episodic memories are being treated as semantic because the categorization logic just checks for a date prefix",
        "expected": [2, 1, 17, 16, 0, 22, 3],
        "description": "Memory consolidation + parentId + categorization (ambiguous, broad)",
    },
    # ── ZERO-MATCH QUERIES (should retrieve nothing relevant) ──
    {
        "query": "what is the airspeed velocity of an unladen swallow and does it differ between african and european species",
        "expected": [],
        "description": "Completely irrelevant — zero expected matches",
    },
    {
        "query": "can you write me a recipe for homemade croissants with laminated butter dough and explain the folding technique step by step",
        "expected": [],
        "description": "Completely irrelevant — zero expected matches",
    },
    {
        "query": "explain the geopolitical implications of the 1947 partition of british india on modern bangladesh-pakistan water sharing disputes",
        "expected": [],
        "description": "Completely irrelevant — zero expected matches",
    },
]

ALL_MODELS = [
    "sentence-transformers/all-MiniLM-L6-v2",
    "BAAI/bge-small-en-v1.5",
    "snowflake/snowflake-arctic-embed-xs",
    "nomic-ai/nomic-embed-text-v1.5",
    "jinaai/jina-embeddings-v2-small-en",
    "BAAI/bge-base-en-v1.5",
    "thenlper/gte-base",
    "mixedbread-ai/mxbai-embed-large-v1",
]

DEFAULT_MODEL = "snowflake/snowflake-arctic-embed-xs"
RESULTS_FILE = Path(__file__).resolve().parent / "benchmark_results.txt"

# ── Live memory + prompt sources ──────────────────────────────────────────────
_BRAIN_DIR = Path(__file__).resolve().parent.parent / "Brain"
_MEMORY_PATH = _BRAIN_DIR / "Memory.json"
_PROTECTED_PATH = _BRAIN_DIR / "Protected.json"
_DB_PATH = Path(__file__).resolve().parent.parent / "sable.db"


def _load_live_memories() -> list[str]:
    """Load all memory entry texts from Memory.json + Protected.json."""
    entries: list[str] = []
    for path in (_MEMORY_PATH, _PROTECTED_PATH):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            cats = data.get("protected", []) if path == _PROTECTED_PATH else []
            if path == _MEMORY_PATH:
                cats = []
                for cat in ("semantic", "episodic", "procedural", "ephemeral"):
                    cats.extend(data.get(cat, []))
            for e in cats:
                if not isinstance(e, dict):
                    continue
                k = str(e.get("key", "")).strip()
                v = str(e.get("value", "")).strip()
                if not v:
                    continue
                entries.append(f"{k}: {v}" if k else v)
    return entries


def _load_live_prompts(n: int = 50) -> list[str]:
    """Pull the last N user messages from sable.db, stripped of timestamp prefix."""
    if not _DB_PATH.exists():
        print(f"⚠️  DB not found at {_DB_PATH}")
        return []
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT content FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    conn.close()
    prompts: list[str] = []
    for row in rows:
        text = row["content"] or ""
        # Strip "[YYYY-MM-DD HH:MM:SS]\n" prefix injected by server.py
        if text.startswith("[") and "\n" in text[:25]:
            text = text.split("\n", 1)[1]
        text = text.strip()
        if text and len(text) >= 30:
            prompts.append(text)
    return prompts


def benchmark_live(model_name: str, top_k: int = 5) -> dict | None:
    """Run last-N real prompts against real Memory.json, show raw scores, return stats."""
    memories = _load_live_memories()
    prompts = _load_live_prompts(50)
    if not memories:
        print("❌ No memory entries found in Brain/Memory.json")
        return None
    if not prompts:
        print("❌ No user prompts found in sable.db")
        return None

    print(f"\n{'='*70}")
    print(f"🔴 LIVE MODE: {model_name}")
    print(f"   {len(memories)} memory entries | {len(prompts)} recent prompts | top-{top_k}")
    print(f"{'='*70}")

    t0 = time.perf_counter()
    try:
        model = TextEmbedding(model_name=model_name)
    except Exception as e:
        print(f"❌ Failed to load {model_name}: {e}")
        return None
    load_s = time.perf_counter() - t0
    print(f"✅ Loaded in {load_s:.2f}s")

    t_enc = time.perf_counter()
    # Embed one-at-a-time to avoid fastembed inhomogeneous-shape bug with variable-length tokens
    mem_vecs = np.array([list(model.embed([m]))[0] for m in memories], dtype="float32")
    encode_s = time.perf_counter() - t_enc
    avg_encode_ms = (encode_s / len(memories)) * 1000

    norms = np.linalg.norm(mem_vecs, axis=1, keepdims=True)
    normed = mem_vecs / np.where(norms == 0, 1.0, norms)

    all_top_scores: list[float] = []
    all_kth_scores: list[float] = []
    query_times: list[float] = []

    for i, prompt in enumerate(prompts):
        tq = time.perf_counter()
        q_vec = np.array(list(model.embed([prompt]))[0], dtype="float32")
        q_norm_val = np.linalg.norm(q_vec)
        q_n = q_vec if q_norm_val == 0 else q_vec / q_norm_val
        scores = normed @ q_n
        ranked = np.argsort(-scores)[:top_k]
        query_times.append((time.perf_counter() - tq) * 1000)

        all_top_scores.append(float(scores[ranked[0]]))
        all_kth_scores.append(float(scores[ranked[-1]]))

        p_short = prompt.replace("\n", " ")
        print(f"\n  [{i+1}] \"{p_short[:90]}{'...' if len(p_short) > 90 else ''}\"")
        for rank, idx in enumerate(ranked):
            marker = "★" if scores[idx] >= 0.6 else " "
            m_text = memories[idx].replace("\n", " ")[:80]
            print(f"      {marker} [{scores[idx]:.4f}] {m_text}")

    # ── Threshold analysis ──
    all_top_scores.sort()
    all_kth_scores.sort()
    n = len(all_top_scores)
    print(f"\n{'═'*70}")
    print(f"📐 SCORE DISTRIBUTION ({n} prompts)")
    print(f"{'═'*70}")
    print(f"   Top-1 scores:  min={all_top_scores[0]:.4f}  "
          f"p25={all_top_scores[n//4]:.4f}  "
          f"median={all_top_scores[n//2]:.4f}  "
          f"p75={all_top_scores[3*n//4]:.4f}  "
          f"max={all_top_scores[-1]:.4f}")
    print(f"   Top-{top_k} scores: min={all_kth_scores[0]:.4f}  "
          f"p25={all_kth_scores[n//4]:.4f}  "
          f"median={all_kth_scores[n//2]:.4f}  "
          f"p75={all_kth_scores[3*n//4]:.4f}  "
          f"max={all_kth_scores[-1]:.4f}")

    noise_floor = all_top_scores[n // 4]
    typical = all_top_scores[n // 2]
    suggested = round((noise_floor + typical) / 2, 3)
    print(f"\n   💡 Suggested threshold: {suggested:.3f}")
    print(f"      (midpoint of p25={noise_floor:.3f} and median={typical:.3f})")
    print(f"{'═'*70}")

    avg_query_ms = sum(query_times) / len(query_times) if query_times else 0.0
    return {
        "model": model_name,
        "mode": "live",
        "memory_count": len(memories),
        "prompt_count": len(prompts),
        "top_k": top_k,
        "encode_ms": avg_encode_ms,
        "encode_total_s": encode_s,
        "query_ms": avg_query_ms,
        "query_total_s": sum(query_times) / 1000,
        "top1_min": all_top_scores[0],
        "top1_median": all_top_scores[n // 2],
        "top1_max": all_top_scores[-1],
        "topk_min": all_kth_scores[0],
        "topk_median": all_kth_scores[n // 2],
        "topk_max": all_kth_scores[-1],
        "suggested_threshold": suggested,
        "load_s": load_s,
    }


def benchmark_model(model_name: str) -> dict | None:
    print(f"\n{'='*70}")
    print(f"🔄 Testing: {model_name}")
    print(f"{'='*70}")

    t0 = time.perf_counter()
    try:
        model = TextEmbedding(model_name=model_name)
    except Exception as e:
        print(f"❌ Failed to load {model_name}: {e}")
        return None
    load_time = time.perf_counter() - t0
    print(f"✅ Model loaded in {load_time:.2f}s")

    # Encode all memory entries
    t0 = time.perf_counter()
    mem_vectors = np.array(list(model.embed(MEMORY_ENTRIES)), dtype="float32")
    encode_time = time.perf_counter() - t0
    avg_encode_ms = (encode_time / len(MEMORY_ENTRIES)) * 1000
    print(f"🧪 Encoded {len(MEMORY_ENTRIES)} memory entries in {encode_time:.3f}s ({avg_encode_ms:.1f} ms/text)")

    # Normalize memory vectors once
    norms = np.linalg.norm(mem_vectors, axis=1, keepdims=True)
    normed_mem = mem_vectors / norms

    # Run query tests
    print(f"\n🔍 Running {len(TEST_QUERIES)} query tests...")
    total_recall = 0.0
    total_precision = 0.0
    scored_queries = 0
    zero_match_queries = 0
    zero_match_correct = 0
    query_times = []

    for i, test in enumerate(TEST_QUERIES):
        tq = time.perf_counter()
        q_vec = np.array(list(model.embed([test["query"]]))[0], dtype="float32")
        q_norm = q_vec / np.linalg.norm(q_vec)
        scores = normed_mem @ q_norm
        ranked_indices = np.argsort(-scores)
        query_time = (time.perf_counter() - tq) * 1000
        query_times.append(query_time)

        expected = set(test["expected"])
        K = max(len(expected), 3)  # retrieve at least top-3, up to len(expected)
        topK = ranked_indices[:K].tolist()

        # Zero-match queries: check if top result score is low (model correctly ignores)
        if len(expected) == 0:
            zero_match_queries += 1
            top_score = scores[ranked_indices[0]]
            # If top score < 0.3, model correctly treats as irrelevant
            is_correct = top_score < 0.3
            if is_correct:
                zero_match_correct += 1
            marker = "✅" if is_correct else "⚠️"
            print(f"\n   {marker} Q{i+1}: {test['description']}")
            q_text = test["query"].replace("\n", " ")
            print(f"      Query: \"{q_text[:80]}...\"" if len(q_text) > 80 else f"      Query: \"{q_text}\"")
            print(f"      Top score: {top_score:.4f} (want < 0.3) | Query time: {query_time:.1f}ms")
            entry_text = MEMORY_ENTRIES[ranked_indices[0]].replace("\n", " ")
            print(f"        [{top_score:.4f}] {entry_text[:75]}")
            continue

        # Recall: how many expected entries appear in top-K
        hits = len(set(topK) & expected)
        recall = hits / len(expected)
        # Precision: how many of top-K are actually relevant
        precision = hits / K

        total_recall += recall
        total_precision += precision
        scored_queries += 1

        hit_marker = "✅" if recall >= 0.6 else "⚠️" if recall >= 0.3 else "❌"
        print(f"\n   {hit_marker} Q{i+1}: {test['description']}")
        q_text = test["query"].replace("\n", " ")
        print(f"      Query: \"{q_text[:80]}...\"" if len(q_text) > 80 else f"      Query: \"{q_text}\"")
        print(f"      Recall@{K}: {recall:.0%} ({hits}/{len(expected)}) | Precision: {precision:.0%} | Query time: {query_time:.1f}ms")
        for rank, idx in enumerate(topK[:5]):  # show top 5
            is_expected = "★" if idx in expected else " "
            entry_text = MEMORY_ENTRIES[idx].replace("\n", " ")
            print(f"      {is_expected} [{scores[idx]:.4f}] {entry_text[:75]}")

    avg_recall = (total_recall / scored_queries * 100) if scored_queries else 0
    avg_precision = (total_precision / scored_queries * 100) if scored_queries else 0
    zero_match_acc = (zero_match_correct / zero_match_queries * 100) if zero_match_queries else 0
    avg_query_ms = sum(query_times) / len(query_times)
    total_query_time = sum(query_times) / 1000

    # ── THRESHOLD ANALYSIS ──
    print(f"\n{'═'*70}")
    print(f"📐 THRESHOLD ANALYSIS for {model_name}")
    print(f"{'═'*70}")
    print(f"\n   Zero-match queries (irrelevant — should score LOW):")
    for i, test in enumerate(TEST_QUERIES):
        if len(test["expected"]) == 0:
            q_vec = np.array(list(model.embed([test["query"]]))[0], dtype="float32")
            q_norm = q_vec / np.linalg.norm(q_vec)
            scores_z = normed_mem @ q_norm
            top5_idx = np.argsort(-scores_z)[:5]
            q_text = test["query"].replace("\n", " ")[:60]
            print(f"\n   \"{q_text}...\"")
            for idx in top5_idx:
                entry = MEMORY_ENTRIES[idx].replace("\n", " ")[:60]
                print(f"      {scores_z[idx]:.4f} | {entry}")

    print(f"\n   Relevant queries — top score distribution (should score HIGH):")
    relevant_top_scores = []
    relevant_min_hit_scores = []
    for i, test in enumerate(TEST_QUERIES):
        if len(test["expected"]) == 0:
            continue
        q_vec = np.array(list(model.embed([test["query"]]))[0], dtype="float32")
        q_norm = q_vec / np.linalg.norm(q_vec)
        scores_r = normed_mem @ q_norm
        ranked = np.argsort(-scores_r)
        relevant_top_scores.append(scores_r[ranked[0]])
        # Score of the lowest-scoring expected entry that was actually found
        expected = set(test["expected"])
        hit_scores = [scores_r[idx] for idx in ranked if idx in expected]
        if hit_scores:
            relevant_min_hit_scores.append(min(hit_scores))

    relevant_top_scores.sort()
    relevant_min_hit_scores.sort()
    print(f"      Top-1 scores:  min={relevant_top_scores[0]:.4f}  median={relevant_top_scores[len(relevant_top_scores)//2]:.4f}  max={relevant_top_scores[-1]:.4f}")
    print(f"      Lowest hit:    min={relevant_min_hit_scores[0]:.4f}  median={relevant_min_hit_scores[len(relevant_min_hit_scores)//2]:.4f}  max={relevant_min_hit_scores[-1]:.4f}")

    # Suggest threshold
    zero_top_scores = []
    for test in TEST_QUERIES:
        if len(test["expected"]) == 0:
            q_vec = np.array(list(model.embed([test["query"]]))[0], dtype="float32")
            q_norm = q_vec / np.linalg.norm(q_vec)
            scores_z = normed_mem @ q_norm
            zero_top_scores.append(scores_z.max())
    zero_max = max(zero_top_scores) if zero_top_scores else 0
    relevant_min = relevant_top_scores[0] if relevant_top_scores else 1.0
    suggested = (zero_max + relevant_min) / 2
    print(f"\n   💡 Suggested threshold: {suggested:.4f}")
    print(f"      (midpoint between max irrelevant top-score {zero_max:.4f} and min relevant top-score {relevant_min:.4f})")
    print(f"{'═'*70}")

    print(f"\n{'─'*70}")
    print(f"📊 RESULTS for {model_name}")
    print(f"   Encode speed:      {avg_encode_ms:.1f} ms/text (total: {encode_time:.3f}s)")
    print(f"   Query speed:       {avg_query_ms:.1f} ms/query (total: {total_query_time:.3f}s)")
    print(f"   Avg Recall:        {avg_recall:.1f}% ({scored_queries} scored queries)")
    print(f"   Avg Precision:     {avg_precision:.1f}%")
    print(f"   Zero-match reject: {zero_match_acc:.0f}% ({zero_match_correct}/{zero_match_queries} correctly ignored)")
    print(f"   Load time:         {load_time:.2f}s")
    print(f"{'─'*70}")

    return {
        "model": model_name,
        "encode_ms": avg_encode_ms,
        "encode_total_s": encode_time,
        "query_ms": avg_query_ms,
        "query_total_s": total_query_time,
        "recall": avg_recall,
        "precision": avg_precision,
        "zero_match_acc": zero_match_acc,
        "scored_queries": scored_queries,
        "zero_match_queries": zero_match_queries,
        "load_s": load_time,
    }


def save_results(results: list[dict], console_log: str = "") -> None:
    lines = []
    if console_log:
        lines.append("=" * 90)
        lines.append("FULL CONSOLE OUTPUT")
        lines.append("=" * 90)
        lines.append(console_log.rstrip("\n"))
        lines.append("")
        lines.append("=" * 90)
        lines.append("SUMMARY")
        lines.append("=" * 90)
        lines.append("")
    lines.append("=" * 90)
    lines.append("SABLE EMBEDDING MODEL STRESS TEST RESULTS")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Memory entries: {len(MEMORY_ENTRIES)} | Test queries: {len(TEST_QUERIES)}")
    lines.append(f"Query style: real verbatim + big ambiguous multi-topic + zero-match rejection")
    lines.append(f"Scoring: variable-length expected sets (1-7 relevant), recall@K + precision + zero-match")
    lines.append("=" * 90)
    lines.append("")

    # Summary table
    lines.append(f"{'Model':<40} {'Enc ms':>7} {'Qry ms':>7} {'Recall%':>8} {'Prec%':>7} {'ZeroM%':>7} {'Load':>6}")
    lines.append(f"{'─'*40} {'─'*7} {'─'*7} {'─'*8} {'─'*7} {'─'*7} {'─'*6}")
    for r in sorted(results, key=lambda x: -x["recall"]):
        name = r["model"].split("/")[-1][:39]
        lines.append(
            f"{name:<40} {r['encode_ms']:>7.1f} {r['query_ms']:>7.1f} "
            f"{r['recall']:>7.1f}% {r['precision']:>6.1f}% "
            f"{r['zero_match_acc']:>6.0f}% {r['load_s']:>5.2f}s"
        )
    lines.append("")

    # Detailed per-model breakdown
    for r in sorted(results, key=lambda x: -x["recall"]):
        lines.append("-" * 90)
        lines.append(f"MODEL: {r['model']}")
        lines.append(f"  Encode: {r['encode_ms']:.1f} ms/text avg | {r['encode_total_s']:.3f}s total for {len(MEMORY_ENTRIES)} texts")
        lines.append(f"  Query:  {r['query_ms']:.1f} ms/query avg | {r['query_total_s']:.3f}s total for {len(TEST_QUERIES)} queries")
        lines.append(f"  Recall:    {r['recall']:.1f}% avg across {r['scored_queries']} scored queries")
        lines.append(f"  Precision: {r['precision']:.1f}% avg")
        lines.append(f"  Zero-match rejection: {r['zero_match_acc']:.0f}% ({r['zero_match_queries']} irrelevant queries)")
        lines.append(f"  Load time: {r['load_s']:.2f}s")
        lines.append("")

    lines.append("=" * 90)
    lines.append("END OF REPORT")
    lines.append("=" * 90)

    RESULTS_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 Results saved to {RESULTS_FILE}")


def save_live_results(results: list[dict], console_log: str = "") -> None:
    lines = []
    if console_log:
        lines.append("=" * 90)
        lines.append("FULL CONSOLE OUTPUT")
        lines.append("=" * 90)
        lines.append(console_log.rstrip("\n"))
        lines.append("")
        lines.append("=" * 90)
        lines.append("SUMMARY")
        lines.append("=" * 90)
        lines.append("")
    lines.append("=" * 90)
    lines.append("SABLE LIVE EMBEDDING BENCHMARK RESULTS")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("Mode: LIVE (real Memory.json + real user prompts from sable.db)")
    lines.append("=" * 90)
    lines.append("")
    hdr = f"{'Model':<40} {'Enc ms':>7} {'Qry ms':>7} {'Thresh':>7} {'Top1 Med':>9} {'Load':>6}"
    lines.append(hdr)
    lines.append(f"{'─'*40} {'─'*7} {'─'*7} {'─'*7} {'─'*9} {'─'*6}")
    for r in sorted(results, key=lambda x: -x["top1_median"]):
        name = r["model"].split("/")[-1][:39]
        row = f"{name:<40} {r['encode_ms']:>7.1f} {r['query_ms']:>7.1f} "
        row += f"{r['suggested_threshold']:>7.3f} {r['top1_median']:>9.4f} {r['load_s']:>5.2f}s"
        lines.append(row)
    lines.append("")
    for r in sorted(results, key=lambda x: -x["top1_median"]):
        lines.append("-" * 90)
        lines.append(f"MODEL: {r['model']}")
        mc = r['memory_count']
        pc = r['prompt_count']
        tk = r['top_k']
        lines.append(f"  Memory entries: {mc} | Prompts: {pc} | Top-K: {tk}")
        lines.append(f"  Encode: {r['encode_ms']:.1f} ms/text avg | {r['encode_total_s']:.3f}s total")
        lines.append(f"  Query:  {r['query_ms']:.1f} ms/query avg | {r['query_total_s']:.3f}s total")
        t1min = r['top1_min']
        t1med = r['top1_median']
        t1max = r['top1_max']
        lines.append(f"  Top-1 scores:  min={t1min:.4f}  median={t1med:.4f}  max={t1max:.4f}")
        tkmin = r['topk_min']
        tkmed = r['topk_median']
        tkmax = r['topk_max']
        lines.append(f"  Top-{tk} scores: min={tkmin:.4f}  median={tkmed:.4f}  max={tkmax:.4f}")
        lines.append(f"  Suggested threshold: {r['suggested_threshold']:.3f}")
        lines.append(f"  Load time: {r['load_s']:.2f}s")
        lines.append("")
    lines.append("=" * 90)
    lines.append("END OF LIVE REPORT")
    lines.append("=" * 90)
    RESULTS_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 Live results saved to {RESULTS_FILE}")



def main() -> None:
    parser = argparse.ArgumentParser(description="Embedding model stress test")
    parser.add_argument("--model", default=None, help="Single model name to test")
    parser.add_argument("--all", action="store_true", help="Benchmark all recommended models")
    parser.add_argument(
        "--live", action="store_true",
        help="Live mode: last 50 real prompts vs real Memory.json with raw scores",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Top-K results to show in live mode (default 5)")
    args = parser.parse_args()

    # Tee stdout so every print() is captured for the output file
    tee = TeeWriter(sys.stdout)
    sys.stdout = tee
    try:
        _run(args)
    finally:
        sys.stdout = tee._original

    console_log = tee.getvalue()

    # Re-inject the captured log into whichever save function was called
    # We detect which file was written by checking if it already exists with summary-only content
    # Simpler: just re-save with the full log appended. The save functions are idempotent on RESULTS_FILE.
    # But we need to know which mode ran. Store a flag via a module-level variable.
    global _LAST_SAVE_MODE
    if _LAST_SAVE_MODE == "live":
        save_live_results(_LAST_RESULTS, console_log)
    elif _LAST_SAVE_MODE == "benchmark":
        save_results(_LAST_RESULTS, console_log)


_LAST_SAVE_MODE: str = ""
_LAST_RESULTS: list[dict] = []


def _run(args: argparse.Namespace) -> None:
    global _LAST_SAVE_MODE, _LAST_RESULTS

    if args.live:
        models = [args.model] if args.model else ALL_MODELS
        live_results: list[dict] = []
        for m in models:
            r = benchmark_live(m, top_k=args.top_k)
            if r:
                live_results.append(r)
        if live_results:
            _LAST_SAVE_MODE = "live"
            _LAST_RESULTS = live_results
            save_live_results(live_results)
        return

    if args.all:
        models = ALL_MODELS
    elif args.model:
        models = [args.model]
    else:
        models = ALL_MODELS

    results = []
    for m in models:
        r = benchmark_model(m)
        if r:
            results.append(r)

    if results:
        _LAST_SAVE_MODE = "benchmark"
        _LAST_RESULTS = results
        save_results(results)

        if len(results) > 1:
            print(f"\n{'='*70}")
            print("🏆 COMPARISON SUMMARY")
            print(f"{'='*70}")
            print(f"{'Model':<40} {'Enc ms':>7} {'Qry ms':>7} {'Recall':>7} {'Prec':>6} {'ZeroM':>6} {'Load':>6}")
            print(f"{'─'*40} {'─'*7} {'─'*7} {'─'*7} {'─'*6} {'─'*6} {'─'*6}")
            for r in sorted(results, key=lambda x: -x["recall"]):
                name = r["model"].split("/")[-1][:39]
                print(f"{name:<40} {r['encode_ms']:>7.1f} {r['query_ms']:>7.1f} {r['recall']:>6.1f}% {r['precision']:>5.1f}% {r['zero_match_acc']:>5.0f}% {r['load_s']:>5.2f}s")


if __name__ == "__main__":
    main()
