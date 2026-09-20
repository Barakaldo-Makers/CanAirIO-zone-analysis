# SPDX-License-Identifier: GPL-3.0-or-later
#
# canairio-zone-analysis — official reference: Basque Government air-quality network
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
euskadi.py — Referencia OFICIAL local: Red de Control de Calidad del Aire
del Gobierno Vasco (Open Data Euskadi).

v2 (09/2026): selección AUTOMÁTICA de estaciones por cercanía geográfica.

Antes había que escribir a mano la lista de estaciones (EUSKADI_STATIONS) y
marcar qué zonas tenían referencia oficial (ZONE_HAS_OFFICIAL=ezt). Ahora el
módulo descubre la red entera y elige por distancia:

  1. estaciones.csv  → las 62 estaciones con latitud/longitud.
  2. url.txt         → las URLs REALES de cada CSV horario.
  3. nearest_stations(lat, lon) → las N más próximas dentro de un radio.

Por qué url.txt y no construir el nombre de fichero: el slug NO se deduce del
nombre de forma consistente.
    AÑORGA            -> ANORGA.csv          (tilde eliminada)
    ALGORTA (BBIZI2)  -> ALGORTA_BBIZI2.csv
    ZIERBENA (Puerto) -> ZIERBENA_Puerto.csv (minúsculas conservadas)
    BANDERAS (meteo)  -> BANDERAS_meteo.csv  (minúsculas)
    ZUBIETA METEO     -> ZUBIETA_METEO.csv   (mayúsculas)
    LASARTE-ORIA      -> LASARTE-ORIA.csv    (guion conservado)
url.txt es la fuente de verdad; el slug calculado solo se usa para emparejar.

Formato del CSV horario: separador ';', decimal con coma, hora 01:00..24:00 en
GMT. El valor etiquetado "H" es el promedio del intervalo [H-1, H), así que se
sella al INICIO del intervalo (H-1). Ver EUSKADI_HOUR_OFFSET.
Unidades: µg/m3 (CO en mg/m3), iguales a las del sensor ya convertido.
"""
import os
import csv
import io
import time
import math
import calendar
import logging
import unicodedata
import requests

log = logging.getLogger("euskadi")

EUSKADI_ENABLED = os.getenv("EUSKADI_ENABLED", "1") == "1"
EUSKADI_CACHE_TTL = int(os.getenv("EUSKADI_CACHE_TTL", "3600"))
EUSKADI_INDEX_TTL = int(os.getenv("EUSKADI_INDEX_TTL", "86400"))
EUSKADI_HTTP_TIMEOUT = int(os.getenv("EUSKADI_HTTP_TIMEOUT", "40"))

# Selección automática por cercanía.
EUSKADI_RADIUS_KM = float(os.getenv("EUSKADI_RADIUS_KM", "25"))
# Candidatas a considerar dentro del radio (solo índice, no se descargan todas).
EUSKADI_MAX_STATIONS = int(os.getenv("EUSKADI_MAX_STATIONS", "12"))
# Tope de CSV que se descargan por zona y ciclo.
EUSKADI_MAX_FETCH = int(os.getenv("EUSKADI_MAX_FETCH", "8"))
# Mínimo de estaciones a promediar aunque ya esté todo cubierto (estabilidad).
EUSKADI_MIN_STATIONS = int(os.getenv("EUSKADI_MIN_STATIONS", "3"))
# Métricas que se intenta cubrir antes de dejar de descargar.
# Las unidades móviles (Zorroza, Elorrieta) suelen ser las únicas con NH3 y O3,
# y por distancia pura quedaban fuera: por eso se selecciona por COBERTURA.
EUSKADI_TARGET_METRICS = [m.strip() for m in os.getenv(
    "EUSKADI_TARGET_METRICS", "pm25,pm10,no2,o3,nh3,co,so2").split(",")
    if m.strip()]

# Lista fija OPCIONAL. Vacía => selección automática por cercanía.
# Se mantiene para poder forzar estaciones concretas en una comparación.
EUSKADI_STATIONS = [s.strip() for s in os.getenv("EUSKADI_STATIONS", "").split(",")
                    if s.strip()]

EUSKADI_YEAR = os.getenv("EUSKADI_YEAR", "2026")
EUSKADI_ROOT = os.getenv(
    "EUSKADI_ROOT",
    "https://opendata.euskadi.eus/contenidos/ds_informes_estudios/"
    f"calidad_aire_{EUSKADI_YEAR}/es_def/adjuntos")
EUSKADI_BASE = os.getenv("EUSKADI_BASE", f"{EUSKADI_ROOT}/datos_horarios")
EUSKADI_STATIONS_CSV = os.getenv("EUSKADI_STATIONS_CSV",
                                 f"{EUSKADI_ROOT}/estaciones.csv")
EUSKADI_URL_INDEX = os.getenv("EUSKADI_URL_INDEX", f"{EUSKADI_ROOT}/url.txt")

# La etiqueta horaria "H" cubre [H-1, H). Se sella al inicio => -1.
# Puesto como variable para poder verificar empíricamente qué desplazamiento
# maximiza la correlación con el sensor (ver /compare-official).
EUSKADI_HOUR_OFFSET = int(os.getenv("EUSKADI_HOUR_OFFSET", "-1"))

# Estaciones solo meteorológicas: no aportan contaminantes, no vale gastar
# una descarga en ellas cuando se eligen por cercanía.
_METEO_HINTS = ("meteo", "METEO")

# encabezado de columna del CSV -> clave del sensor CanAirIO.
HEADER_TO_SENSOR = {
    "NO2": "no2",
    "O3": "o3",
    "PM10": "pm10",
    "PM2,5": "pm25",
    "PM2.5": "pm25",
    "CO": "co",
    "NH3": "nh3",
    "SO2": "so2",
}

_cache = {}          # slug -> (ts, series)
_index_cache = {}    # "index" -> (ts, [station dicts])


# ───────────────────────── utilidades ─────────────────────────

def _num(s):
    """Convierte '1,37' -> 1.37; vacío/--- -> None."""
    if s is None:
        return None
    s = s.strip()
    if not s or s in ("-", "--", "---"):
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _col_key(header):
    """'NO2 (ug/m3)' -> 'NO2'; 'PM2,5 (ug/m3)' -> 'PM2,5'."""
    h = header.strip()
    if "(" in h:
        h = h[:h.index("(")].strip()
    return h


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _slug(name):
    """
    Nombre de estación -> slug de fichero, replicando la convención observada:
    quita tildes y paréntesis, sustituye espacios y puntos por '_', conserva
    guiones y el caso original.
    """
    s = _strip_accents(name).replace("(", " ").replace(")", " ")
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch == "-") else "_")
    slug = "".join(out)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")


def _match_key(s):
    """Clave laxa para emparejar nombre <-> slug: solo alfanuméricos, mayúsculas."""
    return "".join(c for c in _strip_accents(s).upper() if c.isalnum())


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _http_get(url, fetch_url=None):
    if fetch_url:
        return fetch_url(url)
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
        "Accept": "text/csv,text/plain,*/*",
    }
    r = requests.get(url, timeout=EUSKADI_HTTP_TIMEOUT, headers=headers)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "latin-1"
    return r.text


# ───────────────────── índice de estaciones ─────────────────────

def fetch_station_index(fetch_url=None, force=False):
    """
    Descubre la red completa. Devuelve lista de dicts:
      {name, slug, csv_url, lat, lon, province, town, meteo_only}
    Cacheado EUSKADI_INDEX_TTL (24 h por defecto): la red no cambia a diario.
    """
    now = time.time()
    hit = _index_cache.get("index")
    if hit and not force and now - hit[0] < EUSKADI_INDEX_TTL:
        return hit[1]

    # 1) url.txt -> slugs realmente publicados
    slugs = {}
    try:
        txt = _http_get(EUSKADI_URL_INDEX, fetch_url=fetch_url)
        for line in txt.splitlines():
            line = line.strip()
            if not line.endswith(".csv") or "/datos_horarios/" not in line:
                continue
            slug = line.rsplit("/", 1)[-1][:-4]
            slugs[_match_key(slug)] = (slug, line)
    except (requests.RequestException, ValueError) as e:
        log.warning("Euskadi url.txt fallo: %s", e)

    # 2) estaciones.csv -> coordenadas
    stations = []
    try:
        txt = _http_get(EUSKADI_STATIONS_CSV, fetch_url=fetch_url)
        reader = csv.reader(io.StringIO(txt), delimiter=";")
        header = next(reader, None) or []
        idx = {}
        for i, col in enumerate(header):
            c = col.strip().lower()
            if c == "name":
                idx["name"] = i
            elif c == "province":
                idx["province"] = i
            elif c == "town":
                idx["town"] = i
            elif c.startswith("latitude"):
                idx["lat"] = i
            elif c.startswith("longitude"):
                idx["lon"] = i
        for row in reader:
            if not row or len(row) <= max(idx.values() or [0]):
                continue
            name = row[idx["name"]].strip()
            if not name:
                continue
            lat = _num(row[idx["lat"]]) if "lat" in idx else None
            lon = _num(row[idx["lon"]]) if "lon" in idx else None
            mk = _match_key(name)
            slug, url = slugs.get(mk, (None, None))
            if slug is None:
                # el slug calculado suele coincidir; si no, se descarta
                cand = _slug(name)
                slug, url = slugs.get(_match_key(cand), (None, None))
            if slug is None:
                log.debug("Euskadi: sin CSV publicado para '%s'", name)
                continue
            stations.append({
                "name": name,
                "slug": slug,
                "csv_url": url,
                "lat": lat,
                "lon": lon,
                "province": row[idx["province"]].strip() if "province" in idx else "",
                "town": row[idx["town"]].strip() if "town" in idx else "",
                "meteo_only": any(h in name for h in _METEO_HINTS),
            })
    except (requests.RequestException, ValueError, StopIteration, KeyError) as e:
        log.warning("Euskadi estaciones.csv fallo: %s", e)

    if not stations:
        # Sin índice no hay selección automática, pero las estaciones fijas
        # siguen funcionando por URL construida.
        log.warning("Euskadi: indice vacio (url.txt=%d slugs)", len(slugs))
        return _index_cache.get("index", (0, []))[1]

    _index_cache["index"] = (now, stations)
    log.info("Euskadi: %d estaciones indexadas (%d slugs en url.txt)",
             len(stations), len(slugs))
    return stations


def nearest_stations(lat, lon, max_n=None, radius_km=None,
                     include_meteo=False, fetch_url=None):
    """
    Estaciones dentro del radio, ordenadas por distancia. Devuelve lista de
    dicts con 'dist_km' añadido. Lista vacía => la zona no está cubierta por
    la red vasca (esto sustituye a ZONE_HAS_OFFICIAL).
    """
    if lat is None or lon is None:
        return []
    max_n = EUSKADI_MAX_STATIONS if max_n is None else max_n
    radius_km = EUSKADI_RADIUS_KM if radius_km is None else radius_km
    out = []
    for st in fetch_station_index(fetch_url=fetch_url):
        if st["lat"] is None or st["lon"] is None:
            continue
        if st["meteo_only"] and not include_meteo:
            continue
        d = _haversine_km(lat, lon, st["lat"], st["lon"])
        if d <= radius_km:
            s = dict(st)
            s["dist_km"] = round(d, 2)
            out.append(s)
    out.sort(key=lambda s: s["dist_km"])
    return out[:max_n]


# ───────────────────────── parseo CSV ─────────────────────────

def parse_csv(text):
    """
    Parsea el CSV horario de una estación. Devuelve { sensor_key: {epoch_s: val} }.
    La hora 01:00..24:00 es GMT y etiqueta el FINAL del intervalo; se sella al
    inicio aplicando EUSKADI_HOUR_OFFSET (-1 por defecto).
    """
    series = {}
    lines = text.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if ln.startswith("Date;"):
            start = i
            break
    reader = csv.reader(io.StringIO("\n".join(lines[start:])), delimiter=";")
    header = next(reader, None)
    if not header:
        return series
    col_map = {}
    for idx, col in enumerate(header):
        key = HEADER_TO_SENSOR.get(_col_key(col))
        if key:
            col_map[idx] = key
    for row in reader:
        if len(row) < 2 or not row[0].strip():
            continue
        date = row[0].strip()          # dd/mm/YYYY
        hour = row[1].strip()          # 'HH:00'
        if not hour:
            continue
        try:
            hh = int(hour.split(":")[0])
            d, m, y = (int(x) for x in date.split("/"))
        except ValueError:
            continue
        # timegm interpreta GMT sin depender de la TZ del servidor.
        try:
            ep = calendar.timegm((y, m, d, 0, 0, 0, 0, 0, 0)) \
                 + (hh + EUSKADI_HOUR_OFFSET) * 3600
        except (ValueError, OverflowError):
            continue
        for idx, key in col_map.items():
            if idx < len(row):
                v = _num(row[idx])
                if v is not None:
                    series.setdefault(key, {})[ep] = v
    return series


def fetch_station(station, fetch_url=None):
    """
    Descarga y parsea el CSV de una estación (con caché).
    'station' puede ser un slug, un nombre de estación, o un dict del índice.
    """
    if isinstance(station, dict):
        slug, url = station["slug"], station.get("csv_url")
    else:
        slug, url = station, None
        for st in fetch_station_index(fetch_url=fetch_url):
            if st["slug"] == station or _match_key(st["name"]) == _match_key(station):
                slug, url = st["slug"], st["csv_url"]
                break
    if not url:
        url = f"{EUSKADI_BASE}/{slug}.csv"

    now = time.time()
    hit = _cache.get(slug)
    if hit and now - hit[0] < EUSKADI_CACHE_TTL:
        return hit[1]
    try:
        text = _http_get(url, fetch_url=fetch_url)
    except requests.RequestException as e:
        log.warning("Euskadi CSV fallo (%s): %s", url, e)
        return {}
    if not text:
        log.warning("Euskadi CSV vacio: %s", url)
        return {}
    series = parse_csv(text)
    if not series:
        log.warning("Euskadi CSV sin series parseables: %s (len=%d)",
                    url, len(text))
    _cache[slug] = (now, series)
    return series


# ──────────────────── serie oficial combinada ────────────────────

def resolve_stations(lat=None, lon=None, fetch_url=None):
    """
    Decide qué estaciones usar:
      - EUSKADI_STATIONS si está definida (forzado manual).
      - si no, las más próximas a (lat, lon) dentro de EUSKADI_RADIUS_KM.
    Devuelve lista de dicts (o de slugs si vienen forzados sin índice).
    """
    if EUSKADI_STATIONS:
        idx = {st["slug"]: st for st in fetch_station_index(fetch_url=fetch_url)}
        by_name = {_match_key(st["name"]): st
                   for st in fetch_station_index(fetch_url=fetch_url)}
        out = []
        for s in EUSKADI_STATIONS:
            out.append(idx.get(s) or by_name.get(_match_key(s)) or {"name": s, "slug": s})
        return out
    return nearest_stations(lat, lon, fetch_url=fetch_url)


def fetch_official_series(hours=48, fetch_url=None, lat=None, lon=None,
                          stations=None):
    """
    Combina las series de las estaciones seleccionadas PROMEDIANDO, por métrica
    y por hora, los valores de todas las que la midan. Esto da una referencia
    regional más estable y suaviza picos de una sola cabina.

    Selección: 'stations' explícita > EUSKADI_STATIONS > cercanía a (lat, lon).
    Si no hay ninguna estación aplicable devuelve ({}, {}), que es la señal de
    "esta zona no tiene red oficial local".

    Returns: (series, source_by_metric) donde source_by_metric[metric] es la
    lista de estaciones que contribuyen a esa métrica.
    """
    if not EUSKADI_ENABLED:
        return {}, {}
    sel = stations if stations is not None else resolve_stations(
        lat=lat, lon=lon, fetch_url=fetch_url)
    if not sel:
        return {}, {}

    forced = stations is not None or bool(EUSKADI_STATIONS)
    cutoff = time.time() - hours * 3600
    acc = {}
    source = {}
    fetched = 0

    # Recorre las candidatas en orden de distancia. No para en las N más
    # próximas: sigue bajando mientras falten métricas de EUSKADI_TARGET_METRICS,
    # porque la estación que mide NH3 u O3 puede ser la sexta más cercana.
    for st in sel:
        if not forced:
            covered = set(acc)
            faltan = [m for m in EUSKADI_TARGET_METRICS if m not in covered]
            if fetched >= EUSKADI_MIN_STATIONS and not faltan:
                break                      # todo cubierto y ya hay promedio
            if fetched >= EUSKADI_MAX_FETCH:
                log.info("Euskadi: tope de %d descargas; sin cubrir %s",
                         EUSKADI_MAX_FETCH, ",".join(faltan) or "-")
                break

        label = st["name"] if isinstance(st, dict) else st
        s = fetch_station(st, fetch_url=fetch_url)
        fetched += 1
        for key, pts in s.items():
            contributed = False
            for t, v in pts.items():
                if t < cutoff:
                    continue
                acc.setdefault(key, {}).setdefault(t, []).append(v)
                contributed = True
            if contributed:
                source.setdefault(key, []).append(label)

    combined = {}
    for key, by_hour in acc.items():
        combined[key] = {t: round(sum(vs) / len(vs), 2)
                         for t, vs in by_hour.items()}
    return combined, source
