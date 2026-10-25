# SPDX-License-Identifier: GPL-3.0-or-later
#
# acustica.py — indicadores acústicos de largo plazo y veredicto normativo
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
Indicadores acústicos de largo plazo (Ld, Le, Ln, Lden) y contraste con el
RD 1367/2007 y con la guía de la OMS 2018.

NO CALCULA LOS ÍNDICES. Los nodos ya los publican, así que este módulo:

  1. descubre con qué nombre están los campos en InfluxDB (no se adivinan),
  2. los resume por nodo sobre la ventana de análisis,
  3. coteja los nodos entre sí EN DECIBELIOS, no en porcentaje,
  4. emite dos veredictos separados: el legal y el de salud.

Por qué en decibelios: el cotejo genérico de app.py mide el sesgo como
porcentaje del nivel medio, y eso no significa nada en una escala logarítmica
con cero arbitrario. Con un consenso de 40 dB, su umbral de 'divergente'
(300 %) exigiría que un nodo leyera 160 dB —por encima del rango físico— así
que ningún nodo de ruido podía salir divergente. Y la rho de Spearman es
ciega a un desplazamiento constante: el mismo patrón diario desplazado 42 dB
da rho = 1,000 exacto. Aquí el sesgo se mide como diferencia en dB y los
umbrales son acústicos: 1,5 dB es la tolerancia de un sonómetro de clase 2 y
3 dB es un doblaje de energía.

Aviso importante sobre los veredictos: son ORIENTATIVOS. El RD 1367/2007 se
refiere a valores anuales y una evaluación formal exige el procedimiento de la
ISO 1996-2 (altura de micrófono, distancia a superficies reflectantes, pantalla
antiviento, viento < 5 m/s, sin lluvia) y un equipo calibrado y verificado.
Esto es una red de sensores de bajo coste: sirve para orientar y para detectar
tendencias, no para acreditar cumplimiento ni para un informe pericial.

Referencias:
  RD 1367/2007, Anexo II, Tabla A, texto consolidado tras el RD 1038/2012
    https://www.boe.es/buscar/act.php?id=BOE-A-2007-18397
  RD 1513/2005, Anexo I (definición de Lden y de los periodos)
    https://www.boe.es/buscar/act.php?id=BOE-A-2005-20792
  WHO Environmental Noise Guidelines for the European Region, 2018
"""
import os
import math
import logging

log = logging.getLogger("acustica")

# ── Configuración ────────────────────────────────────────────────────────────
ACU_ENABLE = os.getenv("ACU_ENABLE", "1") not in ("0", "false", "no")

# Tipo de área acústica del RD 1367/2007 por zona: "ezt:a,u33:e". Si una zona
# no aparece, se usa ACU_AREA_DEFAULT. Esto NO se puede deducir de los datos:
# depende del planeamiento urbanístico, así que lo decide quien despliega.
ACU_AREA = os.getenv("ACU_AREA", "")
ACU_AREA_DEFAULT = os.getenv("ACU_AREA_DEFAULT", "a").strip().lower()

# Fuente dominante de ruido, para elegir la fila de la guía de la OMS.
ACU_FUENTE = os.getenv("ACU_FUENTE", "trafico").strip().lower()

# Estadístico con el que se resume la ventana: median (robusto), last o max.
ACU_STAT = os.getenv("ACU_STAT", "median").strip().lower()

# Umbrales del cotejo entre nodos, en dB.
ACU_COH_DB = float(os.getenv("ACU_COH_DB", "3.0"))   # <= esto, coherente
ACU_DIV_DB = float(os.getenv("ACU_DIV_DB", "6.0"))   # >  esto, divergente

# Recorrido máximo entre nodos para atreverse a dar un valor de zona.
ACU_MAX_SPREAD_DB = float(os.getenv("ACU_MAX_SPREAD_DB", "6.0"))

# Mínimo de muestras por nodo e indicador para tenerlo en cuenta.
ACU_MIN_N = int(os.getenv("ACU_MIN_N", "6"))

# Rango físico plausible para un nivel acústico de largo plazo. Un Lden de 0 o
# de 130 dB en un entorno urbano es un fallo de lectura, no una medida.
ACU_RANGE = (float(os.getenv("ACU_MIN_DB", "20")),
             float(os.getenv("ACU_MAX_DB", "110")))

# Nombres de campo forzados, si la autodetección no acierta:
#   ACU_FIELDS="lden=mi_lden,ld=mi_ld,le=mi_le,ln=mi_ln"
ACU_FIELDS = os.getenv("ACU_FIELDS", "")

# ── Normativa ────────────────────────────────────────────────────────────────
# RD 1367/2007, Anexo II, Tabla A: objetivos de calidad acústica para ruido
# aplicables a ÁREAS URBANIZADAS EXISTENTES, en dBA. Son objetivos por periodo,
# no en Lden.
RD_OBJETIVOS = {
    "e": {"ld": 60, "le": 60, "ln": 50,
          "desc": "Sanitario, docente y cultural"},
    "a": {"ld": 65, "le": 65, "ln": 55,
          "desc": "Residencial"},
    "d": {"ld": 70, "le": 70, "ln": 65,
          "desc": "Terciario distinto del recreativo"},
    "c": {"ld": 73, "le": 73, "ln": 63,
          "desc": "Recreativo y de espectáculos"},
    "b": {"ld": 75, "le": 75, "ln": 65,
          "desc": "Industrial"},
}

# OMS 2018. Recomendación FUERTE para tráfico rodado, ferrocarril y aeronaves;
# condicional para aerogeneradores y ocio.
OMS_GUIA = {
    "trafico":     {"lden": 53, "ln": 45, "fuerza": "fuerte",
                    "desc": "Tráfico rodado"},
    "ferrocarril": {"lden": 54, "ln": 44, "fuerza": "fuerte",
                    "desc": "Ferrocarril"},
    "aeronaves":   {"lden": 45, "ln": 40, "fuerza": "fuerte",
                    "desc": "Aeronaves"},
    "aerogenerador": {"lden": 45, "ln": None, "fuerza": "condicional",
                      "desc": "Aerogeneradores"},
}

# Periodos del RD 1513/2005, Anexo I. Solo informativos: no calculamos nada con
# ellos, los publicamos para que se pueda comprobar que coinciden con los que
# usa el nodo y con los del mapa estratégico contra el que se compare.
PERIODOS = {
    "ld": {"horas": 12, "penalizacion_db": 0,  "tramo_defecto": "07:00-19:00"},
    "le": {"horas": 4,  "penalizacion_db": 5,  "tramo_defecto": "19:00-23:00"},
    "ln": {"horas": 8,  "penalizacion_db": 10, "tramo_defecto": "23:00-07:00"},
}

ETIQUETAS = {"lden": "Lden (día-tarde-noche)", "ld": "Ld (día)",
             "le": "Le (tarde)", "ln": "Ln (noche)",
             "l90": "L90 (ruido de fondo)"}

# Indicadores sobre los que opina la normativa. El L90 NO está aquí a
# propósito: ni el RD 1367/2007 ni la guía de la OMS se definen sobre él, así
# que se lee y se usa para diagnosticar, pero nunca para dar un veredicto.
IND_NORMATIVOS = ("lden", "ld", "le", "ln")
IND_TODOS = IND_NORMATIVOS + ("l90",)

# Por debajo de esto, un L90 urbano es sospechoso: el fondo de un barrio rara
# vez baja de 30 dBA, así que apunta a suelo de ruido eléctrico del micrófono,
# a nodo en interior o a canal muerto.
ACU_L90_SOSPECHOSO = float(os.getenv("ACU_L90_SOSPECHOSO", "30"))

# ── Autodetección de los nombres de campo ────────────────────────────────────
# Alias por indicador, en minúsculas. OJO: 'ldn' NO es alias de 'lden'. El Ldn
# (day-night level, uso estadounidense) usa 15 h de día y 9 de noche y no lleva
# penalización de tarde, así que es otro indicador y confundirlos falsearía el
# veredicto.
_ALIAS = {
    "lden": ("lden", "l_den", "ldentarde", "laeq_den", "lden_db", "lden_dba",
             "ruido_lden", "noise_lden"),
    "ld":   ("ld", "lday", "l_d", "l_day", "laeq_d", "laeqd", "ldia", "l_dia",
             "ld_db", "ruido_ld", "noise_ld"),
    "le":   ("le", "levening", "l_e", "l_evening", "laeq_e", "laeqe", "ltarde",
             "l_tarde", "le_db", "ruido_le", "noise_le"),
    "ln":   ("ln", "lnight", "l_n", "l_night", "laeq_n", "laeqn", "lnoche",
             "l_noche", "ln_db", "ruido_ln", "noise_ln"),
    # L90: nivel superado el 90 % del tiempo, estimador habitual del ruido
    # residual o de fondo (ISO 1996 y la mayoría de ordenanzas municipales).
    "l90":  ("dbl90", "db_l90", "l90", "la90", "l_90", "laeq_l90",
             "ruido_l90", "noise_l90", "fondo", "l90_db"),
}
_RECHAZADOS = {"ldn", "l_dn", "ldn_db"}   # Ldn != Lden

def _forzados():
    out = {}
    for par in ACU_FIELDS.split(","):
        if "=" not in par:
            continue
        k, _, v = par.partition("=")
        k = k.strip().lower(); v = v.strip()
        if k in _ALIAS and v:
            out[k] = v
    return out

def descubre_campos(client, measurement):
    """
    Devuelve {indicador: nombre_de_campo} con los que existen de verdad en el
    measurement. Se consulta SHOW FIELD KEYS en vez de dar por supuesto el
    nombre, porque lo fija el firmware del nodo.
    """
    forz = _forzados()
    claves = []
    try:
        res = client.query(f'SHOW FIELD KEYS FROM "{measurement}"')
        for _clave, pts in res.items():
            for p in pts:
                k = p.get("fieldKey")
                if k:
                    claves.append(k)
    except Exception as e:
        log.debug("acustica: no pude listar los campos: %s", e)
        return forz, [], []
    por_min = {}
    for k in claves:
        por_min.setdefault(k.lower(), k)
    hallados = dict(forz)
    for ind, alias in _ALIAS.items():
        if ind in hallados:
            continue
        for a in alias:
            if a in por_min:
                hallados[ind] = por_min[a]
                break
    avisos = []
    for mal in _RECHAZADOS:
        if mal in por_min and "lden" not in hallados:
            avisos.append(
                f"existe un campo '{por_min[mal]}' pero NO se usa como Lden: "
                f"el Ldn usa 15 h de día y 9 de noche y no penaliza la tarde, "
                f"así que no es el mismo indicador. Si tu nodo publica Lden con "
                f"ese nombre, fuérzalo con ACU_FIELDS=lden={por_min[mal]}.")
    return hallados, sorted(claves), avisos

# ── Lectura ──────────────────────────────────────────────────────────────────
def lee_indicadores(client, measurement, geo3, hours, campos, mac=None):
    """
    Devuelve {mac: {indicador: [valores]}}.

    Usa last() por hora y no mean(): estos índices son promedios de largo plazo
    que el nodo ya ha calculado, y hacer la media aritmética de decibelios
    vuelve a introducir el error que el propio indicador evita. El último valor
    de cada hora es el que el nodo daba por bueno en ese momento.
    """
    if not campos:
        return {}
    sel = ", ".join(f'last("{f}") AS "{ind}"' for ind, f in sorted(campos.items()))
    q = (f'SELECT {sel} FROM "{measurement}" '
         f'WHERE "geo3" = \'{geo3}\' AND time > now() - {int(hours)}h ')
    if mac:
        q += f'AND "mac" = \'{mac}\' '
    q += 'GROUP BY time(1h), "mac" fill(none)'
    lo, hi = ACU_RANGE
    datos = {}
    try:
        res = client.query(q)
    except Exception as e:
        log.debug("acustica: consulta fallida en %s: %s", geo3, e)
        return {}
    for (_m, tags), pts in res.items():
        m = (tags or {}).get("mac")
        if not m:
            continue
        # ResultSet.items() devuelve generadores: materializar antes de usarlos
        # más de una vez
        for p in list(pts):
            for ind in campos:
                v = p.get(ind)
                if v is None:
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if not (lo <= v <= hi) or v != v:
                    continue
                datos.setdefault(m, {}).setdefault(ind, []).append(v)
    return datos

# ── Resumen y combinación ────────────────────────────────────────────────────
def _mediana(xs):
    s = sorted(xs); n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

def media_energetica(niveles):
    """
    10·log10( media( 10^(L/10) ) ). Es la forma correcta de combinar niveles en
    dB; la media aritmética de decibelios subestima siempre, y tanto más cuanto
    más variable es la señal.
    """
    xs = [x for x in niveles if x is not None]
    if not xs:
        return None
    return 10.0 * math.log10(sum(10.0 ** (x / 10.0) for x in xs) / len(xs))

def resume(valores):
    xs = [x for x in valores if x is not None]
    if len(xs) < ACU_MIN_N:
        return None
    if ACU_STAT == "last":
        val = xs[-1]
    elif ACU_STAT == "max":
        val = max(xs)
    else:
        val = _mediana(xs)
    lo_x, hi_x = float(min(xs)), float(max(xs))
    # Media energetica ADEMAS del estadistico elegido. Hace falta porque muchos
    # firmwares publican los indices redondeados a entero, y entonces la mediana
    # de dos nodos con distribuciones distintas empata en el mismo entero: en el
    # despliegue original tres nodos con medias de 32,8 / 33,8 / 36,8 dB daban
    # los tres mediana 32,0 y parecia que concordaban perfectamente. La media
    # energetica no colapsa y es la forma correcta de promediar niveles.
    ener = media_energetica(xs)
    return {"valor": round(float(val), 1),
            "estadistico": ACU_STAT,
            "energetica_db": round(float(ener), 1) if ener is not None else None,
            "n": len(xs),
            "min": round(lo_x, 1),
            "max": round(hi_x, 1),
            "ultimo": round(float(xs[-1]), 1),
            # un indice que no se mueve en toda la ventana no es una medida:
            # es un valor por defecto del firmware o un canal muerto
            "constante": bool(hi_x - lo_x < 1e-9),
            # publicado redondeado a entero: la mediana pierde resolucion
            "cuantizado": bool(all(abs(x - round(x)) < 1e-9 for x in xs))}

# ── Veredictos ───────────────────────────────────────────────────────────────
def _area_de(geo3):
    for par in ACU_AREA.split(","):
        if ":" not in par:
            continue
        z, _, a = par.partition(":")
        if z.strip().lower() == (geo3 or "").lower():
            a = a.strip().lower()
            if a in RD_OBJETIVOS:
                return a
    return ACU_AREA_DEFAULT if ACU_AREA_DEFAULT in RD_OBJETIVOS else "a"

def veredicto_rd(res, geo3):
    """
    Contraste con el RD 1367/2007, Anexo II, Tabla A. Por periodo, no en Lden.
    """
    area = _area_de(geo3)
    obj = RD_OBJETIVOS[area]
    detalle = {}
    faltan = []
    excesos = []
    for ind in ("ld", "le", "ln"):
        lim = obj[ind]
        r = (res or {}).get(ind)
        if not r:
            faltan.append(ind)
            detalle[ind] = {"objetivo_dba": lim, "valor": None}
            continue
        v = r["valor"]
        exceso = round(v - lim, 1)
        detalle[ind] = {"valor": v, "objetivo_dba": lim,
                        "exceso_db": exceso,
                        "dentro": bool(v <= lim)}
        if exceso > 0:
            excesos.append((ind, exceso))
    if faltan:
        return {"norma": "RD 1367/2007 Anexo II Tabla A",
                "area_acustica": area, "area_desc": obj["desc"],
                "estado": "sin_datos",
                "detalle": detalle,
                "nota": ("faltan " + ", ".join(ETIQUETAS[i] for i in faltan)
                         + ": el RD fija objetivos por periodo, así que sin los "
                           "tres no hay contraste posible.")}
    if not excesos:
        nota = (f"los tres periodos quedan dentro del objetivo de área tipo "
                f"'{area}' ({obj['desc']}): {obj['ld']}/{obj['le']}/{obj['ln']} dBA.")
        estado = "dentro_del_objetivo"
    else:
        peor = max(excesos, key=lambda x: x[1])
        estado = "fuera_del_objetivo"
        nota = ("supera el objetivo en "
                + ", ".join(f"{ETIQUETAS[i]} +{e} dB" for i, e in excesos)
                + f". El exceso mayor es {ETIQUETAS[peor[0]]}, +{peor[1]} dB.")
    return {"norma": "RD 1367/2007 Anexo II Tabla A",
            "area_acustica": area, "area_desc": obj["desc"],
            "objetivos_dba": {k: obj[k] for k in ("ld", "le", "ln")},
            "estado": estado, "detalle": detalle, "nota": nota}

def veredicto_oms(res):
    """
    Contraste con la guía de la OMS 2018: Lden y Lnight, que es la vara de
    'sonoramente saludable'. Es más exigente que el RD: con Ld/Le/Ln en
    65/65/55 se cumple el RD y se incumple la OMS por unos 13 dB.
    """
    fuente = ACU_FUENTE if ACU_FUENTE in OMS_GUIA else "trafico"
    g = OMS_GUIA[fuente]
    det = {}
    faltan = []
    excesos = []
    for ind, lim in (("lden", g["lden"]), ("ln", g["ln"])):
        if lim is None:
            continue
        r = (res or {}).get(ind)
        if not r:
            faltan.append(ind)
            det[ind] = {"recomendado_db": lim, "valor": None}
            continue
        v = r["valor"]
        exceso = round(v - lim, 1)
        det[ind] = {"valor": v, "recomendado_db": lim,
                    "exceso_db": exceso, "dentro": bool(v < lim)}
        if exceso >= 0:
            excesos.append((ind, exceso))
    if faltan:
        return {"guia": "OMS 2018 (Environmental Noise Guidelines)",
                "fuente": fuente, "fuente_desc": g["desc"],
                "fuerza_recomendacion": g["fuerza"],
                "estado": "sin_datos", "detalle": det,
                "nota": ("faltan " + ", ".join(ETIQUETAS[i] for i in faltan)
                         + " para poder contrastar.")}
    if not excesos:
        estado = "saludable"
        nota = (f"por debajo de lo que la OMS recomienda para {g['desc'].lower()} "
                f"(Lden < {g['lden']} dB, Lnight < {g['ln']} dB).")
    else:
        estado = "no_saludable"
        nota = ("por encima de la recomendación de la OMS en "
                + ", ".join(f"{ETIQUETAS[i]} +{e} dB" for i, e in excesos)
                + ". Es una recomendación de salud, no un límite legal: se puede "
                  "cumplir el RD y estar bastante por encima de esto.")
    return {"guia": "OMS 2018 (Environmental Noise Guidelines)",
            "fuente": fuente, "fuente_desc": g["desc"],
            "fuerza_recomendacion": g["fuerza"],
            "recomendado_db": {"lden": g["lden"], "ln": g["ln"]},
            "estado": estado, "detalle": det, "nota": nota}

# ── Cotejo entre nodos, en decibelios ────────────────────────────────────────
def cotejo_db(por_nodo, ind, nombres=None):
    """
    Cada nodo contra la mediana de los DEMÁS, con el sesgo expresado en dB.

    Umbrales acústicos, no porcentuales: <= ACU_COH_DB coherente (3 dB es un
    doblaje de energía; 1,5 dB la tolerancia de un sonómetro de clase 2),
    > ACU_DIV_DB divergente.
    """
    nombres = nombres or {}
    # si el indicador viene redondeado a entero, la mediana empata entre nodos
    # y el cotejo no distingue nada: se usa la media energetica, que si lo hace
    cuant = any((r.get(ind) or {}).get("cuantizado") for r in por_nodo.values())
    clave = "energetica_db" if cuant else "valor"
    vals = {m: ((r.get(ind) or {}).get(clave)
                or (r.get(ind) or {}).get("valor"))
            for m, r in por_nodo.items()}
    vals = {m: v for m, v in vals.items() if v is not None}
    if len(vals) < 3:
        return {"evaluable": False,
                "n_nodos": len(vals),
                "nota": (f"hacen falta al menos 3 nodos con {ETIQUETAS[ind]} "
                         f"para hablar de consenso; hay {len(vals)}.")}
    out = {}
    for m, v in vals.items():
        otros = [x for k, x in vals.items() if k != m]
        cons = _mediana(otros)
        dif = round(v - cons, 1)
        a = abs(dif)
        if a <= ACU_COH_DB:
            et = "coherente"
        elif a > ACU_DIV_DB:
            et = "divergente"
        else:
            et = "dudoso"
        out[m] = {"name": nombres.get(m, (m or "")[-6:]),
                  "valor": v, "consenso_db": round(cons, 1),
                  "dif_db": dif, "etiqueta": et}
    serie = list(vals.values())
    return {"evaluable": True, "indicador": ind, "n_nodos": len(vals),
            "base": clave, "cuantizado": cuant,
            "umbral_coherente_db": ACU_COH_DB,
            "umbral_divergente_db": ACU_DIV_DB,
            "recorrido_db": round(max(serie) - min(serie), 1),
            "nodos": out}

# -- Diagnostico: calibracion o exposicion real -------------------------------
def diagnostico(zona, por_nodo, nombres=None):
    """
    El L90 es el nivel superado el 90 % del tiempo, o sea el ruido de fondo de
    cuando no pasa nada. Eso lo convierte en el mejor separador entre dos
    explicaciones que el Lden no distingue:

      - si los nodos coinciden en L90 y discrepan en Lden, estan calibrados
        igual y lo que cambia es la EXPOSICION: distinta calle, distinto
        trafico. Cada nodo describe bien su sitio.
      - si discrepan ya en L90, el problema es de CALIBRACION o de
        emplazamiento, y entonces ningun otro indice es comparable entre nodos,
        porque todos arrastran el mismo desplazamiento.

    Un desplazamiento de calibracion se ve limpio en el L90 precisamente porque
    el fondo es estable y no lo dominan los eventos locales.
    """
    nombres = nombres or {}
    rec = {ind: (zona.get(ind) or {}).get("recorrido_db") for ind in IND_TODOS}
    out = {"recorridos_db": {k: v for k, v in rec.items() if v is not None}}

    def nom(mc):
        return nombres.get(mc, (mc or "")[-6:])

    def val(mc, ind):
        """Media energetica si la hay: no colapsa cuando el indice viene
        redondeado a entero, al contrario que la mediana."""
        d = (por_nodo.get(mc) or {}).get(ind) or {}
        return d.get("energetica_db") if d.get("energetica_db") is not None \
            else d.get("valor")

    # Conjuntos de nodos por indicador. Comparar el recorrido de L90 con el de
    # Lden solo vale si se calculan sobre LOS MISMOS nodos: si al nodo que
    # discrepa le falta el L90, el recorrido de L90 sale de los demas y la
    # comparacion no dice nada.
    con90 = {m for m in por_nodo if val(m, "l90") is not None}
    conden = {m for m in por_nodo if val(m, "lden") is not None}
    comunes = con90 & conden
    out["nodos_con_l90"] = sorted(nom(m) for m in con90)
    out["nodos_con_lden"] = sorted(nom(m) for m in conden)
    out["sin_l90"] = sorted(nom(m) for m in (conden - con90)) or None

    # L90 constante: tres microfonos distintos no coinciden a la decima, y un
    # valor que no se mueve en toda la ventana tampoco es una medida.
    const = [nom(m) for m in con90
             if ((por_nodo[m].get("l90") or {}).get("constante"))]
    v90 = [val(m, "l90") for m in con90]
    # 'plano entre nodos' solo cuenta sobre la media energetica: un empate de
    # medianas con el indice redondeado a entero es un artefacto del redondeo,
    # no un canal muerto. Confundirlos daba un falso 'L90 no utilizable'.
    plano_entre_nodos = (len(v90) > 1 and (max(v90) - min(v90)) < 0.05
                         and len(const) == len(con90))
    if const and len(const) == len(con90) or plano_entre_nodos:
        out["causa"] = "l90_no_utilizable"
        det = []
        if plano_entre_nodos:
            det.append(f"los {len(v90)} nodos dan el mismo L90 energetico "
                       f"({v90[0]} dBA) sin variacion en la ventana")
        if const:
            det.append("no varia en toda la ventana en " + ", ".join(const))
        out["l90_constante"] = const or None
        out["nota"] = (
            "el L90 no sirve para diagnosticar aqui: " + "; ".join(det) + ". "
            "Microfonos distintos en sitios distintos no pueden coincidir a la "
            "decima ni quedarse quietos una semana, asi que eso es un valor por "
            "defecto del firmware, un suelo fijo o un canal muerto, no ruido de "
            "fondo. Hasta aclararlo no se puede separar un problema de "
            "calibracion de una diferencia real de exposicion.")
        return out

    # el nodo que discrepa en Lden, ¿tiene L90?
    if len(conden) >= 3:
        discrepantes = []
        for m in conden:
            v = val(m, "lden")
            otros = [val(o, "lden") for o in conden if o != m]
            c = _mediana([x for x in otros if x is not None])
            if c is not None and abs(v - c) > ACU_MAX_SPREAD_DB:
                discrepantes.append(m)
        huerfanos = [nom(m) for m in discrepantes if m not in con90]
        if huerfanos:
            out["causa"] = "cobertura_insuficiente"
            out["discrepantes_sin_l90"] = sorted(huerfanos)
            out["nota"] = (
                "no puedo diagnosticar: los nodos que discrepan en Lden (%s) son "
                "justo los que no publican L90, asi que el recorrido de L90 se "
                "calcularia con los demas y no diria nada sobre ellos. Antes de "
                "concluir nada hay que conseguir el L90 de esos nodos."
                % ", ".join(sorted(huerfanos)))
            return out

    sospechosos = []
    for mc in con90:
        v = val(mc, "l90")
        if v is not None and v < ACU_L90_SOSPECHOSO:
            sospechosos.append({"name": nom(mc), "l90_db": v})
    if sospechosos:
        out["l90_sospechoso"] = sospechosos
        out["l90_sospechoso_nota"] = (
            "L90 por debajo de %g dBA: el fondo de un barrio rara vez baja de "
            "ahi, asi que apunta al suelo de ruido electrico del microfono, a "
            "un nodo en interior o a un canal muerto." % ACU_L90_SOSPECHOSO)

    # recorridos recalculados SOLO sobre los nodos comunes
    if len(comunes) >= 3:
        a90 = [val(m, "l90") for m in comunes]
        aden = [val(m, "lden") for m in comunes]
        r90 = round(max(a90) - min(a90), 1)
        rden = round(max(aden) - min(aden), 1)
        out["recorridos_db_comunes"] = {"l90": r90, "lden": rden,
                                        "n_nodos": len(comunes)}
    else:
        r90 = rden = None
        out["causa"] = "indeterminada"
        out["nota"] = (
            "solo %d nodo(s) publican L90 y Lden a la vez; hacen falta 3 para "
            "comparar los dos recorridos sobre el mismo conjunto."
            % len(comunes))
        return out

    if r90 > ACU_MAX_SPREAD_DB and (rden is None
                                    or rden > ACU_MAX_SPREAD_DB):
        out["causa"] = "calibracion_o_emplazamiento"
        out["nota"] = (
            "los nodos discrepan %s dB en el propio ruido de FONDO, por encima "
            "del maximo de %s dB, y arrastran la misma diferencia en los indices "
            "de exposicion. El fondo de una misma zona deberia parecerse mucho "
            "mas que los eventos, asi que esto no es exposicion distinta: es un "
            "desplazamiento de calibracion o de emplazamiento. Mientras siga "
            "asi, ningun indice es comparable entre nodos. Lo que toca es un "
            "calibrador acustico de 94 dB/1 kHz, no mas estadistica."
            % (r90, ACU_MAX_SPREAD_DB))
    elif r90 > ACU_MAX_SPREAD_DB:
        # el fondo discrepa pero los indices de exposicion no: no es un
        # desplazamiento global, es que a algun nodo le falla el suelo
        out["causa"] = "fondo_incoherente"
        out["nota"] = (
            "los nodos concuerdan en Lden (%s dB de recorrido) pero discrepan "
            "%s dB en el ruido de FONDO. Un desplazamiento de calibracion "
            "arrastraria los dos por igual, asi que esto apunta a uno o varios "
            "nodos con el suelo mal: canal muerto, microfono en interior o "
            "ganancia distinta a nivel bajo. Los indices de exposicion siguen "
            "siendo comparables; el L90 de los nodos senalados, no."
            % (rden, r90))
    elif rden is not None and rden > ACU_MAX_SPREAD_DB:
        out["causa"] = "exposicion_real"
        out["nota"] = (
            "los nodos concuerdan en el fondo (%s dB de recorrido) y discrepan "
            "en Lden (%s dB). Eso es coherente con estar bien calibrados y "
            "medir sitios distintos: calles con trafico distinto. El veredicto "
            "de cada nodo vale para SU emplazamiento; lo que no tiene sentido "
            "es un unico numero para toda la zona." % (r90, rden))
    else:
        out["causa"] = "nodos_concordantes"
        out["nota"] = (
            "concuerdan tanto en el fondo (%s dB) como en Lden (%s dB): los "
            "indices son comparables entre nodos y el valor de zona es "
            "defendible." % (r90, rden if rden is not None else "n/d"))
    return out

# ── Informe completo ─────────────────────────────────────────────────────────
def informe(client, measurement, geo3, hours, nombres=None):
    """
    Informe acústico de una zona. 'client' debe venir ya apuntando a la base de
    datos de los sensores. Devuelve None si no hay nada que informar.
    """
    if not ACU_ENABLE:
        return None
    campos, todos, avisos = descubre_campos(client, measurement)
    if not campos:
        return {"disponible": False,
                "campos_detectados": {},
                "campos_en_la_bd": todos[:60],
                "avisos": avisos or None,
                "nota": ("no encuentro ningún campo de Lden/Ld/Le/Ln en "
                         f"'{measurement}'. Si tus nodos los publican con otro "
                         "nombre, dímelo con "
                         "ACU_FIELDS=lden=...,ld=...,le=...,ln=...")}
    datos = lee_indicadores(client, measurement, geo3, hours, campos)
    if not datos:
        return {"disponible": False, "campos_detectados": campos,
                "avisos": avisos or None,
                "nota": (f"los campos existen pero ningún nodo de la zona "
                         f"{geo3} publicó valores dentro del rango "
                         f"{ACU_RANGE[0]:g}-{ACU_RANGE[1]:g} dB en las últimas "
                         f"{hours} h.")}
    nombres = nombres or {}
    por_nodo = {}
    for m, inds in datos.items():
        r = {}
        for ind, vals in inds.items():
            s = resume(vals)
            if s:
                r[ind] = s
        if not r:
            continue
        por_nodo[m] = r
    if not por_nodo:
        return {"disponible": False, "campos_detectados": campos,
                "nota": (f"ningún nodo alcanza las {ACU_MIN_N} muestras "
                         f"mínimas por indicador.")}

    nodos = {}
    for m, r in por_nodo.items():
        nodos[m] = {
            "name": nombres.get(m, (m or "")[-6:]),
            "indicadores": r,
            "rd_1367": veredicto_rd(r, geo3),
            "oms_2018": veredicto_oms(r),
        }

    cotejos = {ind: cotejo_db(por_nodo, ind, nombres)
               for ind in IND_TODOS
               if any(ind in r for r in por_nodo.values())}

    # ── valor de zona, solo si los nodos se parecen lo suficiente ───────────
    zona = {}
    for ind in IND_TODOS:
        vals = [(r.get(ind) or {}).get("valor") for r in por_nodo.values()]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        rec = round(max(vals) - min(vals), 1) if len(vals) > 1 else 0.0
        bloque = {"n_nodos": len(vals), "recorrido_db": rec,
                  "mediana_db": round(_mediana(vals), 1),
                  "energetica_db": round(media_energetica(vals), 1)}
        if len(vals) > 1 and rec > ACU_MAX_SPREAD_DB:
            bloque["fiable"] = False
            bloque["nota"] = (
                f"los nodos discrepan {rec} dB en {ETIQUETAS[ind]}, por encima "
                f"del máximo admitido ({ACU_MAX_SPREAD_DB} dB). No publico un "
                f"valor de zona: con esa dispersión cada nodo daría un veredicto "
                f"distinto para la misma calle, y el problema es de calibración, "
                f"no de aritmética.")
        else:
            bloque["fiable"] = True
            bloque["nota"] = (
                f"los nodos concuerdan dentro de {ACU_MAX_SPREAD_DB} dB "
                f"(recorrido {rec} dB).")
        zona[ind] = bloque

    # veredicto de zona solo con los indicadores fiables
    res_zona = {ind: {"valor": b["energetica_db"]}
                for ind, b in zona.items()
                if b.get("fiable") and ind in IND_NORMATIVOS}
    if res_zona:
        v_rd = veredicto_rd(res_zona, geo3)
        v_oms = veredicto_oms(res_zona)
    else:
        v_rd = v_oms = None

    diag = diagnostico(zona, por_nodo, nombres)

    divergentes = [d["name"] for c in cotejos.values() if c.get("evaluable")
                   for d in c["nodos"].values() if d["etiqueta"] == "divergente"]

    return {
        "disponible": True,
        "geo3": geo3,
        "hours": hours,
        "campos_detectados": campos,
        "avisos": avisos or None,
        "estadistico": ACU_STAT,
        "periodos_rd_1513_2005": PERIODOS,
        "nodos": nodos,
        "cotejo_entre_nodos": cotejos,
        "zona": zona,
        "zona_rd_1367": v_rd,
        "zona_oms_2018": v_oms,
        "diagnostico": diag,
        "nodos_divergentes": sorted(set(divergentes)) or None,
        "limitaciones": [
            "Veredictos ORIENTATIVOS: el RD 1367/2007 se refiere a valores "
            "anuales, y una evaluación formal exige el procedimiento de la "
            "ISO 1996-2 con equipo calibrado y verificado.",
            "Los índices los calcula el firmware del nodo; este módulo solo los "
            "lee, los resume y los contrasta.",
            "La altura del micrófono, la distancia a superficies reflectantes y "
            "la pantalla antiviento cambian el nivel varios dB, así que dos "
            "nodos bien calibrados pueden diferir legítimamente unos pocos dB.",
            "Comprueba que los tramos horarios del nodo coinciden con los del "
            "mapa estratégico contra el que compares: el RD 1513/2005 permite a "
            "la administración recortar la tarde una o dos horas.",
        ],
    }
