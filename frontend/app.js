(() => {
  "use strict";

  // ===== Núcleo puro (sin DOM ni red) =====
  const MAX_SCRIPT_CHARS = 2000000;   // igual que tools/script_parser.py
  const JUMP_INSTANT = 30;            // saltos de más palabras se desplazan sin animación
  const START_TIMEOUT_MS = 6000;      // espera máxima de status: listening tras start
  const BACKOFF_MIN_MS = 500;
  const BACKOFF_MAX_MS = 8000;
  const CLOSE_REPLACED = 4000;        // el servidor cerró esta conexión por otra más nueva
  const SYNC_FLASH_MS = 1600;         // duración del indicador "Resincronizado" del overlay

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
    selGuion: $("sel-guion"),
    letra: $("letra"),
    letraLabel: $("letra-label"),
    lineHeight: $("line-height"),
    lineHeightLabel: $("line-height-label"),
    fondo: $("fondo"),
    fondoLabel: $("fondo-label"),
    fondoNota: $("fondo-nota"),
    chkBanda: $("chk-banda"),
    chkHalo: $("chk-halo"),
    btnPreajuste: $("btn-preajuste"),
    btnPreview: $("btn-preview"),
    modoNota: $("modo-nota"),
    velocidad: $("velocidad"),
    velocidadLabel: $("velocidad-label"),
    pausaAsistido: $("pausa-asistido"),
    pausaAsistidoLabel: $("pausa-asistido-label"),
    frenoConfirmar: $("freno-confirmar"),
    frenoConfirmarLabel: $("freno-confirmar-label"),
    frenoAdelanto: $("freno-adelanto"),
    frenoAdelantoLabel: $("freno-adelanto-label"),
    frenoVelmax: $("freno-velmax"),
    frenoVelmaxLabel: $("freno-velmax-label"),
    chkFrase: $("chk-frase"),
    chkEspejo: $("chk-espejo"),
    chkTopmost: $("chk-topmost"),
    chkStealth: $("chk-stealth"),
    btnLoad: $("btn-load"),
    btnAuto: $("btn-auto"),
    btnFullscreen: $("btn-fullscreen"),
    scriptView: $("script-view"),
    previewSample: $("preview-sample"),
    previewBanner: $("preview-banner"),
    barraMarcha: $("barra-marcha"),
    barraSeccion: $("barra-seccion"),
    barraVelocidad: $("barra-velocidad"),
    barraTiempo: $("barra-tiempo"),
    chipConn: $("chip-conn"),
    chipModel: $("chip-model"),
    chipSync: $("chip-sync"),
    chipStealth: $("chip-stealth"),
    chipTopmost: $("chip-topmost"),
    chipStats: $("chip-stats"),
    cuenta: $("cuenta"),
    cuentaNum: $("cuenta-num"),
    // Hito 85: referencias
    refOpacidad: $("ref-opacidad"),
    refOpacidadLabel: $("ref-opacidad-label"),
    refZoom: $("ref-zoom"),
    refZoomLabel: $("ref-zoom-etiqueta"),
    refZoomBody: $("ref-zoom-cuerpo"),
    chipRef: $("chip-ref"),
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
    desync: false,           // el backend busca la posición (status: desync); no es un error
    syncFlash: false,        // destello "Resincronizado" tras un scroll con resync: true
    syncFlashTimer: null,
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
    // Manual (local, no necesita backend)
    autoTimer: null,
    // Hito 84
    voiceKind: "voz",        // "voz" | "asistido" mientras se escucha
    assist: { speaking: false, heard: false, silenceSince: null, step: {}, timer: null },
    countdown: null,         // número que se muestra, o null
    countdownTimer: null,
    clock: { since: null, elapsed: 0 },   // tiempo en marcha
    rate: [],                // Voz: (t, idx) de los últimos segundos
    paraStarts: [],          // primera palabra de cada párrafo
    secStarts: [],           // inicio de cada sección (directivas)
    secNames: [],
    scriptName: null,        // nombre en guiones/, si vino de ahí
    pendingName: null,
    debugStats: false,
    // Hito 85: referencias (tarjetas fuera del flujo del texto)
    refs: [],                // { seg, at, card, body, state, loaded, loading, url }
    refAts: [],              // at de cada referencia (no decreciente)
    refNear: -1,             // índice de la referencia cerca (anunciada o actual), o -1
    zoom: null,              // { r, timer } mientras se muestra «Ampliar»
  };
  let prefs = TPCore.prefsDefecto();   // se cargan de localStorage al iniciar

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
      case "activity": onActivity(msg); break;   // Asistido: ¿hay voz?
      case "control": onControl(msg); break;     // orden de un Stream Deck (/control)
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
    state.debugStats = msg.debug === true;
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
      // (el nombre del guion no cambia en un reenvío)
      state.restoreIdx = null;
      updateUI();
      return;
    }

    let idx = 0;
    if (kind === "resync") idx = state.restoreIdx !== null ? state.restoreIdx : state.currentIdx;
    else if (!kind && msg.script_id === state.serverScriptId) idx = state.serverIdx;
    state.restoreIdx = null;
    if (state.mode === "auto" && kind !== "resync") setMode("idle");

    // El nombre va antes de dibujar: las referencias se buscan en <nombre>_recursos/.
    state.scriptName = typeof msg.name === "string" && msg.name ? msg.name
      : (kind === "user" ? state.pendingName : null);
    renderScript(msg, idx);
    state.clock = { since: null, elapsed: 0 };
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
    // Una resincronización salta sin animación: la posición cambia de golpe,
    // hacia adelante o hacia atrás, y animar ese tramo solo distrae.
    const resync = !echo && msg.resync === true;
    applyIndex(msg.current_idx, { instant: resync });
    if (state.mode !== "voice") return;
    // Cualquier scroll aplicado en voz termina la búsqueda del overlay.
    const searching = state.desync;
    state.desync = false;
    if (resync) flashResync();
    else if (searching) updateOverlay();
  }

  /** Destella "Resincronizado" en el overlay (reinicia el destello si ya estaba). */
  function flashResync() {
    clearTimeout(state.syncFlashTimer);
    state.syncFlash = true;
    state.syncFlashTimer = setTimeout(() => {
      state.syncFlashTimer = null;
      state.syncFlash = false;
      updateOverlay();
    }, SYNC_FLASH_MS);
    updateOverlay();
  }

  /** Apaga el destello sin redibujar (quien llama actualiza la UI). */
  function clearSyncFlash() {
    clearTimeout(state.syncFlashTimer);
    state.syncFlashTimer = null;
    state.syncFlash = false;
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
        // No es un error: el backend busca la posición en el guion. Basta un
        // indicador discreto en el overlay, sin aviso; se apaga con el próximo
        // scroll o con el status: listening que marca el fin de la búsqueda.
        if (state.mode === "voice") {
          state.desync = true;
          clearSyncFlash();
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

  // ===== Modos (hito 84, T4) =====
  // prefs.avance es el modo ELEGIDO (manual | asistido | voz; Manual por defecto).
  // state.mode es la MARCHA: idle | auto (Manual) | voice_pending | voice. En
  // voice_pending/voice, state.voiceKind dice si es "voz" o "asistido".
  function setMode(mode) {
    const prev = state.mode;
    if (prev === mode) return;
    if (prev === "auto") stopAutoTimer();
    if (prev === "voice_pending") clearStartTimer();
    if (prev === "voice" && state.voiceKind === "asistido") stopAssistTimer();
    state.mode = mode;
    if (mode !== "voice") {
      state.desync = false;
      clearSyncFlash();
    }
    if (mode === "voice" && state.voiceKind === "asistido") startAssistTimer();
    if (mode === "idle") pauseClock();
    else resumeClock();
    clearNotices("server");
    updateUI();
  }

  function isListening() {
    return state.mode === "voice" || state.mode === "voice_pending";
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

  /** Motivo por el que no se puede escuchar (Voz o Asistido), o null. */
  function voiceBlocker() {
    if (!state.script) return "Carga un guion primero.";
    if (state.conn === "replaced") return "La app está abierta en otra ventana: retoma el control primero.";
    if (state.conn !== "open") return "Sin conexión con el backend local. Manual funciona igual.";
    if (loadPending()) return LOAD_PENDING_MSG;
    if (state.model === "error") return "El modelo de voz no está disponible. Manual funciona igual.";
    if (state.model !== "ready") return "El modelo aún se está cargando. Manual ya está disponible.";
    return null;
  }

  /** Escucha en Voz (con frenos) o en Asistido: start con el índice actual. */
  function startVoice(kind = "voz") {
    const why = voiceBlocker();
    if (why) {
      notify("warning", why, { channel: "voice" });
      return;
    }
    if (isListening()) return;
    if (state.mode === "auto") setMode("idle");
    clearNotices("voice");
    enterReaderMode();
    const msg = { action: "start", idx: state.currentIdx, mode: kind };
    if (kind === "voz") msg.brakes = { ...prefs.frenos };
    if (!sendSeq(msg)) return;
    state.voiceKind = kind;
    state.assist.speaking = false;
    state.assist.heard = false;
    state.assist.silenceSince = null;     // Asistido espera a oír voz antes de avanzar
    state.assist.step = {};
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
    if (!isListening()) return;
    setMode("idle");
    sendPause();   // sin conexión el backend ya dejó de escuchar
    if (state.voiceKind === "asistido") syncPosition();   // la posición la movió el visor
  }

  /** Sale del Manual y deja la posición local en el backend. */
  function stopAuto() {
    if (state.mode !== "auto") return;
    setMode("idle");
    syncPosition();
  }

  /** Pausa lo que esté en marcha (y la cuenta atrás). */
  function pauseAll() {
    cancelCountdown();
    if (isListening()) pauseVoice();
    else if (state.mode === "auto") stopAuto();
  }

  /** Empieza a leer en el modo elegido. */
  function startReading() {
    cancelCountdown();
    if (prefs.avance === "manual") startAuto();
    else startVoice(prefs.avance);
  }

  function running() {
    return state.mode !== "idle";
  }

  function togglePlay() {
    if (state.countdown !== null) { cancelCountdown(); updateUI(); return; }
    if (running()) pauseAll();
    else startReading();
  }

  /** Cambia el modo elegido; si estaba en marcha, sigue en el modo nuevo. */
  function setAvance(modo) {
    if (!TPCore.MODOS.includes(modo) || modo === prefs.avance) return;
    const wasRunning = running();
    if (wasRunning) pauseAll();
    prefs = { ...prefs, avance: modo };
    applyPrefs();
    if (wasRunning) startReading();
  }

  // ===== Manual (local, no necesita backend) =====
  function startAuto() {
    if (!state.script) {
      notify("warning", "Carga un guion primero.", { channel: "script" });
      return;
    }
    if (state.currentIdx >= state.script.total) {
      notify("info", "Fin del guion: pulsa R para volver al inicio.", { channel: "script" });
      return;
    }
    if (isListening()) pauseVoice();
    enterReaderMode();
    setMode("auto");
    scheduleAuto();
  }

  function toggleAuto() {
    if (state.mode === "auto") stopAuto();
    else {
      if (prefs.avance !== "manual") { prefs = { ...prefs, avance: "manual" }; applyPrefs(); }
      startAuto();
    }
  }

  function scheduleAuto() {
    stopAutoTimer();
    state.autoTimer = setTimeout(autoTick, 1000 / pps());
  }

  function stopAutoTimer() {
    clearTimeout(state.autoTimer);
    state.autoTimer = null;
  }

  function autoTick() {
    state.autoTimer = null;
    if (state.mode !== "auto" || !state.script) return;
    if (!advanceOne()) {
      stopAuto();
      notify("info", "Fin del guion.", { channel: "script" });
      return;
    }
    scheduleAuto();
  }

  /** Avanza una palabra; false si ya estaba al final. */
  function advanceOne() {
    const total = state.script.total;
    if (state.currentIdx >= total) return false;
    applyIndex(skipForward(state.currentIdx + 1, state.skips, total));
    return true;
  }

  // ===== Asistido (velocidad fija; se para si callas más de la pausa) =====
  function startAssistTimer() {
    stopAssistTimer();
    state.assist.step = {};
    state.assist.timer = setInterval(assistTick, 40);
  }

  function stopAssistTimer() {
    clearInterval(state.assist.timer);
    state.assist.timer = null;
  }

  function assistCan(now) {
    return TPCore.asistidoPuede(now, state.assist.speaking, state.assist.silenceSince,
                                prefs.pausa_asistido_s);
  }

  function assistTick() {
    if (state.mode !== "voice" || state.voiceKind !== "asistido" || !state.script) return;
    const now = performance.now() / 1000;
    const can = assistCan(now);
    const marcha = can ? "corriendo" : "espera";
    if (els.body.dataset.marcha !== marcha) updateUI();
    if (TPCore.pasoFijo(state.assist.step, now, pps(), can) && !advanceOne()) {
      pauseVoice();
      notify("info", "Fin del guion.", { channel: "script" });
    }
  }

  function onActivity(msg) {
    if (state.mode !== "voice" || state.voiceKind !== "asistido") return;
    const now = performance.now() / 1000;
    if (msg.speaking === true) {
      state.assist.speaking = true;
      state.assist.heard = true;
      state.assist.silenceSince = null;
    } else {
      // Antes de oír la primera palabra no hay silencio que contar: espera.
      const silence = Number.isFinite(msg.silence_ms) ? msg.silence_ms / 1000 : 0;
      state.assist.speaking = false;
      state.assist.silenceSince = state.assist.heard ? now - silence : null;
    }
  }

  function pps() {
    return TPCore.velocidadAPps(prefs.velocidad);
  }

  function changeSpeed(delta) {
    prefs = { ...prefs, velocidad: TPCore.clamp(prefs.velocidad + delta, TPCore.VEL_MIN, TPCore.VEL_MAX) };
    applyPrefs();
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

  function jumpTo(idx, opts) {
    if (!state.script) return;
    const total = state.script.total;
    setPosition(skipForward(clampIdx(idx, total), state.skips, total), opts);
  }

  function step(delta) {
    if (!state.script || positionLocked()) return;
    jumpTo(state.currentIdx + delta);
  }

  function resetPosition() {
    if (!state.script || positionLocked()) return;
    if (isListening()) pauseVoice();
    else if (state.mode === "auto") setMode("idle");   // el set_position de abajo informa la posición
    setPosition(0, { instant: true, recenter: true });
  }

  /** Primera palabra de cada renglón dibujado (para atrás N líneas y el dial). */
  function lineStarts() {
    const out = [];
    let lastTop = null;
    const words = state.wordEls;
    for (let i = 0; i < words.length; i++) {
      const el = words[i];
      if (!el || state.skips[i]) continue;
      const top = el.offsetTop;
      if (top !== lastTop) { out.push(i); lastTop = top; }
    }
    return out;
  }

  function moveLines(delta) {
    if (!state.script) return;
    if (!els.body.classList.contains("mode-reader")) enterReaderMode();
    jumpTo(TPCore.objetivoLineas(state.currentIdx, lineStarts(), delta, state.script.total),
           { instant: true });
  }

  // ===== Cuenta atrás =====
  function startCountdown(n) {
    cancelCountdown();
    if (!n || n <= 0) { updateUI(); return; }
    if (!state.script) { notify("warning", "Carga un guion primero.", { channel: "script" }); return; }
    if (running()) pauseAll();
    enterReaderMode();
    state.countdown = n;
    const tick = () => {
      if (state.countdown === null) return;
      if (state.countdown <= 0) {
        state.countdown = null;
        updateUI();
        startReading();
        return;
      }
      updateUI();
      state.countdown -= 1;
      state.countdownTimer = setTimeout(tick, 1000);
    };
    tick();
  }

  function cancelCountdown() {
    clearTimeout(state.countdownTimer);
    state.countdownTimer = null;
    if (state.countdown !== null) state.countdown = null;
  }

  // ===== Reloj de lectura (transcurrido) y ritmo observado (Voz) =====
  function resumeClock() {
    if (state.clock.since === null) state.clock.since = performance.now();
  }

  function pauseClock() {
    if (state.clock.since !== null) {
      state.clock.elapsed += (performance.now() - state.clock.since) / 1000;
      state.clock.since = null;
    }
  }

  function elapsedS() {
    const live = state.clock.since === null ? 0 : (performance.now() - state.clock.since) / 1000;
    return state.clock.elapsed + live;
  }

  /** Palabras por segundo observadas en Voz (últimos 20 s); null sin base. */
  function observedPps() {
    const h = state.rate;
    const now = performance.now() / 1000;
    while (h.length && now - h[0].t > 20) h.shift();
    if (h.length < 2) return null;
    const dt = h[h.length - 1].t - h[0].t;
    const dw = h[h.length - 1].idx - h[0].idx;
    if (dt < 5 || dw <= 0) return null;
    return dw / dt;
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
    const paraStarts = [];  // primera palabra de cada párrafo
    const rawAts = [];      // at_idx recibido, recortado y no decreciente
    const refSegs = [];     // hito 85: referencias (no son secciones ni cortan el texto)
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
          paraStarts.push(i);
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
      } else if (seg.type === "ref") {
        refSegs.push(seg);
      }
    }

    // Fin del rango: at_idx tras saltar los tokens sin unidades que la siguen
    // (sus palabras llegan después en `segments`, por eso se calcula aquí).
    // Así "[Intro]\n— Hola" es actual tanto en 0 (carga, reinicio) como en 1.
    const dirEnds = rawAts.map((at) => skipForward(at, skips, total));
    for (let k = 0; k < dirEls.length; k++) {
      dirEls[k].classList.add(directiveState(dirAts[k], dirEnds[k], cur));
    }

    closeZoom();
    const refs = buildRefCards(refSegs, total);
    for (const r of refs) frag.appendChild(r.card);
    els.scriptView.textContent = "";
    els.scriptView.appendChild(frag);
    els.scriptView.scrollTop = 0;
    state.refs = refs;
    state.refAts = refs.map((r) => r.at);
    state.refNear = -1;
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
    state.paraStarts = paraStarts;
    state.secStarts = dirAts.slice();
    state.secNames = dirEls.map((el) => el.textContent.replace(/^\[|\]$/g, "").trim());
    state.currentIdx = cur;
    state.lastLineTop = null;
    updateRefStates();
    layoutRefs();
    loadAllRefs();
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
      if (state.refs.length) updateRefStates();
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
    measureLine();
    layoutRefs();   // oculto no tenía medidas
    followCurrent(true, true);
  }

  function exitReaderMode() {
    endPreview();
    els.body.classList.remove("mode-reader");
    els.body.classList.add("mode-setup");
    state.lastLineTop = null;
  }

  function backToSetup() {
    pauseAll();
    exitReaderMode();
    updateUI();
  }

  // ===== Referencias (hito 85, R1-R3) =====
  // Cada referencia es una tarjeta absoluta dentro de script-view, al costado
  // del renglón de su palabra `at`: se desplaza con el texto y no lo tapa
  // (con la vista «Con referencias» el texto deja libre la columna derecha).
  const ZOOM_MS = 8000;           // «Ampliar» se cierra solo, o al volver a pulsar
  const REF_GAP_PX = 10;          // separación mínima entre tarjetas seguidas
  const REF_CONCURRENCIA = 2;     // miniaturas pedidas a la vez

  function refName(seg) {
    return String(seg.file || "").replace(/\\/g, "/").split("/").pop() || String(seg.file || "");
  }

  function buildRefCards(refSegs, total) {
    for (const r of state.refs) if (r.url) URL.revokeObjectURL(r.url);
    const refs = [];
    let last = 0;
    for (const seg of refSegs) {
      let at = Number.isInteger(seg.at_idx) ? clampIdx(seg.at_idx, total) : last;
      if (at < last) at = last;
      last = at;
      const card = document.createElement("figure");
      card.className = "ref-card ref-lejos";
      card.dataset.tipo = String(seg.kind || "");
      const cap = document.createElement("figcaption");
      cap.className = "ref-etiqueta";
      cap.textContent = TPCore.refEtiqueta(seg);
      const body = document.createElement("div");
      body.className = "ref-cuerpo";
      card.append(cap, body);
      refs.push({ seg, at, card, body, state: "ref-lejos", loaded: false, loading: null, url: null });
    }
    return refs;
  }

  function showRefError(r, text) {
    r.card.classList.add("ref-error");
    r.body.textContent = text;
  }

  /** Pide la miniatura (o el código) una sola vez; un fallo queda escrito en la tarjeta. */
  function loadRef(r) {
    if (r.loaded) return r.loading || Promise.resolve();
    r.loaded = true;
    const seg = r.seg;
    if (seg.kind === "nota") {
      r.body.textContent = String(seg.note ?? "");
      return Promise.resolve();
    }
    if (seg.bad_frag) {
      showRefError(r, `No encontrado: ${refName(seg)}#${seg.frag}`);
      return Promise.resolve();
    }
    const url = TPCore.refUrl(seg, state.scriptName);
    if (!url) {
      showRefError(r, `No encontrado: ${refName(seg)}`);
      return Promise.resolve();
    }
    r.card.classList.add("ref-cargando");
    r.body.textContent = "Cargando…";
    r.loading = (async () => {
      let resp;
      try {
        resp = await fetch(url);
      } catch {
        showRefError(r, `No encontrado: ${refName(seg)}`);
        return;
      }
      if (!resp.ok) {
        let detail = null;
        try { detail = (await resp.json()).detail; } catch { /* sin cuerpo JSON */ }
        showRefError(r, typeof detail === "string" && detail ? detail : `No encontrado: ${refName(seg)}`);
        return;
      }
      if (seg.kind === "code") {
        const data = await resp.json();
        const pre = document.createElement("pre");
        pre.className = "ref-codigo";
        pre.innerHTML = String(data.html ?? "");   // HTML de Pygments: el texto viene escapado
        pre.dataset.desde = String(data.desde ?? "");
        r.body.replaceChildren(pre);
      } else {
        r.url = URL.createObjectURL(await resp.blob());
        const img = document.createElement("img");
        img.alt = TPCore.refEtiqueta(seg);
        img.src = r.url;
        try { await img.decode(); } catch { /* se dibuja igual al cargar */ }
        r.body.replaceChildren(img);
      }
    })().catch((err) => {
      console.error("Referencia:", err);
      showRefError(r, `No encontrado: ${refName(seg)}`);
    }).finally(() => {
      r.card.classList.remove("ref-cargando");
      layoutRefs();
      if (state.zoom && state.zoom.r === r) renderZoom();
    });
    return r.loading;
  }

  /** Con la vista «Con referencias», pide todas las tarjetas (como mucho dos a la vez). */
  function loadAllRefs() {
    if (prefs.vista !== "referencias") return;
    const queue = state.refs.filter((r) => !r.loaded);
    let active = 0;
    const next = () => {
      while (active < REF_CONCURRENCIA && queue.length) {
        const r = queue.shift();
        active++;
        loadRef(r).finally(() => { active--; next(); });
      }
    };
    next();
  }

  function refAnchor(at) {
    const s = state.script;
    if (!s) return null;
    if (at < s.total && state.wordEls[at]) return state.wordEls[at];
    for (let i = Math.min(at, s.total) - 1; i >= 0; i--) if (state.wordEls[i]) return state.wordEls[i];
    return null;
  }

  /** Coloca cada tarjeta a la altura del renglón de su palabra, sin pisar a la anterior. */
  function layoutRefs() {
    if (prefs.vista !== "referencias" || !state.refs.length) return;
    if (!els.body.classList.contains("mode-reader")) return;   // oculto no hay medidas
    let bottom = -Infinity;
    for (const r of state.refs) {
      const a = refAnchor(r.at);
      let top = a ? a.offsetTop : 0;
      if (top < bottom + REF_GAP_PX) top = bottom + REF_GAP_PX;
      r.card.style.top = `${Math.round(top)}px`;
      bottom = top + r.card.offsetHeight;
    }
  }

  /** Clases de anuncio (próxima, actual, leída) y la referencia cerca para los controles. */
  function updateRefStates() {
    const cur = state.currentIdx;
    for (const r of state.refs) {
      const st = `ref-${TPCore.refEstado(cur, r.at)}`;
      if (st !== r.state) {
        r.card.classList.remove(r.state);
        r.card.classList.add(st);
        r.state = st;
      }
    }
    const near = TPCore.refCercana(cur, state.refAts);
    if (near !== state.refNear) {
      state.refNear = near;
      updateRefChip();
      queueEstado();
    }
  }

  function nearRef() {
    return state.refNear >= 0 ? state.refs[state.refNear] : null;
  }

  function updateRefChip() {
    const r = nearRef();
    setChip(els.chipRef, r && prefs.vista === "referencias" && !state.zoom
      ? [`${TPCore.refEtiqueta(r.seg)} · Z ampliar`, "ok"] : null);
  }

  // «Ampliar» (tecla Z o la orden ref_zoom): la referencia cerca, grande, unos segundos.
  function toggleZoom() {
    if (state.zoom) { closeZoom(); return; }
    const r = nearRef();
    if (!r) {
      notify("info", "No hay una referencia cerca para ampliar.", { channel: "ref" });
      return;
    }
    state.zoom = { r, timer: setTimeout(closeZoom, ZOOM_MS) };
    renderZoom();
    els.refZoom.classList.remove("hidden");
    loadRef(r);
    updateRefChip();
    updateUI();
    sendEstado(true);
  }

  function renderZoom() {
    const z = state.zoom;
    if (!z) return;
    els.refZoom.dataset.tipo = String(z.r.seg.kind || "");
    els.refZoom.classList.toggle("ref-error", z.r.card.classList.contains("ref-error"));
    els.refZoomLabel.textContent = TPCore.refEtiqueta(z.r.seg);
    els.refZoomBody.replaceChildren(...Array.from(z.r.body.childNodes, (n) => n.cloneNode(true)));
  }

  function closeZoom() {
    const z = state.zoom;
    if (!z) return;
    clearTimeout(z.timer);
    state.zoom = null;
    els.refZoom.classList.add("hidden");
    els.refZoomBody.replaceChildren();
    updateRefChip();
    updateUI();
    sendEstado(true);
  }

  // ===== Preferencias (lectura, fondo, avance, frenos) =====
  const PREFS_KEY = "tp.prefs.v3";

  function loadPrefs() {
    try {
      return TPCore.sanearPrefs(JSON.parse(localStorage.getItem(PREFS_KEY) || "null"));
    } catch {
      return TPCore.prefsDefecto();
    }
  }

  function savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* sin almacenamiento: no se recuerda */ }
  }

  const fmt1 = (v) => String(Math.round(v * 100) / 100).replace(".", ",");

  /** Lleva las preferencias a la página (atributos, variables CSS, controles) y las guarda. */
  function applyPrefs() {
    const b = els.body;
    b.dataset.modo = prefs.avance;
    b.dataset.banda = prefs.banda ? "on" : "off";
    b.dataset.halo = prefs.halo ? "on" : "off";
    b.dataset.texto = prefs.texto;
    b.dataset.espejo = prefs.espejo ? "on" : "off";
    b.dataset.vista = prefs.vista;
    const root = document.documentElement.style;
    root.setProperty("--tp-ref-pct", String(TPCore.REF_TAMANOS[prefs.ref_tam]));
    root.setProperty("--tp-ref-opacidad", String(prefs.ref_opacidad / 100));
    root.setProperty("--tp-fondo-alfa", String(prefs.fondo_pct / 100));
    root.setProperty("--tp-escala", String(TPCore.escalaLetra(prefs.letra)));
    root.setProperty("--tp-interlineado", String(prefs.interlineado));

    setRange(els.letra, prefs.letra, els.letraLabel, `${fmt1(TPCore.escalaLetra(prefs.letra))}×`);
    setRange(els.lineHeight, prefs.interlineado, els.lineHeightLabel, fmt1(prefs.interlineado));
    setRange(els.fondo, prefs.fondo_pct, els.fondoLabel, `${prefs.fondo_pct} %`);
    setRange(els.velocidad, prefs.velocidad, els.velocidadLabel,
             `${prefs.velocidad} · ${Math.round(pps() * 60)} ppm`);
    setRange(els.pausaAsistido, prefs.pausa_asistido_s, els.pausaAsistidoLabel, `${fmt1(prefs.pausa_asistido_s)} s`);
    const f = prefs.frenos;
    setRange(els.frenoConfirmar, f.confirm_words, els.frenoConfirmarLabel, `${f.confirm_words} palabras`);
    setRange(els.frenoAdelanto, f.max_ahead, els.frenoAdelantoLabel, `${f.max_ahead} palabras`);
    setRange(els.frenoVelmax, f.max_wps, els.frenoVelmaxLabel, `${fmt1(f.max_wps)} pal/s`);
    els.chkFrase.checked = f.sentence_guard;
    els.chkBanda.checked = prefs.banda;
    els.chkHalo.checked = prefs.halo;
    els.chkEspejo.checked = prefs.espejo;
    for (const btn of document.querySelectorAll(".muestra-color")) {
      btn.setAttribute("aria-pressed", String(btn.dataset.color === prefs.texto));
    }
    for (const btn of document.querySelectorAll(".segmentado [data-modo]")) {
      btn.setAttribute("aria-pressed", String(btn.dataset.modo === prefs.avance));
    }
    for (const btn of document.querySelectorAll(".segmentado [data-vista]")) {
      btn.setAttribute("aria-pressed", String(btn.dataset.vista === prefs.vista));
    }
    for (const btn of document.querySelectorAll(".segmentado [data-reftam]")) {
      btn.setAttribute("aria-pressed", String(btn.dataset.reftam === prefs.ref_tam));
    }
    setRange(els.refOpacidad, prefs.ref_opacidad, els.refOpacidadLabel, `${prefs.ref_opacidad} %`);
    const notas = {
      manual: "Velocidad constante; el micrófono no interviene.",
      asistido: "Avanza a tu velocidad fija; si callas más que la pausa, se detiene y sigue al volver a hablar.",
      voz: "Sigue tus palabras con los frenos de abajo.",
    };
    els.modoNota.textContent = notas[prefs.avance];
    applyNativeBackdrop();
    measureLine();
    layoutRefs();
    loadAllRefs();
    updateRefChip();
    savePrefs();
    state.lastLineTop = null;
    recenterSoon();
    updateUI();
  }

  function setRange(input, value, label, text) {
    if (document.activeElement !== input) input.value = String(value);
    label.textContent = text;
  }

  /** Alto real de un renglón de lectura: lo usa la banda (--tp-linea). */
  function measureLine() {
    const cs = getComputedStyle(els.scriptView);
    const fs = parseFloat(cs.fontSize) || 64;
    const lh = parseFloat(cs.lineHeight);
    const px = Number.isFinite(lh) ? lh : fs * prefs.interlineado;
    document.documentElement.style.setProperty("--tp-linea", `${Math.round(px)}px`);
  }

  // ===== UI =====
  const CONN_LABEL = {
    connecting: ["Conectando…", "aviso"],
    open: ["Servidor", "ok"],
    closed: ["Reconectando…", "aviso"],
    replaced: ["Abierto en otra ventana", "error"],
  };
  const MODEL_LABEL = {
    unknown: null,
    loading: ["Cargando modelo…", "aviso"],
    ready: ["Modelo listo", "ok"],
    error: ["Error de modelo", "error"],
  };

  function setChip(el, spec) {
    if (!spec) { el.classList.add("hidden"); return; }
    el.classList.remove("hidden");
    el.textContent = spec[0];
    el.dataset.nivel = spec[1];
  }

  function marcha() {
    if (state.countdown !== null) return "cuenta";
    if (state.mode === "idle") return "pausa";
    if (state.mode === "voice_pending") return "espera";
    if (state.mode === "voice" && state.voiceKind === "asistido" && !assistCan(performance.now() / 1000)) return "espera";
    return "corriendo";
  }

  function updateUI() {
    updateButtons();
    updateBarra();
    updateStatusLine();
    queueEstado();
  }

  function updateButtons() {
    const has = !!state.script;
    els.btnAuto.disabled = !has || loadPending();
  }

  const MARCHA_LABEL = {
    corriendo: "▶ En marcha",
    pausa: "⏸ En pausa",
    cuenta: "Cuenta atrás",
  };

  function updateBarra() {
    const m = marcha();
    els.body.dataset.marcha = m;
    let label = MARCHA_LABEL[m];
    if (m === "espera") label = state.mode === "voice_pending" ? "Iniciando…" : "Esperando tu voz";
    els.barraMarcha.textContent = label;
    els.cuenta.classList.toggle("hidden", state.countdown === null);
    els.cuentaNum.textContent = state.countdown === null ? "" : String(state.countdown);

    const conn = state.conn === "connecting" && state.everOpened ? CONN_LABEL.closed : CONN_LABEL[state.conn];
    setChip(els.chipConn, conn);
    // El modelo solo importa si el modo elegido lo usa.
    const needsModel = prefs.avance !== "manual";
    setChip(els.chipModel, state.conn === "open" && needsModel && state.model !== "ready" ? MODEL_LABEL[state.model] : null);
    let sync = null;
    if (state.mode === "voice" && state.voiceKind === "voz") {
      if (state.desync) sync = ["Buscando posición…", "aviso"];
      else if (state.syncFlash) sync = ["Resincronizado", "ok"];
    }
    setChip(els.chipSync, sync);
    els.chipStats.classList.toggle("hidden", !(state.mode === "voice" && state.voiceKind === "voz" && state.debugStats));
    setChip(els.chipStealth, native.available && native.locked ? ["Invisible", "ok"] : null);
    setChip(els.chipTopmost, native.available && native.topmost ? ["Adelante", "ok"] : null);

    const sec = currentSection();
    els.barraSeccion.textContent = sec ? sec.name : "";
    const e = buildEstado();
    els.barraVelocidad.textContent = prefs.avance === "voz"
      ? (e.ppm === null ? "voz · — ppm" : `voz · ${Math.round(e.ppm)} ppm`)
      : `vel ${prefs.velocidad} · ${Math.round(e.ppm)} ppm`;
    const rest = e.restante_s === null ? "—" : `−${TPCore.formatoTiempo(e.restante_s)}`;
    els.barraTiempo.textContent = `${TPCore.formatoTiempo(e.transcurrido_s)}  ${rest}`;
    document.documentElement.style.setProperty("--tp-progreso", String(e.progreso ?? 0));
  }

  function showStats(msg) {
    const parts = [];
    if (Number.isFinite(msg.lag_ms)) parts.push(`lag ${Math.round(msg.lag_ms)} ms`);
    if (Number.isFinite(msg.stt_ms)) parts.push(`stt ${Math.round(msg.stt_ms)} ms`);
    els.chipStats.textContent = parts.join(" · ");
  }

  function updateSyncIndicator() { updateBarra(); }
  function updateOverlay() { updateBarra(); }

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

  // ===== Secciones =====
  function currentSection() {
    const k = TPCore.seccionActual(state.currentIdx, state.secStarts);
    return k < 0 ? null : { idx: k, name: state.secNames[k] };
  }

  // ===== Estado para los controles (/control, contrato T6) =====
  function buildEstado() {
    const total = state.script ? state.script.total : 0;
    const sec = currentSection();
    let rate = null;
    if (prefs.avance === "voz") rate = observedPps();
    else rate = pps();
    const left = Math.max(0, total - state.currentIdx);
    const corriendo = running() && state.countdown === null;
    return {
      corriendo,
      modo: prefs.avance,
      velocidad: prefs.velocidad,
      ppm: rate === null ? null : rate * 60,
      progreso: total ? state.currentIdx / total : null,
      transcurrido_s: elapsedS(),
      restante_s: rate ? left / rate : null,
      seccion: sec ? sec.name : null,
      seccion_idx: sec ? sec.idx : null,
      secciones: state.script ? state.secStarts.length : null,
      letra: prefs.letra,
      espejo: prefs.espejo,
      fondo_pct: prefs.fondo_pct,
      banda: prefs.banda,
      halo: prefs.halo,
      bloqueado: native.available ? native.locked : null,
      adelante: native.available ? native.topmost : null,
      guion: state.script ? state.scriptName : null,
      cuenta: state.countdown,
      // Hito 85 (R5)
      vista: prefs.vista,
      ref_actual: nearRef() ? nearRef().seg.label || null : null,
      ref_ampliada: !!state.zoom,
    };
  }

  let estadoQueued = false;
  let lastEstadoJson = "";

  /** Envía el estado al backend si cambió (como mucho una vez por tarea). */
  function queueEstado() {
    if (estadoQueued) return;
    estadoQueued = true;
    setTimeout(() => {
      estadoQueued = false;
      sendEstado(false);
    }, 0);
  }

  function sendEstado(force) {
    if (!isOpen()) return;
    const e = buildEstado();
    // Lo que cambia solo con el reloj no cuenta como cambio (va en el envío de cada segundo).
    const key = JSON.stringify({ ...e, transcurrido_s: 0, restante_s: 0, ppm: prefs.avance === "voz" ? 0 : e.ppm });
    if (!force && key === lastEstadoJson) return;
    lastEstadoJson = key;
    sendRaw({ action: "estado", estado: e });
  }

  // Cada segundo mientras corre (transcurrido, restante, progreso).
  setInterval(() => {
    if (state.mode === "voice" && state.voiceKind === "voz" && state.script) {
      state.rate.push({ t: performance.now() / 1000, idx: state.currentIdx });
    }
    if (running() || state.countdown !== null) {
      updateBarra();
      sendEstado(true);
    }
  }, 1000);

  // ===== Órdenes de los controles (Stream Deck) =====
  function onControl(msg) {
    const cmd = msg.cmd;
    const value = msg.value;
    const p2 = TPCore.aplicarOrdenPrefs(prefs, cmd, value);
    if (p2) {
      if (cmd === "mode") setAvance(p2.avance);
      else { prefs = p2; applyPrefs(); }
      if ((cmd === "faster" || cmd === "slower" || cmd === "set_speed") && state.mode === "auto") scheduleAuto();
      sendEstado(true);
      return;
    }
    const total = state.script ? state.script.total : 0;
    switch (cmd) {
      case "play_pause": togglePlay(); break;
      case "prev_paragraph":
        if (state.script) jumpTo(TPCore.objetivoParrafoAnterior(state.currentIdx, state.paraStarts), { instant: true });
        break;
      case "back_lines": moveLines(-Math.abs(value || 3)); break;
      case "scroll": moveLines(value || 0); break;
      case "next_section":
        if (state.script) {
          jumpTo(TPCore.objetivoSeccionSiguiente(state.currentIdx, state.secStarts, state.paraStarts, total),
                 { instant: true });
        }
        break;
      case "top": resetPosition(); break;
      case "lock": setStealth(!native.locked); break;
      case "topmost": setTopmost(!native.topmost); break;
      case "countdown": startCountdown(value); break;
      case "ref_zoom": toggleZoom(); break;   // hito 85 (R5); «view» va por aplicarOrdenPrefs
      default: break;
    }
    updateUI();
    sendEstado(true);
  }

  // ===== Guiones de la carpeta guiones/ =====
  async function loadScriptList() {
    try {
      const r = await fetch("/api/guiones");
      if (!r.ok) return;
      const names = await r.json();
      const sel = els.selGuion;
      for (const opt of Array.from(sel.options).slice(1)) opt.remove();
      for (const n of names) {
        const opt = document.createElement("option");
        opt.value = n;
        opt.textContent = n;
        sel.appendChild(opt);
      }
    } catch (err) {
      console.error("No se pudo leer la lista de guiones:", err);
    }
  }

  async function openFromList() {
    const name = els.selGuion.value;
    if (!name) return;
    try {
      const r = await fetch(`/api/guiones/${encodeURIComponent(name)}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      els.scriptText.value = await r.text();
      state.pendingName = name;
      loadFromTextarea();
    } catch (err) {
      notify("error", `No se pudo abrir ${name}: ${err.message}`, { channel: "script" });
    }
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
    // El backend pausa la escucha al recibir load_script. Al salir de Manual no
    // se informa la posición: es del guion que se va a reemplazar.
    cancelCountdown();
    if (isListening()) pauseVoice();
    else if (state.mode === "auto") setMode("idle");
    state.pendingText = text;
    if (els.selGuion.value !== state.pendingName) state.pendingName = null;
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

  // ===== Eventos de la configuración =====
  function onRange(input, fn) {
    input.addEventListener("input", () => {
      const v = parseFloat(input.value);
      if (Number.isFinite(v)) { fn(v); applyPrefs(); }
    });
  }
  onRange(els.letra, (v) => { prefs = { ...prefs, letra: v }; });
  onRange(els.lineHeight, (v) => { prefs = { ...prefs, interlineado: v }; });
  onRange(els.fondo, (v) => { prefs = { ...prefs, fondo_pct: v }; });
  onRange(els.velocidad, (v) => {
    prefs = { ...prefs, velocidad: v };
    if (state.mode === "auto") scheduleAuto();
  });
  onRange(els.pausaAsistido, (v) => { prefs = { ...prefs, pausa_asistido_s: v }; });
  onRange(els.frenoConfirmar, (v) => { prefs = { ...prefs, frenos: { ...prefs.frenos, confirm_words: v } }; });
  onRange(els.frenoAdelanto, (v) => { prefs = { ...prefs, frenos: { ...prefs.frenos, max_ahead: v } }; });
  onRange(els.frenoVelmax, (v) => { prefs = { ...prefs, frenos: { ...prefs.frenos, max_wps: v } }; });
  els.chkFrase.addEventListener("change", () => {
    prefs = { ...prefs, frenos: { ...prefs.frenos, sentence_guard: els.chkFrase.checked } };
    applyPrefs();
  });
  els.chkBanda.addEventListener("change", () => { prefs = { ...prefs, banda: els.chkBanda.checked }; applyPrefs(); });
  els.chkHalo.addEventListener("change", () => { prefs = { ...prefs, halo: els.chkHalo.checked }; applyPrefs(); });
  els.chkEspejo.addEventListener("change", () => { prefs = { ...prefs, espejo: els.chkEspejo.checked }; applyPrefs(); });
  for (const btn of document.querySelectorAll(".muestra-color")) {
    btn.addEventListener("click", () => { prefs = { ...prefs, texto: btn.dataset.color }; applyPrefs(); });
  }
  for (const btn of document.querySelectorAll(".segmentado [data-modo]")) {
    btn.addEventListener("click", (ev) => {
      ev.currentTarget.blur();
      setAvance(btn.dataset.modo);
    });
  }
  for (const btn of document.querySelectorAll(".segmentado [data-vista]")) {
    btn.addEventListener("click", (ev) => {
      ev.currentTarget.blur();
      prefs = { ...prefs, vista: btn.dataset.vista };
      applyPrefs();
    });
  }
  for (const btn of document.querySelectorAll(".segmentado [data-reftam]")) {
    btn.addEventListener("click", (ev) => {
      ev.currentTarget.blur();
      prefs = { ...prefs, ref_tam: btn.dataset.reftam };
      applyPrefs();
    });
  }
  onRange(els.refOpacidad, (v) => { prefs = { ...prefs, ref_opacidad: v }; });
  els.btnPreajuste.addEventListener("click", () => { prefs = TPCore.preajustePresentacion(prefs); applyPrefs(); });
  els.btnLoad.addEventListener("click", loadFromTextarea);
  els.selGuion.addEventListener("change", openFromList);
  els.btnAuto.addEventListener("click", startReading);
  els.btnFullscreen.addEventListener("click", toggleFullscreen);
  window.addEventListener("resize", () => {
    state.lastLineTop = null;
    measureLine();
    layoutRefs();
    recenterSoon();
  });

  // ===== App nativa (pywebview, launch.py): invisible en capturas, siempre adelante, fondo =====
  // En un navegador normal no existe `window.pywebview` y estos controles se ocultan.
  const native = { available: false, locked: true, topmost: false, alphaFloor: 1 };

  function updateNativeUI() {
    for (const el of document.querySelectorAll(".nativo")) el.classList.toggle("hidden", !native.available);
    els.chkStealth.checked = native.locked;
    els.chkTopmost.checked = native.topmost;
    els.fondoNota.textContent = native.available && native.alphaFloor < 1
      ? `En la app nativa el fondo transparente también aclara el texto (límite de WebView2): como mucho baja al ${Math.round(native.alphaFloor * 100)} %. La banda y el halo compensan.`
      : "";
    updateBarra();
  }

  async function setStealth(on) {
    if (!native.available) return;
    try {
      native.locked = !!(await window.pywebview.api.set_lock(!!on));
    } catch (err) {
      console.error("set_lock falló:", err);
      notify("warning", "No se pudo cambiar «invisible en capturas».", { channel: "native" });
    }
    updateNativeUI();
    sendEstado(true);
  }

  async function setTopmost(on) {
    if (!native.available) return;
    try {
      native.topmost = !!(await window.pywebview.api.set_topmost(!!on));
    } catch (err) {
      console.error("set_topmost falló:", err);
      notify("warning", "No se pudo cambiar «siempre adelante».", { channel: "native" });
    }
    updateNativeUI();
    sendEstado(true);
  }

  let backdropPending = null;
  /** En la app nativa el fondo se aplica también como alfa de la ventana (con piso). */
  function applyNativeBackdrop() {
    if (!native.available) return;
    const pct = prefs.fondo_pct;
    if (backdropPending === null) {
      requestAnimationFrame(async () => {
        const v = backdropPending;
        backdropPending = null;
        try { await window.pywebview.api.set_backdrop(v); } catch (err) { console.error("set_backdrop falló:", err); }
      });
    }
    backdropPending = pct;
  }

  /** Tecla O: fondo al 100 % y vuelta al elegido. */
  let backdropBeforeO = null;
  function toggleBackdrop() {
    if (prefs.fondo_pct < 100) {
      backdropBeforeO = prefs.fondo_pct;
      prefs = { ...prefs, fondo_pct: 100 };
    } else {
      prefs = { ...prefs, fondo_pct: backdropBeforeO ?? 40 };
    }
    applyPrefs();
  }

  window.addEventListener("pywebviewready", async () => {
    native.available = true;
    try { native.locked = !!(await window.pywebview.api.is_locked()); } catch { /* noop */ }
    try { native.topmost = !!(await window.pywebview.api.is_topmost()); } catch { /* noop */ }
    try { native.alphaFloor = Number(await window.pywebview.api.backdrop_floor()) || 1; } catch { /* noop */ }
    updateNativeUI();
    applyNativeBackdrop();
    sendEstado(true);
  });
  els.chkStealth.addEventListener("change", () => setStealth(els.chkStealth.checked));
  els.chkTopmost.addEventListener("change", () => setTopmost(els.chkTopmost.checked));

  // ===== Vista previa (de la copia estable) =====
  const PREVIEW_MS = 6000;
  const preview = { timer: null };

  function endPreview() {
    if (!preview || !els.previewSample) return;
    clearTimeout(preview.timer);
    preview.timer = null;
    els.previewSample.classList.add("hidden");
    els.previewBanner.classList.add("hidden");
  }

  function startPreview() {
    endPreview();
    if (!els.scriptView.querySelector("[data-idx]")) els.previewSample.classList.remove("hidden");
    els.previewBanner.textContent = `Vista previa · fondo ${prefs.fondo_pct} % · Esc para volver`;
    els.previewBanner.classList.remove("hidden");
    enterReaderMode();
    preview.timer = setTimeout(() => {
      preview.timer = null;
      if (state.mode === "idle") exitReaderMode();
      else endPreview();
    }, PREVIEW_MS);
  }
  els.btnPreview.addEventListener("click", startPreview);

  // ===== Atajos (un único keymap) =====
  function isEditable(el) {
    if (!el || el === document.body) return false;
    const tag = el.tagName;
    if (tag === "INPUT" && (el.type === "range" || el.type === "checkbox")) return false;
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
    if (arrow && e.target && e.target.type === "range") return;   // el deslizador usa sus flechas
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
        if (active && (active.tagName === "BUTTON" || active.tagName === "INPUT")) active.blur();
        togglePlay();
        break;
      }
      case "a":
      case "A":
        toggleAuto();
        break;
      case "m":
      case "M":
        setAvance(TPCore.aplicarOrdenPrefs(prefs, "mode", "siguiente").avance);
        break;
      case "ArrowUp":
        e.preventDefault();
        changeSpeed(1);
        if (state.mode === "auto") scheduleAuto();
        break;
      case "ArrowDown":
        e.preventDefault();
        changeSpeed(-1);
        if (state.mode === "auto") scheduleAuto();
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
        setStealth(!native.locked);   // invisible en capturas (solo app nativa)
        break;
      case "t":
      case "T":
        setTopmost(!native.topmost);  // siempre adelante (solo app nativa)
        break;
      case "o":
      case "O":
        toggleBackdrop();
        break;
      case "v":
      case "V":
        prefs = TPCore.aplicarOrdenPrefs(prefs, "view", "siguiente");
        applyPrefs();
        break;
      case "z":
      case "Z":
        toggleZoom();
        break;
      default:
        break;
    }
  }

  document.addEventListener("keydown", onKeyDown);

  // Solo lectura, para depurar y para las pruebas de extremo a extremo.
  window.TPDebug = Object.freeze({
    state: () => state, prefs: () => prefs, lineStarts,
    // Hito 85: lo que muestra cada tarjeta (medido en el DOM, no en los segmentos).
    refs: () => state.refs.map((r) => ({
      at: r.at, kind: r.seg.kind, label: r.seg.label, estado: r.state,
      error: r.card.classList.contains("ref-error") ? r.body.textContent : null,
      img: !!r.body.querySelector("img"), codigo: !!r.body.querySelector("pre.ref-codigo"),
      visible: r.card.getBoundingClientRect().width > 0, top: r.card.offsetTop,
    })),
  });

  // Inicialización
  prefs = loadPrefs();
  applyPrefs();
  updateUI();
  connect();
  loadScriptList();
})();
