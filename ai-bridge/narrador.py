# SPDX-License-Identifier: GPL-3.0-or-later
#
# narrador.py — redacta el informe en castellano llamando a una API remota
# Copyright (C) 2026 Barakaldo Makers
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
Narrador: convierte el análisis YA CALCULADO en prosa legible, llamando a una
API compatible con OpenAI (OpenRouter, la de Nous, o un llama.cpp propio).

El servidor solo hace de cliente: manda unos KB de JSON y recibe texto. Eso un
Core 2 Quad lo hace sin enterarse, al contrario que la inferencia local.

LA REGLA DEL MÓDULO
-------------------
El modelo NO calcula nada. Recibe las cifras hechas y solo las redacta. Y no se
confía en que obedezca: después de generar el texto se extrae CADA número que
contiene y se comprueba que aparezca en los datos de entrada. Si aparece una
cifra que no está en los datos, el texto se DESCARTA y el sistema sigue con el
motor de reglas de siempre.

Esto no es paranoia de manual. En esta misma red, antes de arreglarlos, el
análisis afirmaba que cuatro nodos de ruido eran 'coherentes' cuando se
llevaban 42 dB, que la mediana del factor de corrección era x0,732 cuando
mezclaba dos referencias distintas, y que un día tenía 0 horas. Un modelo
encima de esos números habría escrito párrafos impecables sosteniendo las tres
cosas. La fluidez no valida nada, así que la validación va aparte y es
determinista.

Reglas de la comprobación numérica:
  - todo número con decimales, y todo entero mayor de 31, tiene que estar en
    los datos de entrada (con tolerancia de redondeo);
  - los enteros de 0 a 31 se permiten sin más: son cuentas, horas, días y
    ordinales ("tres nodos", "las 8 horas de noche");
  - los umbrales normativos viajan dentro de los datos, así que el modelo
    puede citarlos y la comprobación los acepta.

Coste y seguridad:
  - NARRA_ENABLE viene a 0: sin activarlo no se llama a nadie y no se gasta.
  - no hay llamadas automáticas en el ciclo; solo el endpoint, a petición.
  - se envía un resumen compacto, no el JSON entero.
  - caché por contenido: si el análisis no ha cambiado, no se vuelve a pagar.
  - NARRA_MIN_INTERVAL_H limita la frecuencia aunque se pida más a menudo.
  - la clave sale solo de una variable de entorno y nunca se registra.
  - si la API falla, tarda o devuelve algo raro, no pasa nada: se devuelve
    None y el sistema funciona como hasta ahora.
"""
import os
import re
import json
import time
import hashlib
import logging

import requests

log = logging.getLogger("narrador")

NARRA_ENABLE = os.getenv("NARRA_ENABLE", "0") == "1"
# Cualquier endpoint compatible con OpenAI: OpenRouter, la API de Nous, o un
# llama.cpp/Ollama propio el dia que haya una maquina capaz.
NARRA_BASE_URL = os.getenv("NARRA_BASE_URL",
                           "https://openrouter.ai/api/v1").rstrip("/")
NARRA_MODEL = os.getenv("NARRA_MODEL", "nousresearch/hermes-4-70b")
NARRA_API_KEY = os.getenv("NARRA_API_KEY", "").strip()
NARRA_MAX_TOKENS = int(os.getenv("NARRA_MAX_TOKENS", "700"))
NARRA_TEMPERATURE = float(os.getenv("NARRA_TEMPERATURE", "0.3"))
NARRA_TIMEOUT = float(os.getenv("NARRA_TIMEOUT", "60"))
NARRA_MIN_INTERVAL_H = float(os.getenv("NARRA_MIN_INTERVAL_H", "6"))
# Precios por millon de tokens, para estimar el gasto. Se dejan vacios a
# proposito: los pone quien despliega con los de su proveedor, porque
# hardcodearlos aqui solo garantiza que envejezcan mal.
NARRA_PRICE_IN = float(os.getenv("NARRA_PRICE_IN", "0") or 0)
NARRA_PRICE_OUT = float(os.getenv("NARRA_PRICE_OUT", "0") or 0)

_cache = {}        # hash del resumen -> (epoch, resultado)
_ultima = [0.0]    # epoch de la ultima llamada de verdad

SISTEMA = """Eres el redactor de un proyecto vecinal de medición de calidad del
aire y ruido. Recibes un resumen en JSON con mediciones YA CALCULADAS y escribes
un texto breve en castellano para vecinos sin formación técnica.

REGLAS ABSOLUTAS:
1. No calcules nada. No sumes, no promedies, no conviertas unidades, no deduzcas
   cifras nuevas. Usa EXCLUSIVAMENTE los números que aparecen en el JSON.
2. Si una cifra no está en el JSON, no la escribas. Ni redondeada, ni estimada,
   ni "aproximadamente". Es mejor una frase sin número que un número inventado.
3. Si el JSON dice que un dato no es fiable, que una métrica se descartó o que
   un veredicto no se puede emitir, dilo. No rellenes el hueco.
4. No afirmes que se cumple o se incumple una normativa: los datos vienen de
   sensores de bajo coste y los veredictos del JSON son orientativos. Di
   "queda por debajo del objetivo" o "lo supera", y menciona que es orientativo.
5. Nada de alarmismo ni de tranquilizar de más. Describe lo que hay.

FORMATO: dos o tres párrafos cortos, sin títulos, sin listas, sin markdown.
Tono llano y directo. Si hay algo que la vecindad deba saber (un sensor
averiado, un episodio), que salga en el primer párrafo."""


# ── Resumen compacto ─────────────────────────────────────────────────────────
def digest(result, acustica_inf=None):
    """
    Extrae del analisis lo que merece narrarse. Se manda esto y no el JSON
    entero: es mas barato, y reduce la superficie de cifras que el modelo
    podria mezclar.
    """
    if not result:
        return None
    d = {"zona": result.get("geo3"), "horas": result.get("hours")}
    st = result.get("statistics") or {}
    conf = result.get("confidence") or {}
    met = {}
    for m, s in st.items():
        if not isinstance(s, dict):
            continue
        e = {"media": s.get("mean"), "maximo": s.get("max"),
             "ultimo": s.get("last_value"), "tendencia": s.get("trend"),
             "n": s.get("n")}
        c = conf.get(m) or {}
        if c.get("label"):
            e["fiabilidad"] = c["label"]
            e["rho"] = c.get("spearman_rho")
            e["sesgo_pct"] = c.get("bias_pct")
        met[m] = {k: v for k, v in e.items() if v is not None}
    if met:
        d["metricas"] = met
    for k in ("units", "caqi", "status", "air_quality_index",
              "official_provider", "official_stations", "episodes",
              "discarded_metrics", "factor_drift"):
        v = result.get(k)
        if v:
            d[k] = v
    w = result.get("weather") or {}
    if w:
        d["meteo"] = {k: w.get(k) for k in
                      ("temperature", "humidity", "wind_kmh", "wind_dir_txt",
                       "rain_mm") if w.get(k) is not None}
    if acustica_inf and acustica_inf.get("disponible"):
        a = {"diagnostico": (acustica_inf.get("diagnostico") or {}).get("nota"),
             "causa": (acustica_inf.get("diagnostico") or {}).get("causa")}
        nodos = {}
        for _mac, n in (acustica_inf.get("nodos") or {}).items():
            ind = {k: (v or {}).get("valor")
                   for k, v in (n.get("indicadores") or {}).items()}
            nodos[n.get("name")] = {
                "indices_db": {k: v for k, v in ind.items() if v is not None},
                "rd_1367": (n.get("rd_1367") or {}).get("estado"),
                "oms_2018": (n.get("oms_2018") or {}).get("estado"),
            }
        if nodos:
            a["nodos"] = nodos
        for k in ("zona_rd_1367", "zona_oms_2018"):
            z = acustica_inf.get(k)
            if z:
                a[k] = {"estado": z.get("estado"), "nota": z.get("nota"),
                        "objetivos": z.get("objetivos_dba")
                        or z.get("recomendado_db")}
        d["acustica"] = a
    return d


# ── Comprobación numérica ────────────────────────────────────────────────────
# Antes de buscar cifras hay que tapar lo que LLEVA digitos pero no es una
# cantidad, o el verificador se dispara solo: "PM2.5" se leia como el numero
# 2,5 y el "m3" de µg/m3 como un 3, asi que un texto impecable salia descartado.
_MASCARA = re.compile(
    r"""(?ix)
      pm\s?2[.,]5 | pm\s?10 | pm\s?1\b |          # nombres de metrica
      no\s?2 | o\s?3 | so\s?2 | co\s?2 | nh\s?3 |
      l\s?90 | laeq(?:[,_]?\d+\s?h)? | l\s?den |
      [\u00b5u]?g\s?/\s?m\s?[\u00b2\u00b33] |      # ug/m3, µg/m³
      mg\s?/\s?m\s?[\u00b2\u00b33] |
      \bm\s?[\u00b2\u00b33]\b |
      iso\s?1996(?:\s?-\s?\d)? |                 # referencias normativas
      (?:real\s+decreto|rd)\s?\d+\s?/\s?\d+ |
      directiva\s?\d+\s?/\s?\d+\s?/\s?ce |
      \b(?:oms|who)\s?20\d\d\b
    """)

# Numeros en texto español: 1.234,56 / 42,1 / -19 / 65 / 0,64
_NUM = re.compile(r'[-+]?\d{1,3}(?:\.\d{3})+(?:,\d+)?|[-+]?\d+(?:[.,]\d+)?')

def _a_float(s):
    s = s.strip()
    if re.fullmatch(r'[-+]?\d{1,3}(?:\.\d{3})+(?:,\d+)?', s):
        s = s.replace(".", "").replace(",", ".")       # 1.234,56
    else:
        s = s.replace(",", ".")                        # 42,1
    try:
        return float(s)
    except ValueError:
        return None

def numeros_de(texto):
    """Cifras del texto, tras tapar nombres de metrica, unidades y referencias
    normativas (que llevan digitos sin ser cantidades)."""
    texto = _MASCARA.sub(lambda m: " " * len(m.group(0)), texto or "")
    out = []
    for m in _NUM.finditer(texto):
        v = _a_float(m.group(0))
        if v is not None:
            out.append((m.group(0), v))
    return out

def numeros_permitidos(dig):
    """Todos los numeros que aparecen en el resumen, a cualquier profundidad."""
    vals = set()
    def rec(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            vals.add(float(x))
        elif isinstance(x, dict):
            for k, v in x.items():
                for _t, n in numeros_de(str(k)):
                    vals.add(n)
                rec(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                rec(v)
        elif isinstance(x, str):
            for _t, n in numeros_de(x):
                vals.add(n)
    rec(dig)
    return vals

def verifica(texto, dig, tol_rel=0.005):
    """
    Devuelve (ok, inventados). Un numero se acepta si:
      - es un entero de 0 a 31 (cuentas, horas, dias, ordinales), o
      - coincide con algun numero del resumen, o con su redondeo a 0 o 1
        decimales, dentro de una tolerancia relativa.
    """
    permitidos = numeros_permitidos(dig)
    amplios = set(permitidos)
    for v in permitidos:
        amplios.add(round(v))
        amplios.add(round(v, 1))
        amplios.add(abs(v))
        amplios.add(round(abs(v)))
        amplios.add(round(abs(v), 1))
    inventados = []
    for txt, v in numeros_de(texto):
        if float(v).is_integer() and 0 <= v <= 31:
            continue
        if any(abs(v - p) <= max(tol_rel * max(abs(p), 1.0), 0.051)
               for p in amplios):
            continue
        inventados.append(txt)
    # sin duplicados, conservando el orden de aparicion
    vistos = set(); limpio = []
    for x in inventados:
        if x not in vistos:
            vistos.add(x); limpio.append(x)
    return (not limpio), limpio


# ── Llamada a la API ─────────────────────────────────────────────────────────
def _hash(dig):
    return hashlib.sha256(
        json.dumps(dig, sort_keys=True, ensure_ascii=False,
                   default=str).encode("utf-8")).hexdigest()[:16]

def disponible():
    falta = []
    if not NARRA_ENABLE:
        falta.append("NARRA_ENABLE=1")
    if not NARRA_API_KEY:
        falta.append("NARRA_API_KEY")
    return (not falta), falta

def narra(result, acustica_inf=None, forzar=False):
    """
    Devuelve un dict con el texto y su comprobacion, o un dict con 'error'.
    Nunca lanza: si algo va mal, el sistema sigue con el motor de reglas.
    """
    ok, falta = disponible()
    if not ok:
        return {"disponible": False, "falta": falta,
                "nota": ("narrador desactivado: no se llama a ninguna API ni "
                         "se gasta nada. Para activarlo hace falta " +
                         " y ".join(falta) + ".")}
    dig = digest(result, acustica_inf)
    if not dig:
        return {"disponible": False, "nota": "sin analisis que narrar"}
    h = _hash(dig)
    hit = _cache.get(h)
    if hit and not forzar:
        r = dict(hit[1]); r["cacheado"] = True
        r["cache_edad_min"] = round((time.time() - hit[0]) / 60, 1)
        return r
    espera = NARRA_MIN_INTERVAL_H * 3600 - (time.time() - _ultima[0])
    if espera > 0 and not forzar:
        return {"disponible": True, "omitido": True,
                "nota": (f"han pasado menos de {NARRA_MIN_INTERVAL_H:g} h desde "
                         f"la ultima llamada; faltan {espera/60:.0f} min. Usa "
                         f"?forzar=1 para saltarte el limite.")}

    cuerpo = {
        "model": NARRA_MODEL,
        "messages": [
            {"role": "system", "content": SISTEMA},
            {"role": "user", "content":
                "Datos de la zona (todas las cifras ya calculadas):\n\n"
                + json.dumps(dig, ensure_ascii=False, indent=1, default=str)},
        ],
        "max_tokens": NARRA_MAX_TOKENS,
        "temperature": NARRA_TEMPERATURE,
    }
    try:
        r = requests.post(f"{NARRA_BASE_URL}/chat/completions",
                          headers={"Authorization": f"Bearer {NARRA_API_KEY}",
                                   "Content-Type": "application/json"},
                          json=cuerpo, timeout=NARRA_TIMEOUT)
        if r.status_code != 200:
            # nunca registrar la cabecera: lleva la clave
            log.warning("narrador: la API devolvio %s", r.status_code)
            return {"disponible": True, "error": f"HTTP {r.status_code}",
                    "detalle": (r.text or "")[:300],
                    "nota": "el sistema sigue con el motor de reglas."}
        j = r.json()
        texto = ((j.get("choices") or [{}])[0].get("message") or {}
                 ).get("content") or ""
        texto = texto.strip()
    except Exception as e:
        log.warning("narrador: fallo la llamada: %s", e)
        return {"disponible": True, "error": str(e)[:200],
                "nota": "el sistema sigue con el motor de reglas."}
    _ultima[0] = time.time()
    if not texto:
        return {"disponible": True, "error": "respuesta vacia"}

    bien, inventados = verifica(texto, dig)
    uso = j.get("usage") or {}
    ent = uso.get("prompt_tokens"); sal = uso.get("completion_tokens")
    coste = None
    if ent and sal and (NARRA_PRICE_IN or NARRA_PRICE_OUT):
        coste = round(ent / 1e6 * NARRA_PRICE_IN + sal / 1e6 * NARRA_PRICE_OUT, 6)
    out = {
        "disponible": True,
        "modelo": j.get("model") or NARRA_MODEL,
        "verificado": bien,
        "tokens": {"entrada": ent, "salida": sal},
        "coste_estimado": coste,
        "hash_datos": h,
    }
    if bien:
        out["texto"] = texto
        out["nota"] = ("todas las cifras del texto estan en los datos de "
                       "entrada.")
    else:
        # el texto NO se publica: se guarda aparte para poder inspeccionarlo
        out["texto"] = None
        out["texto_rechazado"] = texto
        out["numeros_inventados"] = inventados
        out["nota"] = ("DESCARTADO: el texto contiene cifras que no estan en "
                       "los datos (" + ", ".join(inventados[:8]) + "). Se sigue "
                       "con el motor de reglas. Un numero que el modelo se "
                       "inventa es exactamente el fallo que este modulo existe "
                       "para no publicar.")
    _cache[h] = (time.time(), out)
    if len(_cache) > 64:
        for k in sorted(_cache, key=lambda x: _cache[x][0])[:32]:
            _cache.pop(k, None)
    return out
