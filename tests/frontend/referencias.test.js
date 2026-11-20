// Hito 85: vista y referencias en el núcleo puro (frontend/core.js).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const C = require(path.join(__dirname, "..", "..", "frontend", "core.js"));

test("la vista por defecto es Solo texto y se recuerda saneada", () => {
  assert.equal(C.prefsDefecto().vista, "texto");
  assert.equal(C.sanearPrefs({ vista: "referencias" }).vista, "referencias");
  assert.equal(C.sanearPrefs({ vista: "imagenes" }).vista, "texto");
  assert.equal(C.sanearPrefs({ ref_tam: "grande" }).ref_tam, "grande");
  assert.equal(C.sanearPrefs({ ref_tam: "enorme" }).ref_tam, "mediana");
  assert.equal(C.sanearPrefs({ ref_opacidad: 30 }).ref_opacidad, 100);   // fuera de 40..100
  assert.equal(C.sanearPrefs({ ref_opacidad: 55 }).ref_opacidad, 55);
  // Unas preferencias guardadas antes del hito 85 siguen valiendo.
  const viejas = { ...C.prefsDefecto() };
  delete viejas.vista; delete viejas.ref_tam; delete viejas.ref_opacidad;
  assert.deepEqual(C.sanearPrefs(viejas), C.prefsDefecto());
});

test("orden view: texto, referencias y siguiente", () => {
  const p = C.prefsDefecto();
  assert.equal(C.aplicarOrdenPrefs(p, "view", "referencias").vista, "referencias");
  assert.equal(C.aplicarOrdenPrefs({ ...p, vista: "referencias" }, "view", "texto").vista, "texto");
  assert.equal(C.aplicarOrdenPrefs(p, "view", "siguiente").vista, "referencias");
  assert.equal(C.aplicarOrdenPrefs({ ...p, vista: "referencias" }, "view", "siguiente").vista, "texto");
  assert.equal(C.aplicarOrdenPrefs(p, "view", "otra").vista, "texto");
  // ref_zoom no es una preferencia: la ejecuta el visor.
  assert.equal(C.aplicarOrdenPrefs(p, "ref_zoom"), null);
});

test("Sobre presentación deja las referencias al 85 %", () => {
  const q = C.preajustePresentacion({ ...C.prefsDefecto(), ref_opacidad: 40 });
  assert.equal(q.ref_opacidad, 85);
  assert.equal(q.fondo_pct, 0);
});

test("tamaños entre el 15 y el 35 % del ancho", () => {
  assert.deepEqual(C.REF_TAMANOS, { chica: 15, mediana: 25, grande: 35 });
});

test("la referencia que viene se anuncia antes de llegar", () => {
  const A = C.REF_ANUNCIO, D = C.REF_DESPUES;
  assert.equal(C.refEstado(100 - A - 1, 100), "lejos");
  assert.equal(C.refEstado(100 - A, 100), "proxima");
  assert.equal(C.refEstado(99, 100), "proxima");
  assert.equal(C.refEstado(100, 100), "actual");
  assert.equal(C.refEstado(100 + D, 100), "actual");
  assert.equal(C.refEstado(100 + D + 1, 100), "leida");
  const ats = [50, 200];
  assert.equal(C.refCercana(0, ats), -1);
  assert.equal(C.refCercana(50 - A, ats), 0);
  assert.equal(C.refCercana(50 + D, ats), 0);
  assert.equal(C.refCercana(50 + D + 1, ats), -1);
  assert.equal(C.refCercana(200 - A, ats), 1);
  // Dos seguidas: en cuanto se anuncia la segunda, es la de ahora.
  assert.equal(C.refCercana(60 - A, [50, 60]), 1);
});

test("URL local de cada tipo y etiqueta de la tarjeta", () => {
  const u = (r, g) => new URL(C.refUrl(r, g), "http://127.0.0.1");
  let x = u({ kind: "ppt", file: "informe.pptx", frag: "5", page: 5 }, "charla.txt");
  assert.equal(x.pathname, "/api/ref");
  assert.deepEqual(Object.fromEntries(x.searchParams), { tipo: "ppt", archivo: "informe.pptx", guion: "charla.txt", pagina: "5" });
  x = u({ kind: "code", file: "app.py", frag: "L10-L25", lines: [10, 25] }, null);
  assert.deepEqual(Object.fromEntries(x.searchParams), { tipo: "code", archivo: "app.py", desde: "10", hasta: "25" });
  assert.equal(C.refUrl({ kind: "nota", file: null, note: "x" }, "g"), null);
  assert.equal(C.refEtiqueta({ kind: "pdf", file: "C:\\docs\\informe.pdf", frag: "3" }), "PDF · informe.pdf #3");
  assert.equal(C.refEtiqueta({ kind: "img", file: "fotos/portada.png", frag: null }), "IMG · portada.png");
});
