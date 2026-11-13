# SPDX-License-Identifier: GPL-3.0-or-later
# CanAirIO-zone-analysis — pruebas
# Copyright (C) 2026 Barakaldo Makers
#
# Pruebas de eea.py (1.5.2): referencia oficial europea.
#
#   cd tests && python3 test_eea.py     (necesita pyarrow y requests)
#
# Todo hermetico: metadato sintetico con el formato REAL del AQViewer,
# parquet fabricado con pyarrow, HTTP suplantado. Dos reglas del proyecto se
# mantienen: si la zona esta fuera de Europa el test EXIGE que no se haga ni
# una llamada de red, y los mocks imitan la forma real de la respuesta
# (contexto + iter_content), no una comoda.
import datetime as dt
import io
import os
import sys
import time
import types
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-bridge"))

import pyarrow as pa
import pyarrow.parquet as pq

os.environ.setdefault("EEA_CACHE_DIR", "/tmp/eea-cache-test")
import eea

FALLOS = []
DIR = "/tmp/eea-cache-test"
os.makedirs(DIR, exist_ok=True)


def check(nombre, cond, detalle=""):
    if cond:
        print(f"  OK  {nombre}")
    else:
        print(f"FALLO {nombre}  {detalle}")
        FALLOS.append(nombre)


# ─────────────────────────────────────────────────────────────────────────────
print("== A. metadato (formato real del AQViewer) ==")
# cabeceras CON ESPACIOS, 'Country' con NOMBRES, 'Year' y la fecha de cierre
CAB = ("Country,Year,Air Quality Station EoI Code,"
       "Air Quality Station Nat Code,Sampling Point Id,Air Pollutant,"
       "Longitude,Latitude,Air Quality Station Area,"
       "Air Quality Station Type,Operational Activity End")
FILAS = [
    # dos instrumentos de la misma estacion, cerca de Barakaldo (43.29,-2.99)
    "Spain,2026,ES0001A,01055001,SP_01055001_9_49,PM2.5,-2.98,43.30,urban,background,",
    "Spain,2026,ES0001A,01055001,SP_01055001_5_49,PM10,-2.98,43.30,urban,background,",
    # su id lleva un prefijo que NO esta en el nombre del fichero: tendra que
    # emparejarse por codigo nacional
    "Spain,2026,ES0002A,01059009,ES_SP_01059009_9_49,PM2.5,-3.01,43.28,urban,traffic,",
    # contaminante fuera de objetivo
    "Spain,2026,ES0002A,01059009,ES_SP_01059009_99_49,BaP,-3.01,43.28,urban,traffic,",
    # Madrid: dentro del pais, fuera del radio
    "Spain,2026,ES0003A,28079004,SP_28079004_9_46,PM2.5,-3.70,40.42,urban,background,",
    # fila rota
    "Spain,2026,ES0004A,04000000,SP_04000000_9_49,PM2.5,no_numero,43.0,,,",
    # DADA DE BAJA explicitamente, y cerquisima: el filtro de fin_op la saca
    "Spain,2026,ES0009A,09999999,SP_09999999_9_49,PM2.5,-2.99,43.29,urban,background,2015-12-31",
    # duplicado de un anio viejo con otras coordenadas: gana 2026
    "Spain,2019,ES0001A,01055001,SP_01055001_9_49,PM2.5,-2.50,43.00,urban,background,",
]
ruta_meta = f"{DIR}/meta.csv"
with open(ruta_meta, "w", encoding="utf-8") as f:
    f.write(CAB + "\n" + "\n".join(FILAS) + "\n")

spos = eea._parsea_metadatos(ruta_meta)
nombres = sorted(s["spo"] for s in spos)
check("A1 descarta no-objetivo, filas rotas y duplicados de anio viejo",
      len(spos) == 4, f"{len(spos)}: {nombres}")
check("A2 la baja explicita (Operational Activity End) queda fuera",
      all("09999999" not in n for n in nombres), str(nombres))
check("A3 'Spain' no se recorta a 'SP': el ISO sale del EoI",
      {s["pais"] for s in spos} == {"ES"}, str({s["pais"] for s in spos}))
check("A4 gana la fila del anio mas alto (lat 43.30, no 43.00)",
      all(abs(s["lat"] - 43.30) < 1e-9 for s in spos
          if s["spo"] == "SP_01055001_9_49"))
check("A5 se guarda el codigo nacional",
      {s["natcode"] for s in spos} == {"01055001", "01059009", "28079004"},
      str({s["natcode"] for s in spos}))
check("A6 _iso_de_eoi ignora el prefijo literal SPO-",
      eea._iso_de_eoi("", "SPO-ES0001A-0001") == "ES"
      and eea._iso_de_eoi("AD0942A") == "AD")

eea._meta_cache.update(t=time.time(), spos=spos)
cerca = eea.nearest_stations(43.29, -2.99)
check("A7 dos estaciones en el radio, por distancia",
      [st["eoi"] for st in cerca] == ["ES0001A", "ES0002A"],
      str([(st["eoi"], st["dist_km"]) for st in cerca]))
check("A8 arrastra los codigos nacionales a la estacion",
      cerca[1]["natcodes"] == ["01059009"], str(cerca[1].get("natcodes")))

# ─────────────────────────────────────────────────────────────────────────────
print("== B. eleccion de ficheros ==")
BLOB = "https://eeadmz1batchservice02.blob.core.windows.net/airquality-p/ES"
URLS = [f"{BLOB}/SP_01055001_9_49.parquet",      # id exacto
        f"{BLOB}/SP_01055001_5_49.parquet",      # id exacto
        f"{BLOB}/SP_01059009_9_49.parquet",      # solo por codigo nacional
        f"{BLOB}/SP_28079004_9_46.parquet",      # Madrid: no elegida
        f"{BLOB}/SP_99999999_9_49.parquet"]      # ajena
eleg = eea._elige_urls(URLS, cerca)
pares = sorted((u.rsplit("/", 1)[-1], est) for u, est in eleg)
check("B1 elige los tres ficheros y los atribuye a SU estacion",
      pares == [("SP_01055001_5_49.parquet", "ES0001A"),
                ("SP_01055001_9_49.parquet", "ES0001A"),
                ("SP_01059009_9_49.parquet", "ES0002A")],
      str(pares))
check("B2 Madrid no entra aunque este en la lista del pais",
      all("28079004" not in u for u, _e in eleg))
check("B3 sin coincidencias devuelve vacio (y avisa en el log)",
      eea._elige_urls([f"{BLOB}/SP_11111111_9_49.parquet"], cerca) == [])

# ─────────────────────────────────────────────────────────────────────────────
print("== C. descarga selectiva ==")
AHORA = int(time.time() // 3600 * 3600)


def parquet_de(spo, pol, n=24, val=10.0, unidad="ug/m3", validez=1,
               horas_viejas=0):
    """Un parquet como los del blob: historico del punto, no solo la ventana."""
    f = {"Samplingpoint": [], "Pollutant": [], "Start": [], "End": [],
         "Value": [], "Unit": [], "Validity": []}
    for i in range(1, n + 1):
        f["Samplingpoint"].append(f"ES/{spo}")
        f["Pollutant"].append(pol)
        f["Start"].append(dt.datetime.fromtimestamp(AHORA - i * 3600,
                                                    dt.timezone.utc))
        f["End"].append(dt.datetime.fromtimestamp(AHORA - (i - 1) * 3600,
                                                  dt.timezone.utc))
        f["Value"].append(val)
        f["Unit"].append(unidad)
        f["Validity"].append(validez)
    # filas muy antiguas: el fichero real las trae y hay que descartarlas AL
    # PARSEAR, no acumularlas en memoria
    for i in range(horas_viejas):
        f["Samplingpoint"].append(f"ES/{spo}")
        f["Pollutant"].append(pol)
        f["Start"].append(dt.datetime.fromtimestamp(AHORA - (900 + i) * 3600,
                                                    dt.timezone.utc))
        f["End"].append(dt.datetime.fromtimestamp(AHORA - (899 + i) * 3600,
                                                  dt.timezone.utc))
        f["Value"].append(777.0)
        f["Unit"].append(unidad)
        f["Validity"].append(validez)
    buf = io.BytesIO()
    pq.write_table(pa.table(f), buf)
    return buf.getvalue()


# un fichero por punto; el de ES0002A llega con el contaminante en CODIGO y
# el PM10 de ES0001A en mg/m3 para probar la conversion
CUERPOS = {
    "SP_01055001_9_49.parquet": parquet_de("SP_01055001_9_49", "PM2.5",
                                           horas_viejas=50),
    "SP_01055001_5_49.parquet": parquet_de("SP_01055001_5_49", "5",
                                           val=0.03, unidad="mg/m3"),
    "SP_01059009_9_49.parquet": parquet_de("SP_01059009_9_49", "6001",
                                           val=20.0),
}
pedidas = []


class _RespTexto:
    def __init__(self, texto):
        self.text = texto
    def raise_for_status(self):
        pass


class _RespStream:
    def __init__(self, datos):
        self._d = datos
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def raise_for_status(self):
        pass
    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._d), chunk_size):
            yield self._d[i:i + chunk_size]


def _post(url, json=None, timeout=None, headers=None):
    pedidas.append(("POST", url, json))
    return _RespTexto("ParquetFileUrl\n" + "\n".join(URLS))


def _get(url, stream=False, timeout=None, headers=None):
    pedidas.append(("GET", url, None))
    base = url.rsplit("/", 1)[-1]
    if base not in CUERPOS:
        raise AssertionError(f"no deberia pedirse {base}")
    return _RespStream(CUERPOS[base])


eea.requests = types.SimpleNamespace(post=_post, get=_get)
eea._data_cache.clear()
datos = eea._descarga_parquet(cerca, 48)

check("C1 solo pide los ficheros elegidos",
      sum(1 for m, _u, _j in pedidas if m == "GET") == 3,
      str([u.rsplit('/', 1)[-1] for m, u, _ in pedidas if m == "GET"]))
check("C2 usa /ParquetFile/urls, no /ParquetFile",
      any(u.endswith("/ParquetFile/urls") for m, u, _ in pedidas
          if m == "POST"),
      str([u for m, u, _ in pedidas if m == "POST"]))
# las claves son ESTACIONES, no puntos: el id del punto del metadato puede
# no coincidir con el del fichero (ES0002A se empareja por codigo nacional),
# y volver a cruzar ids despues dejaria la serie vacia en silencio.
check("C3 agrupa por estacion, no por punto de muestreo",
      sorted(datos) == ["ES0001A", "ES0002A"], str(sorted(datos)))
check("C4 24 horas de pm25 en ES0001A",
      len(datos["ES0001A"]["pm25"]) == 24,
      str(len(datos["ES0001A"]["pm25"])))
check("C5 las filas fuera de ventana NO se guardan (50 antiguas)",
      all(v == 10.0 for v in datos["ES0001A"]["pm25"].values()),
      "se colo un 777")
check("C6 mg/m3 x1000 y en la misma estacion que su pm25",
      abs(list(datos["ES0001A"]["pm10"].values())[0] - 30.0) < 1e-6,
      str(list(datos["ES0001A"]["pm10"].values())[:1]))
check("C7 ES0002A entra por codigo nacional y su 6001 vale como PM2.5",
      len(datos["ES0002A"]["pm25"]) == 24,
      str(list(datos.get("ES0002A", {}))))

# tope de ficheros
eea._data_cache.clear()
pedidas.clear()
eea.EEA_MAX_FILES = 1
eea._descarga_parquet(cerca, 48)
check("C8 EEA_MAX_FILES recorta la descarga",
      sum(1 for m, _u, _j in pedidas if m == "GET") == 1,
      str(sum(1 for m, _u, _j in pedidas if m == "GET")))
eea.EEA_MAX_FILES = 40

# un fichero roto no tumba el resto
eea._data_cache.clear()
CUERPOS["SP_01055001_9_49.parquet"] = b"esto no es un parquet"
d2 = eea._descarga_parquet(cerca, 48)
check("C9 un fichero ilegible se salta y los demas siguen",
      sorted(d2) == ["ES0001A", "ES0002A"]
      and "pm25" not in d2["ES0001A"], str(d2.get("ES0001A", {}).keys()))
CUERPOS["SP_01055001_9_49.parquet"] = parquet_de("SP_01055001_9_49", "PM2.5",
                                                 horas_viejas=50)

# ─────────────────────────────────────────────────────────────────────────────
print("== D. fetch_official_series ==")
eea._data_cache.clear()
series, fuente = eea.fetch_official_series(48, lat=43.29, lon=-2.99)
vals = list(series.get("pm25", {}).values())
check("D1 promedia las dos estaciones (10 y 20 -> 15)",
      vals and all(abs(v - 15.0) < 1e-6 for v in vals), str(vals[:3]))
check("D2 el pm10 sale de una sola estacion",
      all(abs(v - 30.0) < 1e-6 for v in series.get("pm10", {}).values()))
check("D3 fuente por metrica con codigos EoI",
      sorted(fuente.get("pm25", [])) == ["ES0001A", "ES0002A"], str(fuente))

eea._data_cache.clear()


def _prohibido(*a, **k):
    raise AssertionError("fuera de Europa no se toca la red")


eea.requests = types.SimpleNamespace(post=_prohibido, get=_prohibido)
s, f = eea.fetch_official_series(48, lat=3.42, lon=-76.52)      # Cali
check("D4 fuera de la caja europea: ({}, {}) sin red", s == {} and f == {})
eea.requests = types.SimpleNamespace(post=_post, get=_get)

# ─────────────────────────────────────────────────────────────────────────────
print("== F. lector aislado: motores y veto por señal ==")
# pyarrow oficial exige SSE4.2 y en una CPU anterior mata el proceso con
# SIGILL (exit 132). fastparquet TAMBIEN murio en produccion via sus
# dependencias compiladas. Por eso la lectura corre en un SUBPROCESO: una
# instruccion ilegal mata al hijo (codigo -4), el padre veta ese motor y el
# servicio sigue. Aqui: F1 fuerza el motor alternativo de verdad, F2 simula
# la muerte por señal de TODOS los motores.
cpu_orig = eea._cpu_tiene_sse42
eea._cpu_tiene_sse42 = lambda: False          # candidatos = [fastparquet]
eea._data_cache.clear()
datos_fp = eea._descarga_parquet(cerca, 48)
check("F1 con fastparquet (CPU sin SSE4.2) sale exactamente lo mismo",
      sorted(datos_fp) == sorted(datos)
      and datos_fp["ES0001A"]["pm25"] == datos["ES0001A"]["pm25"]
      and datos_fp["ES0001A"]["pm10"] == datos["ES0001A"]["pm10"]
      and datos_fp["ES0002A"]["pm25"] == datos["ES0002A"]["pm25"],
      f"claves {sorted(datos_fp)} vs {sorted(datos)}")
eea._cpu_tiene_sse42 = cpu_orig


class _MuertePorSenal:
    """subprocess.run que simula un hijo matado por SIGILL."""
    @staticmethod
    def run(*a, **k):
        return types.SimpleNamespace(returncode=-4, stdout=b"", stderr=b"")


subproc_orig = eea.subprocess
eea.subprocess = _MuertePorSenal
eea._motores_vetados.clear()
eea._data_cache.clear()
datos_muerte = eea._descarga_parquet(cerca, 48)
check("F2 si todos los lectores mueren con señal, devuelve {} sin reventar",
      datos_muerte == {}, str(datos_muerte))
check("F3 y ambos motores quedan vetados para no reintentar",
      eea._motores_vetados == {"pyarrow", "fastparquet"},
      str(eea._motores_vetados))
eea.subprocess = subproc_orig
eea._motores_vetados.clear()

# ─────────────────────────────────────────────────────────────────────────────
print("== G. zona horaria, unidades del CO y cotejo por estacion ==")
check("G1 _tz_horas entiende las formas del vocabulario",
      eea._tz_horas("UTC+01") == 1 and eea._tz_horas("UTC") == 0
      and eea._tz_horas("UTC-01") == -1 and eea._tz_horas("UTC+5:30") == 5.5
      and eea._tz_horas("http://dd.eionet.europa.eu/vocabulary/aq/timezone/UTC+02") == 2
      and eea._tz_horas("") is None and eea._tz_horas("basura") is None)


def parquet_naive(spo, pol, tz_h, n=24, val=10.0, unidad="ug.m-3"):
    """Hora LOCAL sin zona, como publica la EEA: Start = UTC + tz_h."""
    f = {"Samplingpoint": [], "Pollutant": [], "Start": [], "Value": [],
         "Unit": [], "Validity": []}
    for i in range(1, n + 1):
        utc = AHORA - i * 3600
        f["Samplingpoint"].append(f"ES/{spo}")
        f["Pollutant"].append(pol)
        f["Start"].append(dt.datetime.fromtimestamp(
            utc + tz_h * 3600, dt.timezone.utc).replace(tzinfo=None))
        f["Value"].append(val + i)          # varia: cada hora es distinguible
        f["Unit"].append(unidad)
        f["Validity"].append(1)
    buf = io.BytesIO()
    pq.write_table(pa.table(f), buf)
    return buf.getvalue()


guarda = dict(CUERPOS)
CUERPOS["SP_01055001_9_49.parquet"] = parquet_naive("SP_01055001_9_49",
                                                    "6001", 1)
est_tz = [dict(st, tz_h=1.0) for st in cerca]
esperado = {float(AHORA - i * 3600): 10.0 + i for i in range(1, 25)}

eea.EEA_TZ_MODE = "metadato"
eea._data_cache.clear()
d_tz = eea._descarga_parquet(est_tz, 48)
check("G2 hora local sin zona + estacion UTC+01 -> epochs UTC exactos",
      d_tz["ES0001A"]["pm25"] == esperado,
      str(sorted(d_tz["ES0001A"]["pm25"].items())[:2]))

eea.EEA_TZ_MODE = "utc"
eea._data_cache.clear()
d_utc = eea._descarga_parquet(est_tz, 48)
check("G3 con EEA_TZ_MODE=utc no se corrige (queda 1 h tarde)",
      min(d_utc["ES0001A"]["pm25"]) == min(esperado) + 3600,
      f'{min(d_utc["ES0001A"]["pm25"])} vs {min(esperado) + 3600}')
eea.EEA_TZ_MODE = "metadato"

eea._data_cache.clear()
d_aware = eea._descarga_parquet(est_tz, 48)
check("G4 hora CON zona (tz-aware) no se toca aunque la estacion diga UTC+01",
      d_aware["ES0002A"]["pm25"] == datos["ES0002A"]["pm25"],
      "ES0002A cambio")

CUERPOS["SP_01055001_5_49.parquet"] = parquet_de("SP_01055001_5_49", "10",
                                                 val=0.4, unidad="mg.m-3")
CUERPOS["SP_01059009_9_49.parquet"] = parquet_de("SP_01059009_9_49", "10",
                                                 val=400.0, unidad="ug.m-3")
eea._data_cache.clear()
d_co = eea._descarga_parquet(cerca, 48)
check("G5 CO en mg/m3 se queda en mg/m3 (convencion del sistema)",
      all(abs(v - 0.4) < 1e-9 for v in d_co["ES0001A"]["co"].values()),
      str(list(d_co["ES0001A"]["co"].values())[:2]))
check("G6 CO en ug/m3 pasa a mg/m3",
      all(abs(v - 0.4) < 1e-9 for v in d_co["ES0002A"]["co"].values()),
      str(list(d_co["ES0002A"]["co"].values())[:2]))
CUERPOS.clear()
CUERPOS.update(guarda)

# cotejo: misma cabina por dos caminos
import math as _m
base = AHORA - 48 * 3600
serie_a = {float(base + i * 3600): 20 + 8 * _m.sin(i / 3.0) + 0.2 * i
           for i in range(48)}
EST_EEA = [{"eoi": "ES0001A", "name": "ES0001A", "lat": 43.30, "lon": -2.98,
            "tz_h": 1.0, "dist_km": 1.0}]


class _OtroFalso:
    def __init__(self, estaciones, series):
        self.estaciones, self.series = estaciones, series
    def nearest_stations(self, lat, lon, max_n=None, radius_km=None):
        return self.estaciones
    def fetch_official_series(self, hours, stations=None):
        return self.series.get(stations[0]["name"], {}), {}


nearest_orig, descarga_orig = eea.nearest_stations, eea._descarga_parquet
eea.nearest_stations = lambda lat, lon, **k: EST_EEA
eea._descarga_parquet = lambda est, h: {"ES0001A": {"o3": serie_a}}
AQUI = {"name": "BARAKALDO", "lat": 43.3001, "lon": -2.9801}
LEJOS = {"name": "LEJANA", "lat": 43.35, "lon": -2.90}

r = eea.cotejo_con(_OtroFalso([AQUI], {"BARAKALDO": {"o3": dict(serie_a)}}),
                   48, 43.29, -2.99)
check("G7 misma cabina, mismas horas -> alineado, lag 0",
      r["veredicto"] == "alineado" and r["comparaciones"][0]["lag_h"] == 0,
      str(r.get("veredicto")))

# la EEA etiqueta 1 h mas tarde: b(t) = a(t + 1h)
b1 = {t: serie_a[t + 3600] for t in serie_a if (t + 3600) in serie_a}
eea.EEA_TZ_MODE = "utc"
r = eea.cotejo_con(_OtroFalso([AQUI], {"BARAKALDO": {"o3": b1}}),
                   48, 43.29, -2.99)
check("G8 EEA 1 h tarde -> desfase RELATIVO +1, sin recomendar ajuste",
      r["veredicto"] == "desfase_relativo" and r["desfase_h"] == 1
      and "/lag-scan" in r["nota"] and "OFFSET=" not in r["nota"],
      r.get("nota", "")[-140:])
eea.EEA_TZ_MODE = "metadato"

r = eea.cotejo_con(_OtroFalso([AQUI], {"BARAKALDO": {
    "o3": {t: v * 1.5 for t, v in serie_a.items()}}}), 48, 43.29, -2.99)
check("G9 series distintas -> inconcluso, sin recomendar nada",
      r["veredicto"] == "inconcluso" and "no toques" in r["nota"],
      str(r.get("veredicto")))

r = eea.cotejo_con(_OtroFalso([LEJOS], {"LEJANA": {"o3": dict(serie_a)}}),
                   48, 43.29, -2.99)
check("G10 cabina a >500 m de cualquier EEA -> sin_parejas",
      r["veredicto"] == "sin_parejas", str(r.get("veredicto")))

EST_EEA.append({"eoi": "ES0002A", "name": "ES0002A", "lat": 43.28,
                "lon": -3.01, "tz_h": 1.0, "dist_km": 2.0})
eea._descarga_parquet = lambda est, h: {"ES0001A": {"o3": serie_a},
                                         "ES0002A": {"o3": serie_a}}
OTRA = {"name": "OTRA", "lat": 43.2801, "lon": -3.0101}
r = eea.cotejo_con(_OtroFalso([AQUI, OTRA], {"BARAKALDO": {"o3": dict(serie_a)},
                                             "OTRA": {"o3": b1}}),
                   48, 43.29, -2.99)
check("G11 parejas que casan con desfases distintos -> inconsistente",
      r["veredicto"] == "inconsistente", str(r.get("veredicto")))
# G12: el caso real del 13-nov. SO2 casi plano (enteros bajos) empata en
# todos los desfases; antes votaba -3 y el veredicto salia 'inconsistente'.
EST_EEA[:] = EST_EEA[:1]
plana = {t: float(3 + (i % 9 == 0)) for i, t in enumerate(sorted(serie_a))}
eea._descarga_parquet = lambda est, h: {"ES0001A": {"o3": serie_a,
                                                    "so2": plana}}
r = eea.cotejo_con(_OtroFalso([AQUI], {"BARAKALDO": {"o3": b1,
                                                     "so2": dict(plana)}}),
                   48, 43.29, -2.99)
so2 = [c for c in r["comparaciones"] if c["metrica"] == "so2"][0]
check("G12 serie plana: no fechable, no vota, y el veredicto sale limpio",
      so2["fechable"] is False and so2["lag_h"] is None
      and r["veredicto"] == "desfase_relativo" and r["desfase_h"] == 1,
      f'so2={so2.get("lags_empatados")} veredicto={r.get("veredicto")}')
eea.nearest_stations, eea._descarga_parquet = nearest_orig, descarga_orig

# ─────────────────────────────────────────────────────────────────────────────
print("== E. cadena de app.py ==")


def _stub(nombre, **attrs):
    mod = types.ModuleType(nombre)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[nombre] = mod
    return mod


class _Flask:
    def __init__(self, *a, **k): pass
    def route(self, *a, **k):
        def deco(fn):
            return fn
        return deco


class _Req:
    args = {}


_stub("flask", Flask=_Flask, request=_Req(),
      jsonify=lambda *a, **k: (a[0] if a else dict(k)))


class _Influx:
    def __init__(self, *a, **k): pass
    def switch_database(self, *a): pass
    def query(self, *a, **k): return None
    def write_points(self, *a, **k): return True
    def close(self): pass


_stub("influxdb", InfluxDBClient=_Influx)
_aps = _stub("apscheduler")
_sch = _stub("apscheduler.schedulers")
_aps.schedulers = _sch


class _Sched:
    def __init__(self, *a, **k): pass
    def add_job(self, *a, **k): pass
    def start(self, *a, **k): pass


_sch.background = _stub("apscheduler.schedulers.background",
                        BackgroundScheduler=_Sched)
_stub("publisher")
_stub("euskadi", EUSKADI_ENABLED=False)
_stub("openaq", OPENAQ_ENABLED=False, OPENAQ_API_KEY="")
_stub("narrador")

import app  # noqa: E402

app.zone_sensor_center = lambda geo3, hours=168, mac=None: (43.29, -2.99, 3)
app.eea.EEA_ENABLED = True
eea._data_cache.clear()
off, src, meta = app.fetch_official_any("ezt", 48)
check("E1 sin euskadi ni openaq, el proveedor es eea",
      meta.get("provider") == "eea", str(meta))
check("E2 llegan las series promediadas",
      all(abs(v - 15.0) < 1e-6 for v in off.get("pm25", {}).values()))

app.eea.fetch_official_series = lambda *a, **k: ({}, {})
off, src, meta = app.fetch_official_any("ezt", 48)
check("E3 si la EEA no da nada, se cae a CAMS (provider None)",
      off == {} and meta.get("provider") is None)

# ─────────────────────────────────────────────────────────────────────────────
print("== H. /lag-scan: los sensores arbitran la hora ==")
import random as _rnd
_rnd.seed(3)
H0 = 1_790_000_000 - (1_790_000_000 % 3600)
verdad, x = {}, 8.0
for i in range(14 * 24):                       # 14 dias de aire "real"
    x = 0.85 * x + 0.15 * 8.0 + _rnd.gauss(0, 1.2)
    verdad[H0 + i * 3600] = max(0.5, x + 3 * _m.sin(2 * _m.pi * i / 24))
sensor = {h: v * 0.8 + _rnd.gauss(0, 0.4) for h, v in verdad.items()}
ref_bien = {h: v + _rnd.gauss(0, 0.3) for h, v in verdad.items()}
ref_tarde = {h + 3600: v for h, v in ref_bien.items()}   # etiqueta 1 h tarde
ref_pronto = {h - 3600: v for h, v in ref_bien.items()}  # etiqueta 1 h pronto

r0 = app._lag_scan_series(sensor, ref_bien)
r1 = app._lag_scan_series(sensor, ref_tarde)
rm = app._lag_scan_series(sensor, ref_pronto)
check("H1 referencia bien sellada -> mejor lag 0",
      r0["mejor_lag_h"] == 0, str(r0["mejor_lag_h"]))
check("H2 referencia 1 h tarde -> mejor lag +1",
      r1["mejor_lag_h"] == 1, str(r1["mejor_lag_h"]))
check("H3 referencia 1 h pronto -> mejor lag -1",
      rm["mejor_lag_h"] == -1, str(rm["mejor_lag_h"]))
check("H4 las diferencias afilan el maximo mas que los niveles",
      r0["criterio"] == "rho_diferencias"
      and r0["margen"] > (r0["por_lag"]["0"]["rho_niveles"]
                          - r0["por_lag"]["1"]["rho_niveles"]),
      f'margen dif {r0["margen"]} vs niveles '
      f'{r0["por_lag"]["0"]["rho_niveles"] - r0["por_lag"]["1"]["rho_niveles"]:.3f}')
print(f'      margen del maximo: diferencias {r0["margen"]:.3f} | niveles '
      f'{r0["por_lag"]["0"]["rho_niveles"] - r0["por_lag"]["1"]["rho_niveles"]:.3f}')

v = app._veredicto_lag({"pm25": rm, "pm10": rm}, -1, "EUSKADI_HOUR_OFFSET")
check("H5 fuente 1 h pronto con offset -1 -> recomienda 0",
      v["veredicto"] == "desfasada" and "EUSKADI_HOUR_OFFSET=0" in v["nota"],
      v.get("nota", ""))
v = app._veredicto_lag({"pm25": r1}, 0, "EEA_HOUR_OFFSET")
check("H6 un solo voto -> indicio, sin recomendar ajuste",
      v["veredicto"] == "indicio" and "OFFSET=" not in v["nota"],
      v.get("nota", ""))
v = app._veredicto_lag({"pm25": r1, "pm10": r1}, 0, "EEA_HOUR_OFFSET")
check("H6b dos votos 1 h tarde con offset 0 -> recomienda -1",
      v["veredicto"] == "desfasada" and "EEA_HOUR_OFFSET=-1" in v["nota"],
      v.get("nota", ""))
v = app._veredicto_lag({"pm25": r0, "pm10": r0}, -1, "EUSKADI_HOUR_OFFSET")
check("H7 fuente bien sellada -> hora_buena, sin recomendar nada",
      v["veredicto"] == "hora_buena" and "OFFSET=" not in v["nota"])
v = app._veredicto_lag({"pm25": r0, "pm10": r1}, -1, "EUSKADI_HOUR_OFFSET")
check("H8 metricas que discrepan -> inconsistente", v["veredicto"] ==
      "inconsistente", v["veredicto"])
ruido = {h: _rnd.gauss(10, 3) for h in sensor}
v = app._veredicto_lag({"pm25": app._lag_scan_series(sensor, ruido)}, -1,
                       "EUSKADI_HOUR_OFFSET")
check("H9 referencia sin relacion -> inconcluso, no vota", v["veredicto"] ==
      "inconcluso", v["veredicto"])
check("H1b con señal fuerte el desfase bueno gana >= 90 % de remuestreos",
      r0["estabilidad"] >= 0.9 and r1["estabilidad"] >= 0.9,
      f'{r0["estabilidad"]} / {r1["estabilidad"]}')


def zona_suavizada(ruido_sensor, suaviza=3, dias=14, semilla=1):
    """Sensor y cabina ven el mismo aire suavizado (media movil de 3 h,
    como la dispersion entre kilometros) y el sensor añade su ruido local.
    Con ruido 2,0 reproduce el regimen de produccion del 13-nov: rho de
    diferencias ~0,2 y margen ~0,03 entre desfases contiguos."""
    rnd = _rnd.Random(semilla)
    x, ver = 8.0, []
    for i in range(dias * 24):
        x = 0.85 * x + 0.15 * 8.0 + rnd.gauss(0, 1.2)
        ver.append(max(0.5, x + 3 * _m.sin(2 * _m.pi * i / 24)))
    def ma(i):
        v = ver[max(0, i - suaviza + 1):i + 1]
        return sum(v) / len(v)
    return ({H0 + i * 3600: ma(i) + rnd.gauss(0, ruido_sensor)
             for i in range(len(ver))},
            {H0 + i * 3600: ma(i) + rnd.gauss(0, 0.3)
             for i in range(len(ver))})


s_ok, c_ok = zona_suavizada(1.0)
s_prod, c_prod = zona_suavizada(2.0)
s_mal, c_mal = zona_suavizada(4.0)
r_ok = app._lag_scan_series(s_ok, c_ok)
r_prod = app._lag_scan_series(s_prod, c_prod)
r_mal = app._lag_scan_series(s_mal, c_mal)
print(f'      ruido 1/2/4: estabilidad {r_ok["estabilidad"]} / '
      f'{r_prod["estabilidad"]} / {r_mal["estabilidad"]}; mejor lag '
      f'{r_ok["mejor_lag_h"]} / {r_prod["mejor_lag_h"]} / {r_mal["mejor_lag_h"]}'
      ' (verdad 0)')
check("H10 señal moderada (rho_dif ~0,4) -> estable y vota",
      r_ok["estabilidad"] >= 0.8 and r_ok["mejor_lag_h"] == 0,
      str(r_ok["estabilidad"]))
check("H11 regimen de produccion (rho_dif ~0,2, margen ~0,03) -> NO vota "
      "aunque la estimacion acierte",
      r_prod["estabilidad"] < 0.8 and app._veredicto_lag(
          {"pm25": r_prod, "pm10": r_prod}, -1, "X")["veredicto"]
      == "inconcluso", str(r_prod["estabilidad"]))
check("H12 señal debil -> inestable: no puede votar un desfase, sea cual sea",
      r_mal["estabilidad"] < 0.8, str(r_mal["estabilidad"]))

print()
if FALLOS:
    print(f"{len(FALLOS)} PRUEBAS FALLIDAS: {FALLOS}")
    sys.exit(1)
print("TODAS LAS PRUEBAS PASAN")
