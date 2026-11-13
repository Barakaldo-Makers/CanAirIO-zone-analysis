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
import datetime
import io
import json
import subprocess
import sys
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
# Topes de seguridad. El diseño anterior pedía un ZIP con TODOS los ficheros
# del país (2310 ficheros / 220 MB para España) y lo cargaba en memoria con
# r.content: tumbó el contenedor en producción. Ahora se pide solo la LISTA
# de URLs (unos KB) y se bajan los ficheros de las estaciones elegidas, uno
# a uno y a disco. Estos topes son el cinturón: si se superan, el módulo
# avisa y se aparta para que la cadena siga a CAMS.
EEA_MAX_FILES = int(os.getenv("EEA_MAX_FILES", "40"))
EEA_MAX_MB = float(os.getenv("EEA_MAX_MB", "60"))
EEA_BLOB_TIMEOUT = int(os.getenv("EEA_BLOB_TIMEOUT", "60"))
EEA_SUBPROC_TIMEOUT = int(os.getenv("EEA_SUBPROC_TIMEOUT", "120"))
EEA_META_TIMEOUT = int(os.getenv("EEA_META_TIMEOUT", "300"))
EEA_HOUR_OFFSET = int(os.getenv("EEA_HOUR_OFFSET", "0"))
# Zona horaria de Start/End. La EEA no la documenta (formato
# "yyyy-mm-dd H:M:S", sin desfase) pero el metadato declara una por
# estacion ('Timezone', p.ej. 'UTC+01'). Con 'metadato' (defecto), si el
# parquet trae la hora SIN zona se interpreta en la zona de su estacion;
# con 'utc' se lee como UTC. El cotejo de /eea-debug dice cual es la buena:
# compara la MISMA cabina por la EEA y por la red local.
EEA_TZ_MODE = os.getenv("EEA_TZ_MODE", "metadato").strip().lower()
# Unidades del sistema, igual que openaq.py y euskadi.py: ug/m3 para todo
# salvo CO, que va en mg/m3 (como el sensor ya convertido).
_MG_METRICS = {"co"}
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


def _tz_horas(txt):
    """'UTC+01' / 'UTC+1' / 'UTC-01:00' / 'UTC' / URI .../UTC+01 -> horas
    (float), o None si no se reconoce. Las zonas de la EEA son desfases
    FIJOS (sin horario de verano): el vocabulario Eionet aq/timezone."""
    t = (txt or "").strip().rsplit("/", 1)[-1].upper().replace(" ", "")
    if not t:
        return None
    if t in ("UTC", "GMT", "Z"):
        return 0.0
    m = re.match(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", t)
    if not m:
        return None
    h = int(m.group(2)) + int(m.group(3) or 0) / 60.0
    return h if m.group(1) == "+" else -h


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

    compacto = _cache_file("spos_compacto_v3.csv")
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
        w.writerow(["spo", "pais", "eoi", "natcode", "lat", "lon",
                    "metrica", "tipo", "area", "fin_op", "tz"])
        for s in spos:
            w.writerow([s["spo"], s["pais"], s["eoi"], s.get("natcode", ""),
                        s["lat"], s["lon"], s["metrica"], s["tipo"],
                        s["area"], s.get("fin_op", ""), s.get("tz", "")])
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
        # Codigo nacional de estacion: el nombre del fichero parquet lo lleva
        # dentro ('SP_01055001_9_49.parquet'), asi que sirve de segundo
        # criterio para emparejar ficheros con estaciones.
        i_nat = col("AirQualityStationNatCode", "Air Quality Station Nat Code")
        # Fecha de cierre del punto. Es mejor criterio de baja que el año del
        # fichero: lo dice la propia EEA en vez de inferirlo.
        i_fin = col("OperationalActivityEnd", "Operational Activity End")
        # zona horaria declarada de la estacion ('UTC+01'): ver EEA_TZ_MODE
        i_tz = col("Timezone", "TimeZone", "Time Zone")
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
                "natcode": celda(i_nat),
                "lat": lat, "lon": lon, "metrica": met,
                "tipo": celda(i_tipo), "area": celda(i_area),
                "fin_op": celda(i_fin),
                "tz": celda(i_tz),
            })

        todos = [(a, d) for a, d in mejor.values()]
        if i_fin is not None:
            # criterio explicito: sin fecha de cierre = sigue operando
            vivos = [d for _a, d in todos if not d.get("fin_op")]
            if vivos:
                log.info("EEA: %d de %d puntos sin fecha de cierre "
                         "(Operational Activity End)", len(vivos), len(todos))
                return vivos
            log.warning("EEA: todos los puntos traen fecha de cierre; se "
                        "usara el filtro por anio como respaldo")
        if i_anio is not None and todos:
            # respaldo: solo el ultimo anio y el anterior
            tope = max(a for a, _ in todos)
            vivos = [d for a, d in todos if a >= tope - 1]
            log.info("EEA: metadato con anios hasta %d; %d de %d puntos vivos",
                     tope, len(vivos), len(todos))
            return vivos
        return [d for _a, d in todos]


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
            "lat": s["lat"], "lon": s["lon"], "tz_h": None,
            "spos": {}, "spos_todos": [], "natcodes": []})
        st["dist_km"] = min(st["dist_km"], round(d, 2))
        if st["tz_h"] is None:
            st["tz_h"] = _tz_horas(s.get("tz"))
        if s["spo"] not in st["spos_todos"]:
            st["spos_todos"].append(s["spo"])
        nc = (s.get("natcode") or "").strip()
        if nc and nc not in st["natcodes"]:
            st["natcodes"].append(nc)
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
                    "spos": {}, "spos_todos": [spo], "natcodes": []})
    return out


def _cpu_tiene_sse42():
    """Los binarios oficiales de pyarrow exigen SSE4.2 (CPUs de 2008 en
    adelante) sin comprobarlo en tiempo de ejecucion. Se mira la CPU para
    no gastar un intento en un motor condenado; la SEGURIDAD ya no depende
    de esta guarda, porque la lectura corre en un subproceso."""
    try:
        with open("/proc/cpuinfo", encoding="ascii", errors="replace") as f:
            return "sse4_2" in f.read()
    except OSError:
        return True


# Motores que han muerto con una señal en esta maquina: no se reintentan.
_motores_vetados = set()

# Resumen del ultimo intento de descarga, para /eea-debug: cuantas URLs habia
# en el pais, cuantas casaron con las estaciones, cuantas se bajaron y con
# que lector se leyeron. Asi una serie vacia dice EN QUE TRAMO se quedo.
_ultima_descarga = {}

# El lector corre en un SUBPROCESO. Motivo, medido en produccion: en un
# Core 2 Quad sin SSE4.2, pyarrow mato el servicio con SIGILL (exit 132)
# al primer uso... y fastparquet TAMBIEN: desde la 2026.5.0 su setup.py
# fuerza -march=x86-64-v2 (SSE4.1/4.2/POPCNT) siempre que el COMPILADOR lo
# acepte, sin mirar la CPU, asi que ni compilarlo del fuente lo salva.
# requirements.txt fija una version anterior (verificada bajo QEMU con CPU
# Conroe), pero la proteccion no puede depender de que nadie suba un pin:
# aislada en un hijo, una instruccion ilegal mata al hijo (codigo -4), el
# padre lo ve, veta ese motor y lo cuenta en el log. El hijo ademas filtra
# por ventana temporal y devuelve en JSON solo esas filas, con start ya en
# epoch.
_LECTOR_HIJO = 'import datetime\nimport json\nimport sys\n\nmotor, cutoff, rutas = sys.argv[1], float(sys.argv[2]), sys.argv[3:]\nQUIERO = ("samplingpoint", "pollutant", "start", "value", "unit", "validity")\n\n\ndef epoch(t):\n    if t is None:\n        return None\n    try:\n        if t != t:                    # NaT\n            return None\n    except Exception:\n        pass\n    try:\n        return (t.timestamp() if t.tzinfo else\n                t.replace(tzinfo=datetime.timezone.utc).timestamp())\n    except Exception:\n        return None\n\n\nout = {}\nfor ruta in rutas:\n    try:\n        if motor == "pyarrow":\n            import pyarrow.parquet as pq\n            pf = pq.ParquetFile(ruta)\n            nombres = [n for n in pf.schema_arrow.names\n                       if n.lower() in QUIERO]\n            tabla = pf.read(columns=nombres)\n            cols = {n.lower(): tabla.column(n).to_pylist() for n in nombres}\n        elif motor == "fastparquet":\n            import fastparquet\n            pf = fastparquet.ParquetFile(ruta)\n            nombres = [n for n in pf.columns if n.lower() in QUIERO]\n            df = pf.to_pandas(nombres)\n            cols = {n.lower(): df[n].tolist() for n in nombres}\n        else:\n            raise RuntimeError("motor desconocido: %s" % motor)\n    except Exception as e:\n        print("fichero %s: %s" % (ruta, e), file=sys.stderr)\n        continue\n    st = cols.get("start") or []\n    # La EEA no documenta en que zona va Start ("yyyy-mm-dd H:M:S", sin\n    # desfase). Se informa si venia SIN zona: entonces el padre aplica la\n    # zona horaria de la estacion que declara el metadato.\n    naive = True\n    for t in st:\n        if t is not None and t == t:\n            naive = getattr(t, "tzinfo", None) is None\n            break\n    idx, eps = [], []\n    for i, t in enumerate(st):\n        e = epoch(t)\n        if e is not None and e >= cutoff:\n            idx.append(i)\n            eps.append(e)\n    sel = {"start": eps, "_naive": naive}\n    for k, v in cols.items():\n        if k == "start":\n            continue\n        fila = []\n        for i in idx:\n            x = v[i]\n            if x is None or isinstance(x, (int, float, str)):\n                fila.append(x)\n            else:\n                fila.append(str(x))\n        sel[k] = fila\n    out[ruta] = sel\nprint(json.dumps(out))\n'


def _lee_parquets_aislado(rutas, cutoff):
    """
    Lee varios parquet en un subproceso: ({ruta: {columna: lista}}, motor),
    con start en epoch y filtrado a la ventana, o (None, None) si ningun
    motor sobrevive en esta maquina.
    """
    candidatos = (["pyarrow"] if _cpu_tiene_sse42() else []) + ["fastparquet"]
    for motor in candidatos:
        if motor in _motores_vetados:
            continue
        try:
            r = subprocess.run(
                [sys.executable, "-c", _LECTOR_HIJO, motor,
                 str(float(cutoff))] + list(rutas),
                capture_output=True, timeout=EEA_SUBPROC_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("EEA: lector %s agoto %ds; se prueba el siguiente",
                        motor, EEA_SUBPROC_TIMEOUT)
            continue
        if r.returncode == 0:
            if r.stderr:
                log.warning("EEA: lector %s avisa: %s", motor,
                            r.stderr.decode("utf-8", "replace")[-300:])
            try:
                return json.loads(r.stdout.decode("utf-8")), motor
            except ValueError as e:
                log.warning("EEA: salida del lector %s ilegible: %s",
                            motor, e)
                continue
        if r.returncode < 0:
            _motores_vetados.add(motor)
            log.warning(
                "EEA: el lector %s murio con la señal %d en esta CPU "
                "(4 = SIGILL: instrucciones que este procesador no tiene); "
                "queda vetado y el servicio sigue intacto",
                motor, -r.returncode)
            continue
        log.warning("EEA: lector %s fallo (rc=%d): %s", motor,
                    r.returncode,
                    r.stderr.decode("utf-8", "replace")[-300:])
    log.error("EEA: ningun lector de parquet sobrevive en esta maquina; "
              "referencia EEA sin datos (la cadena sigue a CAMS)")
    return None, None


def _urls_del_pais(paises, horas):
    """
    Lista de URLs de parquet del servicio para esos paises y contaminantes.
    Es texto plano (una URL por linea, unos KB): /ParquetFile/urls existe
    precisamente para no tener que pedir el ZIP entero. Cacheada por
    (paises, cubo horario).
    """
    clave = ("urls", ",".join(sorted(paises)), int(horas),
             int(time.time() // EEA_DATA_TTL))
    hit = _data_cache.get(clave)
    if hit is not None:
        return hit

    ahora = time.time()
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    cuerpo = {
        "countries": sorted(paises),
        "cities": [],
        "pollutants": [MET_A_EEA[m][0] for m in EEA_TARGET_METRICS
                       if m in MET_A_EEA],
        "dataset": EEA_DATASET,
        "source": "API",
        "dateTimeStart": time.strftime(fmt, time.gmtime(
            ahora - (horas + 3) * 3600)),
        "dateTimeEnd": time.strftime(fmt, time.gmtime(ahora)),
        # el PDF dice entero, el esquema Swagger dice cadena: gana el esquema
        "aggregationType": "hour",
    }
    r = requests.post(f"{EEA_API}/ParquetFile/urls", json=cuerpo,
                      headers={"User-Agent": EEA_USER_AGENT},
                      timeout=EEA_HTTP_TIMEOUT)
    r.raise_for_status()
    urls = [l.strip() for l in r.text.splitlines()
            if l.strip().lower().endswith(".parquet")]
    _data_cache[clave] = urls
    log.info("EEA: %d ficheros disponibles para %s", len(urls),
             ",".join(sorted(paises)))
    return urls


def _elige_urls(urls, estaciones):
    """
    Qué ficheros de la lista corresponden a las estaciones elegidas, como
    pares (url, nombre_de_estacion).

    El nombre del fichero es el id del punto de muestreo
    ('SP_01055001_9_49.parquet'), asi que el criterio principal es ese id.
    Como respaldo se usa el CODIGO NACIONAL de estacion, que el nombre lleva
    dentro: si el vocabulario del metadato y el del blob no coincidieran
    exactamente, el emparejamiento por id daria cero ficheros y el modulo se
    quedaria sin datos EN SILENCIO, que es el fallo que hay que evitar.
    """
    por_spo, por_nat_map = {}, {}
    for st in estaciones:
        for x in (st.get("spos_todos") or []):
            por_spo[_spo_norm(x)] = st["name"]
        for n in (st.get("natcodes") or []):
            if n:
                por_nat_map[n] = st["name"]

    elegidas, por_nat = [], 0
    for u in urls:
        base = u.rsplit("/", 1)[-1]
        if base.lower().endswith(".parquet"):
            base = base[:-8]
        est = por_spo.get(base)
        if est is None:
            est = next((e for n, e in por_nat_map.items() if n in base), None)
            if est is not None:
                por_nat += 1
        if est is not None:
            elegidas.append((u, est))

    if not elegidas and (por_spo or por_nat_map):
        # ruidoso a proposito: con ejemplos de los dos lados se ve al momento
        # si el formato del id ha cambiado
        log.warning(
            "EEA: ningun fichero coincide con las %d estaciones elegidas. "
            "Ejemplos de fichero: %s | ejemplos de id buscado: %s | "
            "codigos nacionales: %s",
            len(estaciones),
            [u.rsplit("/", 1)[-1] for u in urls[:3]],
            sorted(por_spo)[:3], sorted(por_nat_map)[:3])
    elif por_nat:
        log.info("EEA: %d de %d ficheros emparejados por codigo nacional",
                 por_nat, len(elegidas))
    return elegidas


def _parsea_tabla(cols, dest, cutoff, descartes, tz_h=None):
    """
    Indexa un parquet ya leido ({columna: lista}, del lector hijo) en
    dest[metrica][epoch] = valor en la unidad del sistema (ug/m3; CO en
    mg/m3), donde 'dest' es el hueco de UNA estacion. Si la estacion ya
    tiene esa metrica de otro punto de muestreo, el nuevo no la pisa: un
    instrumento por metrica. 'tz_h' es la zona declarada de la estacion; se
    aplica solo si el fichero traia la hora sin zona (ver EEA_TZ_MODE).
    """
    corr_tz = 0.0
    if (cols.get("_naive") and EEA_TZ_MODE == "metadato"
            and tz_h is not None):
        corr_tz = -tz_h * 3600.0       # hora local -> UTC
    spos = cols.get("samplingpoint")
    pols = cols.get("pollutant")
    starts = cols.get("start")
    vals = cols.get("value")
    unis = cols.get("unit")
    valz = cols.get("validity")
    if not spos or not starts or not vals:
        return
    for i in range(len(spos)):
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
        t = starts[i]
        if isinstance(t, (int, float)):
            epoch = float(t)           # el hijo ya lo dio en epoch UTC
        else:
            try:
                epoch = t.timestamp() if t.tzinfo else (
                    t.replace(tzinfo=datetime.timezone.utc).timestamp())
            except (AttributeError, ValueError):
                continue
        if epoch != epoch:             # NaT
            continue
        epoch += corr_tz + EEA_HOUR_OFFSET * 3600
        # se filtra AQUI, no despues: cada fichero trae el historico entero
        # del punto y quedarselo todo en memoria no tiene ningun sentido
        if epoch < cutoff:
            descartes["fuera_de_ventana"] += 1
            continue
        u = (str(unis[i]).strip().lower().replace("\u00b5", "u")
             if unis else "ug/m3")
        # misma regla que openaq.py: ug/m3 para todo salvo CO, en mg/m3.
        # La primera version pasaba el CO a ug/m3 y el cotejo contra
        # Euskadi lo cazo: mediana_dif 169,86, mil veces el nivel real.
        en_mg = met in _MG_METRICS
        if u.startswith("ug"):
            factor = 0.001 if en_mg else 1.0
        elif u.startswith("mg"):
            factor = 1.0 if en_mg else 1000.0
        else:
            descartes["unidad"] += 1
            continue
        ya = dest.get(met)
        if ya is not None and epoch in ya:
            continue                   # mismo instante, otro instrumento
        dest.setdefault(met, {})[epoch] = float(v) * factor


def _descarga_parquet(estaciones, horas):
    """
    Datos horarios de las estaciones elegidas, YA AGRUPADOS POR ESTACION:
    {nombre_estacion: {metrica: {epoch: ug/m3}}}.

    Se agrupa aqui, y no en quien llama, porque el id del punto que trae el
    metadato puede no ser identico al del fichero del blob (se empareja por
    codigo nacional); volver a cruzar ids despues dejaria la serie vacia en
    silencio. Quien elige el fichero sabe de que estacion es: se aprovecha.

    Baja UN fichero por punto de muestreo, en streaming a disco. El diseño
    anterior pedia el ZIP completo del pais —2310 ficheros y 220 MB para
    España— y lo cargaba en memoria con r.content para acabar usando dos o
    tres estaciones: tumbo el contenedor en produccion. Los topes
    EEA_MAX_FILES y EEA_MAX_MB son el cinturon.
    """
    paises = sorted({st["pais"] for st in estaciones if st.get("pais")})
    if not paises:
        return {}

    # las horas van en la clave: /lag-scan pide 14 dias y /eea-debug 48 h,
    # y sin ellas la segunda llamada recibia la ventana de la primera
    clave = ("datos", ",".join(sorted(st["name"] for st in estaciones)),
             int(horas), int(time.time() // EEA_DATA_TTL))
    hit = _data_cache.get(clave)
    if hit is not None:
        return hit

    _ultima_descarga.clear()
    urls_pais = _urls_del_pais(paises, horas)
    pares = _elige_urls(urls_pais, estaciones)
    _ultima_descarga.update({
        "urls_en_pais": len(urls_pais),
        "urls_casadas": len(pares),
        "ejemplo_fichero": (urls_pais[0].rsplit("/", 1)[-1]
                            if urls_pais else None),
        "ejemplo_id_buscado": next(
            (x for st in estaciones for x in (st.get("spos_todos") or [])),
            None),
        "ejemplo_codigo_nacional": next(
            (x for st in estaciones for x in (st.get("natcodes") or [])),
            None),
    })
    if not pares:
        return {}
    if len(pares) > EEA_MAX_FILES:
        log.warning("EEA: %d ficheros para %d estaciones supera el tope de "
                    "%d; se recorta (sube EEA_MAX_FILES o baja "
                    "EEA_MAX_STATIONS)", len(pares), len(estaciones),
                    EEA_MAX_FILES)
        pares = pares[:EEA_MAX_FILES]

    cutoff = time.time() - horas * 3600
    out = {}
    descartes = {"unidad": 0, "validez": 0, "contaminante": 0,
                 "fuera_de_ventana": 0}
    bajados = mb = 0
    descargas = []                     # (ruta_local, estacion)
    for k, (u, est) in enumerate(pares):
        if mb >= EEA_MAX_MB:
            log.warning("EEA: alcanzado el tope de %.0f MB tras %d ficheros; "
                        "se para aqui", EEA_MAX_MB, bajados)
            break
        tmp = _cache_file("datos_%02d.parquet" % k)
        try:
            with requests.get(u, stream=True,
                              headers={"User-Agent": EEA_USER_AGENT},
                              timeout=EEA_BLOB_TIMEOUT) as r:
                r.raise_for_status()
                n = 0
                with open(tmp, "wb") as f:
                    for trozo in r.iter_content(chunk_size=1 << 18):
                        n += len(trozo)
                        if n > EEA_MAX_MB * (1 << 20):
                            raise RuntimeError(
                                "un solo fichero supera %.0f MB" % EEA_MAX_MB)
                        f.write(trozo)
            mb += n / (1 << 20)
            bajados += 1
            descargas.append((tmp, est))
        except Exception as e:
            log.warning("EEA: fichero %s no descargable: %s",
                        u.rsplit("/", 1)[-1], e)

    motor = None
    if descargas:
        # UNA lectura para todo el lote, en subproceso (ver _LECTOR_HIJO).
        # Al hijo se le da un corte holgado: aun no sabe la zona horaria de
        # cada estacion, y el corte exacto lo hace el padre tras corregirla.
        tz_por_est = {st["name"]: st.get("tz_h") for st in estaciones}
        leido, motor = _lee_parquets_aislado([r for r, _ in descargas],
                                             cutoff - 6 * 3600)
        if leido:
            for ruta, est in descargas:
                cols = leido.get(ruta)
                if cols:
                    _parsea_tabla(cols, out.setdefault(est, {}), cutoff,
                                  descartes, tz_por_est.get(est))
    for ruta, _est in descargas:
        try:
            os.remove(ruta)
        except OSError:
            pass
    _ultima_descarga.update({"ficheros_bajados": bajados,
                             "mb": round(mb, 2), "lector": motor,
                             "estaciones_con_datos": len(out)})
    log.info("EEA: %d ficheros, %.1f MB, lector=%s, %d estaciones con "
             "datos; descartes %s", bajados, mb, motor, len(out), descartes)
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

    datos = _descarga_parquet(stations, hours)
    if not datos:
        return {}, {}

    # QUÉ mide cada punto lo dice el parquet, no el metadato: el parquet trae
    # su propia columna Pollutant y es la autoritativa. El metadato solo ha
    # servido para decir qué puntos hay cerca. Y el filtrado temporal ya lo
    # hizo _descarga_parquet, que es donde no cuesta memoria.
    acc, fuente = {}, {}
    for est, por_met in sorted(datos.items()):
        for met, serie in sorted(por_met.items()):
            if not serie:
                continue
            for t, v in serie.items():
                acc.setdefault(met, {}).setdefault(t, []).append(v)
            fuente.setdefault(met, []).append(est)

    combined = {met: {t: round(sum(vs) / len(vs), 2)
                      for t, vs in por_h.items()}
                for met, por_h in acc.items()}
    return combined, fuente


def diagnose(lat, lon, hours=48):
    """Diagnóstico para /eea-debug: qué hay en el radio y qué se obtiene."""
    out = {"enabled": EEA_ENABLED, "radius_km": EEA_RADIUS_KM,
           "hour_offset": EEA_HOUR_OFFSET, "dataset": EEA_DATASET,
           "cpu_sse42": _cpu_tiene_sse42(),
           "metadata_url": EEA_METADATA_URL,
           "spos_forzados": EEA_FORCE_SPOS or None,
           "dentro_de_cobertura": _dentro_de_europa(lat, lon)}
    if EEA_FORCE_SPOS:
        series, fuente = fetch_official_series(hours)
        out["stations"] = [{"eoi": st["eoi"], "pais": st["pais"]}
                           for st in _estaciones_forzadas()]
        out["series_horas"] = {m: len(p) for m, p in sorted(series.items())}
        out["fuente_por_metrica"] = fuente
        out["lectores_vetados"] = sorted(_motores_vetados) or None
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
    # se lee DESPUES de la descarga: antes salia siempre vacio en la primera
    # llamada, aunque el lector acabara de morir en esta misma peticion
    out["lectores_vetados"] = sorted(_motores_vetados) or None
    # y el ultimo paso que llego a dar la descarga, para no tener que ir al
    # log a averiguar en que tramo se quedo vacia
    out["descarga"] = dict(_ultima_descarga) or None
    faltan = [m for m in EEA_TARGET_METRICS if m not in series]
    if faltan:
        out["sin_cubrir"] = faltan
    return out


def _mejor_desfase(a, b, lags=(-3, -2, -1, 0, 1, 2, 3), min_n=12):
    """
    Desfase (h) que mejor casa la serie EEA 'a' con la serie 'b' de otro
    proveedor: el lag que minimiza la mediana de |a(t+lag) - b(t)|.
    lag > 0 significa que la EEA etiqueta la misma medida 'lag' horas MAS
    TARDE que el otro proveedor.

    Si el minimo no es UNICO, el desfase no se puede fechar: una serie casi
    plana (SO2 en enteros de 1 a 5 ug/m3) da diferencia 0 en todos los
    desfases, y la primera version se quedaba con el primero que probaba
    (-3) y lo contaba como dato. Ahora sale fechable=False y no vota.
    """
    meds = {}
    for lag in lags:
        difs = sorted(abs(a[t + lag * 3600] - b[t]) for t in b
                      if (t + lag * 3600) in a)
        if len(difs) >= min_n:
            meds[lag] = (difs[len(difs) // 2], len(difs))
    if not meds:
        return None
    lag_min = min(meds, key=lambda k: (meds[k][0], abs(k)))
    med, n = meds[lag_min]
    empatados = [k for k, (m, _n) in meds.items() if abs(m - med) <= 1e-9]
    niveles = sorted(abs(v) for v in b.values())
    nivel = niveles[len(niveles) // 2] if niveles else 0.0
    rel = (med / nivel) if nivel > 0 else None
    fechable = len(empatados) == 1
    return {"lag_h": lag_min if fechable else None,
            "fechable": fechable,
            **({} if fechable else {"lags_empatados": sorted(empatados)}),
            "mediana_dif": round(med, 3), "horas_comunes": n,
            "nivel_mediano": round(nivel, 3),
            "dif_relativa": round(rel, 3) if rel is not None else None,
            # "casa" = es la MISMA medida, no solo parecida: la misma cabina
            # por dos caminos tiene que coincidir casi al dato
            "casa": bool(med == 0 or (rel is not None and rel <= 0.05))}


def cotejo_con(otro, hours, lat, lon, radio_pareja_km=0.5, max_parejas=6):
    """
    Coteja la MISMA cabina por dos caminos: la EEA y otro proveedor (el
    modulo euskadi). La primera version comparaba los PROMEDIOS de cada
    fuente, que mezclan conjuntos distintos de cabinas: salian desfases de
    -2 a +2 h segun la metrica, que no significaban nada (un error de
    etiquetado seria el mismo para todas). Aqui cada cabina del otro
    proveedor se empareja con la estacion EEA que esta en su mismo sitio
    (< radio_pareja_km) y se compara serie contra serie.

    Solo se dictamina un desfase si TODAS las parejas que casan al dato
    coinciden en el; si no, el veredicto es 'inconcluso' y no se recomienda
    tocar nada.
    """
    cerca = nearest_stations(lat, lon)
    if not cerca:
        return None
    datos = _descarga_parquet(cerca, hours)     # mismo lote: sale de cache
    try:
        otras = otro.nearest_stations(lat, lon, max_n=40,
                                      radius_km=EEA_RADIUS_KM)
    except TypeError:
        otras = otro.nearest_stations(lat, lon)
    parejas = []
    for so in otras or []:
        if so.get("lat") is None or so.get("lon") is None:
            continue
        mejor = None
        for se in cerca:
            if se.get("lat") is None or se["name"] not in datos:
                continue
            d = _haversine_km(so["lat"], so["lon"], se["lat"], se["lon"])
            if d <= radio_pareja_km and (mejor is None or d < mejor[0]):
                mejor = (d, se)
        if mejor:
            parejas.append((mejor[0], so, mejor[1]))
    parejas.sort(key=lambda x: x[0])
    parejas = parejas[:max_parejas]

    filas = []
    for d, so, se in parejas:
        try:
            s_otro, _f = otro.fetch_official_series(hours, stations=[so])
        except Exception as e:
            log.warning("EEA cotejo: %s no descargable: %s",
                        so.get("name"), e)
            continue
        s_eea = datos.get(se["name"]) or {}
        for m in sorted(set(s_otro) & set(s_eea)):
            r = _mejor_desfase(s_eea[m], s_otro[m])
            if r:
                filas.append({"estacion": so.get("name"), "eoi": se["eoi"],
                              "separacion_m": int(round(d * 1000)),
                              "tz_declarada_h": se.get("tz_h"),
                              "metrica": m, **r})

    out = {"tz_mode": EEA_TZ_MODE, "hour_offset": EEA_HOUR_OFFSET,
           "parejas": len(parejas), "comparaciones": filas}
    casan = [f for f in filas if f["casa"]]
    fechables = [f for f in casan if f["fechable"]]
    lags = sorted({f["lag_h"] for f in fechables})
    out["casan"] = len(casan)
    out["fechables"] = len(fechables)
    if not parejas:
        out["veredicto"] = "sin_parejas"
        out["nota"] = ("ninguna cabina del otro proveedor esta a menos de "
                       "%g m de una estacion EEA con datos: no hay nada que "
                       "cotejar dato a dato." % (radio_pareja_km * 1000))
    elif not casan:
        out["veredicto"] = "inconcluso"
        out["nota"] = ("ninguna pareja coincide al dato en ningun desfase "
                       "(+-3 h). No es un problema de horas; no toques "
                       "EEA_HOUR_OFFSET ni EEA_TZ_MODE.")
    elif not fechables:
        out["veredicto"] = "no_fechable"
        out["nota"] = ("las parejas coinciden al dato, pero todas las series "
                       "son demasiado planas para fechar un desfase.")
    elif lags == [0]:
        out["veredicto"] = "alineado"
        out["nota"] = ("%d comparaciones de la misma cabina coinciden al "
                       "dato con desfase 0: las dos fuentes etiquetan igual."
                       % len(fechables))
    elif len(lags) == 1:
        L = lags[0]
        out["veredicto"] = "desfase_relativo"
        out["desfase_h"] = L
        # OJO: esto es RELATIVO. Que dos fuentes discrepen en L horas no
        # dice cual de las dos tiene razon. La primera version recomendaba
        # corregir la EEA dando por hecho que la otra estaba bien; quien
        # arbitra son los sensores (/lag-scan), no este cotejo.
        out["nota"] = ("%d comparaciones de la misma cabina coinciden al "
                       "dato, todas con la EEA %d h %s que la otra fuente. "
                       "Esto NO dice cual de las dos tiene la hora buena: "
                       "lo decide /lag-scan, que correla ambas contra tus "
                       "sensores. No toques ningun desfase hasta verlo."
                       % (len(fechables), abs(L),
                          "mas tarde" if L > 0 else "antes"))
    else:
        out["veredicto"] = "inconsistente"
        out["nota"] = ("las parejas fechables discrepan en el desfase (%s): "
                       "no toques nada y revisa las comparaciones una a una."
                       % lags)
    return out
