(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    body: document.body,
    status: $("status"),
    scriptText: $("script"),
    fontSize: $("font-size"),
    fontSizeLabel: $("font-size-label"),
    lineHeight: $("line-height"),
    lineHeightLabel: $("line-height-label"),
    autoSpeed: $("auto-speed"),
    autoSpeedLabel: $("auto-speed-label"),
    btnLoad: $("btn-load"),
    btnStart: $("btn-start"),
    btnAuto: $("btn-auto"),
    btnPause: $("btn-pause"),
    btnReset: $("btn-reset"),
    btnFullscreen: $("btn-fullscreen"),
    reader: document.querySelector(".reader"),
    scriptView: $("script-view"),
    overlayMode: $("overlay-mode"),
    overlayStatus: $("overlay-status"),
    overlayLatency: $("overlay-latency"),
    overlayConf: $("overlay-conf"),
    overlaySpeed: $("overlay-speed"),
  };

  // ===== State =====
  const state = {
    ws: null,
    /**
     * Parsed segments: each is either
     *   { type: "word", text: "Hola", idx: 0 }   — speakable token
     *   { type: "directive", text: "[Mostrar ...]" } — stage direction
     */
    segments: [],
    /** Only speakable word texts, for backend sync */
    tokens: [],
    currentIdx: 0,
    listening: false,
    autoScrolling: false,
    autoScrollTimer: null,
    autoSpeed: 3,       // words per second
    reconnectDelay: 1000,
    mode: "idle",       // "idle" | "voice" | "auto"
    scriptLoaded: false,
  };

  // ===== Regex to detect [directives] =====
  // Matches text enclosed in square brackets, possibly spanning multiple words
  const DIRECTIVE_RE = /\[([^\]]*)\]/g;

  /**
   * Parse raw script text into segments.
   * Returns { segments: [...], tokens: string[] }
   *
   * Segments are ordered blocks:
   *   { type: "directive", text: "[Show on screen: ...]" }
   *   { type: "word", text: "Hola", idx: 0 }
   *
   * tokens[] contains only speakable words (for backend matching).
   */
  function parseScript(rawText) {
    const segments = [];
    const tokens = [];
    let wordIdx = 0;
    let lastEnd = 0;

    // Find all directive blocks
    let match;
    DIRECTIVE_RE.lastIndex = 0;
    while ((match = DIRECTIVE_RE.exec(rawText)) !== null) {
      // Text before this directive = speakable words
      const before = rawText.slice(lastEnd, match.index);
      const words = before.split(/\s+/).filter(Boolean);
      for (const w of words) {
        segments.push({ type: "word", text: w, idx: wordIdx });
        tokens.push(w);
        wordIdx++;
      }
      // The directive itself
      segments.push({ type: "directive", text: match[0] });
      lastEnd = match.index + match[0].length;
    }

    // Remaining text after last directive
    const remaining = rawText.slice(lastEnd);
    const words = remaining.split(/\s+/).filter(Boolean);
    for (const w of words) {
      segments.push({ type: "word", text: w, idx: wordIdx });
      tokens.push(w);
      wordIdx++;
    }

    return { segments, tokens };
  }

  // ===== WebSocket =====
  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    state.ws = new WebSocket(url);

    state.ws.addEventListener("open", () => {
      setStatus("Conectado. Carga un guion.");
      state.reconnectDelay = 1000;
    });

    state.ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleMessage(msg);
    });

    state.ws.addEventListener("close", () => {
      setStatus("Conexión perdida, reintentando...");
      setListening(false);
      setTimeout(connect, state.reconnectDelay);
      state.reconnectDelay = Math.min(state.reconnectDelay * 2, 30000);
    });

    state.ws.addEventListener("error", () => {
      // close se dispara después
    });
  }

  function send(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
    }
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "status":
        setStatus(msg.message || msg.state);
        if (msg.state === "listening") setListening(true);
        if (msg.state === "paused" || msg.state === "ready") setListening(false);
        if (msg.state === "ready" && /Guion cargado/.test(msg.message || "")) {
          state.scriptLoaded = true;
          updateButtons();
        }
        break;
      case "scroll":
        if (state.mode === "voice") {
          applyScroll(msg.current_idx);
          els.overlayLatency.textContent = `${msg.latency_ms} ms`;
          els.overlayConf.textContent = `conf ${msg.confidence.toFixed(2)}`;
        }
        break;
      case "transcript":
        // reservado para debug
        break;
    }
  }

  // ===== UI =====
  function setStatus(text) { els.status.textContent = text; }

  function setListening(on) {
    state.listening = on;
    els.overlayStatus.style.color = on ? "var(--accent)" : "#888";
    updateButtons();
  }

  function updateButtons() {
    const loaded = state.scriptLoaded;
    const active = state.mode !== "idle";
    els.btnStart.disabled = !loaded || active;
    els.btnAuto.disabled = !loaded || active;
    els.btnPause.disabled = !active;
    els.btnReset.disabled = !loaded;
  }

  function renderScript() {
    els.scriptView.innerHTML = "";
    const frag = document.createDocumentFragment();

    state.segments.forEach((seg) => {
      if (seg.type === "directive") {
        const div = document.createElement("div");
        div.className = "directive upcoming";
        div.textContent = seg.text;
        frag.appendChild(div);
      } else {
        const span = document.createElement("span");
        span.className = "w upcoming";
        span.dataset.idx = seg.idx;
        span.textContent = seg.text + " ";
        frag.appendChild(span);
      }
    });

    els.scriptView.appendChild(frag);
    applyScroll(0);
  }

  function applyScroll(idx) {
    state.currentIdx = idx;
    const children = els.scriptView.children;
    if (!children.length) return;

    let targetEl = null;

    for (const el of children) {
      if (el.classList.contains("directive")) {
        // Directives: mark read/current/upcoming based on surrounding word indices
        // Find the next word element after this directive
        const nextWord = el.nextElementSibling;
        const prevWord = el.previousElementSibling;
        const nextIdx = nextWord && nextWord.dataset.idx !== undefined
          ? parseInt(nextWord.dataset.idx) : Infinity;
        const prevIdx = prevWord && prevWord.dataset.idx !== undefined
          ? parseInt(prevWord.dataset.idx) : -1;

        el.classList.remove("read", "current", "upcoming");
        if (idx > nextIdx || (nextIdx === Infinity && idx >= state.tokens.length)) {
          el.classList.add("read");
        } else if (idx > prevIdx && idx <= nextIdx) {
          el.classList.add("current");
          if (!targetEl) targetEl = el;
        } else {
          el.classList.add("upcoming");
        }
      } else {
        // Word span
        const i = parseInt(el.dataset.idx);
        el.classList.remove("read", "current", "upcoming");
        if (i < idx) el.classList.add("read");
        else if (i === idx) {
          el.classList.add("current");
          targetEl = el;
        }
        else el.classList.add("upcoming");
      }
    }

    if (targetEl) {
      const isActive = state.listening || state.autoScrolling;
      targetEl.scrollIntoView({
        behavior: isActive ? "smooth" : "auto",
        block: "center",
        inline: "nearest",
      });
    }
  }

  function enterReaderMode() {
    els.body.classList.remove("mode-setup");
    els.body.classList.add("mode-reader");
  }
  function exitReaderMode() {
    els.body.classList.remove("mode-reader");
    els.body.classList.add("mode-setup");
  }

  // ===== Auto-scroll =====
  function startAutoScroll() {
    if (state.mode !== "idle") return;
    if (!state.scriptLoaded) return;

    state.mode = "auto";
    state.autoScrolling = true;
    enterReaderMode();
    updateOverlayMode();
    updateButtons();
    scheduleNextAutoWord();
  }

  function scheduleNextAutoWord() {
    if (!state.autoScrolling) return;
    const intervalMs = 1000 / state.autoSpeed;
    state.autoScrollTimer = setTimeout(() => {
      if (!state.autoScrolling) return;
      const next = state.currentIdx + 1;
      if (next > state.tokens.length) {
        stopAutoScroll();
        return;
      }
      applyScroll(next);
      scheduleNextAutoWord();
    }, intervalMs);
  }

  function stopAutoScroll() {
    state.autoScrolling = false;
    if (state.autoScrollTimer) {
      clearTimeout(state.autoScrollTimer);
      state.autoScrollTimer = null;
    }
    state.mode = "idle";
    updateOverlayMode();
    updateButtons();
  }

  function updateOverlayMode() {
    if (state.mode === "voice") {
      els.overlayMode.textContent = "🎙 VOZ";
      els.overlayMode.style.color = "var(--accent)";
      els.overlaySpeed.classList.add("hidden");
      els.overlayLatency.classList.remove("hidden");
      els.overlayConf.classList.remove("hidden");
    } else if (state.mode === "auto") {
      els.overlayMode.textContent = "▶ AUTO";
      els.overlayMode.style.color = "var(--directive)";
      els.overlaySpeed.classList.remove("hidden");
      els.overlaySpeed.textContent = `vel ${state.autoSpeed.toFixed(1)}`;
      els.overlayLatency.classList.add("hidden");
      els.overlayConf.classList.add("hidden");
    } else {
      els.overlayMode.textContent = "--";
      els.overlayMode.style.color = "#555";
      els.overlaySpeed.classList.add("hidden");
      els.overlayLatency.classList.remove("hidden");
      els.overlayConf.classList.remove("hidden");
    }
  }

  // ===== Eventos UI =====
  els.fontSize.addEventListener("input", () => {
    const v = els.fontSize.value;
    els.scriptView.style.fontSize = `${v}px`;
    els.fontSizeLabel.textContent = v;
  });
  els.lineHeight.addEventListener("input", () => {
    const v = els.lineHeight.value;
    els.scriptView.style.lineHeight = v;
    els.lineHeightLabel.textContent = v;
  });
  els.autoSpeed.addEventListener("input", () => {
    const v = parseFloat(els.autoSpeed.value);
    state.autoSpeed = v;
    els.autoSpeedLabel.textContent = v;
    if (state.autoScrolling) {
      els.overlaySpeed.textContent = `vel ${v.toFixed(1)}`;
    }
  });

  els.btnLoad.addEventListener("click", () => {
    const text = els.scriptText.value.trim();
    if (!text) {
      setStatus("Pega un guion primero.");
      return;
    }
    const parsed = parseScript(text);
    state.segments = parsed.segments;
    state.tokens = parsed.tokens;
    state.scriptLoaded = false;
    renderScript();
    // Send only the speakable text (without directives) to backend
    const speakableText = state.tokens.join(" ");
    send({ action: "load_script", text: speakableText });
  });

  els.btnStart.addEventListener("click", () => {
    state.mode = "voice";
    enterReaderMode();
    updateOverlayMode();
    updateButtons();
    send({ action: "start" });
  });

  els.btnAuto.addEventListener("click", () => {
    startAutoScroll();
  });

  els.btnPause.addEventListener("click", () => {
    if (state.mode === "voice") {
      send({ action: "pause" });
      state.mode = "idle";
      updateOverlayMode();
      updateButtons();
    } else if (state.mode === "auto") {
      stopAutoScroll();
    }
  });

  els.btnReset.addEventListener("click", () => {
    if (state.mode === "auto") stopAutoScroll();
    if (state.mode === "voice") {
      send({ action: "pause" });
      state.mode = "idle";
      updateOverlayMode();
    }
    send({ action: "reset" });
    applyScroll(0);
    updateButtons();
  });

  els.btnFullscreen.addEventListener("click", () => {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      document.documentElement.requestFullscreen();
    }
  });

  // ===== Atajos =====
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "TEXTAREA") return;

    if (e.code === "Space") {
      e.preventDefault();
      if (state.mode !== "idle") {
        // Pause whatever is active
        if (state.mode === "voice") {
          send({ action: "pause" });
          state.mode = "idle";
          updateOverlayMode();
          updateButtons();
        } else if (state.mode === "auto") {
          stopAutoScroll();
        }
      } else if (state.scriptLoaded) {
        // Default: start voice mode
        state.mode = "voice";
        enterReaderMode();
        updateOverlayMode();
        updateButtons();
        send({ action: "start" });
      }
    } else if (e.key === "a" || e.key === "A") {
      if (state.mode === "idle" && state.scriptLoaded) {
        startAutoScroll();
      } else if (state.mode === "auto") {
        stopAutoScroll();
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const newSpeed = Math.min(10, state.autoSpeed + 0.5);
      state.autoSpeed = newSpeed;
      els.autoSpeed.value = newSpeed;
      els.autoSpeedLabel.textContent = newSpeed;
      if (state.autoScrolling) {
        els.overlaySpeed.textContent = `vel ${newSpeed.toFixed(1)}`;
      }
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      const newSpeed = Math.max(1, state.autoSpeed - 0.5);
      state.autoSpeed = newSpeed;
      els.autoSpeed.value = newSpeed;
      els.autoSpeedLabel.textContent = newSpeed;
      if (state.autoScrolling) {
        els.overlaySpeed.textContent = `vel ${newSpeed.toFixed(1)}`;
      }
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      const delta = -(e.shiftKey ? 15 : 5);
      const newIdx = Math.max(0, state.currentIdx + delta);
      send({ action: "step", delta });
      applyScroll(newIdx);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      const delta = e.shiftKey ? 15 : 5;
      const newIdx = Math.min(state.tokens.length, state.currentIdx + delta);
      send({ action: "step", delta });
      applyScroll(newIdx);
    } else if (e.key === "r" || e.key === "R") {
      if (state.mode === "auto") stopAutoScroll();
      if (state.mode === "voice") {
        send({ action: "pause" });
        state.mode = "idle";
        updateOverlayMode();
      }
      send({ action: "reset" });
      applyScroll(0);
      updateButtons();
    } else if (e.key === "Escape") {
      if (state.mode === "voice") {
        send({ action: "pause" });
        state.mode = "idle";
        updateOverlayMode();
      }
      if (state.mode === "auto") stopAutoScroll();
      exitReaderMode();
      updateButtons();
    } else if (e.key === "F11") {
      e.preventDefault();
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen();
    }
  });

  // Inicialización
  els.scriptView.style.fontSize = `${els.fontSize.value}px`;
  els.scriptView.style.lineHeight = els.lineHeight.value;
  state.autoSpeed = parseFloat(els.autoSpeed.value);
  setStatus("Conectando al backend local...");
  connect();
})();
