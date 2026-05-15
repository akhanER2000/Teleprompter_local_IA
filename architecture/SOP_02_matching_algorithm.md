# SOP 02 — Algoritmo de Matching Guion ↔ Voz

## Propósito
Dado un texto transcrito por Whisper y la posición actual en el guion, devolver el nuevo índice de palabra a resaltar/scrollear.

## Algoritmo: Sliding Window Monotónico
```
Entrada: transcript (str), current_idx (int), tokens (list)
Salida:  new_idx (int), confidence (float)

1. norm_words = normalizar(transcript).split()
2. window = tokens[current_idx : current_idx + 25]   # ventana lookahead
3. Para cada palabra de norm_words (en orden):
     buscar la palabra en window[probe_pos:]
     si match exacto: probe_pos = match_pos + 1, hits += 1
     si match difuso (distancia <= 1 char y len > 3): probe_pos += 1, hits += 0.7
4. confidence = hits / len(norm_words)
5. si confidence >= 0.5: new_idx = current_idx + last_match_offset + 1
   else: new_idx = current_idx   (no avanzar)
```

## Normalización (Unicode NFD)
```python
import unicodedata, re
def norm(s):
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')  # quita acentos
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    return s.strip()
```

## Monotonicidad
- new_idx >= current_idx siempre (excepto reset manual).
- Si el usuario repite una palabra, no retrocede.
- Si la confianza es baja durante > 3 chunks consecutivos: emitir `status` de "desincronizado" pero NO retroceder.

## Distancia difusa
- Levenshtein limitada a 1 operación (insert/delete/substitute) y solo para palabras de longitud > 3 — evita falsos positivos con artículos.

## Complejidad
- O(W * K) donde W = palabras del chunk (~3-5) y K = tamaño ventana (25). Total: ~125 comparaciones por chunk. Negligible.
