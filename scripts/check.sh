#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# canairio-zone-analysis — read-only health check
# Copyright (C) 2026 Barakaldo Makers
# =====================================================================
#  check.sh — verificacion del ai-bridge (solo lectura)
#  Barakaldo Makers · 19/09/2026
#
#  No despliega ni modifica nada: solo consulta la API y resume el estado.
#  Sustituye a las verificaciones de los parches, que leian la clave
#  equivocada ("metrics" en vez de "statistics") y por eso mostraban
#  "analizadas (0)" aunque el analisis estuviese bien.
#
#  Uso:
#     bash check.sh                     primera zona activa
#     bash check.sh u33                 una zona concreta
#     ZONA=u33 bash check.sh            equivalente
#     bash check.sh u33 --breve         omite la tabla de sensores
# =====================================================================
set -uo pipefail

ENVDIR="${ENVDIR:-$HOME/envmonitor}"
API="${API:-http://localhost:5000}"
# Sin zona por defecto: se toma la primera activa que devuelva la API, para
# que el script no dependa de ninguna zona del despliegue original.
ZONA="${1:-${ZONA:-}}"
case "${1:-}" in --*) ZONA="${ZONA:-}" ;; esac
BREVE=0
for a in "$@"; do [ "$a" = "--breve" ] && BREVE=1; done

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi
paso(){ printf '\n%s== %s ==%s\n' "$B" "$1" "$N"; }
ok(){   printf '   %s[ok]%s   %s\n' "$G" "$N" "$1"; }
avi(){  printf '   %s[aviso]%s %s\n' "$Y" "$N" "$1"; }
err(){  printf '   %s[ERROR]%s %s\n' "$R" "$N" "$1"; }

DC=""
docker compose version >/dev/null 2>&1 && DC="docker compose"
[ -n "$DC" ] || { command -v docker-compose >/dev/null 2>&1 && DC="docker-compose"; }

JS="$(mktemp)"; trap 'rm -f "$JS"' EXIT
FALLOS=0

# trae un endpoint a $JS; 1 si falla
traer() {
  if ! curl -sS --max-time "${2:-180}" "$API$1" > "$JS" 2>&1; then
    err "no pude consultar $1"; return 1
  fi
  return 0
}

if [ -z "$ZONA" ]; then
  ZONA="$(curl -sS --max-time 60 "$API/zones" 2>/dev/null \
    | python3 -c "import sys,json
try:
    d=json.load(sys.stdin); z=d.get('zones') or d
    print((z[0].get('geo3') if isinstance(z[0],dict) else z[0]) if z else '')
except Exception: print('')" 2>/dev/null)"
  [ -n "$ZONA" ] || { echo "No pude determinar ninguna zona activa."; \
    echo "Indica una:  bash \$0 <geo3>"; exit 1; }
fi
printf '%s' "Servicio: $API   Zona: $ZONA"; echo

paso "Servicio"
if traer /health 20 && grep -q '"status"' "$JS"; then
  ok "/health responde"
else
  err "/health no responde"; FALLOS=$((FALLOS+1))
fi
if [ -n "$DC" ]; then
  if ( cd "$ENVDIR" && $DC exec -T ai-bridge python -c "import openaq" ) >/dev/null 2>&1; then
    ok "openaq.py dentro del contenedor"
  else
    err "openaq.py NO esta en la imagen: reconstruye con --no-cache"; FALLOS=$((FALLOS+1))
  fi
fi

paso "Analisis de la zona $ZONA"
if traer "/analysis/$ZONA" 240; then
  python3 - "$JS" <<'PY' || FALLOS=$((FALLOS+1))
import sys, json
try: d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as ex:
    print(f"   [ERROR] respuesta no JSON: {ex}")
    print("           " + open(sys.argv[1], encoding="utf-8", errors="replace").read()[:300])
    sys.exit(1)
if d.get("error"):
    print(f"   [ERROR] {d['error']}"); sys.exit(1)

# OJO: la clave correcta es 'statistics' (zone_statistics la llama 'metrics'
# por dentro, pero analyze_zone_full la publica como 'statistics').
st = d.get("statistics") or {}
# compute_caqi devuelve {"pm25": {...}, "pm10": {...}}, no una etiqueta plana
cq = (d.get("caqi") or {}).get("pm25") or {}
caqi_txt = f"{cq.get('label','-')}" + (f" ({cq.get('value')})" if cq.get("value") is not None else "")
print(f"   estado: {d.get('status')}   AQI: {d.get('air_quality_index')}"
      f"   CAQI: {caqi_txt}")
print(f"   generador: {d.get('generator')}")
print()
print(f"   {'metrica':<7} {'media':>9} {'ultimo':>9} {'n':>4}  {'tendencia':<12} confianza")
conf = d.get("confidence") or {}
for m in sorted(st):
    s = st[m]; c = conf.get(m) or {}
    et = c.get("label") or "-"
    fu = c.get("source")
    print(f"   {m:<7} {str(s.get('mean')):>9} {str(s.get('last_value')):>9} "
          f"{str(s.get('n')):>4}  {str(s.get('trend'))[:12]:<12} {et}"
          + (f" ({fu})" if fu else ""))
if not st:
    print("   [ERROR] ninguna metrica analizada"); sys.exit(1)

ag = d.get("aggregation") or {}
usa, exc = ag.get("sensors_used") or {}, ag.get("sensors_excluded_all_zero") or {}
print(f"\n   agregacion: {ag.get('agg')} entre sensores"
      f"   ({ag.get('sensors_seen')} sensores en la zona)")
if usa or exc:
    print(f"   {'metrica':<7} {'usados':>7} {'excluidos (leen 0)':>19}")
    for m in sorted(set(usa) | set(exc)):
        print(f"   {m:<7} {len(usa.get(m) or []):>7} {len(exc.get(m) or []):>19}")

sk = d.get("skipped_no_signal") or []
if sk: print(f"\n   sin senal (varianza 0): {', '.join(sk)}")

dis = d.get("discarded_metrics") or {}
if dis:
    print(f"\n   descartadas ({len(dis)}):")
    for m, det in sorted(dis.items()):
        print(f"      {m}  [{det.get('reason')}]  "
              f"{det.get('raw_points')} puntos -> {det.get('kept')} validos")
        if det.get("raw_median") is not None:
            print(f"         leido min {det.get('raw_min')} / mediana {det.get('raw_median')}"
                  f" / max {det.get('raw_max')}   valido {det.get('valid_range')}")
        if det.get("note"): print(f"         {det['note']}")

oc = d.get("official_comparison") or {}
prov = d.get("official_provider")
if oc:
    print(f"\n   contra estaciones oficiales ({prov}):")
    print(f"   {'metrica':<7} {'rho':>6} {'sesgo %':>9} {'n':>4}  etiqueta")
    for m, c in sorted(oc.items()):
        print(f"   {m:<7} {str(c.get('spearman_rho')):>6} {str(c.get('bias_pct')):>9} "
              f"{str(c.get('n')):>4}  {(conf.get(m) or {}).get('label','?')}")
else:
    print(f"\n   sin comparacion oficial (fuente: {prov or 'cams'})")

pc = d.get("pm_corrections") or {}
if pc:
    print("\n   correccion de PM:")
    for m, i in sorted(pc.items()):
        f = i.get("factor"); cl = i.get("corrected_last")
        ml = i.get("malm_corrected_last")
        linea = f"      {m}: "
        if f: linea += f"factor x{f} -> {cl}  "
        if ml is not None: linea += f"Malm(HR) -> {ml}"
        print(linea)

ep = d.get("episodes") or []
if ep:
    print(f"\n   episodios sostenidos: {len(ep)}")
    for e in ep[:4]:
        print(f"      {e}")

al = d.get("alerts") or []
print(f"\n   alertas activas: {len(al)}")
for a in al[:6]:
    print(f"      {a.get('parameter','?')}: {a.get('message','')}")
print("\n   [ok]   analisis leido correctamente")
PY
else
  FALLOS=$((FALLOS+1))
fi

paso "Reparto de referencias por zona"
if traer /official-sources 240; then
  python3 - "$JS" <<'PY' || FALLOS=$((FALLOS+1))
import sys, json
try: d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as ex:
    print(f"   [ERROR] respuesta no JSON: {ex}"); sys.exit(1)
if d.get("error"):
    print(f"   [ERROR] {d['error']}"); sys.exit(1)
zs = d.get("zones") or []
cuenta = {}
print(f"   {'zona':<6} {'tier':<5} {'fuente':<9} estaciones")
for z in zs:
    p = z.get("provider") or "cams"
    cuenta[p] = cuenta.get(p, 0) + 1
    print(f"   {z.get('geo3',''):<6} {str(z.get('tier','')):<5} {p:<9} "
          f"{', '.join(str(x) for x in (z.get('stations') or [])[:3])}")
oficial = cuenta.get("euskadi", 0) + cuenta.get("openaq", 0)
print(f"\n   resumen: " + ", ".join(f"{k}={v}" for k, v in sorted(cuenta.items())))
print(f"   zonas con estaciones REALES: {oficial} de {len(zs)}")
PY
else
  FALLOS=$((FALLOS+1))
fi

if [ "$BREVE" = "0" ]; then
  paso "Tabla de sensores"
  if traer /sensors 180; then
    python3 - "$JS" <<'PY' || FALLOS=$((FALLOS+1))
import sys, json
try: d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as ex:
    print(f"   [ERROR] respuesta no JSON: {ex}"); sys.exit(1)
rows = d.get("sensors") or []
cols = ("pm25","pm10","no2","o3","nh3","co","db")
print(f"   {'sensor':<17}" + "".join(f"{c:>9}" for c in cols) + "   fuera_rango")
vacios = 0
for r in sorted(rows, key=lambda x: str(x.get("name"))):
    def f(c):
        v = r.get(c)
        return "-" if v is None else (f"{v:g}" if isinstance(v,(int,float)) else str(v))
    fl = ",".join(sorted((r.get("flags") or {}).keys())) or ""
    vals = [r.get(c) for c in cols]
    if all(v is None for v in vals): vacios += 1
    print(f"   {str(r.get('name'))[:17]:<17}" + "".join(f"{f(c):>9}" for c in cols)
          + (f"   {fl}" if fl else ""))
print(f"\n   sensores sin ninguna metrica de la tabla: {vacios}")
print("   ('-' = el nodo no lleva ese sensor)")
PY
  else
    FALLOS=$((FALLOS+1))
  fi
fi

paso "Resultado"
if [ "$FALLOS" -eq 0 ]; then
  ok "todo consultado sin errores"
else
  err "$FALLOS comprobacion(es) con problemas"
fi
exit "$FALLOS"
