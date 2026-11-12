# Pruebas de eea.py (1.5.0): referencia oficial europea.
#
#   cd tests && python3 test_eea.py     (necesita pyarrow y requests)
#
# Todo hermético: el CSV de metadatos es sintético, el parquet se fabrica con
# pyarrow y el HTTP está suplantado. La regla de los mocks del proyecto se
# mantiene: si la zona está fuera de Europa, el test EXIGE que no se haga ni
# una llamada de red.
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


def check(nombre, cond, detalle=""):
    if cond:
        print(f"  OK  {nombre}")
    else:
        print(f"FALLO {nombre}  {detalle}")
        FALLOS.append(nombre)


# ─────────────────────────────────────────────────────────────────────────────
print("== A. metadatos: parseo tolerante y filtrado ==")
CSV_META = (
    # cabeceras con ESPACIOS y 'Country' con NOMBRES, como el AQViewer real
    "Country,Year,Air Quality Network,Air Quality Station EoI Code,"
    "Sampling Point Id,Longitude,Latitude,Air Pollutant,"
    "Air Quality Station Type,Air Quality Station Area\n"
    # dos estaciones cerca de Barakaldo (43.29, -2.99)
    "Spain,2026,NET-ES001,ES0001A,SPO-ES0001A-0001,-2.98,43.30,PM2.5,background,urban\n"
    "Spain,2026,NET-ES001,ES0001A,SPO-ES0001A-0002,-2.98,43.30,PM10,background,urban\n"
    "Spain,2026,NET-ES001,ES0001A,SPO-ES0001A-0003,-2.98,43.30,PM2.5,background,urban\n"
    "Spain,2026,NET-ES001,ES0002A,SPO-ES0002A-0001,-3.01,43.28,PM2.5,traffic,urban\n"
    # contaminante que no es objetivo: fuera
    "Spain,2026,NET-ES001,ES0002A,SPO-ES0002A-0002,-3.01,43.28,BaP,traffic,urban\n"
    # estacion lejana (Madrid): fuera por radio
    "Spain,2026,NET-ES002,ES0003A,SPO-ES0003A-0001,-3.70,40.42,PM2.5,background,urban\n"
    # fila rota: fuera sin reventar
    "Spain,2026,NET-ES002,ES0004A,SPO-ES0004A-0001,no_numero,43.0,PM2.5,,\n"
    # MISMO punto en un anio viejo y con otras coordenadas: debe ganar 2026
    "Spain,2019,NET-ES001,ES0001A,SPO-ES0001A-0001,-2.50,43.00,PM2.5,background,urban\n"
    # punto DADO DE BAJA en 2015: fuera por anio
    "Spain,2015,NET-ES001,ES0009A,SPO-ES0009A-0001,-2.99,43.29,PM2.5,background,urban\n"
)
ruta_meta = "/tmp/eea-cache-test/meta_sintetico.csv"
os.makedirs("/tmp/eea-cache-test", exist_ok=True)
with open(ruta_meta, "w", encoding="utf-8") as f:
    f.write(CSV_META)
spos = eea._parsea_metadatos(ruta_meta)
# 5 = ES0001A x3 (pm25, pm10, pm25) + ES0002A pm25 + ES0003A pm25 (Madrid,
# que se parsea bien y solo cae despues, por radio). Fuera: BaP, fila rota,
# el duplicado de 2019 y la baja de 2015.
check("A1 filtra objetivo, duplicados, filas rotas y bajas", len(spos) == 5,
      f"{len(spos)} spos: {[s['spo'] for s in spos]}")
check("A1b 'Spain' NO se recorta a 'SP': el ISO sale del EoI",
      {s["pais"] for s in spos} == {"ES"},
      str({s["pais"] for s in spos}))
check("A1c gana la fila del anio mas alto (coordenadas de 2026)",
      all(abs(s["lat"] - 43.30) < 1e-9 for s in spos
          if s["spo"] == "SPO-ES0001A-0001"),
      str([s["lat"] for s in spos if s["spo"] == "SPO-ES0001A-0001"]))
check("A1d el punto dado de baja en 2015 queda fuera",
      all("ES0009A" not in s["eoi"] for s in spos),
      str([s["eoi"] for s in spos]))
check("A2 conserva ambos instrumentos de PM2.5 de ES0001A",
      sum(1 for s in spos if s["eoi"] == "ES0001A"
          and s["metrica"] == "pm25") == 2,
      str([(s["spo"], s["metrica"]) for s in spos]))

eea._meta_cache.update(t=time.time(), spos=spos)   # siembra la caché

cerca = eea.nearest_stations(43.29, -2.99)
check("A3 dos estaciones en el radio, orden por distancia",
      [st["eoi"] for st in cerca] == ["ES0001A", "ES0002A"],
      str([(st["eoi"], st["dist_km"]) for st in cerca]))
check("A4 agrupa sampling points por metrica",
      sorted(cerca[0]["spos"]) == ["pm10", "pm25"]
      and len(cerca[0]["spos"]["pm25"]) == 2, str(cerca[0]["spos"]))
check("A5 Madrid queda fuera del radio",
      all(st["eoi"] != "ES0003A" for st in cerca))
check("A5b spos_todos se rellena siempre",
      sorted(cerca[0]["spos_todos"]) == ["SPO-ES0001A-0001",
                                          "SPO-ES0001A-0002",
                                          "SPO-ES0001A-0003"],
      str(cerca[0].get("spos_todos")))
check("A5c _iso_de_eoi ignora el prefijo literal SPO-",
      eea._iso_de_eoi("", "SPO-ES0001A-0001") == "ES"
      and eea._iso_de_eoi("AD0942A") == "AD",
      f'{eea._iso_de_eoi("", "SPO-ES0001A-0001")} / {eea._iso_de_eoi("AD0942A")}')

# A6: el metadato real llega como ZIP; se decide por los bytes
ruta_zip = "/tmp/eea-cache-test/meta.zip"
with zipfile.ZipFile(ruta_zip, "w") as zf:
    zf.writestr("Airquality_Dissem.b2g.measurements.csv", CSV_META)
with open(ruta_zip, "rb") as f:
    check("A6 un ZIP se reconoce por su magia PK", f.read(2) == b"PK")

# A7: metadato SIN columna de contaminante -> no filtra, lo dira el parquet
sin_pol = "\n".join(
    ",".join(c for i, c in enumerate(linea.split(",")) if i != 7)
    for linea in CSV_META.strip().split("\n"))
ruta_sp = "/tmp/eea-cache-test/meta_sin_pol.csv"
with open(ruta_sp, "w", encoding="utf-8") as f:
    f.write(sin_pol + "\n")
spos_sp = eea._parsea_metadatos(ruta_sp)
# 6 = los 5 de A1 mas el punto de BaP, que sin columna de contaminante ya
# no se puede filtrar aqui: lo resolvera el parquet, que si lo dice.
check("A7 sin columna de contaminante conserva los spos",
      len(spos_sp) == 6 and all(s["metrica"] == "" for s in spos_sp),
      f"{len(spos_sp)} spos, metricas={ {s['metrica'] for s in spos_sp} }")
check("A8 y el pais sigue siendo ES sin columna de contaminante",
      all(s["pais"] == "ES" for s in spos_sp), str(spos_sp[0]))

# A9: respuesta HTML (lo que devuelve el servicio sin User-Agent: 200, no 403)
ruta_html = "/tmp/eea-cache-test/respuesta.html"
with open(ruta_html, "wb") as f:
    f.write(b"<!DOCTYPE html>\n<html><head><title>AQViewer</title></head>")
import zipfile as _zf
try:
    with open(ruta_html, "rb") as f:
        cabeza = f.read(512)
    es_html = cabeza[:2] != b"PK" and b"<html" in cabeza[:200].lower()
except Exception:
    es_html = False
check("A9 una respuesta HTML se reconoce (200 + text/html sin UA)", es_html)

# ─────────────────────────────────────────────────────────────────────────────
print("== B. parquet: vocabulario, unidades, validez, horas ==")
AHORA = int(time.time() // 3600 * 3600)


def hora(i):
    return dt.datetime.fromtimestamp(AHORA - i * 3600, dt.timezone.utc)


def zip_parquet():
    filas = {
        "Samplingpoint": [], "Pollutant": [], "Start": [], "End": [],
        "Value": [], "Unit": [], "AggType": [], "Validity": [],
    }

    def fila(spo, pol, i, val, unit="ug/m3", validez=1):
        filas["Samplingpoint"].append(spo)
        filas["Pollutant"].append(pol)
        filas["Start"].append(hora(i))
        filas["End"].append(hora(i - 1))
        filas["Value"].append(val)
        filas["Unit"].append(unit)
        filas["AggType"].append("hour")
        filas["Validity"].append(validez)

    for i in range(1, 25):
        # ES0001A publica PM2.5 por su primer instrumento; notacion directa
        fila("ES/SPO-ES0001A-0001", "PM2.5", i, 10.0)
        # ES0002A llega como CODIGO y en URI alternando: ambas deben valer
        fila("ES/SPO-ES0002A-0001",
             "6001" if i % 2 else
             "http://dd.eionet.europa.eu/vocabulary/aq/pollutant/6001",
             i, 20.0)
    # PM10 del segundo instrumento con codigo '5'
    fila("ES/SPO-ES0001A-0002", "5", 1, 30.0)
    # CO en mg/m3: x1000 (no es objetivo del test de media porque nadie mas lo da)
    fila("ES/SPO-ES0001A-0002", "10", 2, 0.4, unit="mg/m3")
    # invalida: fuera
    fila("ES/SPO-ES0001A-0001", "PM2.5", 30, 99.0, validez=-1)
    # unidad desconocida: fuera
    fila("ES/SPO-ES0001A-0001", "PM2.5", 31, 88.0, unit="ppb")
    # contaminante no objetivo: fuera
    fila("ES/SPO-ES0001A-0001", "BaP", 1, 7.7)

    tabla = pa.table(filas)
    buf = io.BytesIO()
    pq.write_table(tabla, buf)
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ES_datos.parquet", buf.getvalue())
    return z.getvalue()


class _Resp:
    def __init__(self, contenido):
        self.content = contenido
        self.status_code = 200
    def raise_for_status(self):
        pass


llamadas = []


def _post_falso(url, json=None, timeout=None):
    llamadas.append((url, json))
    return _Resp(zip_parquet())


eea.requests = types.SimpleNamespace(post=_post_falso, get=None)
eea._data_cache.clear()

datos = eea._descarga_parquet(["ES"], 48)
check("B1 indexa por spo sin prefijo de pais",
      "SPO-ES0001A-0001" in datos and "SPO-ES0002A-0001" in datos,
      str(list(datos))[:90])
check("B2 acepta notacion, codigo y URI del contaminante",
      len(datos["SPO-ES0002A-0001"]["pm25"]) == 24,
      str(len(datos.get("SPO-ES0002A-0001", {}).get("pm25", {}))))
check("B3 mg/m3 se convierte x1000",
      abs(list(datos["SPO-ES0001A-0002"]["co"].values())[0] - 400.0) < 1e-6,
      str(datos["SPO-ES0001A-0002"].get("co")))
check("B4 validez<1 y unidad desconocida quedan fuera",
      all(abs(v - 10.0) < 1e-6
          for v in datos["SPO-ES0001A-0001"]["pm25"].values()))
check("B5 el cuerpo pide dataset UTD y aggregationType string",
      llamadas[0][1]["dataset"] == 1
      and llamadas[0][1]["aggregationType"] == "hour", str(llamadas[0][1]))

# ─────────────────────────────────────────────────────────────────────────────
print("== C. fetch_official_series ==")
eea._data_cache.clear()
series, fuente = eea.fetch_official_series(48, lat=43.29, lon=-2.99)
alguna = list(series.get("pm25", {}).values())
check("C1 promedia las dos estaciones (10 y 20 -> 15)",
      alguna and all(abs(v - 15.0) < 1e-6 for v in alguna),
      str(alguna[:4]))
check("C2 veinticuatro horas de pm25", len(series.get("pm25", {})) == 24,
      str(len(series.get("pm25", {}))))
check("C3 fuente por metrica con codigos EoI",
      sorted(fuente.get("pm25", [])) == ["ES0001A", "ES0002A"], str(fuente))
check("C4 un solo sampling point por estacion y metrica (no duplica ST1)",
      fuente.get("pm25", []).count("ES0001A") == 1, str(fuente))

# C6: con metadato CIEGO (sin contaminante) las series salen igual, porque
# el contaminante lo pone el parquet. Es el caso que protege de un cambio de
# nombre en la columna del metadato.
eea._meta_cache.update(t=time.time(), spos=spos_sp)
eea._data_cache.clear()
s6, f6 = eea.fetch_official_series(48, lat=43.29, lon=-2.99)
check("C6 metadato sin contaminante: el parquet lo resuelve",
      [abs(v - 15.0) < 1e-6 for v in s6.get("pm25", {}).values()].count(True) == 24
      and sorted(f6.get("pm25", [])) == ["ES0001A", "ES0002A"],
      f"pm25={len(s6.get('pm25', {}))}h fuente={f6.get('pm25')}")
check("C6b y tambien recupera el co en mg/m3 de ES0001A",
      abs(list(s6.get("co", {}).values())[0] - 400.0) < 1e-6,
      str(s6.get("co")))

# C7: escotilla manual, sin metadato ni coordenadas
eea._meta_cache.update(t=0.0, spos=None)
eea._data_cache.clear()
eea.EEA_FORCE_SPOS = ["ES:SPO-ES0001A-0001"]
s7, f7 = eea.fetch_official_series(48)
check("C7 EEA_FORCE_SPOS funciona sin metadato",
      len(s7.get("pm25", {})) == 24 and f7.get("pm25") == ["SPO-ES0001A-0001"],
      str(f7))
eea.EEA_FORCE_SPOS = []
eea._meta_cache.update(t=time.time(), spos=spos)

eea._data_cache.clear()
llamadas.clear()


def _post_prohibido(url, json=None, timeout=None):
    raise AssertionError("fuera de Europa no se llama a la red")


eea.requests = types.SimpleNamespace(post=_post_prohibido, get=None)
s, f = eea.fetch_official_series(48, lat=3.42, lon=-76.52)   # Cali
check("C5 fuera de la caja europea: ({}, {}) sin tocar la red",
      s == {} and f == {})

# ─────────────────────────────────────────────────────────────────────────────
print("== D. cadena de app.py: euskadi -> openaq -> eea ==")
# stubs de despliegue, como en test_cambios.py
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


class _InfluxStub:
    def __init__(self, *a, **k): pass
    def switch_database(self, *a): pass
    def query(self, *a, **k): return None
    def write_points(self, *a, **k): return True
    def close(self): pass


_stub("influxdb", InfluxDBClient=_InfluxStub)
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
app.eea.nearest_stations = lambda lat, lon, **kw: [
    {"eoi": "ES0001A", "pais": "ES", "dist_km": 1.2, "spos": {}}]
app.eea.fetch_official_series = lambda hours, lat=None, lon=None, \
    stations=None: ({"pm25": {1.0: 12.0}}, {"pm25": ["ES0001A"]})

off, src, meta = app.fetch_official_any("ezt", 48)
check("D1 sin euskadi ni openaq, el proveedor es eea",
      meta.get("provider") == "eea", str(meta))
check("D2 las series llegan enteras", off.get("pm25") == {1.0: 12.0})
check("D3 meta lleva estacion y distancia",
      meta["stations"] == [{"name": "ES0001A", "dist_km": 1.2,
                            "country": "ES"}], str(meta.get("stations")))

app.eea.fetch_official_series = lambda *a, **k: ({}, {})
off, src, meta = app.fetch_official_any("ezt", 48)
check("D4 si la EEA no da series, se cae a CAMS (provider None)",
      off == {} and meta.get("provider") is None, str(meta.get("provider")))

# ─────────────────────────────────────────────────────────────────────────────
print()
if FALLOS:
    print(f"{len(FALLOS)} PRUEBAS FALLIDAS: {FALLOS}")
    sys.exit(1)
print("TODAS LAS PRUEBAS PASAN")