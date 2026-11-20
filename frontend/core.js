/*
 * Núcleo puro del visor (hito 84): sin DOM, sin red, sin reloj propio.
 *
 * Lo usa app.js en el navegador (window.TPCore) y tests/frontend/*.test.js
 * en Node (module.exports). Todo lo que decide QUÉ cambia una orden de
 * control o un paso de avance vive aquí, para poder probarlo sin navegador.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TPCore = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MODOS = ["manual", "asistido", "voz"];
  const VEL_MIN = 1, VEL_MAX = 20;          // contrato T6: velocidad 1..20
  const LETRA_MIN = 1, LETRA_MAX = 20;      // nivel de letra; 8 = escala 1,0
  const LETRA_DEFECTO = 8;

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  /** Velocidad 1..20 en palabras por segundo (pasos de 0,5, como el deslizador de siempre). */
  function velocidadAPps(v) { return clamp(Math.round(v), VEL_MIN, VEL_MAX) / 2; }
  function ppsAVelocidad(pps) { return clamp(Math.round(pps * 2), VEL_MIN, VEL_MAX); }
  /** Escala de la letra: nivel 8 = 1,0; 1 = 0,56; 20 = 1,75. */
  function escalaLetra(nivel) { return 0.5 + clamp(Math.round(nivel), LETRA_MIN, LETRA_MAX) * 0.0625; }

  function prefsDefecto() {
    return {
      avance: "manual",       // T4: Manual es el predeterminado
      velocidad: 6,           // 3 palabras por segundo
      letra: LETRA_DEFECTO,
      interlineado: 1.45,
      texto: "marfil",        // blanco | marfil | oro
      fondo_pct: 100,
      banda: true,             // el diseño v3 la trae encendida en Manual
      halo: false,
      espejo: false,
      pausa_asistido_s: 1.5,
      frenos: { confirm_words: 3, max_ahead: 6, max_wps: 6, sentence_guard: true },
    };
  }

  /** Valida unas preferencias guardadas: lo que no sirva vuelve al valor por defecto. */
  function sanearPrefs(p) {
    const d = prefsDefecto();
    if (!p || typeof p !== "object") return d;
    const num = (v, lo, hi, def) => (typeof v === "number" && Number.isFinite(v) && v >= lo && v <= hi ? v : def);
    const fr = p.frenos && typeof p.frenos === "object" ? p.frenos : {};
    return {
      avance: MODOS.includes(p.avance) ? p.avance : d.avance,
      velocidad: Math.round(num(p.velocidad, VEL_MIN, VEL_MAX, d.velocidad)),
      letra: Math.round(num(p.letra, LETRA_MIN, LETRA_MAX, d.letra)),
      interlineado: num(p.interlineado, 1, 2.5, d.interlineado),
      texto: ["blanco", "marfil", "oro"].includes(p.texto) ? p.texto : d.texto,
      fondo_pct: Math.round(num(p.fondo_pct, 0, 100, d.fondo_pct)),
      banda: typeof p.banda === "boolean" ? p.banda : d.banda,
      halo: typeof p.halo === "boolean" ? p.halo : d.halo,
      espejo: typeof p.espejo === "boolean" ? p.espejo : d.espejo,
      pausa_asistido_s: num(p.pausa_asistido_s, 0.5, 5, d.pausa_asistido_s),
      frenos: {
        confirm_words: Math.round(num(fr.confirm_words, 1, 10, d.frenos.confirm_words)),
        max_ahead: Math.round(num(fr.max_ahead, 1, 30, d.frenos.max_ahead)),
        max_wps: num(fr.max_wps, 0.5, 10, d.frenos.max_wps),
        sentence_guard: typeof fr.sentence_guard === "boolean" ? fr.sentence_guard : d.frenos.sentence_guard,
      },
    };
  }

  /** Preajuste «Sobre presentación»: banda, halo y fondo al 0 %. */
  function preajustePresentacion(p) {
    return { ...p, banda: true, halo: true, fondo_pct: 0 };
  }

  /**
   * Órdenes de control que solo cambian preferencias. Devuelve las prefs
   * nuevas, o null si la orden no es de este tipo (posición, marcha, ventana).
   */
  function aplicarOrdenPrefs(p, cmd, value) {
    switch (cmd) {
      case "faster": return { ...p, velocidad: clamp(p.velocidad + 1, VEL_MIN, VEL_MAX) };
      case "slower": return { ...p, velocidad: clamp(p.velocidad - 1, VEL_MIN, VEL_MAX) };
      case "set_speed": return { ...p, velocidad: clamp(Math.round(value), VEL_MIN, VEL_MAX) };
      case "font": return { ...p, letra: clamp(p.letra + (value > 0 ? 1 : -1), LETRA_MIN, LETRA_MAX) };
      case "mirror": return { ...p, espejo: !p.espejo };
      case "band": return { ...p, banda: !p.banda };
      case "halo": return { ...p, halo: !p.halo };
      case "backdrop": return { ...p, fondo_pct: clamp(p.fondo_pct + Math.round(value), 0, 100) };
      case "mode": {
        const next = value === "siguiente"
          ? MODOS[(MODOS.indexOf(p.avance) + 1) % MODOS.length]
          : (MODOS.includes(value) ? value : p.avance);
        return { ...p, avance: next };
      }
      default: return null;
    }
  }

  // ===== Posición =====

  /** Mayor elemento de `arr` (ordenado) que sea <= x, o -1. */
  function floorIndex(arr, x) {
    let lo = 0, hi = arr.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (arr[mid] <= x) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  /**
   * Párrafo anterior: si ya estás al principio del párrafo (o en sus 2
   * primeras palabras), al anterior; si no, al principio del actual.
   */
  function objetivoParrafoAnterior(cur, paraStarts) {
    const k = floorIndex(paraStarts, cur);
    if (k < 0) return 0;
    if (cur - paraStarts[k] > 2) return paraStarts[k];
    return k > 0 ? paraStarts[k - 1] : 0;
  }

  /** Sección siguiente (directivas); sin secciones por delante, párrafo siguiente; si no, el final. */
  function objetivoSeccionSiguiente(cur, sectionStarts, paraStarts, total) {
    for (const s of sectionStarts) if (s > cur) return s;
    for (const s of paraStarts) if (s > cur) return s;
    return total;
  }

  /** Sección actual: índice en `sectionStarts` de la última que empezó (o -1). */
  function seccionActual(cur, sectionStarts) {
    return floorIndex(sectionStarts, cur);
  }

  /**
   * Mover `delta` líneas visibles. `lineStarts` = primera palabra de cada
   * renglón dibujado (ordenado). Hacia atrás va al principio del renglón;
   * hacia adelante, al principio del renglón `delta` más abajo.
   */
  function objetivoLineas(cur, lineStarts, delta, total) {
    if (!lineStarts.length) return clamp(cur, 0, total);
    const k = Math.max(0, floorIndex(lineStarts, cur));
    const j = k + Math.round(delta);
    if (j < 0) return 0;
    if (j >= lineStarts.length) return total;
    return lineStarts[j];
  }

  // ===== Avance a velocidad fija (Manual y Asistido) =====

  /**
   * Un paso del avance a velocidad fija. `st.next` es el instante (s) del
   * próximo paso permitido. Si `puede` es falso (Asistido: callaste más de la
   * pausa) no avanza y no acumula: al volver a hablar sigue a la misma
   * velocidad, nunca recupera de golpe lo que no avanzó. Devuelve 0 o 1.
   */
  function pasoFijo(st, now, pps, puede) {
    const periodo = 1 / Math.max(0.1, pps);
    if (!puede) { st.next = now + periodo; return 0; }
    if (st.next === undefined || st.next === null) st.next = now;
    if (now + 1e-9 < st.next) return 0;
    st.next = now + periodo;            // desde ahora, no desde el paso perdido
    return 1;
  }

  /**
   * Asistido: ¿se puede avanzar? Habla ahora, o calló hace menos de la pausa.
   * `silencioDesde` es null mientras no se oyó nada desde el arranque: espera.
   */
  function asistidoPuede(now, hablando, silencioDesde, pausaS) {
    if (hablando) return true;
    if (silencioDesde === null || silencioDesde === undefined) return false;
    return now - silencioDesde < pausaS;
  }

  /** Tiempo m:ss (o h:mm:ss). Sin dato, guion. */
  function formatoTiempo(s) {
    if (typeof s !== "number" || !Number.isFinite(s) || s < 0) return "—";
    const t = Math.round(s);
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), ss = t % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
  }

  return {
    MODOS, VEL_MIN, VEL_MAX, LETRA_MIN, LETRA_MAX, LETRA_DEFECTO,
    clamp, velocidadAPps, ppsAVelocidad, escalaLetra, prefsDefecto, sanearPrefs,
    preajustePresentacion, aplicarOrdenPrefs, floorIndex, objetivoParrafoAnterior,
    objetivoSeccionSiguiente, seccionActual, objetivoLineas, pasoFijo, asistidoPuede,
    formatoTiempo,
  };
});
