# Pruebas de los cambios 1.4.0: factor por mediana de cocientes horarios con
# IC 95 % (app.py) y en_el_limite + emergencia Lden-L90 (acustica.py).
#
#   cd tests && python3 test_cambios.py     (solo necesita numpy y scipy)
#
# Los mocks de Influx devuelven GENERADORES de un solo uso, como
# ResultSet.items() de influxdb-python: un recorrido doble debe romper la
# prueba, no pasarla (ya nos costó un «1 día, 0 horas» en producción).
# Las dependencias de despliegue (flask, influxdb, apscheduler...) se
# sustituyen por stubs ANTES de importar app, para poder probar la lógica
# sin levantar nada.
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-bridge"))
import numpy as np


def _stub(nombre, **attrs):
    mod = types.ModuleType(nombre)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[nombre] = mod
    return mod


class _Flask:
    def __init__(self, *a, **k): pass
    def route(self, *a, **k):
        def deco(f):
            return f
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


class _Scheduler:
    def __init__(self, *a, **k): pass
    def add_job(self, *a, **k): pass
    def start(self, *a, **k): pass


_aps = _stub("apscheduler")
_sch = _stub("apscheduler.schedulers")
_aps.schedulers = _sch
_sch.background = _stub("apscheduler.schedulers.background",
                        BackgroundScheduler=_Scheduler)

# modulos del proyecto que estas pruebas no ejercitan: stub para no arrastrar
# sus dependencias (acustica NO se stubea: es uno de los probados)
for _m in ("publisher", "euskadi", "openaq", "narrador"):
    _stub(_m)

import app          # noqa: E402
import acustica     # noqa: E402

FALLOS = []


def check(nombre, cond, detalle=""):
    if cond:
        print(f"  OK  {nombre}")
    else:
        print(f"FALLO {nombre}  {detalle}")
        FALLOS.append(nombre)


# ─────────────────────────────────────────────────────────────────────────────
print("== A. compare_with_reference: mediana de cocientes + IC ==")
rng = np.random.default_rng(42)


def ventana(n_h=46, f_true=1.23, nivel=5.0, ruido=0.20, picos_lam=0.0,
            cuantiza=True, rg=rng):
    """Una ventana de n_h horas emparejadas sensor/cabina. Los picos locales
    que la cabina no ve llegan en numero VARIABLE (Poisson): unos dias hay
    episodio y otros no, que es el mecanismo real. Con contaminacion constante
    el cociente de medias convierte su fragilidad en sesgo fijo y parece
    estable; medido en tests/experimento_estimadores.py."""
    t0 = 1_700_000_000 - (1_700_000_000 % 3600)
    verdad = nivel * rg.lognormal(0.0, 0.45, n_h)
    sv = verdad * (1.0 + ruido * rg.standard_normal(n_h))
    rv = f_true * verdad * (1.0 + ruido * rg.standard_normal(n_h))
    sv = np.clip(sv, 0.0, None)
    rv = np.clip(rv, 0.0, None)
    if cuantiza:
        sv = np.round(sv)          # CanAirIO publica el PM redondeado
    n_picos = min(int(rg.poisson(picos_lam)), n_h) if picos_lam else 0
    for i in rg.choice(n_h, size=n_picos, replace=False):
        sv[i] += 30.0 + rg.exponential(15.0)
    t = np.array([t0 + 3600 * i for i in range(n_h)], dtype=float)
    ref_map = {int(ti): float(x) for ti, x in zip(t, rv)}
    return {"pm25": (t, sv)}, {"pm25": ref_map}


serie, ref = ventana()
comp = app.compare_with_reference(serie, ref)
c = comp["pm25"]
check("A1 calcula factor_mediana", c.get("factor_mediana") is not None, str(c))
check("A2 el IC contiene el factor verdadero",
      c["factor_ci95"][0] <= 1.23 <= c["factor_ci95"][1], str(c.get("factor_ci95")))
check("A3 ratios_n <= n y descartados coherentes",
      c["ratios_n"] + c["ratios_descartados"] == c["n"], str(c))

# A4: estabilidad día a día, el motivo del cambio. 300 ventanas con picos
# locales en numero Poisson (unos dias hay episodio y otros no): el cociente
# de medias se los come; la mediana de cocientes no.
viejos, nuevos = [], []
for _ in range(300):
    s, r = ventana(picos_lam=1.0)
    cc = app.compare_with_reference(s, r)["pm25"]
    viejos.append(cc["reference_mean"] / cc["sensor_mean"])
    nuevos.append(cc["factor_mediana"])
sd_v, sd_n = float(np.std(viejos)), float(np.std(nuevos))
check("A4 la mediana de cocientes es mas estable que el cociente de medias",
      sd_n < 0.7 * sd_v, f"sd nuevo {sd_n:.3f} vs viejo {sd_v:.3f}")
print(f"      sd dia a dia: cociente de medias {sd_v:.3f} | mediana {sd_n:.3f} "
      f"({sd_v / sd_n:.1f}x menos ruido)")
check("A5 la mediana queda cerca del factor verdadero",
      abs(float(np.median(nuevos)) - 1.23) < 0.08,
      f"mediana {float(np.median(nuevos)):.3f}")
check("A5b el cociente de medias se sesga con episodios y la mediana no",
      abs(float(np.median(viejos)) - 1.23)
      > 2 * abs(float(np.median(nuevos)) - 1.23),
      f"sesgo viejo {float(np.median(viejos)) - 1.23:+.3f} "
      f"vs nuevo {float(np.median(nuevos)) - 1.23:+.3f}")

# A6: cobertura del IC (nominal 95 %, el de orden es conservador)
dentro = 0
for _ in range(300):
    s, r = ventana(ruido=0.15, cuantiza=False)
    cc = app.compare_with_reference(s, r)["pm25"]
    lo, hi = cc["factor_ci95"]
    dentro += int(lo <= 1.23 <= hi)
check("A6 cobertura del IC95 >= 85 %", dentro / 300 >= 0.85, f"{dentro / 300:.0%}")
print(f"      cobertura medida del IC95: {dentro / 300:.0%}")

# A7: horas con sensor a 0 quedan fuera sin reventar
s, r = ventana()
s["pm25"][1][:5] = 0.0
cc = app.compare_with_reference(s, r)["pm25"]
check("A7 sensor a 0 se descarta sin romper",
      cc["ratios_descartados"] >= 5 and cc.get("factor_mediana") is not None,
      str(cc.get("ratios_descartados")))

# A8: pocas horas validas -> sin factor_mediana
s, r = ventana(n_h=20)
s["pm25"][1][:12] = 0.0
cc = app.compare_with_reference(s, r)["pm25"]
check("A8 con <12 cocientes validos no hay factor_mediana",
      cc.get("factor_mediana") is None, str(cc))

# ─────────────────────────────────────────────────────────────────────────────
print("== B. pm_corrections ==")
base = {"spearman_rho": 0.8, "n": 46, "sensor_mean": 5.0,
        "reference_mean": 6.15, "factor_mediana": 1.21,
        "factor_ci95": [1.05, 1.42], "ratios_n": 44}
out = app.pm_corrections({"pm25": dict(base)}, basis="euskadi")["pm25"]
check("B1 usa la mediana como factor", out["factor"] == 1.21, str(out))
check("B2 metodo etiquetado", out["metodo"] == "mediana_ratios_horarios")
check("B3 conserva el estimador antiguo", out["factor_medias"] == 1.23, str(out))
check("B4 IC estrecho no marca incierto", "incierto" not in out)

ancho = dict(base); ancho["factor_ci95"] = [0.6, 1.9]
out = app.pm_corrections({"pm25": ancho}, basis="euskadi")["pm25"]
check("B5 IC ancho marca incierto", out.get("incierto") is True, str(out))

sin_med = {"spearman_rho": 0.8, "n": 14, "sensor_mean": 5.0,
           "reference_mean": 6.15}
out = app.pm_corrections({"pm25": sin_med}, basis="euskadi")["pm25"]
check("B6 sin mediana cae al cociente de medias",
      out["metodo"] == "cociente_de_medias" and out["factor"] == 1.23, str(out))

out = app.pm_corrections({"pm25": {**base, "spearman_rho": 0.3}})
check("B7 la puerta de rho sigue cerrando", "pm25" not in out, str(out))

# ─────────────────────────────────────────────────────────────────────────────
print("== C. _save_pm_factor_history: dos campos, una definicion cada uno ==")
escrito = {}


class _CapturaInflux:
    def switch_database(self, *a): pass
    def write_points(self, puntos):
        escrito["puntos"] = puntos
        return True
    def close(self): pass


app.influx_client = lambda: _CapturaInflux()
datos = app.pm_corrections({"pm25": dict(base)}, basis="euskadi")
app._save_pm_factor_history("ezt", datos, basis="euskadi")
f = escrito["puntos"][0]["fields"]
tg = escrito["puntos"][0]["tags"]
check("C1 pm_factor_pm25 conserva el cociente de medias",
      f.get("pm_factor_pm25") == 1.23, str(f))
check("C2 pm_factor_med_pm25 lleva la mediana",
      f.get("pm_factor_med_pm25") == 1.21, str(f))
check("C3 IC guardado",
      f.get("pm_fci_lo_pm25") == 1.05 and f.get("pm_fci_hi_pm25") == 1.42, str(f))
check("C4 pairs guardado", f.get("pm_pairs_pm25") == 46.0, str(f))
check("C5 tag basis", tg.get("basis") == "euskadi", str(tg))

datos_viejo = app.pm_corrections({"pm25": dict(sin_med)}, basis="euskadi")
escrito.clear()
app._save_pm_factor_history("ezt", datos_viejo, basis="euskadi")
f = escrito["puntos"][0]["fields"]
check("C6 el fallback solo escribe la serie antigua",
      f.get("pm_factor_pm25") == 1.23 and "pm_factor_med_pm25" not in f, str(f))

# ─────────────────────────────────────────────────────────────────────────────
print("== D. detect_factor_drift: dos series, nunca mezcladas ==")


def _gen(lista):
    for x in lista:
        yield x


class _ResultSet:
    """items() devuelve generadores de un solo uso, como influxdb-python."""
    def __init__(self, series):
        self._series = series
    def items(self):
        return [((n, t), _gen(p)) for (n, t), p in self._series]


class _InfluxDrift:
    def __init__(self, dias):
        self.dias = dias           # lista de puntos diarios
    def query(self, q):
        if "SHOW FIELD KEYS" in q:
            claves = set()
            for p in self.dias:
                for k in p:
                    if p[k] is not None:
                        claves.add({"pm25": "pm_factor_pm25",
                                    "pm25__pairs": "pm_pairs_pm25",
                                    "pm25__med": "pm_factor_med_pm25"}.get(k, k))
            return _ResultSet([(("pm_factor_history", None),
                                [{"fieldKey": k} for k in sorted(claves)])])
        return _ResultSet([(("pm_factor_history", {"basis": "euskadi"}),
                            list(self.dias))])
    def close(self): pass


def dias_drift(viejos, medianos):
    """viejos: lista de factores cociente-de-medias; medianos: lista (los
    ultimos len(medianos) dias llevan tambien la serie nueva)."""
    out = []
    n = len(viejos)
    for i, v in enumerate(viejos):
        p = {"pm25": v, "pm25__pairs": 45.0}
        j = i - (n - len(medianos))
        if 0 <= j < len(medianos):
            p["pm25__med"] = medianos[j]
        out.append(p)
    return out


rng2 = np.random.default_rng(7)
viejos26 = list(1.6 + 0.9 * rng2.standard_normal(26))   # el ruido real del cociente

app._esod_db = lambda: _InfluxDrift(dias_drift(viejos26, []))
d = app.detect_factor_drift("ezt")["metrics"]["pm25"]
check("D1 sin serie nueva juzga la antigua y lo dice",
      d["estimador"] == "cociente_de_medias", str(d.get("estimador")))

app._esod_db = lambda: _InfluxDrift(
    dias_drift(viejos26, list(1.21 + 0.03 * rng2.standard_normal(8))))
d = app.detect_factor_drift("ezt")["metrics"]["pm25"]
check("D2 con 8 dias nuevos sigue en la antigua",
      d["estimador"] == "cociente_de_medias")
check("D3 y anuncia la transicion", "8 de 12" in d.get("note", ""),
      d.get("note", "")[-120:])

med15 = list(1.21 + 0.03 * rng2.standard_normal(15))
app._esod_db = lambda: _InfluxDrift(dias_drift(viejos26, med15))
d = app.detect_factor_drift("ezt")["metrics"]["pm25"]
check("D4 con 15 dias se pasa a la mediana",
      d["estimador"] == "mediana_ratios_horarios", str(d))
check("D5 y juzga SOLO esos 15 dias (sin mezclar)", d["days"] == 15, str(d["days"]))
banda = d["band"][1] - d["band"][0]
check("D6 la banda de la serie nueva es mucho mas estrecha",
      banda < 0.6, f"banda {d['band']} (ancho {banda:.3f})")

med_deriva = list(1.21 + 0.03 * rng2.standard_normal(14)) + [1.9] * 6
app._esod_db = lambda: _InfluxDrift(dias_drift(
    list(1.6 + 0.9 * rng2.standard_normal(26)), med_deriva))
d = app.detect_factor_drift("ezt")["metrics"]["pm25"]
check("D7 una deriva real en la serie nueva se detecta",
      d["detected"] is True and d["status"] == "deriva", str(d.get("status")))
check("D8 con la direccion correcta",
      d.get("direction") == "sensor_lee_mas_bajo", str(d.get("direction")))

# ─────────────────────────────────────────────────────────────────────────────
print("== E. acustica: en_el_limite ==")
os.environ.setdefault("ACU_AREA_DEFAULT", "a")


def res_de(**kw):
    return {k: {"valor": v} for k, v in kw.items()}


v = acustica.veredicto_oms(res_de(lden=52.5, ln=35.0))
check("E1 Lden 52.5 (umbral 53) -> en_el_limite", v["estado"] == "en_el_limite",
      v["estado"])
v = acustica.veredicto_oms(res_de(lden=50.1, ln=37.3))
check("E2 Lden 50.1 -> saludable (margen 2.9 > 2)", v["estado"] == "saludable",
      v["estado"])
v = acustica.veredicto_oms(res_de(lden=73.4, ln=65.1))
check("E3 Lden 73.4 -> no_saludable", v["estado"] == "no_saludable", v["estado"])
v = acustica.veredicto_oms(res_de(lden=55.1, ln=35.0))
check("E4 Lden 55.1 (umbral+2.1) -> no_saludable", v["estado"] == "no_saludable",
      v["estado"])
v = acustica.veredicto_oms(res_de(lden=55.0, ln=35.0))
check("E5 Lden 55.0 (umbral+2.0) -> en_el_limite", v["estado"] == "en_el_limite",
      v["estado"])
v = acustica.veredicto_oms(res_de(lden=50.9, ln=35.0))
check("E6 Lden 50.9 (umbral-2.1) -> saludable", v["estado"] == "saludable",
      v["estado"])
v = acustica.veredicto_oms(res_de(lden=58.9, ln=44.0))
check("E7 Lden claro + Ln en franja -> no_saludable y lo menciona",
      v["estado"] == "no_saludable" and "indecis" in v["nota"], v["nota"][-80:])

# RD 1367, area a: 65/65/55
v = acustica.veredicto_rd(res_de(ld=60.1, le=46.8, ln=47.5), "ezt")
check("E8 DE32E9E real -> dentro_del_objetivo",
      v["estado"] == "dentro_del_objetivo", v["estado"])
v = acustica.veredicto_rd(res_de(ld=64.0, le=50.0, ln=40.0), "ezt")
check("E9 Ld 64 (objetivo 65) -> en_el_limite", v["estado"] == "en_el_limite",
      v["estado"])
v = acustica.veredicto_rd(res_de(ld=68.0, le=50.0, ln=54.0), "ezt")
check("E10 Ld claro fuera + Ln en franja -> fuera y lo menciona",
      v["estado"] == "fuera_del_objetivo" and "indecis" in v["nota"],
      v["nota"][-90:])
v = acustica.veredicto_rd(res_de(ld=60.0, le=50.0), "ezt")
check("E11 sin Ln sigue siendo sin_datos", v["estado"] == "sin_datos", v["estado"])
check("E12 detalle lleva en_limite por indicador",
      acustica.veredicto_oms(res_de(lden=52.5, ln=35.0))
      ["detalle"]["lden"]["en_limite"] is True)

# ─────────────────────────────────────────────────────────────────────────────
print("== F. acustica: emergencia Lden-L90 (datos reales del 19-oct) ==")
NODOS = {
    "M1": {"lden": {"valor": 73.4}, "l90": {"valor": 55.0}},
    "M2": {"lden": {"valor": 58.9}, "l90": {"valor": 47.0}},
    "M3": {"lden": {"valor": 50.1}, "l90": {"valor": 35.0}},
    "M4": {"lden": {"valor": 52.5}, "l90": {"valor": 34.0}},
    "XIAO": {"lden": {"valor": 82.2}},          # sin L90, como en produccion
}
NOMBRES = {m: m for m in NODOS}
zona = {"lden": {"recorrido_db": 32.1}, "l90": {"recorrido_db": 21.0}}
dg = acustica.diagnostico(zona, NODOS, NOMBRES)
check("F1 emergencias de los 4 nodos comunes",
      dg.get("emergencia_db") == {"M1": 18.4, "M2": 11.9, "M3": 15.1, "M4": 18.5},
      str(dg.get("emergencia_db")))
check("F2 recorrido de emergencia 6.6",
      dg.get("recorrido_emergencia_db") == 6.6,
      str(dg.get("recorrido_emergencia_db")))
check("F3 la causa sigue siendo cobertura_insuficiente (XIAO sin L90)",
      dg.get("causa") == "cobertura_insuficiente", str(dg.get("causa")))

# F4: con L90 de firmware (plano e identico) NO se publica emergencia
PLANOS = {m: {"lden": {"valor": v},
              "l90": {"valor": 32.0, "constante": True}}
          for m, v in (("M1", 42.5), ("M2", 41.7), ("M3", 41.8))}
dg = acustica.diagnostico({"lden": {"recorrido_db": 0.8},
                           "l90": {"recorrido_db": 0.0}}, PLANOS,
                          {m: m for m in PLANOS})
check("F4 L90 no utilizable -> sin emergencia",
      dg.get("causa") == "l90_no_utilizable" and "emergencia_db" not in dg,
      str(dg.get("causa")))

# ─────────────────────────────────────────────────────────────────────────────
print()
if FALLOS:
    print(f"{len(FALLOS)} PRUEBAS FALLIDAS: {FALLOS}")
    sys.exit(1)
print("TODAS LAS PRUEBAS PASAN")