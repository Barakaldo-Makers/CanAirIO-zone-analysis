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
    "Countrycode\tTimezone\tNamespace\tAirQualityNetwork\tAirQualityStation\t"
    "AirQualityStationEoICode\tSamplingPointId\tLongitude\tLatitude\t"
    "AirPollutant\tAirQualityStationType\tAirQualityStationArea\n"
    # dos estaciones cerca de Barakaldo (43.29, -2.99)
    "ES\tEurope/Madrid\tES.BDCA\tRED1\tST1\tES0001A\tSP_0001_PM25\t-2.98\t43.30\tPM2.5\tbackground\turban\n"
    "ES\tEurope/Madrid\tES.BDCA\tRED1\tST1\tES0001A\tSP_0001_PM10\t-2.98\t43.30\tPM10\tbackground\turban\n"
    "ES\tEurope/Madrid\tES.BDCA\tRED1\tST1\tES0001A\tSP_0001_PM25_B\t-2.98\t43.30\tPM2.5\tbackground\turban\n"
    "ES\tEurope/Madrid\tES.BDCA\tRED1\tST2\tES0002A\tSP_0002_PM25\t-3.01\t43.28\tPM2.5\ttraffic\turban\n"
    # contaminante que no es objetivo: fuera
    "ES\tEurope/Madrid\tES.BDCA\tRED1\tST2\tES0002A\tSP_0002_BAP\t-3.01\t43.28\tBaP\ttraffic\turban\n"
    # estación lejana (Madrid): fuera por radio
    "ES\tEurope/Madrid\tES.BDCA\tRED2\tST3\tES0003A\tSP_0003_PM25\t-3.70\t40.42\tPM2.5\tbackground\turban\n"
    # fila rota: fuera sin reventar
    "ES\tEurope/Madrid\tES.BDCA\tRED2\tST4\tES0004A\tSP_0004_PM25\tno_numero\t43.0\tPM2.5\t\t\n"
    # duplicado exacto spo+metrica: fuera
    "ES\tEurope/Madrid\tES.BDCA\tRED1\tST1\tES0001A\tSP_0001_PM25\t-2.98\t43.30\tPM2.5\tbackground\turban\n"
)
ruta_meta = "/tmp/eea-cache-test/meta_sintetico.csv"
os.makedirs("/tmp/eea-cache-test", exist_ok=True)
with open(ruta_meta, "w", encoding="utf-8") as f:
    f.write(CSV_META)
spos = eea._parsea_metadatos(ruta_meta)
check("A1 filtra objetivo, duplicados y filas rotas", len(spos) == 5,
      f"{len(spos)} spos: {[s['spo'] for s in spos]}")
check("A2 conserva ambos instrumentos de PM2.5 de ES0001A",
      sum(1 for s in spos if s["eoi"] == "ES0001A"
          and s["metrica"] == "pm25") == 2)

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
        fila("ES/SP_0001_PM25", "PM2.5", i, 10.0)
        # ES0002A llega como CODIGO y en URI alternando: ambas deben valer
        fila("ES/SP_0002_PM25",
             "6001" if i % 2 else
             "http://dd.eionet.europa.eu/vocabulary/aq/pollutant/6001",
             i, 20.0)
    # PM10 del segundo instrumento con codigo '5'
    fila("ES/SP_0001_PM10", "5", 1, 30.0)
    # CO en mg/m3: x1000 (no es objetivo del test de media porque nadie mas lo da)
    fila("ES/SP_0001_PM10", "10", 2, 0.4, unit="mg/m3")
    # invalida: fuera
    fila("ES/SP_0001_PM25", "PM2.5", 30, 99.0, validez=-1)
    # unidad desconocida: fuera
    fila("ES/SP_0001_PM25", "PM2.5", 31, 88.0, unit="ppb")
    # contaminante no objetivo: fuera
    fila("ES/SP_0001_PM25", "BaP", 1, 7.7)

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
      "SP_0001_PM25" in datos and "SP_0002_PM25" in datos,
      str(list(datos))[:90])
check("B2 acepta notacion, codigo y URI del contaminante",
      len(datos["SP_0002_PM25"]["pm25"]) == 24,
      str(len(datos.get("SP_0002_PM25", {}).get("pm25", {}))))
check("B3 mg/m3 se convierte x1000",
      abs(list(datos["SP_0001_PM10"]["co"].values())[0] - 400.0) < 1e-6,
      str(datos["SP_0001_PM10"].get("co")))
check("B4 validez<1 y unidad desconocida quedan fuera",
      all(abs(v - 10.0) < 1e-6
          for v in datos["SP_0001_PM25"]["pm25"].values()))
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
