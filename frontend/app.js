(() => {
  "use strict";

  // ===== Núcleo puro (sin DOM ni red) =====
  const MAX_SCRIPT_CHARS = 2000000;   // igual que tools/script_parser.py
  const JUMP_INSTANT = 30;            // saltos de más palabras se desplazan sin animación
  const START_TIMEOUT_MS = 6000;      // espera máxima de status: listening tras start
  const BACKOFF_MIN_MS = 500;
  const BACKOFF_MAX_MS = 8000;
  const CLOSE_REPLACED = 4000;        // el servidor cerró esta conexión por otra más nueva

  /** Recorta un índice a [0, total]. */
  function clampIdx(idx, total) {
    const n = Number.isFinite(idx) ? Math.trunc(idx) : 0;
    return Math.max(0, Math.min(total, n));
  }

  /** Estado de una palabra respecto del índice actual. */
  function wordState(i, cur) {
    if (i < cur) return "read";
    return i === cur ? "current" : "upcoming";
  }

  /**
   * Estado de una directiva por su rango [start, end] (nunca por sus vecinos en el DOM).
   * `start` es la primera palabra que la sigue y `end` es at_idx tras saltar los
   * tokens sin unidades (un "-" de viñeta, "—", "..."). El matcher, el
   * Auto-scroll y las flechas saltan esos tokens, así que la posición pasa de
   * start a end sin detenerse (salvo en 0 tras cargar o reiniciar): la
   * directiva es "actual" mientras current_idx esté en [start, end].
   */
  function directiveState(start, end, cur) {
    if (cur > end) return "read";
    return cur >= start ? "current" : "upcoming";
  }

  /** Primera posición k con arr[k] >= value (arr ordenado de forma no decreciente). */
  function lowerBound(arr, value) {
    let lo = 0;
    let hi = arr.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (arr[mid] < value) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  /**
   * Recorre solo lo que cambia al pasar de `prev` a `cur`: las palabras del
   * rango [min, max] y las directivas cuyo rango [start, end] toca ese rango.
   * `starts` y `ends` son no decrecientes, así que basta empezar en la primera
   * directiva con end >= min y parar en la primera con start > max.
   */
  function forEachDirty(prev, cur, total, starts, ends, onWord, onDirective) {
    const lo = Math.min(prev, cur);
    const hi = Math.max(prev, cur);
    const last = Math.min(hi, total - 1);
    for (let i = lo; i <= last; i++) onWord(i);
    for (let k = lowerBound(ends, lo); k < starts.length && starts[k] <= hi; k++) onDirective(k);
  }

  /** Avanza desde idx saltando los tokens sin unidades (skip); como máximo hasta total. */
  function skipForward(idx, skips, total) {
    let i = idx;
    while (i < total && skips[i]) i++;
    return i;
  }

  /**
   * Eco de un reposicionamiento (set_position o start). Un scroll de voz
   * siempre trae matched_word; stt_ms y lag_ms pueden valer 0 con el STT falso.
   */
  function isEchoScroll(msg) {
    return (msg.matched_word === null || msg.matched_word === undefined)
      && msg.stt_ms === 0 && msg.lag_ms === 0;
  }

  /**
   * Cuenta una respuesta de voz (listening, paused o un error de start).
   * El servidor responde cada start y cada pause una sola vez y en orden, pero
   * sin época: solo la respuesta que deja en cero las peticiones pendientes
   * refleja la última acción del usuario. `answers` indica si el mensaje
   * responde a una petición (un aviso espontáneo no descuenta).
   */
  function settleVoiceReply(pending, answers) {
    const left = answers && pending > 0 ? pending - 1 : pending;
    return { pending: left, current: left === 0 };
  }

  function nextBackoff(delay) {
    return Math.min(delay * 2, BACKOFF_MAX_MS);
  }

  /** Un mensaje es obsoleto si su época es menor que el último seq enviado. */
  function isStale(epoch, lastSeq) {
    return Number.isInteger(epoch) && epoch < lastSeq;
  }

  /**
   * Qué hacer con el primer `state` de una conexión.
   * local: { hadLocal, scriptId, idx, pendingText }; hadLocal indica si había
   * guion dibujado al abrir la conexión (el cliente manda sobre la posición).
   */
  function planHello(local, server) {
    if (local.pendingText !== null) return "load_pending";
    if (local.hadLocal) {
      if (server.script_id !== local.scriptId) return "resend_script";
      if (server.current_idx !== local.idx) return "send_position";
      return "none";
    }
    if (server.script_id) {
      return local.scriptId === server.script_id ? "adopt_position" : "await_script";
    }
    return "none";
  }
  // ===== Fin del núcleo puro =====

  const $ = (id) => document.getElementById(id);

  const els = {
    body: document.body,
    status: $("status"),
    notices: $("notices"),
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
    btnStealth: $("btn-stealth"),
    btnTopmost: $("btn-topmost"),
    scriptView: $("script-view"),
    overlayMode: $("overlay-mode"),
    overlayStatus: $("overlay-status"),
    overlayConn: $("overlay-conn"),
    overlayModel: $("overlay-model"),
    overlayStealth: $("overlay-stealth"),
    overlayTopmost: $("overlay-topmost"),
    overlayLag: $("overlay-lag"),
    overlayStt: $("overlay-stt"),
    overlayConf: $("overlay-conf"),
    overlaySpeed: $("overlay-speed"),
  };

  // ===== Estado =====
  const state = {
    // Conexión: connecting | open | closed | replaced
    ws: null,
    conn: "connecting",
    everOpened: false,
    failNotified: false,     // ya se avisó del corte actual
    reconnectDelay: BACKOFF_MIN_MS,
    reconnectTimer: null,
    helloPhase: null,        // { hadLocal } hasta procesar el primer state de la conexión
    // Modelo: unknown | loading | ready | error
    model: "unknown",
    modelName: null,
    device: null,
    // Modo: idle | voice_pending | voice | auto
    mode: "idle",
    startTimer: null,
    startSeq: null,          // seq del último start enviado
    voiceReqs: 0,            // start y pause enviados que el servidor aún no respondió
    desync: false,
    // Épocas: seq monotónico; se ignoran los scroll con época menor que lastSentSeq
    seq: 0,
    lastSentSeq: 0,
    loadSeq: 0,              // seq del último load_script enviado
    loadKind: null,          // "user" | "resync" mientras se espera su script_loaded
    pendingText: null,       // guion pedido por el usuario y aún no confirmado
    restoreIdx: null,
    serverScriptId: null,
    serverIdx: 0,
    // Guion dibujado (solo desde script_loaded.segments)
    script: null,            // { id, text, total }
    wordEls: [],
    skips: [],
    dirEls: [],
    dirAts: [],              // inicio del rango de cada directiva (primera palabra que la sigue)
    dirEnds: [],             // fin del rango: at_idx tras saltar los tokens sin unidades
    currentIdx: 0,
    lastLineTop: null,       // offsetTop del renglón centrado por última vez
    recenterQueued: false,
    // Auto-scroll (local, no necesita backend)
    autoTimer: null,
    autoSpeed: 3,            // palabras por segundo
  };

  // ===== Avisos (barra fija arriba, visible en setup y en lectura) =====
  const NOTICE_MAX = 4;
  const NOTICE_TTL = { info: 4000, warning: 8000 };
  const noticeTimers = new WeakMap();

  /**
   * Muestra un aviso. Un aviso nuevo reemplaza al del mismo `channel`; los
   * de info y warning se ocultan solos y los de error quedan hasta un clic
   * o hasta que cambie el estado que los causó.
   */
  function notify(kind, text, opts = {}) {
    const channel = opts.channel || "";
    if (channel) clearNotices(channel);
    while (els.notices.children.length >= NOTICE_MAX) removeNotice(els.notices.firstElementChild);

    const el = document.createElement("div");
    el.className = `notice notice-${kind}`;
    if (kind === "error") el.setAttribute("role", "alert");
    el.dataset.channel = channel;

    const msg = document.createElement("span");
    msg.className = "notice-msg";
    msg.textContent = text;
    el.appendChild(msg);

    if (opts.action) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "notice-action";
      btn.textContent = opts.action.label;
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        removeNotice(el);
        opts.action.run();
      });
      el.appendChild(btn);
    } else {
      el.addEventListener("click", () => removeNotice(el));
    }

    const close = document.createElement("button");
    close.type = "button";
    close.className = "notice-close";
    close.setAttribute("aria-label", "Cerrar aviso");
    close.textContent = "×";
    close.addEventListener("click", (ev) => {
      ev.stopPropagation();
      removeNotice(el);
    });
    el.appendChild(close);

    const ttl = opts.sticky ? 0 : (opts.ttl || NOTICE_TTL[kind] || 0);
    if (ttl > 0) noticeTimers.set(el, setTimeout(() => removeNotice(el), ttl));
    els.notices.appendChild(el);
    return el;
  }

  function removeNotice(el) {
    if (!el || !el.parentNode) return;
    const t = noticeTimers.get(el);
    if (t) clearTimeout(t);
    el.remove();
  }

  function clearNotices(channel) {
    for (const el of Array.from(els.notices.children)) {
      if (el.dataset.channel === channel) removeNotice(el);
    }
  }

  function hasNotice(channel) {
    return Array.from(els.notices.children).some((el) => el.dataset.channel === channel);
  }

  function notifyOffline() {
    if (state.conn === "replaced") {
      notify("warning", "La app está abierta en otra ventana: retoma el control para sincronizar.", { channel: "offline" });
    } else {
      notify("warning", "Sin conexión con el backend: la posición y el guion se sincronizan al reconectar.", { channel: "offline" });
    }
  }

  // Canal de aviso por código de status (un aviso nuevo reemplaza al del mismo canal).
  const CODE_CHANNEL = {
    MIC_ERROR: "voice",
    MIC_LOST: "voice",
    NO_SCRIPT: "voice",
    MODEL_NOT_READY: "voice",
    MODEL_ERROR: "model",
    MODEL_MISSING: "model",
    CUDA_FALLBACK: "cuda",
    BAD_REQUEST: "server",
    INTERNAL: "server",
  };

  // ===== WebSocket =====
  function isOpen() {
    return !!state.ws && state.ws.readyState === WebSocket.OPEN;
  }

  function connect() {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
    if (state.ws && (state.ws.readyState === WebSocket.CONNECTING || isOpen())) return;

    state.conn = "connecting";
    updateUI();
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let ws;
    try {
      ws = new WebSocket(`${proto}//${location.host}/ws`);
    } catch (err) {
      console.error("No se pudo crear el WebSocket:", err);
      onClose(1006);
      return;
    }
    state.ws = ws;

    // Los eventos de un socket viejo (reemplazado) se ignoran.
    ws.addEventListener("open", () => {
      if (ws === state.ws) onOpen();
    });
    ws.addEventListener("message", (ev) => {
      if (ws === state.ws) onMessage(ev.data);
    });
    ws.addEventListener("close", (ev) => {
      if (ws !== state.ws) return;
      state.ws = null;
      onClose(ev.code);
    });
    ws.addEventListener("error", () => {
      // close se dispara después
    });
  }

  function onOpen() {
    const wasReconnect = state.everOpened;
    state.conn = "open";
    state.everOpened = true;
    state.failNotified = false;
    state.reconnectDelay = BACKOFF_MIN_MS;
    state.helloPhase = { hadLocal: !!state.script };
    state.voiceReqs = 0;     // conexión nueva: ninguna respuesta pendiente
    clearNotices("conn");
    clearNotices("offline");
    sendRaw({ action: "hello", client: "flow-local-web/2" });
    if (wasReconnect) notify("info", "Conexión restablecida.", { channel: "conn" });
    updateUI();
  }

  function onClose(code) {
    state.helloPhase = null;
    state.loadKind = null;   // un guion pendiente (pendingText) se reenvía al reconectar
    state.model = "unknown"; // se vuelve a leer con hello
    state.voiceReqs = 0;     // las respuestas de esta conexión ya no llegarán
    state.startSeq = null;
    // El Auto-scroll es local y sigue; la voz depende del backend.
    if (state.mode === "voice" || state.mode === "voice_pending") setMode("idle");

    if (code === CLOSE_REPLACED) {
      state.conn = "replaced";
      notify("error", "Abierto en otra ventana: esta ventana dejó de controlar el teleprompter.", {
        channel: "conn",
        sticky: true,
        action: { label: "Retomar control", run: takeControl },
      });
    } else {
      state.conn = "closed";
      // Un solo aviso por corte; los reintentos siguientes se ven en los indicadores.
      if (!state.failNotified) {
        state.failNotified = true;
        const text = state.everOpened
          ? "Conexión perdida. Reconectando…"
          : "No se pudo conectar con el backend local. Reintentando…";
        notify("warning", text, { channel: "conn", sticky: true });
      }
      state.reconnectTimer = setTimeout(connect, state.reconnectDelay);
      state.reconnectDelay = nextBackoff(state.reconnectDelay);
    }
    updateUI();
  }

  function takeControl() {
    clearNotices("conn");
    state.reconnectDelay = BACKOFF_MIN_MS;
    connect();
  }

  /** Envía sin avisar si no hay conexión (hello y pausas de seguridad). */
  function sendRaw(obj) {
    if (!isOpen()) return false;
    try {
      state.ws.send(JSON.stringify(obj));
      return true;
    } catch (err) {
      console.error("No se pudo enviar al backend:", err);
      return false;
    }
  }

  /** Envía y avisa al usuario si no hay conexión (no se encola). */
  function send(obj) {
    if (sendRaw(obj)) return true;
    notifyOffline();
    return false;
  }

  /**
   * Envía una acción con seq sin avisar si no hay conexión. El contador solo
   * avanza si el envío salió: al reconectar, hello resincroniza la posición.
   */
  function sendSeqRaw(obj) {
    const seq = state.seq + 1;
    if (!sendRaw({ ...obj, seq })) return false;
    state.seq = seq;
    state.lastSentSeq = seq;
    return true;
  }

  /** Envía una acción con seq (load_script, start, set_position) y avisa si no hay conexión. */
  function sendSeq(obj) {
    if (sendSeqRaw(obj)) return true;
    notifyOffline();
    return false;
  }

  /** Pide pausa (idempotente en el backend) y cuenta la respuesta que llegará. */
  function sendPause() {
    if (sendRaw({ action: "pause" })) state.voiceReqs++;
  }

  /**
   * Deja en el backend la posición local (al salir del Auto-scroll). Así un
   * `state` posterior (modelo listo, recarga, otra ventana) no trae una
   * posición vieja. Se omite con un guion del usuario pendiente (el índice es
   * del guion anterior) y durante hello (el plan de hello envía la posición).
   */
  function syncPosition() {
    if (!state.script || state.pendingText !== null || state.helloPhase) return;
    sendSeqRaw({ action: "set_position", idx: state.currentIdx });
  }

  /** Mantiene el contador monotónico tras recargar la página o retomar el control. */
  function adoptEpoch(epoch) {
    if (Number.isInteger(epoch) && epoch > state.seq) state.seq = epoch;
  }

  function sendLoad(text, kind) {
    if (!sendSeq({ action: "load_script", text })) return false;
    state.loadSeq = state.lastSentSeq;
    state.loadKind = kind;
    if (kind === "user") state.restoreIdx = null;
    return true;
  }

  /** Reenvía el guion dibujado y la posición local (el backend no lo tiene o tiene otro). */
  function resyncScript() {
    if (!state.script) return;
    if (typeof state.script.text !== "string") {
      notify("warning", "No se pudo reenviar el guion: vuelve a cargarlo.", { channel: "script" });
      return;
    }
    state.restoreIdx = state.currentIdx;
    if (sendLoad(state.script.text, "resync")) {
      sendSeq({ action: "set_position", idx: state.currentIdx });
    }
  }

  function onMessage(data) {
    let msg;
    try { msg = JSON.parse(data); } catch { return; }
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case "state": onState(msg); break;
      case "script_loaded": onScriptLoaded(msg); break;
      case "scroll": onScroll(msg); break;
      case "status": onStatus(msg); break;
      case "level": break;        // medidor de nivel: Fase 6
      case "transcript": break;   // solo depuración (TP_DEBUG)
      default: break;
    }
  }

  function onState(msg) {
    adoptEpoch(msg.epoch);
    if (msg.protocol !== undefined && msg.protocol !== 2) {
      notify("warning", `El backend habla el protocolo ${msg.protocol} y esta página espera el 2. Recarga la página.`, { channel: "proto", sticky: true });
    }
    if (typeof msg.model_name === "string") state.modelName = msg.model_name;
    if (typeof msg.device === "string") state.device = msg.device;
    setModel(msg.model);
    if (state.model === "error" && !hasNotice("model")) {
      notify("error", "El modelo de voz no está disponible. Revisa logs/teleprompter.log.", { channel: "model", sticky: true });
    }
    state.serverScriptId = msg.script_id || null;
    state.serverIdx = Number.isInteger(msg.current_idx) ? msg.current_idx : 0;

    const hp = state.helloPhase;
    if (hp) {
      state.helloPhase = null;
      const plan = planHello({
        hadLocal: hp.hadLocal,
        scriptId: state.script ? state.script.id : null,
        idx: state.currentIdx,
        pendingText: state.pendingText,
      }, msg);
      switch (plan) {
        case "load_pending": sendLoad(state.pendingText, "user"); break;
        case "resend_script": resyncScript(); break;
        case "send_position": sendSeq({ action: "set_position", idx: state.currentIdx }); break;
        case "adopt_position": applyIndex(state.serverIdx, { instant: true }); break;
        default: break;          // await_script (llega script_loaded) o none
      }
    } else if (state.script && msg.script_id === state.script.id
               && (state.mode === "voice" || state.mode === "voice_pending")
               && Number.isInteger(msg.current_idx) && !isStale(msg.epoch, state.lastSentSeq)) {
      // Fuera de la voz la posición es del cliente (Auto-scroll, flechas): un
      // state de modelo listo o con error no debe devolver el texto atrás.
      applyIndex(msg.current_idx);
    }

    // Con start o pause sin responder, `listening` puede ser de antes de ellos:
    // decide la respuesta pendiente.
    if (state.voiceReqs === 0) {
      if (msg.listening === false && state.mode === "voice") {
        setMode("idle");
      } else if (msg.listening === true && (state.mode === "idle" || state.mode === "auto")) {
        sendPause();   // micrófono abierto sin que la UI muestre VOZ
      }
    }
    updateUI();
  }

  function onScriptLoaded(msg) {
    adoptEpoch(msg.epoch);
    const hp = state.helloPhase;
    // Con guion local (o uno pendiente) se decide con el state de hello.
    if (hp && (hp.hadLocal || state.pendingText !== null)) return;

    let kind = null;         // null: guion adoptado del backend (página recargada)
    if (state.loadKind) {
      // Mientras esperamos nuestra carga, solo vale su respuesta.
      if (!(Number.isInteger(msg.epoch) && msg.epoch >= state.loadSeq)) return;
      kind = state.loadKind;
      state.loadKind = null;
      if (kind === "user") state.pendingText = null;
    } else if (state.script && msg.script_id === state.script.id) {
      return;                // ya dibujado
    }
    clearNotices("script");

    // Reenvío del mismo texto: los segmentos son idénticos, solo cambia el id.
    if (kind === "resync" && state.script && typeof msg.text === "string" && msg.text === state.script.text) {
      state.script.id = msg.script_id;
      state.restoreIdx = null;
      updateUI();
      return;
    }

    let idx = 0;
    if (kind === "resync") idx = state.restoreIdx !== null ? state.restoreIdx : state.currentIdx;
    else if (!kind && msg.script_id === state.serverScriptId) idx = state.serverIdx;
    state.restoreIdx = null;
    if (state.mode === "auto" && kind !== "resync") setMode("idle");

    renderScript(msg, idx);
    if (!els.scriptText.value.trim() && typeof msg.text === "string") els.scriptText.value = msg.text;
    const total = state.script.total;
    if (kind === "user") notify("info", `Guion cargado: ${total} palabras.`, { channel: "script" });
    else if (!kind) notify("info", `Guion recuperado del backend: ${total} palabras.`, { channel: "script" });
    updateUI();
  }

  function onScroll(msg) {
    if (state.helloPhase || !state.script) return;
    if (isStale(msg.epoch, state.lastSentSeq)) return;   // de antes del último reposicionamiento
    adoptEpoch(msg.epoch);
    if (!Number.isInteger(msg.current_idx)) return;
    if (state.mode === "auto") return;                   // el Auto-scroll manda en local
    // Solo el start con este seq adopta esta época: prueba de que el backend
    // escucha aunque su `listening` se haya contado como respuesta vieja.
    if (state.mode === "voice_pending" && state.startSeq !== null && msg.epoch === state.startSeq) {
      state.voiceReqs = 0;
      confirmListening();
    }
    const echo = isEchoScroll(msg);
    // Fuera de la voz solo valen los ecos: un scroll de voz en pausa es de
    // antes de pausar (o de un micrófono que aún no se cerró).
    if (!echo && state.mode !== "voice") return;
    if (!echo) showStats(msg);
    applyIndex(msg.current_idx);
    if (msg.resync === true) {
      state.desync = false;
      notify("info", "Posición resincronizada con la voz.", { channel: "voice" });
      updateOverlay();
    }
  }

  /** Descuenta una respuesta de voz; true si refleja la última petición (o no hay ninguna pendiente). */
  function takeVoiceReply(answers) {
    const r = settleVoiceReply(state.voiceReqs, answers);
    state.voiceReqs = r.pending;
    return r.current;
  }

  /** El backend escucha por el último start: la UI pasa a VOZ. */
  function confirmListening() {
    clearStartTimer();
    state.desync = false;
    clearNotices("voice");
    setMode("voice");
  }

  // Errores con los que el backend puede responder a un start o a un pause.
  const VOICE_REPLY_ERRORS = new Set(["NO_SCRIPT", "MODEL_NOT_READY", "MIC_ERROR", "BAD_REQUEST", "INTERNAL"]);

  function onStatus(msg) {
    const code = typeof msg.code === "string" ? msg.code : null;
    const text = typeof msg.message === "string" && msg.message ? msg.message : "";
    // Un BAD_REQUEST (o INTERNAL) con una carga pendiente es el fallo de esa
    // carga y reemplaza al aviso "Enviando guion…".
    const loadFailed = (code === "BAD_REQUEST" || code === "INTERNAL") && state.loadKind !== null;
    const channel = loadFailed ? "script" : (code && CODE_CHANNEL[code]) || "server";
    switch (msg.state) {
      case "loading":
        setModel("loading");
        break;
      case "ready":
        setModel("ready", text);
        break;
      case "listening":
        // Responde a un start anterior al último: la respuesta del último llega después.
        if (!takeVoiceReply(true)) break;
        if (state.mode === "voice_pending" || state.mode === "voice") confirmListening();
        else sendPause();   // respuesta tardía: la UI ya no está en voz
        break;
      case "paused":
        // Un paused viejo no cambia el modo; voice_pending espera listening o error.
        if (takeVoiceReply(true) && state.mode === "voice") setMode("idle");
        break;
      case "desync":
        if (state.mode === "voice") {
          state.desync = true;
          notify("warning", text || "Desincronizado: buscando la posición en el guion…", { channel: "voice", sticky: true });
        }
        break;
      case "warning":
        notify("warning", text || `Aviso del backend (${code || "sin código"}).`, { channel, ttl: 10000 });
        break;
      case "error": {
        // MIC_LOST y MODEL_* son avisos espontáneos: no responden a ninguna petición.
        const answered = VOICE_REPLY_ERRORS.has(code) && state.voiceReqs > 0;
        const current = takeVoiceReply(answered);
        if (code === "MODEL_ERROR" || code === "MODEL_MISSING") setModel("error");
        if (current && (state.mode === "voice" || state.mode === "voice_pending")) {
          setMode("idle");
          // No todo error detiene la escucha (BAD_REQUEST o INTERNAL de otra acción):
          // la pausa explícita evita un micrófono abierto con la UI en PAUSA.
          sendPause();
        }
        if (loadFailed) {
          if (state.loadKind === "user") state.pendingText = null;
          state.loadKind = null;
        }
        notify("error", text || `Error del backend (${code || "sin código"}).`, { channel, sticky: true });
        // Solo si responde al último start: un NO_SCRIPT del set_position de un
        // reenvío fallido no vuelve a reenviar (sin bucle).
        if (code === "NO_SCRIPT" && answered && current && state.script
            && state.pendingText === null && isOpen()) {
          resyncScript();
          notify("info", "Se reenvió el guion al backend: vuelve a iniciar.", { channel: "script" });
        }
        break;
      }
      default:
        break;
    }
    updateUI();
  }

  function setModel(model, message) {
    const prev = state.model;
    const m = model === "loading" || model === "ready" || model === "error" ? model : "unknown";
    state.model = m;
    if (m === "ready") {
      clearNotices("model");
      if (prev === "loading") notify("info", message || "Modelo listo.", { channel: "model" });
    }
  }

  // ===== Modos =====
  function setMode(mode) {
    const prev = state.mode;
    if (prev === mode) return;
    if (prev === "auto") stopAutoTimer();
    if (prev === "voice_pending") clearStartTimer();
    state.mode = mode;
    if (mode !== "voice") state.desync = false;
    clearNotices("server");
    updateUI();
  }

  function clearStartTimer() {
    clearTimeout(state.startTimer);
    state.startTimer = null;
  }

  /**
   * Hay un guion del usuario o un reenvío esperando su script_loaded: el
   * índice local es del guion que se va a reemplazar.
   */
  function loadPending() {
    return state.pendingText !== null || state.loadKind !== null;
  }

  const LOAD_PENDING_MSG = "Esperando a que el backend cargue el guion…";

  /** Motivo por el que no se puede iniciar la voz, o null. */
  function voiceBlocker() {
    if (!state.script) return "Carga un guion primero.";
    if (state.conn === "replaced") return "La app está abierta en otra ventana: retoma el control primero.";
    if (state.conn !== "open") return "Sin conexión con el backend local. Auto-scroll (A) funciona igual.";
    if (loadPending()) return LOAD_PENDING_MSG;
    if (state.model === "error") return "El modelo de voz no está disponible. Auto-scroll (A) funciona igual.";
    if (state.model !== "ready") return "El modelo aún se está cargando. Auto-scroll (A) ya está disponible.";
    return null;
  }

  /** Pasa a voz desde idle o Auto: start con el índice actual (atómico en el backend). */
  function startVoice() {
    const why = voiceBlocker();
    if (why) {
      notify("warning", why, { channel: "voice" });
      return;
    }
    if (state.mode === "voice" || state.mode === "voice_pending") return;
    // Desde Auto no hace falta set_position: el start lleva el índice actual.
    if (state.mode === "auto") setMode("idle");
    clearNotices("voice");
    enterReaderMode();
    if (!sendSeq({ action: "start", idx: state.currentIdx })) return;
    state.startSeq = state.lastSentSeq;
    state.voiceReqs++;
    setMode("voice_pending");
    state.startTimer = setTimeout(onStartTimeout, START_TIMEOUT_MS);
  }

  function onStartTimeout() {
    state.startTimer = null;
    if (state.mode !== "voice_pending") return;
    // El start sigue contado: si su respuesta llega tarde, se descuenta entonces
    // (un listening tardío con la UI en pausa provoca un pause).
    setMode("idle");
    notify("warning", "El backend no confirmó el inicio de la escucha. Vuelve a intentarlo.", { channel: "voice" });
  }

  function pauseVoice() {
    if (state.mode !== "voice" && state.mode !== "voice_pending") return;
    setMode("idle");
    sendPause();   // sin conexión el backend ya dejó de escuchar
  }

  /** Sale del Auto-scroll y deja la posición local en el backend. */
  function stopAuto() {
    if (state.mode !== "auto") return;
    setMode("idle");
    syncPosition();
  }

  function togglePlay() {
    if (state.mode === "voice" || state.mode === "voice_pending") pauseVoice();
    else if (state.mode === "auto") stopAuto();
    else startVoice();
  }

  // ===== Auto-scroll (local) =====
  function startAuto() {
    if (!state.script) {
      notify("warning", "Carga un guion primero.", { channel: "script" });
      return;
    }
    if (state.currentIdx >= state.script.total) {
      notify("info", "Fin del guion: pulsa R para volver al inicio.", { channel: "script" });
      return;
    }
    if (state.mode === "voice" || state.mode === "voice_pending") pauseVoice();
    enterReaderMode();
    setMode("auto");
    scheduleAuto();
  }

  function toggleAuto() {
    if (state.mode === "auto") stopAuto();
    else startAuto();
  }

  function scheduleAuto() {
    stopAutoTimer();
    state.autoTimer = setTimeout(autoTick, 1000 / state.autoSpeed);
  }

  function stopAutoTimer() {
    clearTimeout(state.autoTimer);
    state.autoTimer = null;
  }

  function autoTick() {
    state.autoTimer = null;
    if (state.mode !== "auto" || !state.script) return;
    const total = state.script.total;
    if (state.currentIdx >= total) {
      stopAuto();
      notify("info", "Fin del guion.", { channel: "script" });
      return;
    }
    applyIndex(skipForward(state.currentIdx + 1, state.skips, total));
    scheduleAuto();
  }

  function setSpeed(v) {
    state.autoSpeed = v;
    els.autoSpeed.value = String(v);
    els.autoSpeedLabel.textContent = String(v);
    updateOverlay();
  }

  function changeSpeed(delta) {
    const min = parseFloat(els.autoSpeed.min) || 1;
    const max = parseFloat(els.autoSpeed.max) || 10;
    const v = Math.round((state.autoSpeed + delta) * 2) / 2;
    setSpeed(Math.min(max, Math.max(min, v)));
  }

  // ===== Posición =====
  /**
   * Con una carga pendiente se ignoran las flechas y el reinicio: el backend
   * procesa primero el guion nuevo y el índice local sería del anterior.
   */
  function positionLocked() {
    if (!loadPending()) return false;
    notify("info", LOAD_PENDING_MSG, { channel: "script" });
    return true;
  }

  /** Mueve la posición local y la informa con set_position (absoluta). */
  function setPosition(idx, opts) {
    if (!state.script || positionLocked()) return;
    applyIndex(idx, opts);
    sendSeq({ action: "set_position", idx: state.currentIdx });
  }

  function step(delta) {
    if (!state.script || positionLocked()) return;
    const total = state.script.total;
    setPosition(skipForward(clampIdx(state.currentIdx + delta, total), state.skips, total));
  }

  function resetPosition() {
    if (!state.script || positionLocked()) return;
    if (state.mode === "voice" || state.mode === "voice_pending") pauseVoice();
    else if (state.mode === "auto") setMode("idle");   // el set_position de abajo informa la posición
    setPosition(0, { instant: true, recenter: true });
  }

  // ===== Dibujo del guion =====
  /** Dibuja script_loaded.segments: párrafos <p>, saltos <br>, directivas en bloque. */
  function renderScript(msg, idx) {
    const total = Number.isInteger(msg.total_tokens) && msg.total_tokens > 0 ? msg.total_tokens : 0;
    const segments = Array.isArray(msg.segments) ? msg.segments : [];
    const cur = clampIdx(idx, total);
    const wordEls = new Array(total).fill(null);
    const skips = new Array(total).fill(false);
    const dirEls = [];
    const dirAts = [];      // inicio del rango de cada directiva (no decreciente)
    const rawAts = [];      // at_idx recibido, recortado y no decreciente
    const frag = document.createDocumentFragment();
    let para = null;        // párrafo abierto
    let gap = false;        // el próximo bloque empieza tras un salto de párrafo
    let needSpace = false;  // separador entre palabras del mismo renglón
    let wordsSeen = 0;

    for (const seg of segments) {
      if (!seg || typeof seg !== "object") continue;
      if (seg.type === "word") {
        const i = seg.idx;
        if (!Number.isInteger(i) || i < 0 || i >= total) continue;
        if (!para) {
          para = document.createElement("p");
          para.className = gap ? "para gap" : "para";
          gap = false;
          needSpace = false;
          frag.appendChild(para);
        }
        if (needSpace) para.appendChild(document.createTextNode(" "));
        const span = document.createElement("span");
        span.className = `w${seg.skip ? " skip" : ""} ${wordState(i, cur)}`;
        span.dataset.idx = String(i);
        span.textContent = String(seg.text ?? "");
        para.appendChild(span);
        wordEls[i] = span;
        skips[i] = seg.skip === true;
        needSpace = true;
        wordsSeen = i + 1;
      } else if (seg.type === "break") {
        if (seg.kind === "paragraph") {
          para = null;
          gap = true;
        } else if (para) {
          para.appendChild(document.createElement("br"));
          needSpace = false;
        }
      } else if (seg.type === "directive") {
        let at = Number.isInteger(seg.at_idx) ? clampIdx(seg.at_idx, total) : wordsSeen;
        if (rawAts.length && at < rawAts[rawAts.length - 1]) at = rawAts[rawAts.length - 1];
        const div = document.createElement("div");
        div.className = `directive${gap ? " gap" : ""}`;   // el estado va al final (necesita skips)
        div.textContent = String(seg.text ?? "");
        frag.appendChild(div);
        dirEls.push(div);
        rawAts.push(at);
        // Inicio del rango: la primera palabra que sigue a la directiva en
        // `segments` (at_idx puede venir ya ajustado a la primera con unidades).
        dirAts.push(Math.min(at, wordsSeen));
        gap = false;
        para = null;
      }
    }

    // Fin del rango: at_idx tras saltar los tokens sin unidades que la siguen
    // (sus palabras llegan después en `segments`, por eso se calcula aquí).
    // Así "[Intro]\n— Hola" es actual tanto en 0 (carga, reinicio) como en 1.
    const dirEnds = rawAts.map((at) => skipForward(at, skips, total));
    for (let k = 0; k < dirEls.length; k++) {
      dirEls[k].classList.add(directiveState(dirAts[k], dirEnds[k], cur));
    }

    els.scriptView.textContent = "";
    els.scriptView.appendChild(frag);
    els.scriptView.scrollTop = 0;
    state.script = {
      id: msg.script_id || null,
      text: typeof msg.text === "string" ? msg.text : null,
      total,
    };
    state.wordEls = wordEls;
    state.skips = skips;
    state.dirEls = dirEls;
    state.dirAts = dirAts;
    state.dirEnds = dirEnds;
    state.currentIdx = cur;
    state.lastLineTop = null;
    followCurrent(true, true);
  }

  function setWordClass(i, cur) {
    const el = state.wordEls[i];
    if (!el) return;
    el.classList.remove("read", "current", "upcoming");
    el.classList.add(wordState(i, cur));
  }

  function setDirectiveClass(k, cur) {
    const el = state.dirEls[k];
    el.classList.remove("read", "current", "upcoming");
    el.classList.add(directiveState(state.dirAts[k], state.dirEnds[k], cur));
  }

  /** Cambia el índice actualizando solo las clases del rango que cambió. */
  function applyIndex(idx, opts = {}) {
    const s = state.script;
    if (!s) return;
    const cur = clampIdx(idx, s.total);
    const prev = state.currentIdx;
    if (cur !== prev) {
      state.currentIdx = cur;
      forEachDirty(prev, cur, s.total, state.dirAts, state.dirEnds,
        (i) => setWordClass(i, cur),
        (k) => setDirectiveClass(k, cur));
    }
    if (cur !== prev || opts.recenter) {
      followCurrent(!!opts.instant || Math.abs(cur - prev) > JUMP_INSTANT, !!opts.recenter);
    }
  }

  /** Elemento a centrar: la palabra actual o, al final, el cierre del guion. */
  function targetElement() {
    const s = state.script;
    if (!s) return null;
    if (state.currentIdx < s.total) return state.wordEls[state.currentIdx];
    const n = state.dirEls.length;
    if (n && state.dirEnds[n - 1] >= s.total) return state.dirEls[n - 1];
    return state.wordEls[s.total - 1] || (n ? state.dirEls[n - 1] : null);
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /** Centra la palabra actual solo si cambió de renglón (o si se fuerza). */
  function followCurrent(instant, force) {
    if (!els.body.classList.contains("mode-reader")) {
      state.lastLineTop = null;   // oculto: se centra al entrar en lectura
      return;
    }
    const el = targetElement();
    if (!el) return;
    const top = el.offsetTop;
    if (!force && top === state.lastLineTop) return;
    state.lastLineTop = top;
    el.scrollIntoView({
      behavior: instant || prefersReducedMotion() ? "auto" : "smooth",
      block: "center",
      inline: "nearest",
    });
  }

  function recenterSoon() {
    if (state.recenterQueued) return;
    state.recenterQueued = true;
    requestAnimationFrame(() => {
      state.recenterQueued = false;
      followCurrent(true, true);
    });
  }

  function enterReaderMode() {
    const active = document.activeElement;
    if (active && active !== document.body && typeof active.blur === "function") active.blur();
    if (els.body.classList.contains("mode-reader")) return;
    els.body.classList.remove("mode-setup");
    els.body.classList.add("mode-reader");
    followCurrent(true, true);
  }

  function exitReaderMode() {
    els.body.classList.remove("mode-reader");
    els.body.classList.add("mode-setup");
    state.lastLineTop = null;
  }

  function backToSetup() {
    if (state.mode === "voice" || state.mode === "voice_pending") pauseVoice();
    else if (state.mode === "auto") stopAuto();
    exitReaderMode();
    updateUI();
  }

  // ===== UI =====
  const CONN_LABEL = {
    connecting: ["Conectando…", "warn"],
    open: ["Conectado", "ok"],
    closed: ["Reconectando…", "warn"],
    replaced: ["Abierto en otra ventana", "error"],
  };
  const MODEL_LABEL = {
    unknown: ["Modelo --", "off"],
    loading: ["Cargando modelo…", "warn"],
    ready: ["Modelo listo", "ok"],
    error: ["Error de modelo", "error"],
  };

  function setIndicator(el, label, level) {
    el.textContent = label;
    el.dataset.level = level;
  }

  function updateUI() {
    updateButtons();
    updateOverlay();
    updateStatusLine();
  }

  function updateButtons() {
    const has = !!state.script;
    const idle = state.mode === "idle";
    const pending = loadPending();
    els.btnStart.disabled = !(state.conn === "open" && state.model === "ready" && has && idle && !pending);
    els.btnAuto.disabled = !has || state.mode === "auto";
    els.btnPause.disabled = idle;
    els.btnReset.disabled = !has || pending;
  }

  function updateOverlay() {
    const m = state.mode;
    let label = "⏸ PAUSA";
    if (m === "voice") label = state.desync ? "🎙 VOZ · buscando…" : "🎙 VOZ";
    else if (m === "voice_pending") label = "🎙 INICIANDO…";
    else if (m === "auto") label = "▶ AUTO";
    els.overlayMode.textContent = label;
    els.overlayMode.dataset.mode = m;
    els.overlayStatus.classList.toggle("on", m === "voice");

    const conn = state.conn === "connecting" && state.everOpened ? CONN_LABEL.closed : CONN_LABEL[state.conn];
    setIndicator(els.overlayConn, conn[0], conn[1]);
    const model = state.conn === "open" ? MODEL_LABEL[state.model] : MODEL_LABEL.unknown;
    setIndicator(els.overlayModel, model[0], model[1]);

    const auto = m === "auto";
    els.overlaySpeed.classList.toggle("hidden", !auto);
    els.overlayLag.classList.toggle("hidden", auto);
    els.overlayStt.classList.toggle("hidden", auto);
    els.overlayConf.classList.toggle("hidden", auto);
    if (auto) els.overlaySpeed.textContent = `vel ${state.autoSpeed.toFixed(1)}`;
  }

  function showStats(msg) {
    if (Number.isFinite(msg.lag_ms)) els.overlayLag.textContent = `lag ${Math.round(msg.lag_ms)} ms`;
    if (Number.isFinite(msg.stt_ms)) els.overlayStt.textContent = `stt ${Math.round(msg.stt_ms)} ms`;
    if (Number.isFinite(msg.confidence)) els.overlayConf.textContent = `conf ${msg.confidence.toFixed(2)}`;
  }

  /** Línea de estado del setup: conexión · modelo · guion. */
  function updateStatusLine() {
    const parts = [];
    let level = "ok";
    if (state.conn === "open") {
      parts.push("Conectado");
      if (state.model === "loading") {
        parts.push("Cargando modelo…");
        level = "warn";
      } else if (state.model === "ready") {
        const detail = [state.modelName, state.device].filter(Boolean).join(", ");
        parts.push(detail ? `Modelo listo (${detail})` : "Modelo listo");
      } else if (state.model === "error") {
        parts.push("Error de modelo");
        level = "error";
      }
    } else if (state.conn === "replaced") {
      parts.push("Abierto en otra ventana (recarga la página para retomar el control)");
      level = "error";
    } else {
      parts.push(state.everOpened ? "Sin conexión, reconectando…" : "Conectando al backend local…");
      level = "warn";
    }
    if (state.pendingText !== null) parts.push("Guion pendiente de envío");
    else if (state.script) parts.push(`Guion: ${state.script.total} palabras`);
    else parts.push("Sin guion");
    els.status.textContent = parts.join(" · ");
    els.status.dataset.level = level;
  }

  function loadFromTextarea() {
    const text = els.scriptText.value;
    if (!text.trim()) {
      notify("warning", "Pega un guion primero.", { channel: "script" });
      return;
    }
    if (text.length > MAX_SCRIPT_CHARS) {
      notify("error", `El guion tiene ${text.length} caracteres y el máximo es ${MAX_SCRIPT_CHARS}.`, { channel: "script", sticky: true });
      return;
    }
    // El backend pausa la escucha al recibir load_script. Al salir de Auto no
    // se informa la posición: es del guion que se va a reemplazar.
    if (state.mode === "voice" || state.mode === "voice_pending") pauseVoice();
    else if (state.mode === "auto") setMode("idle");
    state.pendingText = text;
    if (!isOpen()) {
      notify("warning", "Sin conexión con el backend: el guion se enviará al reconectar.", { channel: "script" });
    } else if (sendLoad(text, "user")) {
      notify("info", "Enviando guion…", { channel: "script" });
    }
    updateUI();
  }

  function toggleFullscreen() {
    try {
      const p = document.fullscreenElement
        ? document.exitFullscreen()
        : document.documentElement.requestFullscreen();
      if (p && typeof p.catch === "function") {
        p.catch(() => notify("warning", "No se pudo cambiar la pantalla completa.", { channel: "ui" }));
      }
    } catch (err) {
      console.error("Pantalla completa no disponible:", err);
    }
  }

  // ===== Eventos UI =====
  els.fontSize.addEventListener("input", () => {
    const v = els.fontSize.value;
    els.scriptView.style.fontSize = `${v}px`;
    els.fontSizeLabel.textContent = v;
    state.lastLineTop = null;
  });
  els.lineHeight.addEventListener("input", () => {
    const v = els.lineHeight.value;
    els.scriptView.style.lineHeight = v;
    els.lineHeightLabel.textContent = v;
    state.lastLineTop = null;
  });
  els.autoSpeed.addEventListener("input", () => {
    const v = parseFloat(els.autoSpeed.value);
    if (Number.isFinite(v)) setSpeed(v);
  });

  els.btnLoad.addEventListener("click", loadFromTextarea);
  els.btnStart.addEventListener("click", startVoice);
  els.btnAuto.addEventListener("click", startAuto);
  els.btnPause.addEventListener("click", () => {
    if (state.mode === "voice" || state.mode === "voice_pending") pauseVoice();
    else if (state.mode === "auto") stopAuto();
  });
  els.btnReset.addEventListener("click", resetPosition);
  els.btnFullscreen.addEventListener("click", toggleFullscreen);
  window.addEventListener("resize", () => {
    state.lastLineTop = null;
    recenterSoon();
  });

  // ===== Stealth + Topmost (ventana nativa pywebview) =====
  // Solo activos si la app corre dentro del wrapper pywebview (launch.py).
  // En navegador normal no existe `window.pywebview`.
  const stealth = {
    available: false,
    locked: true,
    topmost: false,
  };

  function updateStealthUI() {
    if (!stealth.available) {
      els.btnStealth.classList.add("hidden");
      els.btnTopmost.classList.add("hidden");
      els.overlayStealth.classList.add("hidden");
      els.overlayTopmost.classList.add("hidden");
      return;
    }
    els.btnStealth.classList.remove("hidden");
    els.btnTopmost.classList.remove("hidden");

    if (stealth.locked) {
      els.btnStealth.textContent = "🛡 Stealth: BLOQUEADO";
      els.btnStealth.classList.remove("unlocked");
      els.overlayStealth.classList.remove("hidden");
    } else {
      els.btnStealth.textContent = "🔓 Stealth: visible";
      els.btnStealth.classList.add("unlocked");
      els.overlayStealth.classList.add("hidden");
    }

    if (stealth.topmost) {
      els.btnTopmost.textContent = "📌 Siempre adelante: ON";
      els.btnTopmost.classList.add("active");
      els.overlayTopmost.classList.remove("hidden");
    } else {
      els.btnTopmost.textContent = "📌 Siempre adelante: OFF";
      els.btnTopmost.classList.remove("active");
      els.overlayTopmost.classList.add("hidden");
    }
  }

  async function toggleStealth() {
    if (!stealth.available) return;
    try {
      const newLocked = await window.pywebview.api.toggle_lock();
      stealth.locked = newLocked;
      updateStealthUI();
    } catch (err) {
      console.error("toggle_lock falló:", err);
      notify("warning", "No se pudo alternar el modo stealth.", { channel: "native" });
    }
  }

  async function toggleTopmost() {
    if (!stealth.available) return;
    try {
      const newTop = await window.pywebview.api.toggle_topmost();
      stealth.topmost = !!newTop;
      updateStealthUI();
    } catch (err) {
      console.error("toggle_topmost falló:", err);
      notify("warning", "No se pudo alternar «siempre adelante».", { channel: "native" });
    }
  }

  window.addEventListener("pywebviewready", async () => {
    stealth.available = true;
    try {
      stealth.locked = await window.pywebview.api.is_locked();
    } catch { /* noop */ }
    try {
      stealth.topmost = await window.pywebview.api.is_topmost();
    } catch { /* noop */ }
    updateStealthUI();
  });

  els.btnStealth.addEventListener("click", toggleStealth);
  els.btnTopmost.addEventListener("click", toggleTopmost);

  // ===== Atajos (un único keymap) =====
  function isEditable(el) {
    if (!el || el === document.body) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (el.isContentEditable) return true;
    return typeof el.closest === "function" && !!el.closest("[contenteditable]");
  }

  const ARROWS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);

  function onKeyDown(e) {
    if (e.isComposing || isEditable(e.target)) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const key = e.key;
    const arrow = ARROWS.has(key);
    if (e.shiftKey && !arrow) return;   // Shift solo modifica las flechas
    if (e.repeat && !arrow) {
      if (key === " ") e.preventDefault();
      return;
    }
    switch (key) {
      case " ":
      case "Spacebar": {
        e.preventDefault();
        // Sin foco en el botón, el keyup no lo vuelve a "pulsar".
        const active = document.activeElement;
        if (active && active.tagName === "BUTTON") active.blur();
        togglePlay();
        break;
      }
      case "a":
      case "A":
        toggleAuto();
        break;
      case "ArrowUp":
        e.preventDefault();
        changeSpeed(0.5);
        break;
      case "ArrowDown":
        e.preventDefault();
        changeSpeed(-0.5);
        break;
      case "ArrowLeft":
        e.preventDefault();
        step(-(e.shiftKey ? 15 : 5));
        break;
      case "ArrowRight":
        e.preventDefault();
        step(e.shiftKey ? 15 : 5);
        break;
      case "r":
      case "R":
        resetPosition();
        break;
      case "Escape":
        backToSetup();
        break;
      case "F11":
        e.preventDefault();
        toggleFullscreen();
        break;
      case "l":
      case "L":
        toggleStealth();   // anti-captura (solo app nativa)
        break;
      case "t":
      case "T":
        toggleTopmost();   // siempre adelante (solo app nativa)
        break;
      default:
        break;
    }
  }

  document.addEventListener("keydown", onKeyDown);

  // Inicialización
  els.scriptView.style.fontSize = `${els.fontSize.value}px`;
  els.scriptView.style.lineHeight = els.lineHeight.value;
  state.autoSpeed = parseFloat(els.autoSpeed.value) || 3;
  updateUI();
  connect();
})();
