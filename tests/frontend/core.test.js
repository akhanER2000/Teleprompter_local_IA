// Núcleo puro del visor (frontend/core.js). Se corre con `node --test tests/frontend`
// (y desde pytest: tests/test_frontend.py).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const C = require(path.join(__dirname, "..", "..", "frontend", "core.js"));

test("Manual es el modo predeterminado", () => {
  assert.equal(C.prefsDefecto().avance, "manual");
  assert.equal(C.sanearPrefs(null).avance, "manual");
  assert.equal(C.sanearPrefs({ avance: "turbo" }).avance, "manual");
});

test("cada orden de preferencias cambia lo esperado", () => {
  const p = C.prefsDefecto();
  const casos = [
    ["faster", undefined, (q) => q.velocidad === p.velocidad + 1],
    ["slower", undefined, (q) => q.velocidad === p.velocidad - 1],
    ["set_speed", 20, (q) => q.velocidad === 20],
    ["set_speed", 1, (q) => q.velocidad === 1],
    ["font", 1, (q) => q.letra === p.letra + 1],
    ["font", -1, (q) => q.letra === p.letra - 1],
    ["mirror", undefined, (q) => q.espejo === !p.espejo],
    ["band", undefined, (q) => q.banda === !p.banda],
    ["halo", undefined, (q) => q.halo === !p.halo],
    ["backdrop", -5, (q) => q.fondo_pct === p.fondo_pct - 5],
    ["mode", "voz", (q) => q.avance === "voz"],
    ["mode", "asistido", (q) => q.avance === "asistido"],
    ["mode", "siguiente", (q) => q.avance === "asistido"],
  ];
  for (const [cmd, value, ok] of casos) {
    const q = C.aplicarOrdenPrefs(p, cmd, value);
    assert.ok(q && ok(q), `${cmd} ${value}`);
  }
  assert.equal(C.aplicarOrdenPrefs(p, "play_pause"), null);
});

test("los topes del contrato se respetan", () => {
  let p = { ...C.prefsDefecto(), velocidad: 20, letra: 20, fondo_pct: 100 };
  assert.equal(C.aplicarOrdenPrefs(p, "faster").velocidad, 20);
  assert.equal(C.aplicarOrdenPrefs(p, "font", 1).letra, 20);
  assert.equal(C.aplicarOrdenPrefs(p, "backdrop", 5).fondo_pct, 100);
  p = { ...p, velocidad: 1, fondo_pct: 0 };
  assert.equal(C.aplicarOrdenPrefs(p, "slower").velocidad, 1);
  assert.equal(C.aplicarOrdenPrefs(p, "backdrop", -5).fondo_pct, 0);
  let m = { ...p, avance: "voz" };
  assert.equal(C.aplicarOrdenPrefs(m, "mode", "siguiente").avance, "manual");
});

test("Sobre presentación junta banda, halo y fondo al 0 %", () => {
  const q = C.preajustePresentacion(C.prefsDefecto());
  assert.deepEqual([q.banda, q.halo, q.fondo_pct], [true, true, 0]);
});

test("posiciones: párrafo anterior, sección siguiente, líneas", () => {
  const paras = [0, 10, 25, 40];
  assert.equal(C.objetivoParrafoAnterior(15, paras), 10);   // a su principio
  assert.equal(C.objetivoParrafoAnterior(11, paras), 0);    // ya al principio: al anterior
  assert.equal(C.objetivoParrafoAnterior(0, paras), 0);
  assert.equal(C.objetivoSeccionSiguiente(5, [10, 40], paras, 50), 10);
  assert.equal(C.objetivoSeccionSiguiente(40, [10, 40], paras, 50), 50);
  assert.equal(C.objetivoSeccionSiguiente(12, [], paras, 50), 25);  // sin secciones: párrafo
  assert.equal(C.seccionActual(12, [10, 40]), 0);
  assert.equal(C.seccionActual(3, [10, 40]), -1);
  const lines = [0, 6, 12, 18, 24];
  assert.equal(C.objetivoLineas(14, lines, -3, 30), 0);
  assert.equal(C.objetivoLineas(14, lines, -1, 30), 6);
  assert.equal(C.objetivoLineas(14, lines, 2, 30), 24);
  assert.equal(C.objetivoLineas(14, lines, 5, 30), 30);
});

/** Simula el avance de Asistido con un patrón de voz; devuelve los instantes de cada paso. */
function simularAsistido({ pps, pausaS, voz, hasta, dt = 0.01 }) {
  const st = {};
  const pasos = [];
  let silencioDesde = null;
  let hablabaAntes = false;
  for (let t = 0; t <= hasta + 1e-9; t += dt) {
    const hablando = voz(t);
    if (!hablando && hablabaAntes) silencioDesde = t;
    if (hablando) silencioDesde = null;
    hablabaAntes = hablando;
    const puede = C.asistidoPuede(t, hablando, hablando ? null : (silencioDesde ?? null), pausaS);
    if (C.pasoFijo(st, t, pps, puede)) pasos.push(t);
  }
  return pasos;
}

test("Asistido nunca acelera ni salta", () => {
  const pps = 3;          // velocidad fija
  // Habla 0–4 s, calla 4–8 s (más que la pausa), vuelve 8–12 s.
  const voz = (t) => (t >= 0.5 && t < 4) || (t >= 8 && t < 12);
  const pasos = simularAsistido({ pps, pausaS: 1.5, voz, hasta: 12 });
  // Un paso a la vez y nunca más rápido que la velocidad fija (con 1 ms de tolerancia del reloj).
  for (let k = 1; k < pasos.length; k++) {
    assert.ok(pasos[k] - pasos[k - 1] >= 1 / pps - 0.011, `paso ${k}: ${pasos[k] - pasos[k - 1]}`);
  }
  // No avanza antes de oír voz ni durante el silencio largo (4+1,5 a 8 s).
  assert.ok(pasos.every((t) => t >= 0.5));
  assert.ok(!pasos.some((t) => t >= 5.51 && t < 8), "avanzó en el silencio");
  // Al volver a hablar sigue a la misma velocidad: no recupera lo perdido.
  const tras = pasos.filter((t) => t >= 8 && t < 9);
  assert.ok(tras.length <= Math.ceil(pps) + 1, `ráfaga al volver: ${tras.length}`);
  // Sí avanza en la pausa corta (menos de 1,5 s de silencio).
  assert.ok(pasos.some((t) => t > 4 && t < 5.5));
});

test("Manual avanza siempre a la misma velocidad", () => {
  const st = {};
  const pasos = [];
  for (let t = 0; t <= 5; t += 0.01) if (C.pasoFijo(st, t, 2, true)) pasos.push(t);
  assert.ok(pasos.length >= 10 && pasos.length <= 11);
});

test("tiempos: sin dato, guion", () => {
  assert.equal(C.formatoTiempo(null), "—");
  assert.equal(C.formatoTiempo(-1), "—");
  assert.equal(C.formatoTiempo(75), "1:15");
  assert.equal(C.formatoTiempo(3725), "1:02:05");
});

test("velocidad y letra", () => {
  assert.equal(C.velocidadAPps(6), 3);
  assert.equal(C.ppsAVelocidad(3), 6);
  assert.equal(C.escalaLetra(8), 1);
});
