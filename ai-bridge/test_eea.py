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
print("== F. lector de parquet: guarda de CPU y motor alternativo ==")
# pyarrow oficial exige SSE4.2 y en una CPU anterior mata el proceso con
# SIGILL (exit 132): paso dos veces en produccion sobre un Q6600. La guarda
# mira /proc/cpuinfo ANTES de importar.
cpu_orig = eea._cpu_tiene_sse42
eea._motor_parquet = None
eea._cpu_tiene_sse42 = lambda: False
motor, _mod = eea._lector_parquet()
check("F1 sin SSE4.2 ni se intenta pyarrow: fastparquet",
      motor == "fastparquet", str(motor))

eea._data_cache.clear()
datos_fp = eea._descarga_parquet(cerca, 48)
check("F2 fastparquet lee exactamente lo mismo que pyarrow",
      sorted(datos_fp) == sorted(datos)
      and datos_fp["ES0001A"]["pm25"] == datos["ES0001A"]["pm25"]
      and datos_fp["ES0001A"]["pm10"] == datos["ES0001A"]["pm10"]
      and datos_fp["ES0002A"]["pm25"] == datos["ES0002A"]["pm25"],
      f"claves {sorted(datos_fp)} vs {sorted(datos)}")

eea._motor_parquet = (None, None)
eea._data_cache.clear()
check("F3 sin ningun lector, la EEA se aparta sin reventar",
      eea._descarga_parquet(cerca, 48) == {})
eea._motor_parquet = None
eea._cpu_tiene_sse42 = cpu_orig

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

print()
if FALLOS:
    print(f"{len(FALLOS)} PRUEBAS FALLIDAS: {FALLOS}")
    sys.exit(1)
print("TODAS LAS PRUEBAS PASAN")
