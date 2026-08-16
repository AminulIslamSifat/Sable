    /* =========================================================================
       Streaming TTS Player — sentence-chunked parallel synthesis + sequential playback
       Splits text into sentences, fetches audio for chunk N+1 while chunk N plays.
       ========================================================================= */
    class TTSStreamPlayer {
      constructor(onStateChange) {
        this.onStateChange = onStateChange; // (state: 'loading'|'playing'|'stopped') => void
        this.queue = [];       // Audio objects waiting to play
        this.playing = null;   // currently playing Audio
        this.stopped = false;
        this.abortCtrl = new AbortController(); // cancels in-flight fetches
      }

      static stripMarkdown(text) {
        // Remove common markdown artifacts that TTS can't speak
        return text
          .replace(/^#{1,6}\s+/gm, "")           // headers
          .replace(/^\*{3,}$|^-{3,}$|^_{3,}$/gm, "") // horizontal rules
          .replace(/\*\*(.+?)\*\*/g, "$1")       // bold
          .replace(/\*(.+?)\*/g, "$1")           // italic
          .replace(/~~(.+?)~~/g, "$1")           // strikethrough
          .replace(/==(.+?)==/g, "$1")           // highlight
          .replace(/`(.+?)`/g, "$1")             // inline code
          .replace(/^>\s*/gm, "")                // blockquotes
          .replace(/^\s*[-*+]\s+/gm, "")         // unordered list markers
          .replace(/^\s*\d+\.\s+/gm, "")         // ordered list markers
          .replace(/\[(.+?)\]\(.+?\)/g, "$1")   // links → keep text
          .replace(/\p{Emoji_Presentation}/gu, "") // strip emojis (TTS can't speak them)
          .replace(/\n{2,}/g, "\n")              // collapse blank lines
          .trim();
      }

      static splitSentences(text) {
        // Clean markdown first so TTS only sees speakable text
        const clean = TTSStreamPlayer.stripMarkdown(text);
        if (!clean) return [];
        // Split on sentence-ending punctuation followed by whitespace/newline or end-of-string.
        // Also splits on double-newlines (paragraph breaks) as natural pause points.
        const raw = clean.match(/[^.!?…\n]+[.!?…]+[\s]?|[^.!?…\n]+(?=\n)|[^.!?…\n]+$/g);
        if (!raw) return [clean];
        // Merge very short fragments (< 15 chars) with next chunk to avoid tiny audio blips
        const merged = [];
        let buf = "";
        for (const s of raw) {
          const trimmed = s.trim();
          if (!trimmed) continue;
          buf += (buf ? " " : "") + trimmed;
          if (buf.length >= 15 || s === raw[raw.length - 1]) {
            merged.push(buf);
            buf = "";
          }
        }
        if (buf.trim()) merged.push(buf.trim());
        return merged.filter(s => s.length > 0);
      }

      async _fetchChunk(text) {
        const res = await fetch("/api/tts/synthesize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
          signal: this.abortCtrl.signal,
        });
        if (!res.ok) throw new Error("TTS chunk failed");
        const blob = await res.blob();
        return new Audio(URL.createObjectURL(blob));
      }

      async _playNext() {
        if (this.stopped) return;
        if (this.queue.length === 0) {
          if (!this.stopped) this.onStateChange("stopped");
          return;
        }
        const audio = this.queue.shift();
        this.playing = audio;
        audio.onended = () => {
          if (this.stopped) return;
          this.playing = null;
          URL.revokeObjectURL(audio.src);
          this._playNext();
        };
        audio.onerror = () => {
          if (this.stopped) return;
          this.playing = null;
          this._playNext();
        };
        try { await audio.play(); } catch {
          if (!this.stopped) this._playNext();
        }
      }

      async play(text) {
        // Hard global guard: if any TTS is already active, refuse to play
        if (_ttsActive && _activeTTS.player !== this) {
          console.warn("[TTS] Blocked: another TTS session is active");
          return;
        }
        // Note: removed this.stop() call — it fired onStateChange("stopped")
        // which reset _ttsActive before playback even started. We always create
        // a fresh player instance so there's nothing to clean up.
        this.stopped = false;
        this.abortCtrl = new AbortController();
        const chunks = TTSStreamPlayer.splitSentences(text);
        if (chunks.length === 0) return;

        this.onStateChange("loading");

        // Fetch first two chunks concurrently to eliminate gap between sentence 1 and 2
        try {
          const initialFetches = [this._fetchChunk(chunks[0])];
          if (chunks.length > 1) initialFetches.push(this._fetchChunk(chunks[1]));
          const initialAudios = await Promise.all(initialFetches);

          for (const audio of initialAudios) {
            if (this.stopped) { URL.revokeObjectURL(audio.src); continue; }
            this.queue.push(audio);
          }
          if (this.stopped) return;

          this.onStateChange("playing");
          this._playNext();

          // Pipeline: pre-fetch remaining chunks in parallel (max 2 concurrent)
          const remaining = chunks.slice(2);
          let idx = 0;
          const prefetch = async () => {
            while (idx < remaining.length && !this.stopped) {
              const i = idx++;
              try {
                const audio = await this._fetchChunk(remaining[i]);
                if (!this.stopped) {
                  this.queue.push(audio);
                  if (!this.playing) this._playNext();
                } else {
                  URL.revokeObjectURL(audio.src);
                }
              } catch (err) {
                if (err.name === "AbortError") return; // clean cancellation
                if (!this.stopped) {
                  console.warn(`TTS chunk ${i} failed:`, err.message);
                }
              }
            }
          };
          // Run up to 2 parallel prefetchers (fire-and-forget)
          prefetch();
          prefetch();
        } catch (e) {
          if (e.name === "AbortError") return;
          if (!this.stopped) {
            showToast("TTS error: " + e.message, "error");
            this.onStateChange("stopped");
          }
        }
      }

      stop() {
        this.stopped = true;
        this.abortCtrl.abort(); // cancel all in-flight fetches
        if (this.playing) {
          this.playing.onended = null;  // kill chain — no more _playNext
          this.playing.onerror = null;
          this.playing.pause();
          this.playing.currentTime = 0;
          try { URL.revokeObjectURL(this.playing.src); } catch {}
          this.playing = null;
        }
        for (const a of this.queue) {
          try { URL.revokeObjectURL(a.src); } catch {}
        }
        this.queue = [];
        this.onStateChange("stopped");
      }
    }

    // Global TTS lock — only one message can play at a time across all buttons
    // Generation counter: incremented on every stop/start so stale callbacks self-invalidate
    let _ttsGeneration = 0;
    let _ttsActive = false; // true while TTS is running OR cooling down after stop
    let _ttsStopping = false; // true when stopGlobalTTS() initiated the stop (vs natural end)
    let _ttsLastAction = 0; // timestamp of last start/stop action (ms)
    const TTS_DEBOUNCE_MS = 400; // minimum ms between any TTS actions
    const _activeTTS = { player: null, btn: null, gen: -1 };
    function stopGlobalTTS() {
      const prevGen = _ttsGeneration;
      _ttsGeneration++; // invalidate ALL pending callbacks from previous sessions
      _ttsStopping = true; // mark that WE initiated this stop
      console.log(`[TTS-DEBUG] stopGlobalTTS called | gen ${prevGen}→${_ttsGeneration} | _ttsActive=${_ttsActive}`);
      // CRITICAL: set _ttsLastAction BEFORE stopping so debounce blocks immediate re-clicks
      _ttsLastAction = Date.now();
      if (_activeTTS.player) {
        _activeTTS.player.stop();
        _activeTTS.player = null;
      }
      if (_activeTTS.btn) {
        _activeTTS.btn.innerHTML = '<i data-lucide="volume-2"></i>';
        _activeTTS.btn.title = "Read aloud";
        _activeTTS.btn.classList.remove("tts-playing");
        activateLucideIcons(_activeTTS.btn);
        _activeTTS.btn = null;
      }
      _activeTTS.gen = -1;
      // Keep _ttsActive true briefly so rapid clicks are blocked
      // onStateChange("stopped") will see _ttsStopping=true and skip _ttsActive reset
      setTimeout(() => {
        console.log(`[TTS-DEBUG] cooldown expired | _ttsActive ${_ttsActive}→false | gen=${_ttsGeneration}`);
        _ttsActive = false;
        _ttsStopping = false;
      }, TTS_DEBOUNCE_MS);
    }

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

