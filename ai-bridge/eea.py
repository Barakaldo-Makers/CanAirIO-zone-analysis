# SPDX-License-Identifier: GPL-3.0-or-later
# CanAirIO-zone-analysis — referencia oficial europea (EEA / AEMA)
# Copyright (C) 2026 Barakaldo Makers
"""
Referencia oficial para TODA Europa vía el Download Service de la Agencia
Europea de Medio Ambiente (eeadmz1). Tercer eslabón de la cadena:

    euskadi.py (red local) -> openaq.py (mundo) -> eea.py (Europa) -> CAMS

¿Por qué existe si OpenAQ ya sirve las redes europeas? Redundancia con fuente
PRIMARIA: la red vasca reporta a la EEA, así que este módulo es el respaldo
automático de euskadi.py si Open Data Euskadi se cae, y cubre cualquier zona
europea futura sin escribir un módulo por país.

Cómo funciona:
  1. Metadatos: baja PanEuropean_metadata.csv (coordenadas y contaminante de
     cada sampling point europeo; decenas de MB), lo filtra a los contaminantes
     objetivo y cachea el resultado compactado en disco EEA_METADATA_TTL s.
     La API de descargas NO da coordenadas (SpoSummary = pk + localid), por
     eso hace falta este fichero aparte.
  2. Selección: sampling points a menos de EEA_RADIUS_KM del punto pedido,
     agrupados por estación (código EoI), orden por distancia.
  3. Datos: POST /ParquetFile con dataset=1 (UTD/E2a: horario sin verificar,
     transmisión continua), país y contaminantes; devuelve un ZIP de parquet
     con columnas Samplingpoint, Pollutant, Start, End, Value, Unit, Validity.
     Se cachea por (países, ventana) EEA_DATA_TTL s: una descarga sirve a
     todas las zonas y sensores del mismo país en el ciclo.

Decisiones defensivas, en el estilo del resto del proyecto:
  - pyarrow se importa EN LA FUNCIÓN: si falta, este módulo se desactiva solo
    con un error claro en el log y la cadena sigue hacia CAMS. El ciclo nunca
    muere por una dependencia opcional.
  - El vocabulario de contaminante se acepta en sus tres formas (notación
    'PM2.5', código '6001', URI '/pollutant/6001'), porque la documentación
    no fija cuál llega en el parquet.
  - Unidades: µg/m³ pasa; mg/m³ se multiplica por 1000 (CO); cualquier otra
    unidad DESCARTA la fila y se cuenta, nunca se adivina.
  - Validity < 1 se descarta (inválido); UTD llega sin verificar
    (Verification=3) y eso es aceptable: es el mismo nivel de madurez que
    publica Open Data Euskadi en horario.
  - Las horas se etiquetan con el INICIO del periodo y tz UTC. Si el cotejo
    con euskadi (/eea-debug) detectara un desfase sistemático de ±1 h, se
    corrige con EEA_HOUR_OFFSET sin tocar código.
"""
import csv
import io
import logging
import math
import os
import re
import time
import zipfile

import requests

log = logging.getLogger("eea")

EEA_ENABLED = os.getenv("EEA_ENABLED", "1") == "1"
EEA_API = os.getenv(
    "EEA_API", "https://eeadmz1-downloads-api-appservice.azurewebsites.net")
# Metadato de sampling points. Es un ZIP con un CSV dentro, servido por el
# AQViewer de la EEA. La URL está verificada contra la implementación del
# paquete R 'EEAaq' (CRAN), que es la que usa la comunidad científica; el
# servicio devuelve 403 a peticiones sin User-Agent, así que se manda uno.
EEA_METADATA_URL = os.getenv(
    "EEA_METADATA_URL",
    "https://discomap.eea.europa.eu/App/AQViewer/download"
    "?fqn=Airquality_Dissem.b2g.measurements&f=csv")
EEA_USER_AGENT = os.getenv(
    "EEA_USER_AGENT",
    "CanAirIO-zone-analysis/1.5 (+https://github.com/Barakaldo-Makers)")
EEA_CACHE_DIR = os.getenv("EEA_CACHE_DIR", "/tmp/eea-cache")
EEA_METADATA_TTL = int(os.getenv("EEA_METADATA_TTL", str(7 * 86400)))
EEA_DATA_TTL = int(os.getenv("EEA_DATA_TTL", "1500"))
EEA_RADIUS_KM = float(os.getenv("EEA_RADIUS_KM", "25"))
EEA_MAX_STATIONS = int(os.getenv("EEA_MAX_STATIONS", "12"))
EEA_HTTP_TIMEOUT = int(os.getenv("EEA_HTTP_TIMEOUT", "90"))
EEA_META_TIMEOUT = int(os.getenv("EEA_META_TIMEOUT", "300"))
EEA_HOUR_OFFSET = int(os.getenv("EEA_HOUR_OFFSET", "0"))
EEA_DATASET = int(os.getenv("EEA_DATASET", "1"))
EEA_TARGET_METRICS = [m.strip() for m in os.getenv(
    "EEA_TARGET_METRICS", "pm25,pm10,no2,o3,so2,co,nh3").split(",")
    if m.strip()]
# Escotilla de emergencia, al estilo de EUSKADI_STATIONS: sampling points
# fijados a mano, con su país. Formato: "ES:SP_1234,ES:SP_5678". Sirve si el
# metadato del AQViewer cambia de sitio otra vez; las coordenadas dejan de
# hacer falta porque la selección ya está hecha.
EEA_FORCE_SPOS = [p.strip() for p in os.getenv("EEA_FORCE_SPOS", "").split(",")
                  if p.strip()]

# métrica del proyecto <-> vocabulario Eionet (notación, código).
# Códigos del vocabulario http://dd.eionet.europa.eu/vocabulary/aq/pollutant,
# confirmados contra GET /Pollutant de la propia API.
MET_A_EEA = {
    "pm25": ("PM2.5", "6001"),
    "pm10": ("PM10", "5"),
    "no2":  ("NO2",  "8"),
    "o3":   ("O3",   "7"),
    "so2":  ("SO2",  "1"),
    "co":   ("CO",   "10"),
    "nh3":  ("NH3",  "35"),
}
# el parquet puede traer notación, código o URI: se aceptan las tres
EEA_A_MET = {}
for _m, (_nota, _cod) in MET_A_EEA.items():
    EEA_A_MET[_nota.upper()] = _m
    EEA_A_MET[_cod] = _m

# Caja que contiene la red que reporta a la EEA (UE + AELC + candidatos, con
# Canarias, Azores e Islandia). Fuera de ella no se baja ni un byte.
_BBOX = {"lat_min": 25.0, "lat_max": 72.5, "lon_min": -32.0, "lon_max": 45.0}

_meta_cache = {"t": 0.0, "spos": None}
_data_cache = {}


def _dentro_de_europa(lat, lon):
    return (lat is not None and lon is not None
            and _BBOX["lat_min"] <= lat <= _BBOX["lat_max"]
            and _BBOX["lon_min"] <= lon <= _BBOX["lon_max"])


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _norm_col(nombre):
    """'Sampling Point Id' / 'SamplingPointId' / 'samplingpoint_id' -> clave única."""
    return "".join(c for c in (nombre or "").lower() if c.isalnum())


def _met_de_eea(valor):
    """Notación, código o URI de contaminante -> métrica del proyecto, o None."""
    if valor is None:
        return None
    s = str(valor).strip()
    if not s:
        return None
    if "/" in s:                      # URI: vale el último tramo
        s = s.rstrip("/").rsplit("/", 1)[-1]
    s = s.strip().upper()
    if s.endswith(".0"):              # códigos que llegan como float
        s = s[:-2]
    return EEA_A_MET.get(s)


def _iso_de_eoi(eoi, spo=""):
    """
    Codigo ISO del pais a partir del codigo EoI de la estacion.

    El metadato del AQViewer trae una columna 'Country' con el NOMBRE del
    pais ('Andorra', 'Spain'), no su codigo, y la API de descargas exige
    codigos. Recortar el nombre a dos letras da 'SP' para Spain y 'AN' para
    Andorra: ambos invalidos, y el fallo seria SILENCIOSO (descarga vacia y
    caida a CAMS sin un solo error). El codigo EoI si lleva el ISO delante
    por convenio: 'AD0942A' -> AD, 'ES1438A' -> ES. El Sampling Point Id
    tambien suele llevarlo ('SPO-AD0942A-0001'), y sirve de respaldo.
    """
    for cand in (eoi or "", spo or ""):
        # dos letras seguidas de un digito: 'ES0001A' -> ES, 'SPO-AD0942A'
        # -> AD. Exigir el digito es lo que descarta el prefijo literal
        # 'SPO-' (un barrido letra a letra devolvia 'PO', lo cazo su test).
        m = re.search(r"([A-Z]{2})\d", (cand or "").upper())
        if m:
            return m.group(1)
    return ""


def _spo_norm(spo):
    """El Samplingpoint del parquet lleva prefijo de país ('ES/SP_...'); el
    metadato puede traerlo sin él. Se compara siempre SIN prefijo."""
    s = str(spo or "").strip()
    if "/" in s:
        s = s.split("/", 1)[1]
    return s


def _cache_file(nombre):
    os.makedirs(EEA_CACHE_DIR, exist_ok=True)
    return os.path.join(EEA_CACHE_DIR, nombre)


def fetch_station_index(force=False):
    """
    Índice de sampling points europeos con coordenadas, filtrado a los
    contaminantes objetivo. Lista de dicts:
      {spo, pais, eoi, lat, lon, metrica, tipo, area}

    El CSV paneuropeo pesa decenas de MB, así que: (a) se descarga en
    streaming a disco, (b) se parsea UNA vez y el resultado compactado se
    guarda en disco, (c) en memoria solo vive el filtrado. Cabeceras y
    delimitador se autodetectan: el fichero ha cambiado de forma entre
    versiones y adivinar es más frágil que mirar.
    """
    now = time.time()
    if not force and _meta_cache["spos"] is not None \
            and now - _meta_cache["t"] < EEA_METADATA_TTL:
        return _meta_cache["spos"]

    compacto = _cache_file("spos_compacto.csv")
    if not force and os.path.exists(compacto) \
            and now - os.path.getmtime(compacto) < EEA_METADATA_TTL:
        spos = _lee_compacto(compacto)
        if spos:
            _meta_cache.update(t=now, spos=spos)
            return spos

    bruto = _cache_file("metadata_bruto.bin")
    log.info("EEA: descargando metadatos de estaciones (%s)", EEA_METADATA_URL)
    with requests.get(EEA_METADATA_URL, stream=True,
                      headers={"User-Agent": EEA_USER_AGENT,
                               "Accept": "*/*"},
                      timeout=EEA_META_TIMEOUT) as r:
        r.raise_for_status()
        with open(bruto, "wb") as f:
            for trozo in r.iter_content(chunk_size=1 << 20):
                f.write(trozo)

    # El AQViewer sirve un ZIP con el CSV dentro, pero una URL alternativa
    # podria servir el CSV pelado: se decide por los bytes, no por la
    # extension ni por la fe.
    csv_path = bruto
    with open(bruto, "rb") as f:
        cabeza = f.read(512)
    magia = cabeza[:4]
    if magia[:2] != b"PK" and b"<html" in cabeza[:200].lower():
        # sin User-Agent este servicio devuelve 200 con una pagina HTML, no
        # un 403: hay que detectarlo o se intentaria parsear HTML como CSV
        raise RuntimeError(
            "metadatos EEA: la respuesta es HTML, no datos. Suele ser el "
            "filtro de User-Agent del AQViewer; revisa EEA_USER_AGENT o "
            "ejecuta scripts/sonda-eea.sh para ver que devuelve la URL.")
    if magia[:2] == b"PK":
        with zipfile.ZipFile(bruto) as z:
            dentro = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not dentro:
                raise RuntimeError(
                    "metadatos EEA: el ZIP no trae ningun .csv (%s)"
                    % z.namelist()[:5])
            csv_path = _cache_file("metadata_bruto.csv")
            with z.open(dentro[0]) as src, open(csv_path, "wb") as dst:
                while True:
                    trozo = src.read(1 << 20)
                    if not trozo:
                        break
                    dst.write(trozo)

    spos = _parsea_metadatos(csv_path)
    if not spos:
        raise RuntimeError("metadatos EEA: 0 sampling points tras filtrar; "
                           "¿ha cambiado el formato del CSV?")
    with open(compacto, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["spo", "pais", "eoi", "lat", "lon", "metrica",
                    "tipo", "area"])
        for s in spos:
            w.writerow([s["spo"], s["pais"], s["eoi"], s["lat"], s["lon"],
                        s["metrica"], s["tipo"], s["area"]])
    for tmp in {bruto, csv_path}:     # el crudo ya no hace falta
        try:
            os.remove(tmp)
        except OSError:
            pass
    _meta_cache.update(t=now, spos=spos)
    log.info("EEA: índice con %d sampling points de %d métricas",
             len(spos), len({s['metrica'] for s in spos}))
    return spos


def _lee_compacto(ruta):
    out = []
    try:
        with open(ruta, newline="", encoding="utf-8") as f:
            for fila in csv.DictReader(f):
                try:
                    fila["lat"] = float(fila["lat"])
                    fila["lon"] = float(fila["lon"])
                except (TypeError, ValueError):
                    continue
                out.append(fila)
    except Exception as e:
        log.warning("EEA: caché compacta ilegible (%s); se regenera", e)
        return None
    return out


def _parsea_metadatos(ruta):
    """Parsea el CSV paneuropeo con tolerancia a cabeceras y delimitador."""
    with open(ruta, newline="", encoding="utf-8-sig", errors="replace") as f:
        primera = f.readline()
        delim = "\t" if primera.count("\t") >= primera.count(",") else ","
        f.seek(0)
        rd = csv.reader(f, delimiter=delim)
        try:
            cab = next(rd)
        except StopIteration:
            return []
        idx = {_norm_col(c): i for i, c in enumerate(cab)}

        def col(*nombres):
            for n in nombres:
                i = idx.get(_norm_col(n))
                if i is not None:
                    return i
            return None

        i_spo = col("SamplingPointId", "Sampling Point Id", "SamplingPoint")
        # el AQViewer llama al pais 'ISO'; otras versiones 'Countrycode'
        i_pais = col("ISO", "Countrycode", "Country Code", "Country")
        i_eoi = col("AirQualityStationEoICode", "EoICode",
                    "Air Quality Station EoI Code")
        i_lat = col("Latitude")
        i_lon = col("Longitude")
        i_pol = col("AirPollutant", "Air Pollutant", "AirPollutantCode")
        i_tipo = col("AirQualityStationType", "StationType")
        i_area = col("AirQualityStationArea", "StationArea")
        # La tabla trae una fila por punto Y AÑO. Sin filtrar, el índice
        # incluye puntos dados de baja hace años, que pueden desplazar a
        # estaciones vivas al recortar a EEA_MAX_STATIONS. Se conserva la
        # fila del año más alto de cada (punto, métrica).
        i_anio = col("Year", "Reporting Year")
        # El contaminante es OPCIONAL a proposito: el parquet de datos trae
        # su propia columna Pollutant y esa es la autoritativa. Al metadato
        # solo se le pide responder "que sampling points hay cerca y de que
        # pais", que es lo unico que no esta en el parquet. Asi un cambio de
        # nombre en la columna de contaminante no tumba el modulo.
        if None in (i_spo, i_lat, i_lon):
            raise RuntimeError(
                "metadatos EEA: faltan columnas imprescindibles "
                "(SamplingPointId/Latitude/Longitude); cabecera = %s"
                % cab[:12])
        if i_pol is None:
            log.info("EEA: el metadato no trae columna de contaminante; "
                     "se tomara del parquet de datos")

        mejor = {}            # (spo, met) -> (anio, dict)
        for fila in rd:
            try:
                if i_pol is not None:
                    met = _met_de_eea(fila[i_pol])
                    # con columna de contaminante se filtra ya aqui, que
                    # recorta el indice de ~150k filas a las utiles
                    if met is None or met not in EEA_TARGET_METRICS:
                        continue
                else:
                    met = ""          # lo dira el parquet
                spo = _spo_norm(fila[i_spo])
                if not spo:
                    continue
                lat = float(fila[i_lat])
                lon = float(fila[i_lon])
            except (IndexError, TypeError, ValueError):
                continue

            def celda(i):
                return (fila[i].strip()
                        if i is not None and i < len(fila) else "")

            eoi = celda(i_eoi) or spo
            # el nombre de pais solo vale si YA es un codigo de dos letras;
            # si no, se saca del EoI (ver _iso_de_eoi)
            crudo = celda(i_pais)
            pais = (crudo.upper() if len(crudo) == 2 and crudo.isalpha()
                    else _iso_de_eoi(eoi, spo))
            try:
                anio = int(celda(i_anio) or 0)
            except ValueError:
                anio = 0
            clave = (spo, met)
            previo = mejor.get(clave)
            if previo is not None and previo[0] >= anio:
                continue
            mejor[clave] = (anio, {
                "spo": spo, "pais": pais, "eoi": eoi,
                "lat": lat, "lon": lon, "metrica": met,
                "tipo": celda(i_tipo), "area": celda(i_area),
            })

        if i_anio is not None and mejor:
            # solo el ultimo anio y el anterior: lo demas esta de baja
            tope = max(a for a, _ in mejor.values())
            vivos = [d for a, d in mejor.values() if a >= tope - 1]
            log.info("EEA: metadato con anios hasta %d; %d de %d puntos vivos",
                     tope, len(vivos), len(mejor))
            return vivos
        return [d for _a, d in mejor.values()]


def nearest_stations(lat, lon, radius_km=None, max_n=None):
    """
    Estaciones (agrupadas por código EoI) a menos de radius_km, con sus
    sampling points por métrica, ordenadas por distancia:
      [{eoi, name, pais, dist_km, tipo, area, spos: {metrica: [spo, ...]}}]
    """
    if not _dentro_de_europa(lat, lon):
        return []
    radius_km = radius_km if radius_km is not None else EEA_RADIUS_KM
    max_n = max_n if max_n is not None else EEA_MAX_STATIONS
    por_eoi = {}
    for s in fetch_station_index():
        d = _haversine_km(lat, lon, s["lat"], s["lon"])
        if d > radius_km:
            continue
        st = por_eoi.setdefault(s["eoi"], {
            "eoi": s["eoi"], "name": s["eoi"], "pais": s["pais"],
            "dist_km": round(d, 2), "tipo": s["tipo"], "area": s["area"],
            "spos": {}, "spos_todos": []})
        st["dist_km"] = min(st["dist_km"], round(d, 2))
        if s["spo"] not in st["spos_todos"]:
            st["spos_todos"].append(s["spo"])
        # 'spos' solo se rellena si el metadato traia contaminante; es
        # informativo (lo enseña /eea-debug). La extraccion de series va
        # siempre por 'spos_todos' + la columna Pollutant del parquet.
        if s["metrica"]:
            st["spos"].setdefault(s["metrica"], []).append(s["spo"])
    cerca = sorted(por_eoi.values(), key=lambda x: x["dist_km"])
    return cerca[:max_n]


def _estaciones_forzadas():
    """EEA_FORCE_SPOS ('ES:SP_1234,...') -> una pseudo-estación por punto.
    Sin coordenadas y sin metadato: la selección ya la hizo el humano."""
    out = []
    for item in EEA_FORCE_SPOS:
        pais, _, spo = item.partition(":")
        pais, spo = pais.strip().upper(), _spo_norm(spo or pais)
        if not spo:
            continue
        out.append({"eoi": spo, "name": spo,
                    "pais": pais if len(pais) == 2 else "",
                    "dist_km": None, "tipo": "", "area": "",
                    "spos": {}, "spos_todos": [spo]})
    return out


def _descarga_parquet(paises, horas):
    """
    Baja el UTD horario de los contaminantes objetivo para esos países y la
    ventana pedida, y lo indexa como {spo_sin_pais: {metrica: {epoch: valor}}}.
    Cacheado por (países, cubo horario): una descarga por ciclo y país.
    """
    try:
        import pyarrow.parquet as pq       # dependencia opcional del módulo
    except ImportError:
        log.error("EEA: falta pyarrow (añádelo a requirements.txt y "
                  "reconstruye la imagen); referencia EEA desactivada")
        return {}

    clave = (",".join(sorted(paises)), int(time.time() // EEA_DATA_TTL))
    hit = _data_cache.get(clave)
    if hit is not None:
        return hit
    _data_cache.clear()                    # una clave viva basta

    ahora = time.time()
    t0 = ahora - (horas + 3) * 3600        # margen para la latencia de E2a
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    cuerpo = {
        "countries": sorted(paises),
        "cities": [],
        "pollutants": [MET_A_EEA[m][0] for m in EEA_TARGET_METRICS
                       if m in MET_A_EEA],
        "dataset": EEA_DATASET,
        "source": "API",
        "dateTimeStart": time.strftime(fmt, time.gmtime(t0)),
        "dateTimeEnd": time.strftime(fmt, time.gmtime(ahora)),
        # el PDF dice entero, el esquema Swagger dice string: gana el esquema
        "aggregationType": "hour",
    }
    r = requests.post(f"{EEA_API}/ParquetFile", json=cuerpo,
                      timeout=EEA_HTTP_TIMEOUT)
    r.raise_for_status()

    out = {}
    descartes = {"unidad": 0, "validez": 0, "contaminante": 0}
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for nombre in z.namelist():
            if not nombre.lower().endswith(".parquet"):
                continue
            try:
                tabla = pq.read_table(io.BytesIO(z.read(nombre)))
            except Exception as e:
                log.warning("EEA: parquet ilegible %s: %s", nombre, e)
                continue
            cols = {c.lower(): c for c in tabla.column_names}

            def serie(nombre_col):
                c = cols.get(nombre_col)
                return tabla.column(c).to_pylist() if c else None

            spos = serie("samplingpoint")
            pols = serie("pollutant")
            starts = serie("start")
            vals = serie("value")
            unis = serie("unit")
            valz = serie("validity")
            if not spos or not starts or not vals:
                continue
            n = len(spos)
            for i in range(n):
                met = _met_de_eea(pols[i]) if pols else None
                if met is None:
                    descartes["contaminante"] += 1
                    continue
                if valz is not None and valz[i] is not None:
                    try:
                        if float(valz[i]) < 1:
                            descartes["validez"] += 1
                            continue
                    except (TypeError, ValueError):
                        pass
                v = vals[i]
                if v is None:
                    continue
                u = (str(unis[i]).strip().lower().replace("µ", "u")
                     if unis else "ug/m3")
                if u.startswith("ug"):
                    factor = 1.0
                elif u.startswith("mg"):
                    factor = 1000.0        # CO suele venir en mg/m3
                else:
                    descartes["unidad"] += 1
                    continue
                t = starts[i]
                try:
                    # pyarrow entrega datetime (con o sin tz); naive = UTC
                    epoch = t.timestamp() if t.tzinfo else (
                        t.replace(tzinfo=__import__("datetime").timezone.utc)
                        .timestamp())
                except AttributeError:
                    continue
                epoch += EEA_HOUR_OFFSET * 3600
                spo = _spo_norm(spos[i])
                out.setdefault(spo, {}).setdefault(met, {})[epoch] = \
                    float(v) * factor
    if any(descartes.values()):
        log.info("EEA: filas descartadas %s", descartes)
    _data_cache[clave] = out
    return out


def fetch_official_series(hours=48, lat=None, lon=None, stations=None):
    """
    Mismo contrato que euskadi.fetch_official_series: combina las estaciones
    seleccionadas PROMEDIANDO por métrica y hora, y devuelve
    (series, source_by_metric). ({}, {}) = esta zona no es cosa de la EEA.
    Dentro de una estación se usa UN sampling point por métrica (el primero
    con datos): dos instrumentos del mismo punto no son dos estaciones.
    """
    if not EEA_ENABLED:
        return {}, {}
    if stations is None:
        if EEA_FORCE_SPOS:
            stations = _estaciones_forzadas()
        else:
            if not _dentro_de_europa(lat, lon):
                return {}, {}
            stations = nearest_stations(lat, lon)
    if not stations:
        return {}, {}

    paises = sorted({st["pais"] for st in stations if st.get("pais")})
    if not paises:
        return {}, {}
    datos = _descarga_parquet(paises, hours)
    if not datos:
        return {}, {}

    cutoff = time.time() - hours * 3600
    acc, fuente = {}, {}
    for st in stations:
        # QUÉ mide cada sampling point lo dice el parquet, no el metadato:
        # el parquet trae su propia columna Pollutant y es la autoritativa.
        # El metadato solo ha servido para decir qué puntos hay cerca.
        spos = st.get("spos_todos") or sorted(
            {s for lst in (st.get("spos") or {}).values() for s in lst})
        ya_puestas = set()             # métricas que esta estación ya aportó
        for spo in spos:
            for met, serie in sorted((datos.get(spo) or {}).items()):
                if met in ya_puestas:
                    continue           # un sampling point por estación y métrica
                aporto = False
                for t, v in serie.items():
                    if t < cutoff:
                        continue
                    acc.setdefault(met, {}).setdefault(t, []).append(v)
                    aporto = True
                if aporto:
                    ya_puestas.add(met)
                    fuente.setdefault(met, []).append(st["name"])

    combined = {met: {t: round(sum(vs) / len(vs), 2)
                      for t, vs in por_h.items()}
                for met, por_h in acc.items()}
    return combined, fuente


def diagnose(lat, lon, hours=48):
    """Diagnóstico para /eea-debug: qué hay en el radio y qué se obtiene."""
    out = {"enabled": EEA_ENABLED, "radius_km": EEA_RADIUS_KM,
           "hour_offset": EEA_HOUR_OFFSET, "dataset": EEA_DATASET,
           "metadata_url": EEA_METADATA_URL,
           "spos_forzados": EEA_FORCE_SPOS or None,
           "dentro_de_cobertura": _dentro_de_europa(lat, lon)}
    if EEA_FORCE_SPOS:
        series, fuente = fetch_official_series(hours)
        out["stations"] = [{"eoi": st["eoi"], "pais": st["pais"]}
                           for st in _estaciones_forzadas()]
        out["series_horas"] = {m: len(p) for m, p in sorted(series.items())}
        out["fuente_por_metrica"] = fuente
        out["nota"] = "seleccion manual por EEA_FORCE_SPOS (sin metadato)"
        return out
    if not out["dentro_de_cobertura"]:
        out["nota"] = ("fuera de la caja de cobertura EEA: este punto no es "
                       "cosa de este módulo (lo cubre OpenAQ o CAMS)")
        return out
    cerca = nearest_stations(lat, lon)
    out["stations"] = [{k: st[k] for k in
                        ("eoi", "pais", "dist_km", "tipo", "area")}
                       | {"metricas": sorted(st["spos"]) or "(las dira el parquet)",
                          "n_spos": len(st.get("spos_todos") or [])}
                       for st in cerca]
    if not cerca:
        out["nota"] = "sin sampling points EEA en el radio"
        return out
    series, fuente = fetch_official_series(hours, lat=lat, lon=lon)
    out["series_horas"] = {m: len(pts) for m, pts in sorted(series.items())}
    out["fuente_por_metrica"] = fuente
    faltan = [m for m in EEA_TARGET_METRICS if m not in series]
    if faltan:
        out["sin_cubrir"] = faltan
    return out