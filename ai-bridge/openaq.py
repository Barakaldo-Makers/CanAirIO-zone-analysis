# SPDX-License-Identifier: GPL-3.0-or-later
#
# CanAirIO-zone-analysis — official reference: worldwide reference-grade stations (OpenAQ v3)
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
openaq.py — Referencia OFICIAL global vía OpenAQ v3.

Resuelve el hueco que tenía el sistema: fuera de Euskadi solo se validaba
contra CAMS, que es un MODELO. OpenAQ agrega las redes oficiales de referencia
de ~100 países, así que las zonas de Berlín, Calgary, Cali o Bogotá pasan a
validarse contra estaciones físicas igual que Barakaldo.

Contrato idéntico a euskadi.fetch_official_series, para que app.py pueda usar
cualquiera de los dos indistintamente:

    series, source_by_metric = fetch_official_series(hours, lat=.., lon=..)
    series[metric] = {epoch_hora_utc: valor}
    source_by_metric[metric] = ["Nombre estación", ...]

API (https://docs.openaq.org):
    GET /v3/locations?coordinates=LAT,LON&radius=METROS   (radio máx. 25 000 m)
    GET /v3/sensors/{id}/hours?datetime_from=..&datetime_to=..
    Cabecera de autenticación: X-API-Key

Unidades: OpenAQ devuelve cada parámetro con su unidad, que varía por
proveedor (µg/m³, ppm, ppb). Se normaliza a la convención del sistema:
µg/m³ para todo salvo CO, que va en mg/m³ (igual que el CSV de Euskadi).
La conversión usa el volumen molar a la T y P reales cuando se pasan.
"""
import os
import time
import logging
import datetime as _dt

import requests

log = logging.getLogger("openaq")

OPENAQ_ENABLED = os.getenv("OPENAQ_ENABLED", "1") == "1"
OPENAQ_API_KEY = os.getenv("OPENAQ_API_KEY", "").strip()
OPENAQ_BASE = os.getenv("OPENAQ_BASE", "https://api.openaq.org/v3")
OPENAQ_HTTP_TIMEOUT = int(os.getenv("OPENAQ_HTTP_TIMEOUT", "30"))

# El radio máximo que admite la API es 25 km. Se recorta si se pide más.
OPENAQ_RADIUS_KM = float(os.getenv("OPENAQ_RADIUS_KM", "25"))
_RADIUS_MAX_M = 25000

# Solo monitores de referencia (regulatorios). Si se pone a 0 entran también
# sensores de bajo coste de la propia red OpenAQ, que NO sirven de referencia:
# validaríamos un sensor barato contra otro sensor barato.
OPENAQ_MONITOR_ONLY = os.getenv("OPENAQ_MONITOR_ONLY", "1") == "1"
# Estaciones móviles fuera: su posición cambia y rompe la comparación.
OPENAQ_EXCLUDE_MOBILE = os.getenv("OPENAQ_EXCLUDE_MOBILE", "1") == "1"

OPENAQ_MAX_LOCATIONS = int(os.getenv("OPENAQ_MAX_LOCATIONS", "8"))
OPENAQ_MAX_SENSORS = int(os.getenv("OPENAQ_MAX_SENSORS", "24"))
OPENAQ_LOC_TTL = int(os.getenv("OPENAQ_LOC_TTL", "86400"))
# Decimales a los que se redondea la coordenada al buscar estaciones.
# 2 decimales ~= 1,1 km: los sensores de una misma zona comparten entrada de
# caché y una sola llamada, en vez de una por sensor. Sobre un radio de 25 km
# ese desplazamiento es irrelevante.
OPENAQ_LOC_GRID = int(os.getenv("OPENAQ_LOC_GRID", "2"))
OPENAQ_CACHE_TTL = int(os.getenv("OPENAQ_CACHE_TTL", "3600"))
# Pausa mínima entre peticiones, para no chocar con el límite de la API.
OPENAQ_MIN_INTERVAL = float(os.getenv("OPENAQ_MIN_INTERVAL", "0.25"))
# Descarta horas con poca cobertura real dentro del promedio horario.
OPENAQ_MIN_COVERAGE = float(os.getenv("OPENAQ_MIN_COVERAGE", "50"))

# parámetro OpenAQ -> clave del sistema
PARAM_TO_SENSOR = {
    "pm25": "pm25",
    "pm10": "pm10",
    "pm1": "pm1",
    "no2": "no2",
    "o3": "o3",
    "co": "co",
    "so2": "so2",
    "nh3": "nh3",
}

# masa molar (g/mol) para convertir ppm/ppb -> masa por volumen
_MOLAR = {"no2": 46.0055, "o3": 47.9982, "co": 28.010,
          "so2": 64.066, "nh3": 17.031}
# métricas que el sistema maneja en mg/m3 (el resto en µg/m3)
_MG_METRICS = {"co"}

_loc_cache = {}      # (lat4, lon4, radius_m) -> (ts, [locations])
_ser_cache = {}      # sensor_id -> (ts, {epoch: value})
_last_call = [0.0]


class OpenAQError(RuntimeError):
    pass


# ───────────────────────── HTTP ─────────────────────────

def _headers():
    if not OPENAQ_API_KEY:
        raise OpenAQError("falta OPENAQ_API_KEY")
    return {"X-API-Key": OPENAQ_API_KEY, "Accept": "application/json"}


def _get(path, params=None):
    """GET con throttling y errores legibles."""
    wait = OPENAQ_MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    url = f"{OPENAQ_BASE}{path}"
    try:
        r = requests.get(url, params=params, headers=_headers(),
                         timeout=OPENAQ_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise OpenAQError(f"red: {e}") from e
    finally:
        _last_call[0] = time.time()

    if r.status_code == 401:
        raise OpenAQError("401: API key invalida o ausente")
    if r.status_code == 429:
        raise OpenAQError("429: limite de peticiones alcanzado")
    if r.status_code == 422:
        raise OpenAQError(f"422: parametros invalidos ({r.text[:200]})")
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise OpenAQError(f"HTTP {r.status_code}: {e}") from e
    try:
        return r.json()
    except ValueError as e:
        raise OpenAQError(f"respuesta no JSON: {r.text[:200]}") from e


# ──────────────────── conversión de unidades ────────────────────

def _molar_volume(t_c=None, p_hpa=None):
    """Volumen molar en L/mol a la T y P dadas (25 °C y 1013,25 hPa si faltan)."""
    t = 25.0 if t_c is None else float(t_c)
    p = 1013.25 if p_hpa is None else float(p_hpa)
    if p <= 0:
        p = 1013.25
    return 22.414 * ((t + 273.15) / 273.15) * (1013.25 / p)


def _normalize_unit(metric, value, units, t_c=None, p_hpa=None):
    """
    Lleva 'value' a la unidad del sistema: µg/m3, salvo CO en mg/m3.
    Devuelve None si la unidad no se reconoce (mejor descartar que mentir).
    """
    if value is None:
        return None
    u = (units or "").strip().lower().replace("μ", "µ")
    target_mg = metric in _MG_METRICS

    # ─ ya viene en masa/volumen
    if u in ("µg/m³", "µg/m3", "ug/m3", "ugm3"):
        return value / 1000.0 if target_mg else value
    if u in ("mg/m³", "mg/m3", "mgm3"):
        return value if target_mg else value * 1000.0
    if u in ("ng/m³", "ng/m3"):
        v_ug = value / 1000.0
        return v_ug / 1000.0 if target_mg else v_ug

    # ─ viene en volumen/volumen
    M = _MOLAR.get(metric)
    if M is None:
        return None                      # PM en ppm no tiene sentido
    vm = _molar_volume(t_c, p_hpa)
    if u in ("ppm", "ppmv"):
        mg = value * M / vm              # ppm -> mg/m3
        return mg if target_mg else mg * 1000.0
    if u in ("ppb", "ppbv"):
        ug = value * M / vm             # ppb -> µg/m3
        return ug / 1000.0 if target_mg else ug
    return None


def _epoch_from(rec):
    """
    Extrae el inicio de la hora en epoch UTC. OpenAQ ha usado varias formas:
    period.datetimeFrom.utc, datetimeFrom.utc, o una cadena ISO suelta.
    """
    cand = None
    per = rec.get("period") or {}
    for src in (per.get("datetimeFrom"), rec.get("datetimeFrom")):
        if isinstance(src, dict):
            cand = src.get("utc") or src.get("local")
        elif isinstance(src, str):
            cand = src
        if cand:
            break
    if not cand:
        return None
    s = cand.replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    # se sella al inicio de la hora
    d = d.replace(minute=0, second=0, microsecond=0)
    return int(d.timestamp())


def _coverage_ok(rec):
    cov = rec.get("coverage") or {}
    pct = cov.get("percentComplete")
    if pct is None:
        pct = cov.get("percentCoverage")
    if pct is None:
        return True
    try:
        return float(pct) >= OPENAQ_MIN_COVERAGE
    except (TypeError, ValueError):
        return True


# ───────────────────── descubrimiento de estaciones ─────────────────────

def find_locations(lat, lon, radius_km=None, force=False):
    """
    Estaciones oficiales dentro del radio. Devuelve lista de dicts:
      {id, name, lat, lon, country, sensors: [{id, metric, units}]}
    Lista vacía => no hay red oficial cerca de esa zona.
    """
    if lat is None or lon is None:
        return []
    radius_m = int(min((radius_km or OPENAQ_RADIUS_KM) * 1000, _RADIUS_MAX_M))
    glat = round(float(lat), OPENAQ_LOC_GRID)
    glon = round(float(lon), OPENAQ_LOC_GRID)
    key = (glat, glon, radius_m)
    now = time.time()
    hit = _loc_cache.get(key)
    if hit and not force and now - hit[0] < OPENAQ_LOC_TTL:
        return hit[1]

    params = {
        "coordinates": f"{glat},{glon}",
        "radius": radius_m,
        "limit": max(OPENAQ_MAX_LOCATIONS * 3, 25),
    }
    if OPENAQ_MONITOR_ONLY:
        params["monitor"] = "true"
    if OPENAQ_EXCLUDE_MOBILE:
        params["mobile"] = "false"

    data = _get("/locations", params)
    out = []
    for loc in data.get("results") or []:
        sensors = []
        for s in loc.get("sensors") or []:
            p = (s.get("parameter") or {})
            metric = PARAM_TO_SENSOR.get((p.get("name") or "").lower())
            if metric and s.get("id") is not None:
                sensors.append({"id": s["id"], "metric": metric,
                                "units": p.get("units")})
        if not sensors:
            continue
        c = loc.get("coordinates") or {}
        out.append({
            "id": loc.get("id"),
            "name": loc.get("name") or f"loc {loc.get('id')}",
            "lat": c.get("latitude"),
            "lon": c.get("longitude"),
            "country": ((loc.get("country") or {}).get("code") or ""),
            "sensors": sensors,
        })
    out = out[:OPENAQ_MAX_LOCATIONS]
    _loc_cache[key] = (now, out)
    log.info("OpenAQ: %d estaciones en %d km de %.4f,%.4f",
             len(out), radius_m // 1000, lat, lon)
    return out


def fetch_sensor_hours(sensor_id, hours=48, metric=None, units=None,
                       t_c=None, p_hpa=None):
    """Serie horaria de un sensor: {epoch_utc: valor_normalizado}."""
    now = time.time()
    ck = (sensor_id, metric, hours // 24)
    hit = _ser_cache.get(ck)
    if hit and now - hit[0] < OPENAQ_CACHE_TTL:
        return hit[1]

    end = _dt.datetime.now(_dt.timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    start = end - _dt.timedelta(hours=hours + 1)
    params = {
        "datetime_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datetime_to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": min(max(hours + 24, 24), 1000),
    }
    data = _get(f"/sensors/{sensor_id}/hours", params)
    series = {}
    for rec in data.get("results") or []:
        if not _coverage_ok(rec):
            continue
        ep = _epoch_from(rec)
        if ep is None:
            continue
        u = units or ((rec.get("parameter") or {}).get("units"))
        v = _normalize_unit(metric, rec.get("value"), u, t_c=t_c, p_hpa=p_hpa)
        if v is not None:
            series[ep] = round(v, 3)
    _ser_cache[ck] = (now, series)
    return series


# ──────────────────── serie oficial combinada ────────────────────

def fetch_official_series(hours=48, lat=None, lon=None, radius_km=None,
                          t_c=None, p_hpa=None):
    """
    Promedia, por métrica y hora, todas las estaciones oficiales del radio.
    Mismo contrato que euskadi.fetch_official_series.

    t_c / p_hpa: temperatura y presión reales de la zona, para convertir
    ppm/ppb con el volumen molar correcto. Si no se pasan usa 25 °C y 1013 hPa.
    """
    if not OPENAQ_ENABLED or not OPENAQ_API_KEY:
        return {}, {}
    try:
        locs = find_locations(lat, lon, radius_km=radius_km)
    except OpenAQError as e:
        log.warning("OpenAQ locations fallo: %s", e)
        return {}, {}
    if not locs:
        return {}, {}

    acc = {}
    source = {}
    used = 0
    for loc in locs:
        for s in loc["sensors"]:
            if used >= OPENAQ_MAX_SENSORS:
                break
            try:
                pts = fetch_sensor_hours(s["id"], hours=hours,
                                         metric=s["metric"], units=s["units"],
                                         t_c=t_c, p_hpa=p_hpa)
            except OpenAQError as e:
                log.warning("OpenAQ sensor %s fallo: %s", s["id"], e)
                continue
            used += 1
            if not pts:
                continue
            for t, v in pts.items():
                acc.setdefault(s["metric"], {}).setdefault(t, []).append(v)
            src = source.setdefault(s["metric"], [])
            if loc["name"] not in src:
                src.append(loc["name"])
        if used >= OPENAQ_MAX_SENSORS:
            log.info("OpenAQ: tope de %d sensores alcanzado", OPENAQ_MAX_SENSORS)
            break

    combined = {}
    for key, by_hour in acc.items():
        combined[key] = {t: round(sum(vs) / len(vs), 3)
                         for t, vs in by_hour.items()}
    return combined, source


def diagnose(lat, lon, radius_km=None, hours=48):
    """Diagnóstico para el endpoint /openaq-debug."""
    out = {
        "enabled": OPENAQ_ENABLED,
        "api_key_present": bool(OPENAQ_API_KEY),
        "base": OPENAQ_BASE,
        "radius_km": min(radius_km or OPENAQ_RADIUS_KM, _RADIUS_MAX_M / 1000),
        "monitor_only": OPENAQ_MONITOR_ONLY,
        "exclude_mobile": OPENAQ_EXCLUDE_MOBILE,
    }
    if not OPENAQ_API_KEY:
        out["error"] = "falta OPENAQ_API_KEY"
        return out
    try:
        locs = find_locations(lat, lon, radius_km=radius_km, force=True)
    except OpenAQError as e:
        out["error"] = str(e)
        return out
    out["locations"] = [
        {"id": l["id"], "name": l["name"], "country": l["country"],
         "metrics": sorted({s["metric"] for s in l["sensors"]})}
        for l in locs
    ]
    try:
        ser, src = fetch_official_series(hours, lat=lat, lon=lon,
                                        radius_km=radius_km)
        out["metrics"] = {k: len(v) for k, v in ser.items()}
        out["sources"] = src
    except OpenAQError as e:
        out["series_error"] = str(e)
    return out
