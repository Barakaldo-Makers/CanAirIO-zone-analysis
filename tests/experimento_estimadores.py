# SPDX-License-Identifier: GPL-3.0-or-later
# CanAirIO-zone-analysis — pruebas
# Copyright (C) 2026 Barakaldo Makers
#
# Que estimador del factor aguanta mejor los mecanismos REALES de ruido:
#   E1 cociente de medias           mean(ref)/mean(sensor)   (el actual)
#   E2 mediana de cocientes         median(ref_i/sensor_i)
#   E3 mediana PONDERADA por nivel  (pesos = sensor_i; robusta Y eficiente:
#                                    es la version robusta de sum(ref)/sum(sensor))
# Escenarios por mecanismo observado en el despliegue:
#   S1 ruido limpio multiplicativo, PM entero (cuantizado)
#   S2 picos locales que la cabina no ve, numero VARIABLE (Poisson)
#   S3 zona limpia de verdad: mediana ~2.5, muchas horas redondean a 0 o 1
#   S4 mezcla: zona limpia + picos Poisson
# Se mide sd dia a dia (lo que infla la banda del detector) y sesgo.
import numpy as np

rng = np.random.default_rng(123)
F = 1.23


def wmedian(vals, w):
    o = np.argsort(vals)
    v, w = np.asarray(vals)[o], np.asarray(w)[o]
    cw = np.cumsum(w) / np.sum(w)
    return float(v[np.searchsorted(cw, 0.5)])


def ventana(nivel, sigma, lam):
    n = 46
    verdad = nivel * rng.lognormal(0.0, sigma, n)
    sv = np.round(np.clip(verdad * (1 + 0.2 * rng.standard_normal(n)), 0, None))
    rv = np.clip(F * verdad * (1 + 0.2 * rng.standard_normal(n)), 0, None)
    for i in rng.choice(n, size=min(rng.poisson(lam), n), replace=False):
        sv[i] += 30 + rng.exponential(15)
    return sv, rv


def estima(sv, rv):
    e1 = rv.mean() / sv.mean() if sv.mean() > 0 else np.nan
    ok = sv > 0
    r = rv[ok] / sv[ok]
    e2 = float(np.median(r)) if r.size >= 12 else np.nan
    e3 = wmedian(r, sv[ok]) if r.size >= 12 else np.nan
    return e1, e2, e3


ESC = {"S1 limpio":        dict(nivel=5.0, sigma=0.45, lam=0.0),
       "S2 picos Poisson": dict(nivel=5.0, sigma=0.45, lam=1.0),
       "S3 nivel bajo":    dict(nivel=2.5, sigma=0.8,  lam=0.0),
       "S4 bajo + picos":  dict(nivel=2.5, sigma=0.8,  lam=0.7)}

print(f"{'escenario':18s} {'E1 medias':>16s} {'E2 mediana':>16s} {'E3 med.pond':>16s}")
for nombre, kw in ESC.items():
    res = np.array([estima(*ventana(**kw)) for _ in range(600)])
    linea = f"{nombre:18s}"
    for j in range(3):
        col = res[:, j][~np.isnan(res[:, j])]
        linea += f"  sd {np.std(col):5.3f} ses {np.median(col) - F:+5.2f}"
    print(linea)
