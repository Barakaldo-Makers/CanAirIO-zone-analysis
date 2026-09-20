# SPDX-License-Identifier: GPL-3.0-or-later
#
# canairio-zone-analysis — statistical analysis engine for CanAirIO air-quality zones
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
ai-bridge v2 — Análisis riguroso de calidad del aire CanAirIO por zona (geo3)
==============================================================================
Flujo por zona:
  InfluxDB (agregado por hora) -> limpieza (rangos físicos + outliers MAD +
  interpolación limitada) -> estadística (Mann-Kendall/Theil-Sen, correlaciones
  Spearman, IC de la media) -> clima Open-Meteo (viento, lluvia, T) ->
  LLM (Ollama) redacta avisos/tendencia/predicción en JSON.

Diseño para 8 GB RAM:
  - Solo numpy + scipy (sin pandas/statsmodels): series pequeñas ya agregadas.
  - InfluxDB hace la agregación pesada (GROUP BY time(1h)).
  - El LLM recibe estadística calculada, no datos crudos (prompts cortos).

Endpoints:
  GET /health                  estado del servicio
  GET /zones                   zonas geo3 activas con nº de puntos recientes
  GET /stats/<geo3>?hours=48   estadística pura de una zona (sin LLM)
  GET /weather/<geo3>          clima actual+forecast de la zona
  GET /analysis/<geo3>?hours=48  análisis completo de una zona (con LLM)
  GET /analysis/all?hours=48   análisis de las N zonas más activas (con LLM)
"""
import os
import json
import math
import time
import logging
import requests
import numpy as np
from scipy import stats as sps
from flask import Flask, jsonify, request
from influxdb import InfluxDBClient
from apscheduler.schedulers.background import BackgroundScheduler
import publisher
import euskadi
import openaq

app = Flask(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ai-bridge")

# ── Configuración ────────────────────────────────────────────────────────────
INFLUX_HOST = os.getenv("INFLUXDB_HOST", "influxdb")
INFLUX_PORT = int(os.getenv("INFLUXDB_PORT", "8086"))
INFLUX_USER = os.getenv("INFLUXDB_USER", "admin")
INFLUX_PASS = os.getenv("INFLUXDB_PASSWORD", "")
INFLUX_DB   = os.getenv("INFLUXDB_DB", "canairio")
MEASUREMENT = os.getenv("MEASUREMENT", "fixed_stations_01")

OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi4-mini")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "350"))
# Modo de redaccion del informe:
#   "rules" = sin LLM, resumen determinista (recomendado en CPU antigua)
#   "llm"   = solo LLM
#   "auto"  = intenta LLM y si falla cae a reglas
ANALYSIS_MODE = os.getenv("ANALYSIS_MODE", "auto").lower()

# Historico de analisis: base de datos InfluxDB SEPARADA de canairio
# (sobrevive a reimportaciones/drops de la base de datos de datos crudos).
ANALYSIS_DB = os.getenv("ANALYSIS_DB", "analysis")
ANALYSIS_MEAS = os.getenv("ANALYSIS_MEAS", "zone_analysis")
# Ventana para considerar una estación "activa" en el listado de sensores web.
SENSORS_ACTIVE_HOURS = int(os.getenv("SENSORS_ACTIVE_HOURS", "48"))
SENSORS_MAX = int(os.getenv("SENSORS_MAX", "2000"))
# horas de ventana para el análisis por sensor en la publicación web
PUBLISH_HOURS = int(os.getenv("PUBLISH_HOURS", "48"))

# Telegram (opcional): si faltan token o chat_id, simplemente no se envia.
# CHAT_ID puede ser un usuario (positivo) o un grupo (negativo, p.ej. -100...).
# THREAD_ID (opcional): id del tema/topic dentro de un grupo con temas.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID", "")
DAILY_SUMMARY_HOUR = int(os.getenv("DAILY_SUMMARY_HOUR", "8"))  # hora local

ANALYSIS_INTERVAL = int(os.getenv("ANALYSIS_INTERVAL", "1800"))   # s (30 min)
MAX_ZONES         = int(os.getenv("MAX_ZONES", "0"))    # 0 = sin límite (tiers)
DEFAULT_HOURS     = int(os.getenv("DEFAULT_HOURS", "48"))
ACTIVE_WINDOW_H   = int(os.getenv("ACTIVE_WINDOW_H", "24"))  # ventana para "zona activa"

# ── Estratificación de zonas por actividad (P2.4) ────────────────────────────
# Permite analizar todas las zonas del mundo sin saturar la CPU:
# cada tier determina con qué frecuencia se analiza y qué análisis se hace.
TIER1_HOURS = int(os.getenv("TIER1_HOURS", "2"))     # activa: análisis completo
TIER2_HOURS = int(os.getenv("TIER2_HOURS", "24"))    # reciente: análisis completo
TIER3_HOURS = int(os.getenv("TIER3_HOURS", "168"))   # dormida: solo estadística
# >TIER3_HOURS: histórica: se calcula una vez al día, sin meteo ni rosa
TIER3_NO_WEATHER = os.getenv("TIER3_NO_WEATHER", "1") == "1"
MAX_GAP_HOURS     = float(os.getenv("MAX_GAP_HOURS", "2"))
MAD_FACTOR        = float(os.getenv("MAD_FACTOR", "5.0"))

# P1.1: Historical Forecast API (inicializada con observaciones reales, más
# precisa que el pronóstico simple). Cobertura global, sin API key.
OPEN_METEO_URL      = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_HIST_URL = os.getenv(
    "OPEN_METEO_HIST_URL",
    "https://historical-forecast-api.open-meteo.com/v1/forecast")
OPEN_METEO_AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WEATHER_CACHE_TTL = int(os.getenv("WEATHER_CACHE_TTL", "1800"))  # s
# Comparacion con referencia regional CAMS (Copernicus, satelite+estaciones).
SATCOMP_ENABLED = os.getenv("SATCOMP_ENABLED", "1") == "1"
SATCOMP_CACHE_TTL = int(os.getenv("SATCOMP_CACHE_TTL", "3600"))

# Campos del schema CanAirIO fixed_stations_01 que analizamos.
# Los gases llegan en ppm del sensor y se CONVIERTEN a µg/m³ (CO a mg/m³)
# usando la T y P medidas por la propia estacion. Rangos en unidad final.
METRICS = {
    "pm25": {"label": "PM2.5 (µg/m³)", "range": (0, 500)},
    "pm10": {"label": "PM10 (µg/m³)",  "range": (0, 1000)},
    "pm1":  {"label": "PM1 (µg/m³)",   "range": (0, 500)},
    "co2":  {"label": "CO2 (ppm)",     "range": (300, 5000)},
    "co":   {"label": "CO (mg/m³)",    "range": (0, 120)},
    "no2":  {"label": "NO2 (µg/m³)",   "range": (0, 1000)},
    "o3":   {"label": "O3 (µg/m³)",    "range": (0, 500)},
    "nh3":  {"label": "NH3 (µg/m³)",   "range": (0, 400)},
    "tmp":  {"label": "Temperatura (°C)", "range": (-40, 60)},
    "hum":  {"label": "Humedad (%)",   "range": (0, 100)},
    "prs":  {"label": "Presión (hPa)", "range": (800, 1100)},
    "db":   {"label": "Ruido (dB)",    "range": (0, 140)},
}

# Masas molares (g/mol) de los gases que el sensor reporta en ppm.
# factor_unidad: 1000 -> µg/m³ ; 1 -> mg/m³ (para CO).
GAS_CONVERSION = {
    "no2": {"molar_mass": 46.01, "to": "µg/m³", "factor": 1000.0},
    "o3":  {"molar_mass": 48.00, "to": "µg/m³", "factor": 1000.0},
    "nh3": {"molar_mass": 17.03, "to": "µg/m³", "factor": 1000.0},
    "co":  {"molar_mass": 28.01, "to": "mg/m³", "factor": 1.0},
}

# Umbrales de aviso en la UNIDAD FINAL (gases ya convertidos).
# no2/o3/nh3 en µg/m³; co en mg/m³ (OMS 8h ≈ 10 mg/m³).
THRESHOLDS = {
    "pm25": {"warn": 25,   "critical": 75},
    "pm10": {"warn": 50,   "critical": 150},
    "co2":  {"warn": 1000, "critical": 2000},
    "co":   {"warn": 10,   "critical": 35},
    "no2":  {"warn": 40,   "critical": 200},
    "nh3":  {"warn": 25,   "critical": 50},
    "o3":   {"warn": 100,  "critical": 180},
    "tmp":  {"warn": 35,   "critical": 40},
    "hum":  {"warn": 80,   "critical": 95},
    "db":   {"warn": 65,   "critical": 85},
}

# ── Geohash -> lat/lon (decodificador mínimo, sin dependencias) ─────────────
_GH32 = "0123456789bcdefghjkmnpqrstuvwxyz"

def geohash_center(gh):
    """Devuelve (lat, lon) del centro de la celda geohash (case-insensitive)."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    even = True
    for ch in gh.lower():
        idx = _GH32.find(ch)
        if idx < 0:
            raise ValueError(f"geohash inválido: {gh!r}")
        for bit in (16, 8, 4, 2, 1):
            if even:
                mid = (lon_lo + lon_hi) / 2
                if idx & bit: lon_lo = mid
                else:         lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if idx & bit: lat_lo = mid
                else:         lat_hi = mid
            even = not even
    return (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2

# ── InfluxDB ─────────────────────────────────────────────────────────────────
def influx_client():
    return InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT,
                          username=INFLUX_USER, password=INFLUX_PASS or None,
                          database=INFLUX_DB)

def active_zones(hours=None):
    """Zonas geo3 con datos en la ventana reciente, ordenadas por nº de puntos."""
    hours = hours or ACTIVE_WINDOW_H
    c = influx_client()
    try:
        q = (f'SELECT count("pm25") FROM "{MEASUREMENT}" '
             f'WHERE time > now() - {hours}h GROUP BY "geo3"')
        res = c.query(q)
        zones = []
        for (meas, tags), pts in res.items():
            n = next(iter(pts), {}).get("count", 0)
            if tags and tags.get("geo3") and n:
                zones.append({"geo3": tags["geo3"], "points": n})
        zones.sort(key=lambda z: -z["points"])
        return zones
    finally:
        c.close()


def all_zones_tiered():
    """
    P2.4: Devuelve TODAS las zonas con datos, clasificadas por tier según la
    antigüedad de su último dato. Sin límite de zonas (análisis global).
    Tier 1 activa  < TIER1_HOURS  -> análisis completo cada ciclo
    Tier 2 reciente < TIER2_HOURS  -> análisis completo cada 2 ciclos
    Tier 3 dormida  < TIER3_HOURS  -> solo estadística (sin meteo/rosa)
    Tier 4 histórica >= TIER3_HOURS -> una vez al día, solo estadística
    """
    c = influx_client()
    try:
        # última lectura por zona (pm25 o cualquier campo presente)
        q = (f'SELECT last("pm25") FROM "{MEASUREMENT}" '
             f'GROUP BY "geo3"')
        res = c.query(q, epoch="s")
        zones = []
        now = time.time()
        for (meas, tags), pts in res.items():
            geo3 = (tags or {}).get("geo3")
            if not geo3:
                continue
            p = next(iter(pts), {})
            last_t = p.get("time") or 0
            age_h = (now - last_t) / 3600 if last_t else 9999
            if age_h < TIER1_HOURS:
                tier = 1
            elif age_h < TIER2_HOURS:
                tier = 2
            elif age_h < TIER3_HOURS:
                tier = 3
            else:
                tier = 4
            zones.append({"geo3": geo3, "tier": tier,
                          "age_h": round(age_h, 1)})
        zones.sort(key=lambda z: (z["tier"], z["age_h"]))
        return zones
    finally:
        c.close()

# Detección de valor congelado: métricas donde una hora entera con el MISMO
# valor exacto (stddev=0) es físicamente imposible -> se descarta como
# no-medición (cuelgue de la tarea de audio observado en nodos XIAO).
# P1.5: detección de valor congelado ampliada a variables continuas que jamás
# son exactamente constantes una hora entera si el sensor funciona correctamente.
# tmp y hum cubren la congelación total del nodo XIAO (todas las métricas a 0).
FREEZE_DETECT_METRICS = set(
    x.strip() for x in os.getenv(
        "FREEZE_DETECT_METRICS", "db,tmp,hum").split(",")
    if x.strip())

def zone_sensors(geo3, hours=None):
    """Lista los sensores (mac + nombre) activos de una zona geo3."""
    hours = hours or ACTIVE_WINDOW_H
    c = influx_client()
    try:
        q = (f'SELECT count("pm25") FROM "{MEASUREMENT}" '
             f"WHERE \"geo3\" = '{geo3}' AND time > now() - {hours}h "
             f'GROUP BY "mac"')
        res = c.query(q)
        macs = []
        for (meas, tags), pts in res.items():
            n = next(iter(pts), {}).get("count", 0)
            if tags and tags.get("mac") and n:
                macs.append({"mac": tags["mac"], "points": n})
        macs.sort(key=lambda s: -s["points"])
        return macs
    finally:
        c.close()


# Combinación entre sensores dentro de una zona: "median" o "mean".
# La mediana aguanta que un sensor esté descalibrado; la media no.
ZONE_AGG = os.getenv("ZONE_AGG", "median")
# Un nodo sin sensor para una métrica publica 0 en todas sus lecturas (el
# firmware de CanAirIO rellena el campo en vez de omitirlo). Si TODAS las
# lecturas de un sensor en la ventana son exactamente 0, ese sensor no mide esa
# métrica y hay que EXCLUIRLO, no promediarlo: los nodos de ruido y de gases
# hundían el PM de la zona (0,33 µg/m³ con 93% de los puntos a cero).
ZERO_MEANS_ABSENT = os.getenv("ZERO_MEANS_ABSENT", "1") == "1"


def fetch_zone_series(geo3, hours, mac=None, info=None):
    """
    Serie horaria de cada métrica para la zona.

    Con `mac` analiza ese sensor solo. Sin `mac` agrega la zona, y lo hace en
    DOS pasos: primero la media horaria de CADA sensor, después la mediana
    entre sensores. Antes se hacía un único mean() sobre todos los puntos, lo
    que ponderaba por frecuencia de publicación: un nodo que publica cada 30 s
    pesaba diez veces más que uno de cada 5 min.

    `info`, si se pasa, se rellena con el detalle de la agregación
    (sensores usados y excluidos por métrica).

    Devuelve dict: { metric: (times_epoch_s ndarray, values ndarray) }
    """
    sel = ", ".join(f'mean("{m}") AS "{m}"' for m in METRICS)
    extra = ", ".join(f'stddev("{m}") AS "{m}__sd"'
                      for m in FREEZE_DETECT_METRICS if m in METRICS)
    if extra:
        sel = sel + ", " + extra
    where = f"\"geo3\" = '{geo3}' AND time > now() - {hours}h"
    if mac:
        where = f"\"mac\" = '{mac}' AND " + where
    q = (f'SELECT {sel} FROM "{MEASUREMENT}" '
         f"WHERE {where} "
         f'GROUP BY "mac", time(1h) fill(none)')
    c = influx_client()
    try:
        res = c.query(q, epoch="s")
        # por_mac[mac][metric] = {hora: valor}
        por_mac = {}
        for (_meas, tags), pts in res.items():
            mk = (tags or {}).get("mac") or mac or "?"
            dest = por_mac.setdefault(mk, {})
            for p in pts:
                t = p["time"]
                for m in METRICS:
                    v = p.get(m)
                    if v is None:
                        continue
                    # hora congelada (mismo valor exacto toda la hora).
                    # stddev es None con un solo punto: sin evidencia, se mantiene.
                    if m in FREEZE_DETECT_METRICS and p.get(f"{m}__sd") == 0:
                        continue
                    dest.setdefault(m, {})[t] = float(v)
    finally:
        c.close()

    usados, excluidos = {}, {}
    # 1) descarta la métrica en los sensores que no la miden (todo a cero)
    for mk, metrics in por_mac.items():
        for m in list(metrics):
            vals = metrics[m]
            if ZERO_MEANS_ABSENT and vals and all(v == 0.0 for v in vals.values()):
                excluidos.setdefault(m, []).append(mk)
                del metrics[m]
            elif vals:
                usados.setdefault(m, []).append(mk)

    # 2) combina entre sensores, hora a hora
    series = {}
    for m in METRICS:
        por_hora = {}
        for metrics in por_mac.values():
            for t, v in (metrics.get(m) or {}).items():
                por_hora.setdefault(t, []).append(v)
        if len(por_hora) < 3:
            continue
        ts = sorted(por_hora)
        if ZONE_AGG == "mean":
            vs = [float(np.mean(por_hora[t])) for t in ts]
        else:
            vs = [float(np.median(por_hora[t])) for t in ts]
        series[m] = (np.asarray(ts, dtype=np.float64),
                     np.asarray(vs, dtype=np.float64))

    if info is not None:
        info["agg"] = ZONE_AGG
        info["sensors_seen"] = len(por_mac)
        info["sensors_used"] = {m: sorted(v) for m, v in usados.items()}
        info["sensors_excluded_all_zero"] = {m: sorted(v)
                                             for m, v in excluidos.items()}
    if excluidos:
        log.info("zona %s: excluidos por leer siempre 0 -> %s", geo3,
                 "; ".join(f"{m}: {len(v)}" for m, v in excluidos.items()))
    return series

# ── Limpieza ─────────────────────────────────────────────────────────────────
def clean_series(t, v, metric):
    """
    1) Rango físico válido. 2) Outliers por MAD robusto (descarta |z|>MAD_FACTOR).
    3) Interpolación lineal solo de huecos <= MAX_GAP_HOURS.
    Devuelve (t, v, info_dict).
    """
    lo, hi = METRICS[metric]["range"]
    mask = (v >= lo) & (v <= hi)
    n_range = int((~mask).sum())
    t, v = t[mask], v[mask]
    n_out = 0
    if len(v) >= 5:
        med = np.median(v)
        mad = np.median(np.abs(v - med))
        if mad > 0:
            z = 0.6745 * (v - med) / mad
            keep = np.abs(z) <= MAD_FACTOR
            n_out = int((~keep).sum())
            t, v = t[keep], v[keep]
    n_interp = 0
    if len(v) >= 3:
        # interpola huecos horarios pequeños sobre una rejilla por hora
        t0, t1 = t[0], t[-1]
        grid = np.arange(t0, t1 + 1, 3600.0)
        vi = np.interp(grid, t, v)
        # invalida tramos cuyo hueco original supera MAX_GAP_HOURS
        gap_ok = np.ones_like(grid, dtype=bool)
        max_gap = MAX_GAP_HOURS * 3600.0
        for i in range(len(t) - 1):
            if (t[i + 1] - t[i]) > max_gap:
                bad = (grid > t[i]) & (grid < t[i + 1])
                gap_ok &= ~bad
        n_interp = int(gap_ok.sum() - len(t)) if gap_ok.sum() > len(t) else 0
        t, v = grid[gap_ok], vi[gap_ok]
    return t, v, {"dropped_range": n_range, "dropped_outlier": n_out,
                  "interpolated": max(n_interp, 0)}

# ── Estadística ──────────────────────────────────────────────────────────────
def analyze_series(t, v):
    """
    Tendencia: Mann-Kendall (tau de Kendall vs tiempo) + pendiente Theil-Sen.
    Incertidumbre: IC95% de la media (t de Student sobre el error estándar).
    """
    n = len(v)
    mean = float(np.mean(v)); std = float(np.std(v, ddof=1)) if n > 1 else 0.0
    sem = std / math.sqrt(n) if n > 1 else 0.0
    tcrit = sps.t.ppf(0.975, n - 1) if n > 1 else 0.0
    th = t / 3600.0  # horas, para pendiente en unidades/hora
    tau, p_mk = sps.kendalltau(th, v)
    slope, intercept, slo, shi = sps.theilslopes(v, th, 0.95)
    if p_mk is None or np.isnan(p_mk):
        trend = "indeterminada"
    elif p_mk >= 0.05:
        trend = "estable"
    else:
        trend = "creciente" if slope > 0 else "decreciente"
    return {
        "n": n,
        "mean": round(mean, 2),
        "std": round(std, 2),
        "min": round(float(np.min(v)), 2),
        "max": round(float(np.max(v)), 2),
        "p95": round(float(np.percentile(v, 95)), 2),
        "ci95_mean": [round(mean - tcrit * sem, 2), round(mean + tcrit * sem, 2)],
        "trend": trend,
        "kendall_tau": round(float(tau), 3) if tau == tau else None,
        "p_value": round(float(p_mk), 4) if p_mk == p_mk else None,
        "slope_per_hour": round(float(slope), 4),
        "slope_ci95": [round(float(slo), 4), round(float(shi), 4)],
        "last_value": round(float(v[-1]), 2),
    }

def correlations(series_clean):
    """Spearman entre pares de contaminantes con suficientes puntos comunes."""
    keys = [k for k in ("pm25", "pm10", "no2", "o3", "co2", "db", "tmp", "hum")
            if k in series_clean]
    out = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            ta, va = series_clean[a]; tb, vb = series_clean[b]
            common, ia, ib = np.intersect1d(ta, tb, return_indices=True)
            if len(common) < 12:
                continue
            rho, p = sps.spearmanr(va[ia], vb[ib])
            if p == p and p < 0.05 and abs(rho) >= 0.4:
                out.append({"pair": f"{a}~{b}", "spearman_rho": round(float(rho), 2),
                            "p_value": round(float(p), 4), "n": int(len(common))})
    return out

def detect_alerts(stats_by_metric, confidence=None, prev_alerts=None,
                  series_recent=None):
    """
    P1.3 — Persistencia e histéresis:
    - Persistencia: la alerta requiere que al menos ALERT_PERSIST_H horas
      consecutivas recientes superen el umbral (no solo el último dato).
      Si series_recent no está disponible, actúa igual que antes (compatible).
    - Histéresis: una alerta activa (prev_alerts) se mantiene hasta que el
      valor baje por debajo de umbral × (1 - HYSTERESIS).
    """
    HYSTERESIS = float(os.getenv("ALERT_HYSTERESIS", "0.10"))
    PERSIST_H  = int(os.getenv("ALERT_PERSIST_H", "1"))
    confidence = confidence or {}
    prev_set = {a["parameter"]: a.get("level") for a in (prev_alerts or [])}
    alerts = []
    for m, s in stats_by_metric.items():
        th = THRESHOLDS.get(m)
        if not th:
            continue
        conf = confidence.get(m, {}).get("label")
        if conf == "no_fiable":
            continue
        val = s.get("last_value")
        if val is None:
            continue
        was_active = prev_set.get(m)
        level = None; thr = None
        for lvl, umb in [("critical", th["critical"]), ("warning", th["warn"])]:
            if val < umb:
                continue
            # persistencia: si tenemos series recientes, verificar N horas
            if series_recent and m in series_recent and PERSIST_H > 1:
                _, v_rec = series_recent[m]
                # contar cuántos de los últimos PERSIST_H valores superan el umbral
                recent_vals = v_rec[-PERSIST_H:] if len(v_rec) >= PERSIST_H else v_rec
                n_over = int(np.sum(recent_vals >= umb))
                if n_over < min(PERSIST_H, len(recent_vals)):
                    continue   # no persistente aún
            level = lvl; thr = umb; break
        # histéresis: mantener alerta activa hasta bajar HYSTERESIS%
        if level is None and was_active:
            for lvl, umb in [("critical", th["critical"]), ("warning", th["warn"])]:
                if was_active == lvl and val >= umb * (1 - HYSTERESIS):
                    level = lvl; thr = umb; break
        if level:
            alerts.append({"parameter": m, "value": val, "level": level,
                           "threshold": thr,
                           "confidence": conf or "sin_referencia"})
    return alerts

# ── Clima Open-Meteo (con caché en memoria) ─────────────────────────────────
_weather_cache = {}

def fetch_weather(geo3):
    now = time.time()
    hit = _weather_cache.get(geo3)
    if hit and now - hit[0] < WEATHER_CACHE_TTL:
        return hit[1]
    lat, lon = geohash_center(geo3)
    params = {
        "latitude": round(lat, 4), "longitude": round(lon, 4),
        "hourly": ("temperature_2m,relative_humidity_2m,precipitation,"
                   "wind_speed_10m,wind_direction_10m,surface_pressure,"
                   "weathercode,cloud_cover,visibility,uv_index,"
                   "snowfall,precipitation_probability"),
        "past_hours": 24, "forecast_hours": 12,
        "wind_speed_unit": "kmh", "timezone": "UTC",
    }
    r = requests.get(OPEN_METEO_URL, params=params, timeout=30)
    r.raise_for_status()
    h = r.json().get("hourly", {})
    times = h.get("time", [])
    if not times:
        return None
    k = min(24, len(times) - 1)
    def at(name, idx):
        arr = h.get(name) or []
        return arr[idx] if idx < len(arr) and arr[idx] is not None else None
    def seg(name, a, b):
        arr = [x for x in (h.get(name) or [])[a:b] if x is not None]
        return arr
    wind_now  = at("wind_speed_10m", k)
    wdir_now  = at("wind_direction_10m", k)
    wcode_now = at("weathercode", k)
    fc_wind   = seg("wind_speed_10m", k, k + 12)
    fc_prec   = seg("precipitation", k, k + 12)
    fc_snow   = seg("snowfall", k, k + 12)
    fc_prob   = seg("precipitation_probability", k, k + 12)
    past_wind = seg("wind_speed_10m", max(0, k - 24), k)
    w = {
        "lat": round(lat, 3), "lon": round(lon, 3),
        "now": {
            "temperature_c":  at("temperature_2m", k),
            "humidity_pct":   at("relative_humidity_2m", k),
            "wind_kmh":       wind_now,
            "wind_dir_deg":   wdir_now,
            "wind_dir_txt":   wind_dir_text(wdir_now),
            "pressure_hpa":   at("surface_pressure", k),
            "cloud_cover_pct": at("cloud_cover", k),
            "visibility_km":  round(at("visibility", k) / 1000, 1)
                              if at("visibility", k) is not None else None,
            "uv_index":       at("uv_index", k),
            "weathercode":    wcode_now,
            "weather_desc":   weathercode_desc(wcode_now),
            "weather_icon":   weathercode_icon(wcode_now),
        },
        "past24h": {
            "wind_mean_kmh":  round(float(np.mean(past_wind)), 1) if past_wind else None,
            "precip_total_mm": round(float(np.sum(seg("precipitation", max(0, k-24), k))), 1),
        },
        "next12h": {
            "wind_mean_kmh":  round(float(np.mean(fc_wind)), 1) if fc_wind else None,
            "wind_max_kmh":   round(float(np.max(fc_wind)), 1) if fc_wind else None,
            "precip_total_mm": round(float(np.sum(fc_prec)), 1) if fc_prec else None,
            "snow_total_cm":  round(float(np.sum(fc_snow)), 1) if fc_snow else None,
            "precip_prob_max_pct": int(max(fc_prob)) if fc_prob else None,
        },
    }
    _weather_cache[geo3] = (now, w)
    return w

def wind_dir_text(deg):
    if deg is None:
        return None
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
    return dirs[int((deg + 11.25) // 22.5) % 16]


# Códigos WMO (weathercode) de Open-Meteo → descripción en español e icono emoji
_WMO = {
    0:  ("Despejado",               "☀️"),
    1:  ("Mayormente despejado",     "🌤️"),
    2:  ("Parcialmente nublado",     "⛅"),
    3:  ("Nublado",                  "☁️"),
    45: ("Niebla",                   "🌫️"),
    48: ("Niebla con escarcha",      "🌫️"),
    51: ("Llovizna ligera",          "🌦️"),
    53: ("Llovizna moderada",        "🌦️"),
    55: ("Llovizna intensa",         "🌧️"),
    61: ("Lluvia ligera",            "🌧️"),
    63: ("Lluvia moderada",          "🌧️"),
    65: ("Lluvia intensa",           "🌧️"),
    66: ("Lluvia helada ligera",     "🌨️"),
    67: ("Lluvia helada intensa",    "🌨️"),
    71: ("Nevada ligera",            "❄️"),
    73: ("Nevada moderada",          "❄️"),
    75: ("Nevada intensa",           "❄️"),
    77: ("Granizo",                  "🌨️"),
    80: ("Chubascos ligeros",        "🌦️"),
    81: ("Chubascos moderados",      "🌧️"),
    82: ("Chubascos violentos",      "⛈️"),
    85: ("Chubascos de nieve",       "❄️"),
    86: ("Chubascos de nieve fuerte","❄️"),
    95: ("Tormenta",                 "⛈️"),
    96: ("Tormenta con granizo",     "⛈️"),
    99: ("Tormenta con granizo fuerte","⛈️"),
}

def weathercode_desc(code):
    if code is None:
        return None
    return _WMO.get(int(code), ("Desconocido", "🌡️"))[0]

def weathercode_icon(code):
    if code is None:
        return None
    return _WMO.get(int(code), ("Desconocido", "🌡️"))[1]

# ── Comparación con referencia regional CAMS (Copernicus) ───────────────────
# Mapeo metrica del sensor -> variable equivalente en la API de Open-Meteo AQ.
SAT_VARS = {"pm25": "pm2_5", "pm10": "pm10", "no2": "nitrogen_dioxide",
            "o3": "ozone"}
_sat_cache = {}

def fetch_reference(geo3, hours):
    """
    Serie horaria de referencia CAMS (modelo europeo 11 km, satelite+estaciones)
    en el centro del geohash. Incluye AOD (espesor optico de aerosoles).
    Devuelve dict: { var_sensor: {epoch_s: valor} , "aod": {...} }.
    """
    now = time.time()
    key = (geo3, hours)
    hit = _sat_cache.get(key)
    if hit and now - hit[0] < SATCOMP_CACHE_TTL:
        return hit[1]
    lat, lon = geohash_center(geo3)
    past_days = max(1, min(92, (hours // 24) + 1))
    hourly = ",".join(list(SAT_VARS.values()) + ["aerosol_optical_depth"])
    params = {"latitude": round(lat, 4), "longitude": round(lon, 4),
              "hourly": hourly, "past_days": past_days, "forecast_days": 1,
              "timezone": "UTC"}
    r = requests.get(OPEN_METEO_AQ_URL, params=params, timeout=30)
    r.raise_for_status()
    h = r.json().get("hourly", {})
    times = h.get("time", [])
    def to_epoch(s):
        return int(time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M"))) \
               - time.timezone
    idx = {}
    for i, t in enumerate(times):
        idx[i] = to_epoch(t)
    out = {}
    for sensor_key, api_key in SAT_VARS.items():
        arr = h.get(api_key) or []
        out[sensor_key] = {idx[i]: arr[i] for i in range(len(arr))
                           if i in idx and arr[i] is not None}
    aod = h.get("aerosol_optical_depth") or []
    out["aod"] = {idx[i]: aod[i] for i in range(len(aod))
                  if i in idx and aod[i] is not None}
    _sat_cache[key] = (now, out)
    return out

def compare_with_reference(series_clean, reference):
    """
    Correlaciona (Spearman) cada metrica del sensor con la referencia CAMS,
    alineando por hora. Calcula tambien el sesgo medio sensor-referencia.
    series_clean: { metric: (t_ndarray, v_ndarray) } ya limpio.
    """
    results = {}
    for m, ref_map in reference.items():
        if m == "aod" or m not in series_clean or not ref_map:
            continue
        t, v = series_clean[m]
        # alinea por hora redondeando el timestamp del sensor a la hora CAMS
        pairs = []
        for ti, vi in zip(t, v):
            hour = int(ti // 3600) * 3600
            if hour in ref_map:
                pairs.append((vi, ref_map[hour]))
        if len(pairs) < 12:
            continue
        sv = np.array([p[0] for p in pairs])
        rv = np.array([p[1] for p in pairs])
        rho, p = sps.spearmanr(sv, rv)
        bias = float(np.mean(sv - rv))
        rel = (100.0 * bias / np.mean(rv)) if np.mean(rv) != 0 else None
        results[m] = {
            "n": len(pairs),
            "spearman_rho": round(float(rho), 2) if rho == rho else None,
            "p_value": round(float(p), 4) if p == p else None,
            "sensor_mean": round(float(np.mean(sv)), 2),
            "reference_mean": round(float(np.mean(rv)), 2),
            "bias": round(bias, 2),
            "bias_pct": round(rel, 1) if rel is not None else None,
        }
    # AOD no tiene equivalente directo en el sensor; lo reportamos como contexto
    if reference.get("aod"):
        vals = list(reference["aod"].values())
        results["_aod_reference"] = {"mean": round(float(np.mean(vals)), 3),
                                     "max": round(float(np.max(vals)), 3),
                                     "note": "espesor optico de aerosoles (columna)"}
    return results

def interpret_comparison(comp, reference="CAMS"):
    """Resumen legible del cotejo sensor vs referencia (CAMS u oficial)."""
    notes = []
    for m, c in comp.items():
        if m.startswith("_") or c.get("spearman_rho") is None:
            continue
        label = METRICS.get(m, {}).get("label", m)
        rho = c["spearman_rho"]; bias = c["bias_pct"]
        corr = ("alta" if rho >= 0.7 else "media" if rho >= 0.4
                else "baja")
        txt = f"{label}: correlacion {corr} (rho={rho})"
        if bias is not None and abs(bias) >= 25:
            signo = "sobreestima" if bias > 0 else "subestima"
            txt += f"; el sensor {signo} ~{abs(bias):.0f}% vs {reference}"
        notes.append(txt)
    return notes

# ── Etiqueta de confianza por métrica ───────────────────────────────────────
# Umbrales (ajustables por entorno) para clasificar la fiabilidad de cada
# metrica comparando el sensor con la referencia CAMS.
CONF_RHO_GOOD   = float(os.getenv("CONF_RHO_GOOD", "0.6"))   # rho >= -> bien
CONF_RHO_FAIR   = float(os.getenv("CONF_RHO_FAIR", "0.3"))   # rho >= -> dudoso
CONF_BIAS_FAIR  = float(os.getenv("CONF_BIAS_FAIR", "50"))   # |sesgo%| <= -> ok
# 50 y no 100: con 100, el O3 con -85% de sesgo salia 'fiable' solo porque
# correlacionaba (rho 0,70). Un sensor que lee el 15% del valor real no es
# fiable por bien que siga la forma de la curva. Con 50, un sesgo mayor baja a
# 'dudoso': sigue sirviendo para tendencias y alertas, pero su valor absoluto
# necesita el factor de correccion antes de darlo por bueno.
CONF_BIAS_BAD   = float(os.getenv("CONF_BIAS_BAD", "300"))   # |sesgo%| > -> malo
CONF_MIN_N      = int(os.getenv("CONF_MIN_N", "12"))         # pares minimos

# Correccion de PM: los sensores opticos baratos subestiman de forma
# consistente frente a CAMS. Si se activa, se calcula un factor por metrica
# (reference_mean / sensor_mean) y se expone el valor corregido junto al crudo.
# No modifica los datos crudos; solo informa el valor ajustado.
PM_CORRECTION_ENABLED = os.getenv("PM_CORRECTION_ENABLED", "1") == "1"
PM_CORRECTION_METRICS = set(
    x.strip() for x in os.getenv("PM_CORRECTION_METRICS", "pm25,pm10,pm1").split(",")
    if x.strip())
# solo corrige si la correlacion es al menos razonable (si no, no tiene sentido)
PM_CORRECTION_MIN_RHO = float(os.getenv("PM_CORRECTION_MIN_RHO", "0.5"))

# Zonas con referencia oficial. Vacío (por defecto) = AUTOMÁTICO: se intenta en
# todas las zonas y cada fuente decide si cubre esa posición.
#   - euskadi.py  -> ¿hay estación de la red vasca dentro de EUSKADI_RADIUS_KM?
#   - openaq.py   -> ¿hay estación oficial dentro de OPENAQ_RADIUS_KM?
# Si ninguna cubre la zona, queda solo CAMS, como antes.
# Rellenarlo sigue sirviendo para limitar el gasto de red a unas zonas concretas.
ZONE_HAS_OFFICIAL = set(
    z.strip() for z in os.getenv("ZONE_HAS_OFFICIAL", "").split(",")
    if z.strip())

# Centro de zona cacheado: la celda geo3 mide ~156 km de lado, así que su
# centro geométrico puede caer a 100 km de los sensores reales. Para buscar
# estaciones oficiales en un radio de 25 km hay que usar el centroide de los
# sensores que de verdad están emitiendo.
_zone_center_cache = {}
ZONE_CENTER_TTL = int(os.getenv("ZONE_CENTER_TTL", "86400"))


def zone_sensor_center(geo3, hours=168, mac=None):
    """
    Posición de referencia de la zona: centroide (lat, lon) de los sensores
    activos, decodificado del geohash fino de 7 caracteres.

    Con 'mac' devuelve la posición de ESE sensor, no el centroide. Importa:
    el centroide de una zona metropolitana puede quedar a varios km de cada
    sensor (en Bilbao/Barakaldo cae entre los dos), así que al analizar un
    sensor concreto hay que cotejarlo con las estaciones que tiene al lado,
    no con la media de toda el área.

    Si no hay posición publicada cae al centro de la celda geo3.
    Devuelve (lat, lon, n_sensores).
    """
    now = time.time()
    ck = (geo3, mac)
    hit = _zone_center_cache.get(ck)
    if hit and now - hit[0] < ZONE_CENTER_TTL:
        return hit[1]
    lats, lons = [], []
    try:
        c = influx_client()
        try:
            where = f"\"geo3\" = '{geo3}' AND time > now() - {int(hours)}h"
            if mac:
                where += f" AND \"mac\" = '{mac}'"
            q = (f'SELECT last("geo") AS geo FROM "{MEASUREMENT}" '
                 f'WHERE {where} GROUP BY "mac"')
            for _key, pts in c.query(q).items():
                g = (next(iter(pts), {}) or {}).get("geo")
                if not g:
                    continue
                try:
                    la, lo = geohash_center(g)
                except Exception:
                    continue
                lats.append(la)
                lons.append(lo)
        finally:
            c.close()
    except Exception as e:
        log.debug("zone_sensor_center(%s, mac=%s) sin posiciones: %s",
                  geo3, mac, e)
    if lats:
        out = (sum(lats) / len(lats), sum(lons) / len(lons), len(lats))
    elif mac:
        # el sensor no publica geo: usa el centroide de la zona
        out = zone_sensor_center(geo3, hours=hours)
    else:
        try:
            la, lo = geohash_center(geo3)
            out = (la, lo, 0)
        except Exception:
            out = (None, None, 0)
    _zone_center_cache[ck] = (now, out)
    return out


def fetch_official_any(geo3, hours, t_c=None, p_hpa=None, mac=None):
    """
    Cadena de referencia oficial: red local (Euskadi) -> OpenAQ global.
    Con 'mac' la referencia se busca alrededor de ESE sensor.
    Devuelve (series, source_by_metric, meta) donde meta describe qué fuente
    respondió. ({}, {}, meta) si ninguna cubre la zona.
    """
    lat, lon, nsens = zone_sensor_center(geo3, mac=mac)
    meta = {"lat": round(lat, 4) if lat is not None else None,
            "lon": round(lon, 4) if lon is not None else None,
            "sensors_located": nsens, "provider": None, "stations": [],
            "centered_on": ("sensor" if mac else "zona"), "mac": mac}
    if lat is None:
        return {}, {}, meta

    # 1) red oficial local: mejor referencia (mismo aire, histórico largo)
    if euskadi.EUSKADI_ENABLED:
        try:
            near = euskadi.resolve_stations(lat=lat, lon=lon)
            if near:
                off, src = euskadi.fetch_official_series(
                    hours, lat=lat, lon=lon)
                if off:
                    meta["provider"] = "euskadi"
                    meta["stations"] = [
                        {"name": s.get("name"), "dist_km": s.get("dist_km")}
                        for s in near if isinstance(s, dict)]
                    return off, src, meta
        except Exception as e:
            log.warning("referencia Euskadi fallo en %s: %s", geo3, e)

    # 2) OpenAQ: redes oficiales del resto del mundo
    if openaq.OPENAQ_ENABLED and openaq.OPENAQ_API_KEY:
        try:
            off, src = openaq.fetch_official_series(
                hours, lat=lat, lon=lon, t_c=t_c, p_hpa=p_hpa)
            if off:
                meta["provider"] = "openaq"
                meta["stations"] = [
                    {"name": l["name"], "country": l["country"]}
                    for l in openaq.find_locations(lat, lon)]
                return off, src, meta
        except Exception as e:
            log.warning("referencia OpenAQ fallo en %s: %s", geo3, e)

    return {}, {}, meta

def confidence_label(c):
    """
    Devuelve 'fiable' | 'dudoso' | 'no_fiable' | 'sin_referencia' para una
    metrica, a partir de su comparacion con CAMS (rho + sesgo + nº de pares).
    Reglas:
      - correlacion negativa fuerte (rho <= -CONF_RHO_FAIR) -> no_fiable
        (el sensor se mueve al reves que la referencia: sintoma claro de fallo)
      - rho alto y sesgo contenido -> fiable
      - rho bajo o sesgo enorme -> no_fiable
      - intermedio -> dudoso
    """
    if not c or c.get("spearman_rho") is None or c.get("n", 0) < CONF_MIN_N:
        return "sin_referencia"
    rho = c["spearman_rho"]
    bias = abs(c["bias_pct"]) if c.get("bias_pct") is not None else 0.0
    if rho <= -CONF_RHO_FAIR:
        return "no_fiable"
    if rho >= CONF_RHO_GOOD and bias <= CONF_BIAS_FAIR:
        return "fiable"
    if rho < CONF_RHO_FAIR or bias > CONF_BIAS_BAD:
        return "no_fiable"
    return "dudoso"

def build_confidence(comp):
    """Mapa metrica -> {label, rho, bias_pct} a partir de la comparacion."""
    out = {}
    if not comp:
        return out
    for m, c in comp.items():
        if m.startswith("_"):
            continue
        out[m] = {"label": confidence_label(c),
                  "spearman_rho": c.get("spearman_rho"),
                  "bias_pct": c.get("bias_pct")}
    return out

def merge_confidence(cams_conf, official_conf, official_source="oficial"):
    """
    Combina la confianza de CAMS y de la estacion oficial tomando, por metrica,
    la MEJOR correlacion de las dos (la referencia mas favorable). Asi un sensor
    no se penaliza por no captar picos locales de la cabina si sigue la tendencia
    regional (CAMS), ni al reves. Anota que referencia se uso: 'cams', 'euskadi'
    u 'openaq'.
    """
    # Se compara por CALIDAD DEL VEREDICTO y solo se desempata por rho.
    # Antes se elegia por rho a secas, y eso daba veredictos peores: con PM2.5,
    # las estaciones oficiales decian 'fiable' (rho 0,64, sesgo -19%) pero
    # ganaba CAMS con 'dudoso' por tener un rho algo mayor y un sesgo mucho
    # peor. En empate gana la oficial: es una cabina real del mismo aire.
    _rango = {"fiable": 3, "dudoso": 2, "no_fiable": 1, "sin_referencia": 0}
    out = dict(cams_conf or {})
    for m, oc in (official_conf or {}).items():
        cc = out.get(m)
        o_rho = abs(oc.get("spearman_rho") or 0)
        c_rho = abs(cc.get("spearman_rho") or 0) if cc else -1
        o_r = _rango.get(oc.get("label"), 0)
        c_r = _rango.get(cc.get("label"), 0) if cc else -1
        if cc is None or (o_r, o_rho) >= (c_r, c_rho):
            out[m] = dict(oc)
            out[m]["source"] = official_source
        else:
            out[m] = dict(cc)
            out[m]["source"] = "cams"
    # marca las que solo tenian CAMS
    for m, cc in out.items():
        cc.setdefault("source", "cams")
    return out

def pm_corrections(comp, basis="CAMS"):
    """
    Factor de correccion por metrica de PM:
    factor = reference_mean / sensor_mean (acerca el sensor a la referencia).
    Solo para metricas configuradas y con correlacion suficiente.

    'basis' dice contra que referencia se calculo. Importa: con CAMS salia
    factor 0,664 para PM2.5 (reduciendo un sensor que YA subestima un 19%),
    mientras que contra las cabinas oficiales sale ~1,23. Cuando hay estacion
    oficial, el factor se recalcula con ella.
    """
    out = {}
    if not (PM_CORRECTION_ENABLED and comp):
        return out
    for m in PM_CORRECTION_METRICS:
        c = comp.get(m)
        if not c or c.get("spearman_rho") is None:
            continue
        if c["spearman_rho"] < PM_CORRECTION_MIN_RHO:
            continue
        sm = c.get("sensor_mean"); rm = c.get("reference_mean")
        if sm and rm and sm > 0:
            out[m] = {"factor": round(rm / sm, 3),
                      "basis": f"reference_mean/sensor_mean vs {basis}"}
    return out

# ── Mejora 2: corrección de PM dependiente de la humedad ────────────────────
# Los sensores ópticos inflan la lectura con humedad alta (las partículas
# absorben agua). Ajustamos por mínimos cuadrados: ref ≈ a·PM + b·HR + c,
# y comparamos contra el modelo simple de factor para quedarnos con el mejor.
def humidity_pm_model(series_clean, ref_map, metric):
    if metric not in series_clean or "hum" not in series_clean or not ref_map:
        return None
    t_pm, v_pm = series_clean[metric]
    hum_map = {int(t // 3600) * 3600: v
               for t, v in zip(*series_clean["hum"])}
    rows = []
    for ti, vi in zip(t_pm, v_pm):
        h = int(ti // 3600) * 3600
        if h in hum_map and h in ref_map:
            rows.append((float(vi), float(hum_map[h]), float(ref_map[h])))
    if len(rows) < 24:      # exige al menos un día de horas comunes
        return None
    A = np.array([[r[0], r[1], 1.0] for r in rows])
    y = np.array([r[2] for r in rows])
    try:
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    a, b, c = (float(x) for x in coef)
    if a <= 0:              # sin sentido físico: descarta
        return None
    pred = A @ coef
    rmse_rh = float(np.sqrt(np.mean((pred - y) ** 2)))
    # modelo simple (factor) sobre las MISMAS horas, para comparar en igualdad
    mean_pm = float(np.mean(A[:, 0]))
    f = float(np.mean(y) / mean_pm) if mean_pm > 0 else None
    rmse_lin = (float(np.sqrt(np.mean((A[:, 0] * f - y) ** 2)))
                if f else None)
    better = rmse_lin is not None and rmse_rh < rmse_lin * 0.95  # mejora >5%
    return {
        "coef": {"a_pm": round(a, 3), "b_rh": round(b, 3), "c": round(c, 2)},
        "n": len(rows),
        "rmse_rh_model": round(rmse_rh, 2),
        "rmse_factor_model": round(rmse_lin, 2) if rmse_lin else None,
        "better_than_factor": bool(better),
    }


def apply_humidity_correction(model, pm_last, rh_last):
    """Último valor corregido con el modelo lineal de humedad (nunca negativo)."""
    if not model or pm_last is None or rh_last is None:
        return None
    co = model["coef"]
    v = co["a_pm"] * pm_last + co["b_rh"] * rh_last + co["c"]
    return round(max(0.0, v), 2)


def apply_malm_correction(pm_val, rh_pct):
    """
    P1.2: Corrección de PM por humedad con la fórmula EPA/Malm & Hand,
    usada por la red PurpleAir y recomendada por EPA para sensores ópticos.
    La relación es superlineal cerca del punto de delicuescencia (~80% HR),
    especialmente relevante en climas húmedos como el Cantábrico.
    PM_corr = PM / (1 + k * HR/(100-HR))
    k=0.24 para HR < 80%,  k=0.52 para HR >= 80%
    """
    if pm_val is None or rh_pct is None or rh_pct >= 100:
        return None
    k = 0.52 if rh_pct >= 80 else 0.24
    denom = 1.0 + k * rh_pct / (100.0 - rh_pct)
    return round(max(0.0, pm_val / denom), 2)


# ── Mejora 1: validación cruzada entre sensores (consenso de vecinos) ───────
# Cada sensor se coteja contra la MEDIANA de los demás sensores de su zona.
# Detecta averías sin referencia externa y valida métricas que ninguna
# referencia cubre (ruido, CO2). Una sola consulta InfluxDB (GROUP BY mac).
def fetch_series_by_sensor(geo3, hours):
    """{ mac: { metric: {hora_epoch: valor} } }, con rango físico aplicado."""
    sel = ", ".join(f'mean("{m}") AS "{m}"' for m in METRICS)
    q = (f'SELECT {sel} FROM "{MEASUREMENT}" '
         f"WHERE \"geo3\" = '{geo3}' AND time > now() - {hours}h "
         f'GROUP BY time(1h), "mac" fill(none)')
    c = influx_client()
    out = {}
    try:
        res = c.query(q, epoch="s")
        for (meas, tags), pts in res.items():
            mac = (tags or {}).get("mac")
            if not mac:
                continue
            d = out.setdefault(mac, {})
            for p in pts:
                t = int(p["time"])
                for m in METRICS:
                    v = p.get(m)
                    if v is None:
                        continue
                    rng = METRICS[m].get("range")
                    if rng and not (rng[0] <= v <= rng[1]):
                        continue
                    d.setdefault(m, {})[t] = float(v)
    finally:
        c.close()
    return out


CROSS_MIN_SENSORS = int(os.getenv("CROSS_MIN_SENSORS", "3"))
CROSS_MIN_N = int(os.getenv("CROSS_MIN_N", "12"))

def cross_sensor_validation(geo3, hours):
    """
    Para cada métrica medida por >= CROSS_MIN_SENSORS sensores, coteja cada
    sensor contra la mediana de los DEMÁS (consenso), hora a hora.
    Etiquetas: coherente / dudoso / divergente / sin_pares.
    """
    data = fetch_series_by_sensor(geo3, hours)
    if len(data) < CROSS_MIN_SENSORS:
        return None
    names = _sensor_names()
    # métricas con suficientes sensores y varianza real
    metric_sensors = {}
    for mac, mm in data.items():
        for m, pts in mm.items():
            if len(pts) >= CROSS_MIN_N and np.std(list(pts.values())) > 1e-9:
                metric_sensors.setdefault(m, []).append(mac)
    evaluated = [m for m, macs in metric_sensors.items()
                 if len(macs) >= CROSS_MIN_SENSORS]
    if not evaluated:
        return None
    sensors_out = {}
    notes = []
    for m in evaluated:
        macs = metric_sensors[m]
        for mac in macs:
            mine = data[mac][m]
            pairs = []
            for h, v in mine.items():
                others = [data[o][m][h] for o in macs
                          if o != mac and h in data[o][m]]
                if len(others) >= 2:      # consenso necesita >=2 vecinos
                    pairs.append((v, float(np.median(others))))
            if len(pairs) < CROSS_MIN_N:
                label, det = "sin_pares", {"n": len(pairs)}
            else:
                sv = np.array([p[0] for p in pairs])
                cv = np.array([p[1] for p in pairs])
                rho, _p = sps.spearmanr(sv, cv)
                rho = float(rho) if rho == rho else 0.0
                cm = float(np.mean(cv))
                bias_pct = (round(100.0 * float(np.mean(sv - cv)) / cm, 1)
                            if cm > 0 else None)
                if rho >= 0.5 and (bias_pct is None or abs(bias_pct) <= 150):
                    label = "coherente"
                elif rho < 0.2 or (bias_pct is not None and abs(bias_pct) > 300):
                    label = "divergente"
                else:
                    label = "dudoso"
                det = {"rho_consenso": round(rho, 2), "bias_pct": bias_pct,
                       "n": len(pairs)}
            s = sensors_out.setdefault(mac, {"name": names.get(mac, mac[-6:]),
                                             "labels": {}, "detail": {}})
            s["labels"][m] = label
            s["detail"][m] = det
            if label == "divergente":
                lbl = METRICS.get(m, {}).get("label", m)
                notes.append(f"{s['name']}: {lbl} divergente del consenso "
                             f"local (rho={det.get('rho_consenso')}, "
                             f"sesgo {det.get('bias_pct')}%)")
    return {"metrics_evaluated": evaluated, "min_sensors": CROSS_MIN_SENSORS,
            "sensors": sensors_out, "notes": notes or None,
            "excluded": _cross_excluded(data, sensors_out, names) or None}


def _cross_excluded(data, sensors_out, names):
    """Sensores presentes pero no evaluables, con el motivo (diagnóstico)."""
    out = {}
    for mac, mm in data.items():
        if mac in sensors_out:
            continue
        has_signal = any(
            len(p) >= CROSS_MIN_N and np.std(list(p.values())) > 1e-9
            for p in mm.values())
        if not has_signal:
            reason = ("sin variacion o sin datos validos en la ventana "
                      "(valores constantes/fuera de rango: posible averia)")
        else:
            reason = "sus metricas no las comparten >=3 sensores"
        out[mac] = {"name": names.get(mac, mac[-6:]), "reason": reason}
    return out


# ── Mejora 3: rosa de contaminación (concentración según dirección viento) ──
import calendar as _calendar
_wind_cache = {}
ROSE_SECTORS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
ROSE_CALM_KMH = float(os.getenv("ROSE_CALM_KMH", "5"))

def fetch_wind_series(geo3, hours):
    """
    P1.1: Viento horario pasado usando la Historical Forecast API de Open-Meteo
    (inicializada con observaciones reales, más precisa que el pronóstico simple).
    También recoge precipitación y presión para correlación PM (P2.5).
    Devuelve {hora_epoch: (vel_kmh, dir_deg, precip_mm, pres_hpa)}.
    """
    key = (geo3, int(hours))
    now_t = time.time()
    hit = _wind_cache.get(key)
    if hit and now_t - hit[0] < WEATHER_CACHE_TTL:
        return hit[1]
    lat, lon = geohash_center(geo3)
    past_days = min(7, max(2, int(hours / 24) + 1))
    params = {
        "latitude": round(lat, 4), "longitude": round(lon, 4),
        "hourly": "wind_speed_10m,wind_direction_10m,precipitation,surface_pressure",
        "past_days": past_days, "forecast_days": 1,
        "wind_speed_unit": "kmh", "timezone": "UTC"
    }
    # P1.1: Historical Forecast API (datos casi idénticos a observaciones reales)
    r = requests.get(OPEN_METEO_HIST_URL, params=params, timeout=30)
    r.raise_for_status()
    hly = r.json().get("hourly", {})
    out = {}
    for s, spd, deg, prec, pres in zip(
            hly.get("time", []),
            hly.get("wind_speed_10m", []),
            hly.get("wind_direction_10m", []),
            hly.get("precipitation", []) or [None] * 9999,
            hly.get("surface_pressure", []) or [None] * 9999):
        if spd is None or deg is None:
            continue
        try:
            ep = _calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M"))
        except ValueError:
            continue
        out[ep] = (float(spd), float(deg),
                   float(prec) if prec is not None else None,
                   float(pres) if pres is not None else None)
    _wind_cache[key] = (now_t, out)
    return out


def pollution_rose(geo3, hours, series_clean, metrics=("pm25", "pm10")):
    """
    Media de concentración por sector de viento (8 sectores + calma).
    Señala de dónde viene la contaminación. Solo métricas con confianza
    razonable (PM); los gases no fiables no se incluyen.
    """
    try:
        wind = fetch_wind_series(geo3, hours)
    except requests.RequestException as e:
        log.warning("viento histórico no disponible para %s: %s", geo3, e)
        return None
    if not wind:
        return None
    rose = {}
    note = None
    for m in metrics:
        if m not in series_clean:
            continue
        t, v = series_clean[m]
        acc, calm = {}, []
        for ti, vi in zip(t, v):
            h = int(ti // 3600) * 3600
            w = wind.get(h)
            if not w:
                continue
            spd, deg = w[0], w[1]   # (vel, dir, precip, pres) -> solo vel+dir
            if spd < ROSE_CALM_KMH:
                calm.append(float(vi))
                continue
            sector = ROSE_SECTORS[int((deg + 22.5) // 45) % 8]
            acc.setdefault(sector, []).append(float(vi))
        entry = {s: {"mean": round(float(np.mean(vals)), 1), "n": len(vals)}
                 for s, vals in acc.items()}
        if calm:
            entry["calma"] = {"mean": round(float(np.mean(calm)), 1),
                              "n": len(calm)}
        if entry:
            rose[m] = entry
    if not rose:
        return None
    # nota interpretativa sobre PM2.5 (la métrica fiable)
    pm = rose.get("pm25") or rose.get("pm10")
    if pm:
        sectors = {s: d for s, d in pm.items() if s != "calma" and d["n"] >= 3}
        if len(sectors) >= 2:
            allv = [d["mean"] for d in sectors.values()]
            top = max(sectors.items(), key=lambda kv: kv[1]["mean"])
            overall = float(np.mean(allv))
            # exige ratio 1.5x Y diferencia absoluta relevante (>=2 µg/m³),
            # para no generar notas absurdas con concentraciones ínfimas
            if (overall > 0 and top[1]["mean"] >= 1.5 * overall
                    and top[1]["mean"] - overall >= 2.0):
                mkey = "pm25" if "pm25" in rose else "pm10"
                lbl = METRICS.get(mkey, {}).get("label", mkey)
                note = (f"{lbl} notablemente más alto con viento del "
                        f"{top[0]} ({top[1]['mean']} de media en "
                        f"{top[1]['n']} h, frente a {round(overall,1)} "
                        f"del conjunto): posible fuente en esa dirección.")
    return {"sectors": rose, "calm_threshold_kmh": ROSE_CALM_KMH,
            "note": note}


# ── Conversión de gases ppm -> µg/m³ (o mg/m³) ──────────────────────────────
def _zone_t_p(raw):
    """Media de T (°C) y P (hPa) medidas por la zona; estandar si faltan."""
    t_c = float(np.mean(raw["tmp"][1])) if "tmp" in raw and len(raw["tmp"][1]) else 25.0
    p_hpa = float(np.mean(raw["prs"][1])) if "prs" in raw and len(raw["prs"][1]) else 1013.25
    # sanea valores absurdos
    if not (-40 <= t_c <= 60): t_c = 25.0
    if not (800 <= p_hpa <= 1100): p_hpa = 1013.25
    return t_c, p_hpa

def convert_gases(raw):
    """
    Convierte in-place las series de gases de ppm a µg/m³ (CO a mg/m³):
      conc = ppm * factor * M / Vm,  con Vm = 22.414 * (T/273.15K) * (1013.25/P)
    Usa la T y P medias medidas por la propia zona (rigor: corrige por
    condiciones reales, no estandar). Devuelve dict con los factores usados.
    """
    t_c, p_hpa = _zone_t_p(raw)
    vm = 22.414 * ((t_c + 273.15) / 273.15) * (1013.25 / p_hpa)  # L/mol
    applied = {"t_c": round(t_c, 1), "p_hpa": round(p_hpa, 1),
               "molar_volume_l": round(vm, 3), "factors": {}}
    for gas, info in GAS_CONVERSION.items():
        if gas in raw:
            f = info["factor"] * info["molar_mass"] / vm
            t, v = raw[gas]
            raw[gas] = (t, v * f)
            applied["factors"][gas] = {"ppm_to": info["to"],
                                       "multiplier": round(f, 3)}
    return applied

# ── Análisis de zona (estadística + clima) ──────────────────────────────────
# ── P2.2: Índice europeo CAQI ─────────────────────────────────────────────────
# Common Air Quality Index (UE): usa medias de 24h de PM2.5 y PM10.
# Umbrales en µg/m³ (PM2.5): muy bajo <10, bajo 10-20, medio 20-25,
# alto 25-50, muy alto >50.  (PM10: ×2 aprox.)
CAQI_LEVELS = [
    (0,  10,  "muy bajo",  "#79BC6A"),
    (10, 20,  "bajo",      "#BBCF4C"),
    (20, 25,  "medio",     "#EEC20B"),
    (25, 50,  "alto",      "#F29305"),
    (50, 800, "muy alto",  "#E8416F"),
]

def _caqi_label(val_pm25):
    for lo, hi, label, color in CAQI_LEVELS:
        if lo <= val_pm25 < hi:
            return {"value": round(val_pm25, 1), "label": label, "color": color}
    return {"value": round(val_pm25, 1), "label": "muy alto", "color": "#E8416F"}

def compute_caqi(stats_by_metric):
    """P2.2: CAQI basado en la media de 24h de PM2.5 (y PM10 como referencia)."""
    pm25 = (stats_by_metric.get("pm25") or {}).get("mean")
    pm10 = (stats_by_metric.get("pm10") or {}).get("mean")
    if pm25 is None:
        return None
    out = {"pm25": _caqi_label(pm25)}
    if pm10 is not None:
        out["pm10"] = _caqi_label(pm10 / 2.0)  # normaliza al índice de PM2.5
    return out


# ── P2.3: Detección de episodios sostenidos ─────────────────────────────────
EPISODE_MIN_HOURS = int(os.getenv("EPISODE_MIN_HOURS", "2"))  # min duración
# Métricas que pueden formar un "episodio". Solo contaminantes y ruido: la
# humedad tiene umbral 80% y en Bilbao eso es un día normal, así que salían
# "episodios sostenidos de humedad" que no dicen nada de la calidad del aire.
# La temperatura queda fuera por el mismo motivo (es contexto, no episodio).
EPISODE_METRICS = set(m.strip() for m in os.getenv(
    "EPISODE_METRICS", "pm25,pm10,pm1,no2,o3,nh3,co,co2,so2,db").split(",")
    if m.strip())

def detect_episodes(series_clean, confidence=None):
    """
    P2.3: Detecta períodos sostenidos de alta concentración.
    Excluye métricas marcadas como no_fiable (misma lógica que las alertas).
    """
    confidence = confidence or {}
    episodes = []
    for m, (t, v) in series_clean.items():
        if m not in EPISODE_METRICS:
            continue
        if confidence.get(m, {}).get("label") == "no_fiable":
            continue
        th = THRESHOLDS.get(m)
        if not th or len(t) < EPISODE_MIN_HOURS:
            continue
        warn = th["warn"]
        # detectar rachas continuas sobre umbral
        in_ep = False; ep_start = None; ep_max = None; ep_vals = []
        for i in range(len(t)):
            over = float(v[i]) >= warn
            if over and not in_ep:
                in_ep = True; ep_start = int(t[i]); ep_vals = [float(v[i])]
                ep_max = float(v[i])
            elif over and in_ep:
                ep_vals.append(float(v[i]))
                ep_max = max(ep_max, float(v[i]))
            elif not over and in_ep:
                if len(ep_vals) >= EPISODE_MIN_HOURS:
                    episodes.append({
                        "metric": m,
                        "start_epoch": ep_start,
                        "duration_h": len(ep_vals),
                        "peak": round(ep_max, 2),
                        "mean": round(float(np.mean(ep_vals)), 2),
                        "threshold": warn,
                    })
                in_ep = False; ep_vals = []
        if in_ep and len(ep_vals) >= EPISODE_MIN_HOURS:
            episodes.append({
                "metric": m,
                "start_epoch": ep_start,
                "duration_h": len(ep_vals),
                "peak": round(ep_max, 2),
                "mean": round(float(np.mean(ep_vals)), 2),
                "threshold": warn,
            })
    return episodes or None


# ── P2.1: Perfil horario (media energética por hora del día) ─────────────────
def hourly_profile(series_clean):
    """
    P2.1: Para cada métrica, media por hora del día (0-23) sobre la ventana
    de análisis. Permite detectar patrones diurnos y calcular anomalías.
    """
    profile = {}
    for m, (t, v) in series_clean.items():
        buckets = {}
        for ti, vi in zip(t, v):
            h = int((ti % 86400) / 3600)
            buckets.setdefault(h, []).append(float(vi))
        if not buckets:
            continue
        profile[m] = {
            str(h): round(float(np.mean(vals)), 2)
            for h, vals in sorted(buckets.items())
        }
    return profile or None


# ── P2.5: Correlación PM con lluvia y presión ─────────────────────────────────
def env_correlations(series_clean, wind_data):
    """
    P2.5: Correlación de Spearman PM2.5 con:
    - Precipitación (lluvia lava partículas: correlación negativa esperada).
    - Tendencia de presión (presión subiendo = acumulación: correlación positiva).
    """
    if not wind_data or "pm25" not in series_clean:
        return None
    t_pm, v_pm = series_clean["pm25"]
    rain_pairs = []; pres_pairs = []
    p_vals = []
    for i, ti in enumerate(t_pm):
        h = int(ti // 3600) * 3600
        w = wind_data.get(h)
        if not w or len(w) < 3:
            continue
        _, _, prec, pres = w
        if prec is not None:
            rain_pairs.append((float(v_pm[i]), prec))
        if pres is not None:
            p_vals.append((int(ti), float(v_pm[i]), pres))
    out = {}
    if len(rain_pairs) >= 12:
        pm_r = np.array([x[0] for x in rain_pairs])
        rain = np.array([x[1] for x in rain_pairs])
        rho, p = sps.spearmanr(pm_r, rain)
        if rho == rho:
            out["pm25_vs_rain"] = {
                "spearman_rho": round(float(rho), 2),
                "p_value": round(float(p), 4),
                "n": len(rain_pairs),
                "note": ("lluvia lava PM (normal)" if rho < -0.2
                         else "sin efecto de lavado claro"),
            }
    if len(p_vals) >= 12:
        # tendencia de presión: diferencia entre hora actual y 3h antes
        pres_trend = []
        for i in range(3, len(p_vals)):
            t0, pm0, ps0 = p_vals[i]
            _, _, ps_ant = p_vals[i - 3]
            pres_trend.append((pm0, ps0 - ps_ant))
        if len(pres_trend) >= 12:
            pm_p = np.array([x[0] for x in pres_trend])
            dp = np.array([x[1] for x in pres_trend])
            rho2, p2 = sps.spearmanr(pm_p, dp)
            if rho2 == rho2:
                out["pm25_vs_pressure_trend"] = {
                    "spearman_rho": round(float(rho2), 2),
                    "p_value": round(float(p2), 4),
                    "n": len(pres_trend),
                    "note": ("presion subiendo acumula PM" if rho2 > 0.2
                             else "sin efecto claro de presion"),
                }
    return out or None


# ── P1.4: guardar factor PM en histórico semanalmente ─────────────────────────
def _save_pm_factor_history(geo3, pm_corrections_data):
    """P1.4: guarda el factor de corrección PM semanal en análisis BD."""
    if not pm_corrections_data:
        return
    try:
        c = influx_client()
        c.switch_database(ANALYSIS_DB)
        fields = {}
        for m, info in pm_corrections_data.items():
            f = (info or {}).get("factor")
            if f is not None:
                fields[f"pm_factor_{m}"] = float(f)
        if fields:
            fields["geo3_tag"] = geo3
            c.write_points([{
                "measurement": "pm_factor_history",
                "tags": {"geo3": geo3},
                "fields": fields,
            }])
        c.close()
    except Exception as e:
        log.debug("no pude guardar factor PM para %s: %s", geo3, e)


def zone_statistics(geo3, hours, mac=None):
    agg_info = {}
    raw = fetch_zone_series(geo3, hours, mac=mac, info=agg_info)
    if not raw:
        return None
    conversion = convert_gases(raw)  # ppm -> µg/m³ (CO -> mg/m³) con T,P reales
    series_clean = {}
    cleaning = {}
    stats_by_metric = {}
    skipped = []
    discarded = {}
    for m, (t, v) in raw.items():
        tc, vc, info = clean_series(t, v, m)
        if len(vc) < 6:
            # Antes esto era un 'continue' mudo y la métrica se esfumaba sin
            # explicación. Caso real: el nodo de gases leía NH3 ~1518 µg/m³, el
            # filtro de rango (0-400) tiraba TODOS los puntos y el NH3
            # desaparecía del análisis sin que nada lo dijera.
            lo, hi = METRICS[m]["range"]
            n_raw = int(len(v))
            por_rango = info.get("dropped_range", 0)
            det = {
                "raw_points": n_raw,
                "kept": int(len(vc)),
                "dropped_range": por_rango,
                "dropped_outlier": info.get("dropped_outlier", 0),
                "valid_range": [lo, hi],
            }
            if n_raw:
                med = float(np.median(v))
                det["raw_median"] = round(med, 2)
                det["raw_min"] = round(float(np.min(v)), 2)
                det["raw_max"] = round(float(np.max(v)), 2)
                if por_rango >= max(1, int(n_raw * 0.9)):
                    det["reason"] = "fuera_de_rango_fisico"
                    lado = "por encima" if med > hi else "por debajo"
                    det["note"] = (
                        f"el sensor lee ~{med:g} {METRICS[m]['label']}, {lado} "
                        f"del rango físico {lo}-{hi}: se descarta todo. "
                        f"Revisa calibración o conversión de unidades.")
                else:
                    det["reason"] = "pocos_datos_tras_limpieza"
                    det["note"] = (
                        f"solo {len(vc)} puntos válidos de {n_raw}: hacen falta "
                        f"6 para analizar.")
            else:
                det["reason"] = "sin_datos"
            discarded[m] = det
            log.info("zona %s: %s descartada (%s)", geo3, m, det.get("reason"))
            continue
        # descarta metricas sin senal real: varianza nula = sensor no mide
        if float(np.std(vc)) < 1e-9:
            skipped.append(m)
            continue
        series_clean[m] = (tc, vc)
        cleaning[m] = info
        stats_by_metric[m] = analyze_series(tc, vc)
    if not stats_by_metric:
        return None
    result = {
        "geo3": geo3,
        "hours": hours,
        "units": {m: METRICS[m]["label"] for m in stats_by_metric},
        "gas_conversion": conversion,
        "metrics": stats_by_metric,
        "correlations": correlations(series_clean),
        "cleaning": cleaning,
        "skipped_no_signal": skipped,
        # métricas que no llegaron al análisis, con el motivo
        "discarded_metrics": discarded,
        # cómo se combinaron los sensores y a quién se excluyó por leer 0
        "aggregation": agg_info,
    }
    # P2.2: Índice europeo CAQI (basado en medias de 24h de PM2.5/PM10) ──────
    result["caqi"] = compute_caqi(stats_by_metric)
    # P2.1: perfil horario (media energética por hora del día) ────────────────
    if mac is None:
        result["hourly_profile"] = hourly_profile(series_clean)
    # P2.5: correlación con lluvia y presión
    try:
        wind_data = fetch_wind_series(geo3, hours)
        result["env_correlations"] = env_correlations(series_clean, wind_data)
    except Exception as e:
        log.debug("env_correlations no disponible para %s: %s", geo3, e)
    # cotejo con referencia regional CAMS (satelite+estaciones, Copernicus)
    confidence = {}
    cams_ref = None
    off_ref = None
    if SATCOMP_ENABLED:
        try:
            ref = fetch_reference(geo3, hours)
            comp = compare_with_reference(series_clean, ref)
            if comp:
                cams_ref = ref
                result["reference_comparison"] = comp
                result["reference_notes"] = interpret_comparison(comp)
                confidence = build_confidence(comp)
                result["confidence"] = confidence
                # factores de correccion de PM y valor corregido del ultimo dato
                corr = pm_corrections(comp)
                if corr:
                    for m, info in corr.items():
                        info["source"] = "cams"
                        if m in stats_by_metric:
                            lv = stats_by_metric[m]["last_value"]
                            info["corrected_last"] = round(lv * info["factor"], 2)
                            info["raw_last"] = lv
                    result["pm_corrections"] = corr
                    _save_pm_factor_history(geo3, corr)   # P1.4
        except Exception as e:
            log.warning("comparacion CAMS no disponible para %s: %s", geo3, e)
    # comparación con estaciones OFICIALES como 2ª referencia. Cadena:
    #   red local (Euskadi, elegida por cercanía y cobertura de métricas)
    #   -> OpenAQ (redes oficiales del resto del mundo)
    # Ya no hace falta una lista de zonas: cada fuente decide si cubre la
    # posición real de los sensores. ZONE_HAS_OFFICIAL, si se rellena, solo
    # limita en qué zonas se gasta red.
    if not ZONE_HAS_OFFICIAL or geo3 in ZONE_HAS_OFFICIAL:
        try:
            # T y P reales de la zona: OpenAQ puede devolver gases en ppm/ppb y
            # la conversión a µg/m³ depende del volumen molar.
            t_c = (stats_by_metric.get("tmp") or {}).get("last_value")
            p_hpa = (stats_by_metric.get("prs") or {}).get("last_value")
            off, off_src, off_meta = fetch_official_any(
                geo3, hours, t_c=t_c, p_hpa=p_hpa, mac=mac)
            result["official_meta"] = off_meta
            if off:
                off_comp = compare_with_reference(series_clean, off)
                if off_comp:
                    off_ref = off
                    prov = off_meta.get("provider") or "oficial"
                    result["official_provider"] = prov
                    result["official_stations"] = [
                        s.get("name") for s in off_meta.get("stations") or []]
                    result["official_sources"] = off_src
                    result["official_comparison"] = off_comp
                    result["official_notes"] = interpret_comparison(
                        off_comp, reference=(
                            "estaciones oficiales (red vasca)"
                            if prov == "euskadi"
                            else "estaciones oficiales (OpenAQ)"))
                    off_conf = build_confidence(off_comp)
                    result["official_confidence"] = off_conf
                    # confianza final = la MEJOR correlación entre CAMS y oficial,
                    # por métrica (no se penaliza por picos locales no captados)
                    confidence = merge_confidence(confidence, off_conf,
                                                  official_source=prov)
                    result["confidence"] = confidence
                    # El factor de correccion de PM se rehace contra la cabina
                    # oficial: es aire real del mismo sitio, no un modelo de
                    # 11 km. Con CAMS salia 0,664 para PM2.5 (empeoraba la
                    # lectura); contra la cabina sale ~1,23.
                    corr_off = pm_corrections(
                        off_comp, basis=f"estaciones oficiales ({prov})")
                    if corr_off:
                        dest = result.setdefault("pm_corrections", {})
                        for m, info in corr_off.items():
                            entry = dest.setdefault(m, {})
                            info["source"] = prov
                            entry.update(info)
                            lv = (stats_by_metric.get(m) or {}).get("last_value")
                            if lv is not None:
                                entry["corrected_last"] = round(
                                    lv * info["factor"], 2)
                                entry["raw_last"] = lv
                        _save_pm_factor_history(geo3, corr_off)
        except Exception as e:
            log.warning("comparacion oficial no disponible para %s: %s", geo3, e)
    # P1.2: corrección de PM por humedad — modelo lineal + fórmula Malm/EPA
    try:
        rh_last = (stats_by_metric.get("hum") or {}).get("last_value")
        for m in ("pm25", "pm10"):
            ref_map = ((off_ref or {}).get(m)) or ((cams_ref or {}).get(m))
            pm_last = (stats_by_metric.get(m) or {}).get("last_value")
            entry = result.setdefault("pm_corrections", {}).setdefault(m, {})
            # fórmula Malm/EPA (siempre disponible si hay HR)
            malm_corr = apply_malm_correction(pm_last, rh_last)
            if malm_corr is not None:
                entry["malm_corrected_last"] = malm_corr
                entry["malm_rh"] = rh_last
                entry["malm_note"] = (
                    "formula EPA (k=0.52, HR>=80%)" if (rh_last or 0) >= 80
                    else "formula EPA (k=0.24, HR<80%)")
            # modelo lineal ajustado contra referencia (si hay datos suficientes)
            model = humidity_pm_model(series_clean, ref_map, m)
            if model:
                model["reference"] = ("estaciones oficiales" if
                                      (off_ref or {}).get(m) else "CAMS")
                cl = apply_humidity_correction(model, pm_last, rh_last)
                if cl is not None:
                    model["corrected_last"] = cl
                    model["raw_last"] = pm_last
                entry["rh_model"] = model
    except Exception as e:
        log.warning("modelo de humedad no disponible para %s: %s", geo3, e)

    # ── Jerarquía de corrección de PM: una sola cifra aplicada ───────────
    # Antes se publicaban dos correcciones contradictorias a la vez. Medido en
    # ezt: el factor contra cabina daba x1,233 (5,47 -> 6,74) y Malm daba
    # 5,47 -> 3,47, es decir uno multiplicando por 1,23 y el otro dividiendo
    # por 1,58. La web mostraba las dos sin decir cuál creer.
    #
    #   1. factor contra cabina oficial  -> es la que se aplica
    #   2. fórmula Malm/EPA por humedad  -> solo si NO hay cabina
    #   3. factor contra CAMS            -> informativo, nunca se aplica
    #
    # Por qué CAMS no se aplica: es un modelo de 11 km y su media puede estar
    # sesgada. Medido aquí: daba factor 0,664 para un sensor que YA subestima
    # un 19% frente a la cabina, o sea empeoraba la lectura.
    #
    # Por qué Malm cede ante la cabina: la fórmula asume que el óptico
    # SOBREESTIMA con humedad alta (las partículas absorben agua). Contra la
    # cabina estos sensores SUBESTIMAN, así que la hipótesis de la fórmula ya
    # está desmentida por los datos. Se conserva como información: que el signo
    # no cuadre dice que la humedad no explica el sesgo de estos equipos.
    for m, entry in (result.get("pm_corrections") or {}).items():
        raw = (stats_by_metric.get(m) or {}).get("last_value")
        src = entry.get("source")
        factor = entry.get("factor")
        malm = entry.get("malm_corrected_last")
        entry["raw_last"] = entry.get("raw_last", raw)
        if factor is not None and src and src != "cams":
            entry["applied"] = "factor_referencia"
            entry["applied_factor"] = factor
            entry["applied_value"] = entry.get("corrected_last")
            entry["applied_source"] = src
            entry["applied_note"] = f"factor x{factor} contra cabina oficial ({src})"
        elif malm is not None:
            entry["applied"] = "malm_humedad"
            entry["applied_factor"] = None
            entry["applied_value"] = malm
            entry["applied_source"] = "formula EPA/Malm"
            entry["applied_note"] = (
                f"sin cabina oficial cerca: {entry.get('malm_note','formula EPA')}")
        else:
            entry["applied"] = None
            entry["applied_value"] = None
            entry["applied_note"] = "sin correccion disponible"
        # ¿coincide Malm en el sentido con lo que dice la cabina?
        if malm is not None and raw:
            entry["malm_applied"] = (entry["applied"] == "malm_humedad")
            if factor is not None and src and src != "cams":
                sube_ref = factor > 1.0
                sube_malm = malm > raw
                entry["malm_consistent"] = (sube_ref == sube_malm)
                if not entry["malm_consistent"]:
                    entry["malm_conflict_note"] = (
                        "la formula de humedad apunta al lado contrario que la "
                        "cabina: la humedad no explica el sesgo de este sensor")
    # recalcula alertas descartando metricas marcadas como no fiables
    result["alerts"] = detect_alerts(stats_by_metric, confidence)
    # P2.3: episodios sostenidos — excluye métricas no_fiable (misma lógica
    # que las alertas, confidence ya calculada aquí)
    result["episodes"] = detect_episodes(series_clean, confidence)
    # Mejoras 1 y 3 (solo a nivel de ZONA, no por sensor individual)
    if mac is None:
        try:
            cross = cross_sensor_validation(geo3, hours)
            if cross:
                result["cross_sensor"] = cross
        except Exception as e:
            log.warning("validacion cruzada no disponible para %s: %s", geo3, e)
        try:
            rose = pollution_rose(geo3, hours, series_clean)
            if rose:
                result["pollution_rose"] = rose
        except Exception as e:
            log.warning("rosa de contaminacion no disponible para %s: %s",
                        geo3, e)
    return result

# ── LLM ──────────────────────────────────────────────────────────────────────
def build_prompt(zs, weather):
    compact = {
        "zona_geo3": zs["geo3"],
        "ventana_horas": zs["hours"],
        "unidades": zs.get("units"),
        "estadistica": {m: {k: s[k] for k in
                            ("mean", "ci95_mean", "trend", "slope_per_hour",
                             "p_value", "last_value", "p95")}
                        for m, s in zs["metrics"].items()},
        "correlaciones_significativas": zs["correlations"],
        "comparacion_referencia_regional_CAMS": zs.get("reference_notes"),
        "fiabilidad_sensores": zs.get("confidence"),
        "alertas_por_umbral": zs["alerts"],
        "clima": weather,
    }
    return f"""Eres un experto en calidad del aire. Te doy estadística YA CALCULADA
(con tests rigurosos Mann-Kendall y Theil-Sen) de los sensores de una zona,
más el clima actual y previsto (viento clave para dispersión de contaminantes).

NO inventes números: usa solo los proporcionados.
Considera la fiabilidad de cada sensor (campo fiabilidad_sensores): si una
metrica es "no_fiable" o "dudoso" frente a la referencia CAMS, no bases
alertas en ella y menciona la limitacion. El viento fuerte dispersa
contaminantes (mejora PM/NO2); calma y estabilidad los acumulan; la lluvia
lava particulas (mejora PM).

DATOS:
{json.dumps(compact, ensure_ascii=False)}

Responde SOLO JSON válido:
{{
  "status": "ok|warning|critical",
  "alerts": [{{"parameter": "x", "value": 0, "level": "warning|critical", "message": "texto breve"}}],
  "trend": "resumen de tendencias significativas (1-2 frases)",
  "prediction_6h": "predicción razonada con el viento/lluvia previstos (1-2 frases)",
  "recommendations": ["acción 1", "acción 2"],
  "air_quality_index": 0
}}"""

def call_ollama(prompt):
    r = requests.post(f"{OLLAMA_URL}/api/generate",
                      json={"model": OLLAMA_MODEL, "prompt": prompt,
                            "stream": False,
                            "options": {"temperature": 0.2,
                                        "num_predict": OLLAMA_NUM_PREDICT}},
                      timeout=OLLAMA_TIMEOUT)
    r.raise_for_status()
    raw = r.json().get("response", "")
    a, b = raw.find("{"), raw.rfind("}") + 1
    if a < 0 or b <= a:
        return {"error": "LLM no devolvió JSON", "raw": raw[:300]}
    try:
        return json.loads(raw[a:b])
    except json.JSONDecodeError:
        return {"error": "JSON inválido del LLM", "raw": raw[a:b][:300]}

def rules_summary(zs, weather):
    """
    Informe determinista sin LLM: redacta estado, tendencias, prediccion
    cualitativa (con viento/lluvia) y recomendaciones desde la estadistica.
    """
    conf = zs.get("confidence") or {}
    alerts = []
    for a in zs["alerts"]:
        m = a["parameter"]
        label = METRICS.get(m, {}).get("label", m)
        alerts.append({
            "parameter": m, "value": a["value"], "level": a["level"],
            "confidence": a.get("confidence", "sin_referencia"),
            "message": f"{label} en {a['value']} supera el umbral "
                       f"{a['level']} ({a['threshold']})."})
    status = ("critical" if any(a["level"] == "critical" for a in alerts)
              else "warning" if alerts else "ok")
    # nota sobre fiabilidad: distingue no_fiable (excluidos) de dudoso (cautela)
    not_reliable = [METRICS.get(m, {}).get("label", m)
                    for m, c in conf.items() if c.get("label") == "no_fiable"]
    doubtful = [METRICS.get(m, {}).get("label", m)
                for m, c in conf.items() if c.get("label") == "dudoso"]

    sig = [(m, s) for m, s in zs["metrics"].items()
           if s.get("p_value") is not None and s["p_value"] < 0.05
           and s["trend"] in ("creciente", "decreciente")]
    if sig:
        partes = [f"{METRICS.get(m, {}).get('label', m)} {s['trend']} "
                  f"({s['slope_per_hour']:+.3f}/h, p={s['p_value']})"
                  for m, s in sig]
        trend_txt = "Tendencias significativas: " + "; ".join(partes) + "."
    else:
        trend_txt = "Sin tendencias estadisticamente significativas en la ventana."

    pred = []
    pm = zs["metrics"].get("pm25") or zs["metrics"].get("pm10")
    w12 = (weather or {}).get("next12h") or {}
    wind12 = w12.get("wind_mean_kmh")
    rain12 = w12.get("precip_total_mm")
    if pm and pm["trend"] == "creciente":
        pred.append("las particulas vienen subiendo")
    if wind12 is not None:
        if wind12 >= 15:
            pred.append(f"viento medio previsto {wind12} km/h favorece la dispersion")
        elif wind12 <= 6:
            pred.append(f"viento flojo previsto ({wind12} km/h) favorece la acumulacion")
        else:
            pred.append(f"viento moderado previsto ({wind12} km/h)")
    if rain12 is not None and rain12 > 0.5:
        pred.append(f"lluvia prevista ({rain12} mm) ayudara a lavar particulas")
    prediction = ("Proximas horas: " + "; ".join(pred) + ".") if pred else \
                 "Sin datos meteorologicos para predecir."

    recs = []
    if status == "critical":
        recs = ["Evitar actividad fisica intensa al aire libre.",
                "Personas sensibles: permanecer en interiores."]
    elif status == "warning":
        recs = ["Grupos sensibles: limitar esfuerzos prolongados al aire libre.",
                "Vigilar la evolucion en las proximas horas."]
    else:
        recs = ["Sin restricciones: calidad del aire dentro de umbrales."]

    pm25 = zs["metrics"].get("pm25", {}).get("last_value")
    aqi = None
    if pm25 is not None:
        # indice simplificado tipo EPA por tramos de PM2.5
        bps = [(12, 0, 50), (35.4, 51, 100), (55.4, 101, 150),
               (150.4, 151, 200), (250.4, 201, 300), (500, 301, 500)]
        lo_c = 0.0
        for hi_c, i_lo, i_hi in bps:
            if pm25 <= hi_c:
                aqi = round(i_lo + (i_hi - i_lo) * (pm25 - lo_c) / (hi_c - lo_c))
                break
            lo_c = hi_c
        if aqi is None:
            aqi = 500
    notes_parts = []
    if not_reliable:
        notes_parts.append(
            "No fiables, excluidos de alertas (validado vs referencias): "
            + ", ".join(not_reliable))
    if doubtful:
        notes_parts.append(
            "Fiabilidad dudosa, usar con cautela: " + ", ".join(doubtful))
    return {"status": status, "alerts": alerts, "trend": trend_txt,
            "prediction_6h": prediction, "recommendations": recs,
            "air_quality_index": aqi, "generator": "rules",
            "data_quality_note": ". ".join(notes_parts) if notes_parts else None}


# ── Historico de analisis (BD separada) ─────────────────────────────────────
_STATUS_CODE = {"ok": 0.0, "warning": 1.0, "critical": 2.0}

def analysis_db_client():
    c = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT,
                       username=INFLUX_USER, password=INFLUX_PASS or None,
                       database=ANALYSIS_DB)
    if ANALYSIS_DB not in [d["name"] for d in c.get_list_database()]:
        log.info("Creando base de datos de historico '%s'", ANALYSIS_DB)
        c.create_database(ANALYSIS_DB)
    return c

def store_analysis(result):
    """Guarda un punto por analisis de zona: numerico para Grafana + textos."""
    try:
        fields = {
            "status_code": _STATUS_CODE.get(result.get("status"), -1.0),
            "aqi": float(result.get("air_quality_index") or 0),
            "n_alerts": float(len(result.get("alerts") or [])),
            "trend_txt": str(result.get("trend", ""))[:500],
            "prediction_txt": str(result.get("prediction_6h", ""))[:500],
            "alerts_txt": "; ".join(a.get("message", a.get("parameter", ""))
                                    for a in (result.get("alerts") or []))[:500],
        }
        for m, s in (result.get("statistics") or {}).items():
            fields[f"{m}_last"] = float(s["last_value"])
            if s.get("p_value") is not None and s["p_value"] < 0.05:
                fields[f"{m}_slope"] = float(s["slope_per_hour"])
        # confianza por metrica (codificada: fiable=2, dudoso=1, no_fiable=0)
        conf_code = {"fiable": 2.0, "dudoso": 1.0, "no_fiable": 0.0}
        for m, c in (result.get("confidence") or {}).items():
            lab = c.get("label")
            if lab in conf_code:
                fields[f"{m}_conf"] = conf_code[lab]
            if c.get("spearman_rho") is not None:
                fields[f"{m}_rho"] = float(c["spearman_rho"])
        for m, info in (result.get("pm_corrections") or {}).items():
            if info.get("factor") is not None:
                fields[f"{m}_corr_factor"] = float(info["factor"])
            rhm = info.get("rh_model") or {}
            if rhm.get("corrected_last") is not None:
                fields[f"{m}_corr_rh"] = float(rhm["corrected_last"])
        # qué referencia oficial respondió: euskadi | openaq | (ninguna -> cams)
        prov = result.get("official_provider")
        if prov:
            fields["ref_source"] = str(prov)
            sts = result.get("official_stations") or []
            if sts:
                fields["ref_stations"] = ", ".join(str(s) for s in sts if s)[:300]
        w = (result.get("weather") or {}).get("now") or {}
        if w.get("wind_kmh") is not None:
            fields["wind_kmh"] = float(w["wind_kmh"])
        if w.get("wind_dir_txt"):
            fields["wind_dir_txt"] = str(w["wind_dir_txt"])
        if w.get("wind_dir_deg") is not None:
            fields["wind_dir_deg"] = float(w["wind_dir_deg"])
        if w.get("weather_desc"):
            fields["weather_desc"] = str(w["weather_desc"])
        if w.get("weather_icon"):
            fields["weather_icon"] = str(w["weather_icon"])
        if w.get("temperature_c") is not None:
            fields["temp_c"] = float(w["temperature_c"])
        if w.get("cloud_cover_pct") is not None:
            fields["cloud_pct"] = float(w["cloud_cover_pct"])
        if w.get("visibility_km") is not None:
            fields["vis_km"] = float(w["visibility_km"])
        if w.get("uv_index") is not None:
            fields["uv"] = float(w["uv_index"])
        nxt = (result.get("weather") or {}).get("next12h") or {}
        if nxt.get("snow_total_cm") is not None:
            fields["snow_12h"] = float(nxt["snow_total_cm"])
        c = analysis_db_client()
        c.write_points([{"measurement": ANALYSIS_MEAS,
                         "tags": {"geo3": result["geo3"],
                                  "status": result.get("status", "?")},
                         "fields": fields}])
        c.close()
    except Exception as e:
        log.error("no pude guardar historico de %s: %s", result.get("geo3"), e)

# ── Telegram ─────────────────────────────────────────────────────────────────
def telegram_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Telegram no configurado; mensaje omitido")
        return False
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if TELEGRAM_THREAD_ID:
            payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=30)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Telegram fallo: %s", e)
        return False

_last_status = {}  # geo3 -> status anterior (aviso solo en transiciones)

# URL pública de la web, para enlazarla en los mensajes de Telegram.
# Vacía por defecto: cada despliegue pone la suya en WEB_URL.
WEB_URL = os.getenv("WEB_URL", "").strip()

_CONF_ICON = {"fiable": "\u2705", "dudoso": "\u26A0\uFE0F",
              "no_fiable": "\u274C", "sin_referencia": "\u2753"}

def _fmt_weather(result):
    """Línea compacta del estado del tiempo."""
    w = (result.get("weather") or {}).get("now") or {}
    parts = []
    if w.get("weather_icon") and w.get("weather_desc"):
        parts.append(f"{w['weather_icon']} {w['weather_desc']}")
    if w.get("temperature_c") is not None:
        parts.append(f"{w['temperature_c']}\u00b0C")
    if w.get("humidity_pct") is not None:
        parts.append(f"{int(w['humidity_pct'])}% HR")
    if w.get("wind_kmh") is not None:
        d = f" {w.get('wind_dir_txt')}" if w.get("wind_dir_txt") else ""
        parts.append(f"\U0001F4A8 {w['wind_kmh']} km/h{d}")
    return " \u00b7 ".join(parts)

def _fmt_caqi(result):
    """Índice europeo CAQI con su etiqueta."""
    cq = (result.get("caqi") or {}).get("pm25")
    if not cq:
        return None
    return f"\U0001F30D CAQI: <b>{cq['label']}</b> (PM2.5 media {cq['value']} \u00b5g/m\u00b3)"

def _fmt_episodes(result):
    """Episodios sostenidos de contaminación."""
    eps = result.get("episodes") or []
    if not eps:
        return None
    labels = {"db": "Ruido", "pm25": "PM2.5", "pm10": "PM10",
              "no2": "NO2", "o3": "O3", "nh3": "NH3", "co": "CO"}
    out = []
    for e in eps[:3]:
        lbl = labels.get(e["metric"], e["metric"])
        out.append(f"  \U0001F534 {lbl}: {e['duration_h']}h sostenido "
                   f"(pico {e['peak']}, media {e['mean']})")
    return "\n".join(out)

def _fmt_confidence(result):
    """Resumen de fiabilidad de los sensores por métrica."""
    conf = result.get("confidence") or {}
    if not conf:
        return None
    no_fiables = [m.upper() for m, c in conf.items()
                  if c.get("label") == "no_fiable"]
    if not no_fiables:
        return None
    return (f"  \u274C No fiables (excluidos de alertas): "
            f"{', '.join(no_fiables)}")

def _fmt_sensor_health(result):
    """Avisos de salud de los sensores (consenso local)."""
    cross = result.get("cross_sensor") or {}
    notes = cross.get("notes") or []
    if not notes:
        return None
    return "\n".join(f"  \U0001F527 {n}" for n in notes[:3])

def maybe_alert_critical(result):
    """Aviso al entrar/salir de estado crítico, con contexto completo."""
    g = result["geo3"]
    st = result.get("status")
    prev = _last_status.get(g)
    _last_status[g] = st

    if st == "critical" and prev != "critical":
        lines = [f"\U0001F6A8 <b>CRITICO en zona {g.upper()}</b>"]
        # alertas activas con su nivel de confianza
        for a in (result.get("alerts") or []):
            ic = _CONF_ICON.get(a.get("confidence", ""), "")
            msg = a.get("message") or a.get("parameter", "")
            lines.append(f"\u2022 {msg} {ic}")
        # episodios sostenidos
        eps = _fmt_episodes(result)
        if eps:
            lines.append("\n<b>Episodios sostenidos:</b>")
            lines.append(eps)
        # contexto meteorológico
        wx = _fmt_weather(result)
        if wx:
            lines.append(f"\n{wx}")
        # rosa de contaminación (de dónde viene)
        rose = (result.get("pollution_rose") or {}).get("note")
        if rose:
            lines.append(f"\U0001F9ED {rose}")
        # predicción
        if result.get("prediction_6h"):
            lines.append(f"\n{result['prediction_6h']}")
        # CAQI
        caqi = _fmt_caqi(result)
        if caqi:
            lines.append(caqi)
        # recomendaciones
        recs = result.get("recommendations") or []
        if recs:
            lines.append("\n<b>Recomendaciones:</b>")
            for r in recs[:3]:
                lines.append(f"  \u2022 {r}")
        if WEB_URL:
            lines.append(f"\n<a href=\"{WEB_URL}\">Ver detalle en la web</a>")
        telegram_send("\n".join(lines))

    elif prev == "critical" and st != "critical":
        st_txt = {"ok": "normal", "warning": "atenci\u00f3n"}.get(st, st)
        wx = _fmt_weather(result)
        msg = [f"\u2705 <b>Zona {g.upper()}</b> vuelve a estado <b>{st_txt}</b>."]
        if wx:
            msg.append(wx)
        if result.get("prediction_6h"):
            msg.append(result["prediction_6h"])
        telegram_send("\n".join(msg))


def daily_summary():
    """
    Resumen diario ampliado: estado por zona, CAQI, tiempo, episodios,
    fiabilidad de sensores, tendencias vs ayer y cobertura global.
    """
    try:
        # ── datos del histórico ───────────────────────────────────────────
        c = analysis_db_client()
        res = c.query(
            f'SELECT last("status_code") AS sc, last("aqi") AS aqi, '
            f'last("alerts_txt") AS al, last("prediction_txt") AS pred, '
            f'last("weather_desc") AS wdesc, last("weather_icon") AS wicon, '
            f'last("temp_c") AS temp, last("wind_kmh") AS wind, '
            f'last("wind_dir_txt") AS wdir, last("pm25_last") AS pm25, '
            f'last("db_last") AS db '
            f'FROM "{ANALYSIS_MEAS}" WHERE time > now() - 24h GROUP BY "geo3"')
        comp = c.query(
            f'SELECT mean("aqi") AS aqi24, mean("db_last") AS db24, '
            f'mean("pm25_last") AS pm25_24 '
            f'FROM "{ANALYSIS_MEAS}" WHERE time > now() - 24h GROUP BY "geo3"')
        prev = c.query(
            f'SELECT mean("aqi") AS aqi48, mean("db_last") AS db48, '
            f'mean("pm25_last") AS pm25_48 FROM "{ANALYSIS_MEAS}" '
            f'WHERE time > now() - 48h AND time <= now() - 24h GROUP BY "geo3"')
        c.close()

        comp_map = {t.get("geo3"): next(iter(p), {})
                    for (mname, t), p in comp.items() if t}
        prev_map = {t.get("geo3"): next(iter(p), {})
                    for (mname, t), p in prev.items() if t}
        emoji = {0: "\u2705", 1: "\u26A0\uFE0F", 2: "\U0001F6A8"}

        # cabecera con cobertura global
        try:
            tiers = all_zones_tiered()
            n_act = sum(1 for z in tiers if z["tier"] == 1)
            header = (f"\U0001F4CA <b>Resumen diario \u00b7 Calidad del aire</b>\n"
                      f"<i>{n_act} zonas activas de {len(tiers)} monitorizadas "
                      f"en el mundo</i>")
        except Exception:
            header = "\U0001F4CA <b>Resumen diario \u00b7 Calidad del aire</b>"
        lines = [header]

        any_zone = False
        for (mname, tags), pts in res.items():
            if not tags:
                continue
            g = tags.get("geo3")
            p = next(iter(pts), {})
            any_zone = True
            sc = int(p.get("sc") or 0)
            aqi = int(p.get("aqi") or 0)
            lines.append(f"\n{emoji.get(sc, '?')} <b>{g.upper()}</b> \u2014 AQI {aqi}")

            # métricas clave
            met = []
            if p.get("pm25") is not None:
                met.append(f"PM2.5 {p['pm25']} \u00b5g/m\u00b3")
            if p.get("db") is not None:
                met.append(f"ruido {p['db']} dB")
            if met:
                lines.append("  " + " \u00b7 ".join(met))

            # tiempo actual
            wx = []
            if p.get("wicon") and p.get("wdesc"):
                wx.append(f"{p['wicon']} {p['wdesc']}")
            if p.get("temp") is not None:
                wx.append(f"{p['temp']}\u00b0C")
            if p.get("wind") is not None:
                d = f" {p['wdir']}" if p.get("wdir") else ""
                wx.append(f"\U0001F4A8 {p['wind']} km/h{d}")
            if wx:
                lines.append("  " + " \u00b7 ".join(wx))

            # alertas
            if p.get("al"):
                lines.append(f"  \u26A0\uFE0F {p['al']}")

            # comparación con ayer
            cm, pm = comp_map.get(g, {}), prev_map.get(g, {})
            difs = []
            for key, label in (("pm25", "PM2.5"), ("db", "ruido"), ("aqi", "AQI")):
                a = cm.get(f"{key}24" if key != "aqi" else "aqi24")
                b = pm.get(f"{key}48" if key != "aqi" else "aqi48")
                if a is not None and b is not None and b != 0:
                    pct = 100.0 * (a - b) / abs(b)
                    if abs(pct) >= 5:
                        arrow = "\u2197\uFE0F" if pct > 0 else "\u2198\uFE0F"
                        difs.append(f"{label} {arrow}{abs(pct):.0f}%")
            if difs:
                lines.append("  vs ayer: " + ", ".join(difs))

            # predicción
            if p.get("pred"):
                lines.append(f"  \U0001F52E {p['pred']}")

        if not any_zone:
            lines.append("\nSin an\u00e1lisis registrados en las \u00faltimas 24h.")

        # ── salud de la red de sensores (una vez, no por zona) ────────────
        try:
            health = []
            for z in (all_zones_tiered() or [])[:6]:
                if z["tier"] != 1:
                    continue
                cross = cross_sensor_validation(z["geo3"], 24)
                for n in ((cross or {}).get("notes") or [])[:2]:
                    health.append(f"  \U0001F527 {n}")
            if health:
                lines.append("\n<b>\u2699\uFE0F Salud de la red</b>")
                lines.extend(health[:5])
        except Exception as e:
            log.debug("salud de red no disponible: %s", e)

        if WEB_URL:
            lines.append(f"\n<a href=\"{WEB_URL}\">Ver an\u00e1lisis completo</a>")
        telegram_send("\n".join(lines))
        log.info("[Telegram] resumen diario enviado")
    except Exception as e:
        log.error("resumen diario fallo: %s", e)


def _sensor_names():
    """Mapa mac -> nombre legible (field name_1), para etiquetar sensores."""
    c = influx_client()
    names = {}
    try:
        q = (f'SELECT last("name_1") AS sname FROM "{MEASUREMENT}" '
             f'WHERE time > now() - {SENSORS_ACTIVE_HOURS}h GROUP BY "mac"')
        res = c.query(q)
        for (meas, tags), pts in res.items():
            if tags and tags.get("mac"):
                sn = next(iter(pts), {}).get("sname")
                if sn:
                    names[tags["mac"]] = sn
    except Exception as e:
        log.warning("no pude leer nombres de sensores: %s", e)
    finally:
        c.close()
    return names


def collect_sensors():
    """
    Lista todas las estaciones CanAirIO con datos en las últimas
    SENSORS_ACTIVE_HOURS horas: última lectura de PM2.5, PM10, NO2 y posición
    (decodificada del geohash). Agrupa por estación única (mac+geo3+name).
    """
    c = influx_client()
    rows = []
    try:
        # agrupamos por los tags que identifican una estación física.
        # 'name' NO es tag (va como field), así que lo traemos con last().
        # Traemos también gases (ppm) y ruido (dB) para la tabla.
        # Además del último valor pedimos la media de la ventana: un nodo que
        # no lleva ese sensor publica 0 en TODAS sus lecturas, y 0 está dentro
        # del rango físico de pm*, no2, o3, nh3, co y db, así que pasaba como
        # dato bueno y la tabla mostraba ceros falsos. media == 0 => no lo mide.
        _tabla = ("pm25", "pm10", "no2", "o3", "nh3", "co", "db")
        medias = ", ".join(f'mean("{m}") AS "{m}__m"' for m in _tabla)
        q = (f'SELECT last("pm25") AS pm25, last("pm10") AS pm10, '
             f'last("no2") AS no2, last("o3") AS o3, last("nh3") AS nh3, '
             f'last("co") AS co, last("db") AS db, '
             f'last("geo") AS geo, last("name_1") AS sname, {medias} '
             f'FROM "{MEASUREMENT}" '
             f'WHERE time > now() - {SENSORS_ACTIVE_HOURS}h '
             f'GROUP BY "mac", "geo3"')
        res = c.query(q)
        # Timestamp real de la última lectura por sensor. Lo pedimos aparte
        # porque combinar varios last() en una query mezcla timestamps; aquí
        # usamos el time del último pm25 (o pm10) de cada mac.
        last_times = {}
        try:
            qt = (f'SELECT last("pm25") FROM "{MEASUREMENT}" '
                  f'WHERE time > now() - {SENSORS_ACTIVE_HOURS}h '
                  f'GROUP BY "mac"')
            for (mn, tg), pts in c.query(qt, epoch=None).items():
                p0 = next(iter(pts), {})
                if tg and tg.get("mac") and p0.get("time"):
                    last_times[tg["mac"]] = p0["time"]
            # algunos sensores no envían pm25; completa con db
            qt2 = (f'SELECT last("db") FROM "{MEASUREMENT}" '
                   f'WHERE time > now() - {SENSORS_ACTIVE_HOURS}h '
                   f'GROUP BY "mac"')
            for (mn, tg), pts in c.query(qt2, epoch=None).items():
                p0 = next(iter(pts), {})
                if tg and tg.get("mac") and p0.get("time") \
                        and tg["mac"] not in last_times:
                    last_times[tg["mac"]] = p0["time"]
        except Exception as e:
            log.warning("no pude obtener last_time por sensor: %s", e)
        # volumen molar estándar (25°C, 1 atm) para conversión ppm->µg/m³ ligera
        VM_STD = 24.45
        def gas_ug(val, molar_mass):
            if val is None:
                return None
            return round(val * 1000.0 * molar_mass / VM_STD, 1)
        def in_range(metric, val):
            """Descarta lecturas fuera del rango físico válido (mismo que el
            análisis). Devuelve el valor si es plausible, None si es imposible."""
            if val is None:
                return None
            rng = METRICS.get(metric, {}).get("range")
            if rng and not (rng[0] <= val <= rng[1]):
                return None
            return val
        def ranged(metric, val, punto=None):
            """
            Devuelve (valor, fuera_de_rango). Marca en vez de descartar.
            Si la media de la ventana es exactamente 0, el nodo no lleva ese
            sensor: devuelve None para que la tabla lo deje en blanco en vez
            de mostrar un 0 que parece una medida.
            """
            if val is None:
                return None, False
            if ZERO_MEANS_ABSENT and punto is not None:
                media = punto.get(f"{metric}__m")
                if media is not None and media == 0:
                    return None, False
            rng = METRICS.get(metric, {}).get("range")
            out = bool(rng and not (rng[0] <= val <= rng[1]))
            return val, out
        for (mname, tags), pts in res.items():
            if not tags:
                continue
            p = next(iter(pts), {})
            geo = p.get("geo")  # geohash de 7 chars (posición fina)
            lat = lon = None
            if geo:
                try:
                    lat, lon = geohash_center(geo)
                except Exception:
                    lat = lon = None
            elif tags.get("geo3"):
                try:
                    lat, lon = geohash_center(tags["geo3"])
                except Exception:
                    lat = lon = None
            pm25, pm25o = ranged("pm25", _r(p.get("pm25")), p)
            pm10, pm10o = ranged("pm10", _r(p.get("pm10")), p)
            no2, no2o   = ranged("no2", gas_ug(p.get("no2"), 46.01), p)
            o3, o3o     = ranged("o3", gas_ug(p.get("o3"), 48.00), p)
            nh3, nh3o   = ranged("nh3", gas_ug(p.get("nh3"), 17.03), p)
            co, coo     = ranged("co", _r(p.get("co")), p)  # ya viene en mg/m³
            db, dbo     = ranged("db", _r(p.get("db")), p)
            rows.append({
                "name": p.get("sname") or (tags.get("mac") or "")[:8],
                "mac": tags.get("mac"),
                "geo3": tags.get("geo3"),
                "lat": round(lat, 4) if lat is not None else None,
                "lon": round(lon, 4) if lon is not None else None,
                "pm25": pm25, "pm10": pm10, "no2": no2, "o3": o3,
                "nh3": nh3, "co": co, "db": db,
                "flags": {k: True for k, v in
                          {"pm25": pm25o, "pm10": pm10o, "no2": no2o, "o3": o3o,
                           "nh3": nh3o, "co": coo, "db": dbo}.items() if v},
                "last_time": last_times.get(tags.get("mac")) or p.get("time"),
            })
            if len(rows) >= SENSORS_MAX:
                break
    finally:
        c.close()
    # orden por zona y nombre para una tabla estable
    rows.sort(key=lambda r: (r.get("geo3") or "", r.get("name") or ""))
    return rows


def publish_site():
    """Recopila estado actual + histórico de la BD analysis y publica a Pages."""
    try:
        c = analysis_db_client()
        # estado actual: ultimo punto por zona
        last = c.query(
            f'SELECT last("status_code") AS sc, last("aqi") AS aqi, '
            f'last("alerts_txt") AS al, last("trend_txt") AS tr, '
            f'last("prediction_txt") AS pred, last("pm25_last") AS pm25, '
            f'last("db_last") AS db, last("no2_last") AS no2, '
            f'last("wind_kmh") AS wind, last("wind_dir_txt") AS wdir, '
            f'last("wind_dir_deg") AS wdeg, '
            f'last("weather_desc") AS weather_desc, last("weather_icon") AS weather_icon, '
            f'last("temp_c") AS temp_c, last("cloud_pct") AS cloud_pct, '
            f'last("vis_km") AS vis_km, last("uv") AS uv, last("snow_12h") AS snow_12h, '
            f'last("pm25_conf") AS pm25c, '
            f'last("pm10_conf") AS pm10c, last("no2_conf") AS no2c, '
            f'last("o3_conf") AS o3c, last("pm25_corr_factor") AS pm25f, '
            f'last("ref_source") AS refsrc, last("ref_stations") AS refsts '
            f'FROM "{ANALYSIS_MEAS}" '
            f'WHERE time > now() - 6h GROUP BY "geo3"')
        st_name = {0: "ok", 1: "warning", 2: "critical"}
        conf_name = {2: "fiable", 1: "dudoso", 0: "no_fiable"}
        zones = []
        for (mname, tags), pts in last.items():
            if not tags:
                continue
            p = next(iter(pts), {})
            def cf(key):
                v = p.get(key)
                return conf_name.get(int(v)) if v is not None else None
            zones.append({
                "geo3": tags.get("geo3"),
                "status": st_name.get(int(p.get("sc") or 0), "?"),
                "aqi": int(p.get("aqi") or 0),
                "pm25": p.get("pm25"), "noise_db": p.get("db"),
                "no2": p.get("no2"), "wind_kmh": p.get("wind"),
                "wind_dir_txt": p.get("wdir"),
                "wind_dir_deg": p.get("wdeg"),
                "weather_now": {
                    "weather_desc":    p.get("weather_desc"),
                    "weather_icon":    p.get("weather_icon"),
                    "temperature_c":   p.get("temp_c"),
                    "cloud_cover_pct": p.get("cloud_pct"),
                    "visibility_km":   p.get("vis_km"),
                    "uv_index":        p.get("uv"),
                    "snow_next12h":    p.get("snow_12h"),
                },
                "alerts": p.get("al") or "",
                "trend": p.get("tr") or "",
                "prediction": p.get("pred") or "",
                "confidence": {"pm25": cf("pm25c"), "pm10": cf("pm10c"),
                               "no2": cf("no2c"), "o3": cf("o3c")},
                "pm25_corr_factor": p.get("pm25f"),
                # referencia usada para validar: euskadi | openaq | cams
                "ref_source": p.get("refsrc") or "cams",
                "ref_stations": p.get("refsts") or "",
            })
        # histórico para timeline/gráficas: series por hora por zona
        hist = c.query(
            f'SELECT mean("aqi") AS aqi, mean("pm25_last") AS pm25, '
            f'mean("db_last") AS noise_db, mean("status_code") AS status '
            f'FROM "{ANALYSIS_MEAS}" WHERE time > now() - {publisher.HISTORY_DAYS}d '
            f'GROUP BY "geo3", time(1h) fill(none)')
        points = []
        for (mname, tags), pts in hist.items():
            if not tags:
                continue
            g = tags.get("geo3")
            for p in pts:
                points.append({"geo3": g, "t": p["time"],
                               "aqi": _r(p.get("aqi")), "pm25": _r(p.get("pm25")),
                               "noise_db": _r(p.get("noise_db")),
                               "status": _r(p.get("status"))})
        c.close()
        # análisis individual por sensor dentro de cada zona
        nombres = _sensor_names()
        for z in zones:
            g = z.get("geo3")
            sens_list = []
            for s in zone_sensors(g):
                r = analyze_zone_full(g, PUBLISH_HOURS, mac=s["mac"])
                if not r:
                    continue
                conf = r.get("confidence") or {}
                stt = r.get("statistics") or {}
                units = r.get("units") or {}
                # todas las métricas que ESTE sensor envía (las que tienen stats)
                metrics = {}
                for mkey, mstats in stt.items():
                    if not mstats:
                        continue
                    metrics[mkey] = {
                        "value": mstats.get("last_value"),
                        "label": units.get(mkey, METRICS.get(mkey, {}).get("label", mkey)),
                        "trend": mstats.get("trend"),
                        "confidence": conf.get(mkey, {}).get("label"),
                    }
                sens_list.append({
                    "mac": s["mac"],
                    "name": nombres.get(s["mac"], s["mac"][-6:]),
                    "status": r.get("status", "ok"),
                    "aqi": r.get("air_quality_index"),
                    "metrics": metrics,
                    "alerts": "; ".join(a.get("message", "") for a in r.get("alerts", [])),
                    "trend": r.get("trend", ""),
                    "prediction": r.get("prediction_6h", ""),
                    "data_quality_note": r.get("data_quality_note"),
                    "pm_corrections": r.get("pm_corrections"),
                    # métricas que el sensor envía pero no se pueden analizar
                    # (ej. NH3 leyendo 1520 µg/m³, fuera del rango 0-400)
                    "discarded_metrics": r.get("discarded_metrics"),
                    "points": s["points"],
                })
            z["sensors"] = sens_list
            # notas de zona: consenso entre sensores, rosa, CAQI, episodios y corr. ambiental
            try:
                zst = zone_statistics(g, PUBLISH_HOURS)
                if zst:
                    cross = zst.get("cross_sensor") or {}
                    if cross.get("notes"):
                        z["peer_notes"] = " · ".join(cross["notes"])
                    peer_by_mac = cross.get("sensors") or {}
                    for se in sens_list:
                        pk = peer_by_mac.get(se["mac"])
                        if pk and pk.get("labels"):
                            se["peer_check"] = pk["labels"]
                    rose = zst.get("pollution_rose") or {}
                    if rose.get("note"):
                        z["rose_note"] = rose["note"]
                    # nuevos campos P2.2, P2.3, P2.5
                    if zst.get("caqi"):
                        z["caqi"] = zst["caqi"]
                    if zst.get("episodes"):
                        z["episodes"] = zst["episodes"]
                    if zst.get("env_correlations"):
                        z["env_correlations"] = zst["env_correlations"]
            except Exception as e:
                log.warning("notas de zona no disponibles para %s: %s", g, e)
        publisher.export_json(zones, points,
                              meta={"measurement": MEASUREMENT, "source": "CanAirIO"})
        # listado completo de sensores CanAirIO activos
        try:
            sensors = collect_sensors()
            publisher.export_sensors(
                sensors,
                ranges={m: list(i["range"]) for m, i in METRICS.items()
                        if i.get("range")})
        except Exception as e:
            log.warning("no pude exportar sensors.json: %s", e)
        publisher.git_publish()
        return len(zones), len(points)
    except Exception as e:
        log.error("publish_site fallo: %s", e)
        return 0, 0


def _r(v):
    return round(float(v), 2) if v is not None else None


def analyze_zone_full(geo3, hours, mac=None, skip_weather=False):
    zs = zone_statistics(geo3, hours, mac=mac)
    if not zs:
        return None
    weather = None
    if not skip_weather:
        try:
            weather = fetch_weather(geo3)
        except Exception as e:
            log.warning("clima no disponible para %s: %s", geo3, e)
    if ANALYSIS_MODE == "rules":
        result = rules_summary(zs, weather)
    else:
        try:
            result = call_ollama(build_prompt(zs, weather))
            if "error" in result and ANALYSIS_MODE == "auto":
                log.warning("LLM devolvio error en %s; uso reglas", geo3)
                result = rules_summary(zs, weather)
            else:
                result.setdefault("generator", "llm")
        except requests.RequestException as e:
            log.error("Ollama fallo para %s: %s", geo3, e)
            if ANALYSIS_MODE == "auto":
                result = rules_summary(zs, weather)
            else:
                result = {"error": f"LLM no disponible: {e}",
                          "hint": ("Revisa 'docker logs ollama'. En CPU "
                                   "antigua usa ANALYSIS_MODE=rules.")}
    result["geo3"] = geo3
    if mac:
        result["mac"] = mac
    result["units"] = zs.get("units")
    result["gas_conversion"] = zs.get("gas_conversion")
    result["statistics"] = zs["metrics"]
    result["correlations"] = zs["correlations"]
    result["threshold_alerts"] = zs["alerts"]
    result["reference_comparison"] = zs.get("reference_comparison")
    result["reference_notes"] = zs.get("reference_notes")
    result["confidence"] = zs.get("confidence")
    result["pm_corrections"] = zs.get("pm_corrections")
    result["cross_sensor"] = zs.get("cross_sensor")
    result["pollution_rose"] = zs.get("pollution_rose")
    result["caqi"] = zs.get("caqi")
    result["episodes"] = zs.get("episodes")
    result["hourly_profile"] = zs.get("hourly_profile")
    result["env_correlations"] = zs.get("env_correlations")
    result["official_stations"] = zs.get("official_stations")
    result["official_sources"] = zs.get("official_sources")
    result["official_comparison"] = zs.get("official_comparison")
    result["official_notes"] = zs.get("official_notes")
    result["official_provider"] = zs.get("official_provider")
    result["official_meta"] = zs.get("official_meta")
    result["aggregation"] = zs.get("aggregation")
    result["cleaning"] = zs.get("cleaning")
    result["skipped_no_signal"] = zs.get("skipped_no_signal")
    result["discarded_metrics"] = zs.get("discarded_metrics")
    result["weather"] = weather
    return result

# ── Endpoints ────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "ai-bridge-v2",
                    "db": INFLUX_DB, "measurement": MEASUREMENT,
                    "model": OLLAMA_MODEL})

@app.route("/zones")
def zones():
    hours = int(request.args.get("hours", ACTIVE_WINDOW_H))
    return jsonify({"zones": active_zones(hours), "window_hours": hours})

@app.route("/zones/all")
def zones_all():
    """Todas las zonas con datos, clasificadas por tier. Para diagnóstico global."""
    zs = all_zones_tiered()
    by_tier = {1: [], 2: [], 3: [], 4: []}
    for z in zs:
        by_tier[z["tier"]].append(z)
    return jsonify({
        "total": len(zs),
        "tier1_active": by_tier[1],
        "tier2_recent": by_tier[2],
        "tier3_dormant": by_tier[3],
        "tier4_historic": by_tier[4],
        "thresholds_h": {"tier1": TIER1_HOURS, "tier2": TIER2_HOURS,
                         "tier3": TIER3_HOURS},
    })

@app.route("/stats/<geo3>")
def stats_endpoint(geo3):
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    zs = zone_statistics(geo3, hours)
    if not zs:
        return jsonify({"error": f"sin datos suficientes para {geo3}"}), 404
    return jsonify(zs)

@app.route("/weather/<geo3>")
def weather_endpoint(geo3):
    try:
        w = fetch_weather(geo3)
        if not w:
            return jsonify({"error": "open-meteo sin datos"}), 502
        return jsonify(w)
    except ValueError:
        return jsonify({"error": f"geohash inválido: {geo3}"}), 400

@app.route("/analysis/<geo3>")
def analysis_zone(geo3):
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    res = analyze_zone_full(geo3, hours)
    if not res:
        return jsonify({"error": f"sin datos suficientes para {geo3}"}), 404
    return jsonify(res)

@app.route("/analysis/all")
def analysis_all():
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    zs = active_zones()[:MAX_ZONES]
    results = []
    for z in zs:
        r = analyze_zone_full(z["geo3"], hours)
        if r:
            results.append(r)
    return jsonify({"zones_analyzed": len(results), "results": results})

@app.route("/zone-sensors/<geo3>")
def zone_sensors_endpoint(geo3):
    return jsonify({"geo3": geo3, "sensors": zone_sensors(geo3)})

@app.route("/cross-validation/<geo3>")
def cross_validation_endpoint(geo3):
    """Validación cruzada: cada sensor vs el consenso de sus vecinos."""
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    try:
        out = cross_sensor_validation(geo3, hours)
        if not out:
            return jsonify({"error": f"menos de {CROSS_MIN_SENSORS} sensores "
                            f"comparables en {geo3}"}), 404
        return jsonify({"geo3": geo3, "hours": hours, **out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/pollution-rose/<geo3>")
def pollution_rose_endpoint(geo3):
    """Concentración media por sector de dirección del viento."""
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    zs = zone_statistics(geo3, hours)
    if not zs:
        return jsonify({"error": f"sin datos suficientes para {geo3}"}), 404
    rose = zs.get("pollution_rose")
    if not rose:
        return jsonify({"error": "rosa no disponible (sin viento o sin PM)"}), 404
    return jsonify({"geo3": geo3, "hours": hours, **rose})

@app.route("/analysis-sensor/<geo3>/<path:mac>")
def analysis_sensor(geo3, mac):
    """Análisis completo de UN sensor concreto (mac) dentro de una zona."""
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    res = analyze_zone_full(geo3, hours, mac=mac)
    if not res:
        return jsonify({"error": f"sin datos para {mac} en {geo3}"}), 404
    return jsonify(res)

@app.route("/analysis-sensors/<geo3>")
def analysis_sensors_zone(geo3):
    """Análisis individual de TODOS los sensores de una zona."""
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    out = []
    for s in zone_sensors(geo3):
        r = analyze_zone_full(geo3, hours, mac=s["mac"])
        if r:
            r["points"] = s["points"]
            out.append(r)
    return jsonify({"geo3": geo3, "sensors_analyzed": len(out), "results": out})

@app.route("/sensors")
def sensors_endpoint():
    try:
        rows = collect_sensors()
        return jsonify({"count": len(rows), "sensors": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _primera_zona_activa():
    """
    geo3 de la zona con el dato más reciente. Los endpoints de diagnóstico la
    usan cuando no se les pasa ninguna, para que el proyecto no dependa de una
    zona concreta del despliegue original.
    """
    try:
        zs = all_zones_tiered()
        return zs[0]["geo3"] if zs else None
    except Exception:
        return None


@app.route("/euskadi-debug")
@app.route("/euskadi-debug/<geo3>")
def euskadi_debug(geo3=None):
    """
    Diagnóstico de la red vasca. Sin argumento usa la zona de referencia
    (EUSKADI_DEBUG_ZONE, o la primera zona activa); con
    /euskadi-debug/<geo3> prueba esa zona.
    Muestra el índice descubierto, qué estaciones elige por cercanía y qué
    métricas aporta cada una.
    """
    geo3 = geo3 or os.getenv("EUSKADI_DEBUG_ZONE") or _primera_zona_activa()
    out = {"enabled": euskadi.EUSKADI_ENABLED, "zone": geo3,
           "radius_km": euskadi.EUSKADI_RADIUS_KM,
           "max_fetch": euskadi.EUSKADI_MAX_FETCH,
           "hour_offset": euskadi.EUSKADI_HOUR_OFFSET,
           "forced_stations": euskadi.EUSKADI_STATIONS or None,
           "detail": {}}
    try:
        idx = euskadi.fetch_station_index()
        out["index_size"] = len(idx)
    except Exception as e:
        out["index_error"] = str(e)
        return jsonify(out), 502

    lat, lon, nsens = zone_sensor_center(geo3)
    out["center"] = {"lat": round(lat, 4) if lat is not None else None,
                     "lon": round(lon, 4) if lon is not None else None,
                     "sensors_located": nsens}
    try:
        near = euskadi.resolve_stations(lat=lat, lon=lon)
    except Exception as e:
        out["select_error"] = str(e)
        return jsonify(out), 502
    out["selected"] = [{"name": s.get("name"), "slug": s.get("slug"),
                        "dist_km": s.get("dist_km"), "town": s.get("town")}
                       for s in near if isinstance(s, dict)]
    for st in near:
        label = st.get("name") if isinstance(st, dict) else st
        try:
            s = euskadi.fetch_station(st)
            out["detail"][label] = {m: len(v) for m, v in s.items()} or "vacio"
        except Exception as e:
            out["detail"][label] = f"error: {e}"
    try:
        combined, src = euskadi.fetch_official_series(72, lat=lat, lon=lon)
        out["combined_metrics"] = {m: len(v) for m, v in combined.items()}
        out["sources"] = src
        faltan = [m for m in euskadi.EUSKADI_TARGET_METRICS if m not in combined]
        out["uncovered_metrics"] = faltan or None
    except Exception as e:
        out["combined_error"] = str(e)
    return jsonify(out)

@app.route("/openaq-debug")
@app.route("/openaq-debug/<geo3>")
def openaq_debug(geo3=None):
    """
    Diagnóstico de la referencia global OpenAQ para una zona: qué estaciones
    oficiales hay en el radio, qué métricas miden y cuántas horas se obtienen.
    """
    geo3 = geo3 or os.getenv("OPENAQ_DEBUG_ZONE") or _primera_zona_activa()
    hours = int(request.args.get("hours", 48))
    lat, lon, nsens = zone_sensor_center(geo3)
    if lat is None:
        return jsonify({"error": f"sin posicion para la zona {geo3}"}), 404
    try:
        out = openaq.diagnose(lat, lon, hours=hours)
    except Exception as e:
        return jsonify({"zone": geo3, "error": str(e)}), 502
    out["zone"] = geo3
    out["center"] = {"lat": round(lat, 4), "lon": round(lon, 4),
                     "sensors_located": nsens}
    return jsonify(out)

@app.route("/official-sources")
def official_sources_endpoint():
    """
    Qué fuente de referencia oficial le toca a cada zona activa. Útil para ver
    de un golpe cuántas zonas han dejado de depender solo de CAMS.
    """
    max_tier = int(request.args.get("max_tier", 2))
    out = {"zones": [], "max_tier": max_tier}
    try:
        zones = all_zones_tiered()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    for z in zones:
        if z.get("tier", 9) > max_tier:
            continue
        geo3 = z["geo3"]
        lat, lon, nsens = zone_sensor_center(geo3)
        row = {"geo3": geo3, "tier": z.get("tier"),
               "lat": round(lat, 4) if lat else None,
               "lon": round(lon, 4) if lon else None,
               "sensors_located": nsens, "provider": None, "stations": []}
        if lat is not None:
            try:
                near = euskadi.resolve_stations(lat=lat, lon=lon)
            except Exception:
                near = []
            if near:
                row["provider"] = "euskadi"
                row["stations"] = [s.get("name") for s in near
                                   if isinstance(s, dict)]
            elif openaq.OPENAQ_ENABLED and openaq.OPENAQ_API_KEY:
                try:
                    locs = openaq.find_locations(lat, lon)
                except Exception as e:
                    row["error"] = str(e)
                    locs = []
                if locs:
                    row["provider"] = "openaq"
                    row["stations"] = [l["name"] for l in locs]
        row["provider"] = row["provider"] or "cams"
        out["zones"].append(row)
    out["count"] = len(out["zones"])
    return jsonify(out)

@app.route("/compare-official/<geo3>")
def compare_official_endpoint(geo3):
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    zs = zone_statistics(geo3, hours)
    if not zs:
        return jsonify({"error": f"sin datos suficientes para {geo3}"}), 404
    return jsonify({"geo3": geo3, "hours": hours,
                    "official_provider": zs.get("official_provider"),
                    "official_meta": zs.get("official_meta"),
                    "official_stations": zs.get("official_stations"),
                    "official_sources": zs.get("official_sources"),
                    "official_comparison": zs.get("official_comparison"),
                    "official_notes": zs.get("official_notes"),
                    "official_confidence": zs.get("official_confidence")})

@app.route("/compare/<geo3>")
def compare_endpoint(geo3):
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    zs = zone_statistics(geo3, hours)
    if not zs:
        return jsonify({"error": f"sin datos suficientes para {geo3}"}), 404
    return jsonify({"geo3": geo3, "hours": hours,
                    "reference_comparison": zs.get("reference_comparison"),
                    "reference_notes": zs.get("reference_notes"),
                    "confidence": zs.get("confidence")})

@app.route("/publish")
def publish_endpoint():
    nz, npts = publish_site()
    return jsonify({"published": bool(nz or npts), "zones": nz, "points": npts,
                    "enabled": publisher.PUBLISH_ENABLED})

@app.route("/run-cycle")
def run_cycle_endpoint():
    """Dispara el ciclo de analisis ahora (analiza zonas, guarda historico, avisa)."""
    scheduled_analysis()
    return jsonify({"triggered": True})

@app.route("/telegram/test")
def telegram_test():
    ok = telegram_send("\u2705 ai-bridge: prueba de Telegram correcta.")
    return jsonify({"sent": ok,
                    "configured": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)})

@app.route("/summary/daily")
def summary_daily_endpoint():
    daily_summary()
    return jsonify({"triggered": True})

# ── Ciclo programado ─────────────────────────────────────────────────────────
def scheduled_analysis():
    """
    P2.4: Análisis estratificado por tier — sin límite de zonas.
    Tier 1 (< TIER1_HOURS): análisis completo cada ciclo.
    Tier 2 (< TIER2_HOURS): análisis completo en ciclos pares (cada 2 ciclos).
    Tier 3 (< TIER3_HOURS): solo estadística, sin meteo ni rosa, cada 6 ciclos.
    Tier 4 (históricas): una vez al día, solo estadística.
    """
    try:
        all_z = all_zones_tiered()
        if not all_z:
            log.info("[AI] sin zonas con datos en la base")
            return
        cycle_n = getattr(scheduled_analysis, "_cycle", 0) + 1
        scheduled_analysis._cycle = cycle_n

        t1 = [z for z in all_z if z["tier"] == 1]
        t2 = [z for z in all_z if z["tier"] == 2 and cycle_n % 2 == 0]
        t3 = [z for z in all_z if z["tier"] == 3 and cycle_n % 6 == 0]
        t4 = [z for z in all_z if z["tier"] == 4]  # se gestionan en daily

        to_process = t1 + t2 + t3
        log.info("[AI] ciclo %d: %d zonas tier1, %d tier2, %d tier3, %d tier4 (históric.)",
                 cycle_n, len(t1), len(t2), len(t3), len(t4))

        for z in to_process:
            g = z["geo3"]; tier = z["tier"]
            try:
                # tier 3: análisis reducido (sin meteo ni rosa para ahorrar peticiones)
                r = analyze_zone_full(g, DEFAULT_HOURS,
                                      skip_weather=(tier >= 3 and TIER3_NO_WEATHER))
                if not r:
                    continue
                store_analysis(r)
                maybe_alert_critical(r)
                log.info("[AI][%s] tier=%d status=%s AQI=%s alertas=%d",
                         g, tier, r.get("status", "?"),
                         r.get("air_quality_index", "?"),
                         len(r.get("alerts", [])))
            except Exception as e:
                log.error("[AI][%s] error: %s", g, e)

        if publisher.PUBLISH_ENABLED:
            nz, npts = publish_site()
            log.info("[publisher] %d zonas, %d puntos historicos", nz, npts)
    except Exception as e:
        log.error("[AI] ciclo fallido: %s", e)

if __name__ == "__main__":
    from datetime import datetime, timedelta
    sch = BackgroundScheduler()
    sch.add_job(scheduled_analysis, "interval", seconds=ANALYSIS_INTERVAL,
                next_run_time=datetime.now() + timedelta(seconds=15))
    sch.add_job(daily_summary, "cron", hour=DAILY_SUMMARY_HOUR, minute=0)
    sch.start()
    log.info("ai-bridge v2 | db=%s meas=%s | modelo=%s | ciclo=%ds | "
             "tiers=%d/%d/%dh | historico=%s | telegram=%s | resumen=%02d:00",
             INFLUX_DB, MEASUREMENT, OLLAMA_MODEL, ANALYSIS_INTERVAL,
             TIER1_HOURS, TIER2_HOURS, TIER3_HOURS, ANALYSIS_DB,
             "on" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "off",
             DAILY_SUMMARY_HOUR)
    app.run(host="0.0.0.0", port=5000)

