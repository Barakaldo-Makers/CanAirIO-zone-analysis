#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# CanAirIO-zone-analysis — estado de la deriva, la acústica y el histórico
# Copyright (C) 2026 Barakaldo Makers
#
# =====================================================================
#  SOLO LECTURA. No instala, no reconstruye, no toca ningún fichero.
#
#  Sustituye a los '--verify' de los parche-*.sh, que quedaron obsoletos
#  cuando la respuesta de /drift pasó a tener la forma
#  {basis, bases_available, metrics} y revientan con KeyError.
#
#  AVISO: si todavía tienes parche-*.sh por ahí, bórralos. Sus payloads
#  llevan versiones de app.py de septiembre, así que ejecutar uno ahora
#  DEGRADARÍA el servidor. Las copias de seguridad (backup-*) sí merece
#  la pena conservarlas.
#
#  Uso:  bash comprobar-deriva.sh [ZONA]
# =====================================================================
set -uo pipefail
API="${API:-http://localhost:5000}"
ZONA="${1:-}"

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi
paso(){ printf '\n%s== %s ==%s\n' "$B" "$1" "$N"; }
avi(){  printf '   %s[aviso]%s %s\n' "$Y" "$N" "$1"; }
err(){  printf '   %s[ERROR]%s %s\n' "$R" "$N" "$1"; }

curl -s --max-time 10 "$API/health" >/dev/null 2>&1 \
  || { err "no responde $API — ¿está arrancado ai-bridge?"; exit 1; }

if [ -z "$ZONA" ]; then
  ZONA="$(curl -s --max-time 20 "$API/zones" 2>/dev/null | python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
z = d.get("zones") or d.get("active") or []
if z: print(z[0] if isinstance(z[0], str) else (z[0].get("geo3") or ""))' 2>/dev/null)"
fi
[ -n "$ZONA" ] || { err "no pude determinar zona; pásala: bash $0 ezt"; exit 1; }
echo "Zona: $ZONA   API: $API"

J="$(mktemp)"; trap 'rm -f "$J"' EXIT

# ─────────────────────────────────────────────────────────────────────
paso "Deriva del factor de corrección (regla α-de-W)"
curl -s --max-time 120 "$API/drift/$ZONA" > "$J" 2>/dev/null
if [ ! -s "$J" ]; then avi "sin respuesta de /drift/$ZONA"; else
python3 - "$J" <<'PY'
import sys, json, textwrap
d = json.load(open(sys.argv[1], encoding="utf-8"))
if "error" in d:
    print("   ERROR:", d["error"]); sys.exit(0)
print("   activo=%s  ventana=%s d  alpha=%s  minimo=%s d  k=%s sigma" % (
    d.get("enabled"), d.get("window_days"), d.get("alpha"),
    d.get("min_days"), d.get("k_sigmas")))
z = d.get("zones") or {}
if not z:
    print("\n   Sin historial de factores todavía."); sys.exit(0)
for g, info in sorted(z.items()):
    print("\n   %s · base usada: %s" % (g, info.get("basis", "?")))
    disp = info.get("bases_available") or {}
    if disp:
        print("      puntos por base: " + ", ".join("%s=%s" % kv
                                                    for kv in sorted(disp.items())))
    if info.get("note"):
        for l in textwrap.wrap(info["note"], 78):
            print("      " + l)
    for k, v in sorted((info.get("metrics") or {}).items()):
        if v.get("status") == "sin_historial_suficiente":
            print("      %-5s %s/%s días" % (k, v.get("days"), v.get("days_needed")))
        else:
            print("      %-5s %-14s x%s -> x%s (%+.1f %%)  fuera %s/%s  banda %s" % (
                k, v.get("status"), v.get("baseline_factor"),
                v.get("recent_factor"), v.get("change_pct", 0),
                v.get("days_out_of_band"), v.get("window"), v.get("band")))
        for l in textwrap.wrap(v.get("note", ""), 76):
            print("            " + l)
        cb = v.get("cobertura")
        if cb:
            print("            cobertura: %s días, horas emparejadas "
                  "med/min/max %s/%s/%s" % (
                      cb.get("dias_con_dato"), cb.get("horas_emparejadas_mediana"),
                      cb.get("horas_emparejadas_min"), cb.get("horas_emparejadas_max")))
            if cb.get("rho_horas_vs_desviacion") is not None:
                print("            rho(horas, desviación) = %s  p = %s  -> %s" % (
                    cb["rho_horas_vs_desviacion"], cb.get("p_value"),
                    cb.get("veredicto")))
            for l in textwrap.wrap(cb.get("nota", ""), 74):
                print("              " + l)
PY
fi

# ─────────────────────────────────────────────────────────────────────
paso "Historial de cabina (requisito de TPB-D)"
curl -s --max-time 120 "$API/ref-history" > "$J" 2>/dev/null
if [ ! -s "$J" ]; then avi "sin respuesta de /ref-history"; else
python3 - "$J" <<'PY'
import sys, json, datetime as dt
d = json.load(open(sys.argv[1], encoding="utf-8"))
if "error" in d:
    print("   ERROR:", d["error"]); sys.exit(0)
filas = [(v["complete_days"], v["hours"], g, m)
         for g, mm in (d.get("zones") or {}).items() for m, v in mm.items()]
if not filas:
    print("   vacío todavía."); sys.exit(0)
filas.sort(reverse=True)
MIN = d.get("tpbd_min_days", 24)
print("   %-13s %5s %6s  %s" % ("zona/métrica", "días", "horas", "TPB-D"))
for dias, horas, g, m in filas[:10]:
    est = "SI" if dias >= MIN else "faltan %d" % (MIN - dias)
    print("   %-13s %5d %6d  %s" % (g + "/" + m, dias, horas, est))
o3 = [f for f in filas if f[3] == "o3"]
if o3:
    print("\n   Solo O3, que es donde TPB-D tiene sentido (la tesis dice que")
    print("   PM2.5 es demasiado irregular para el método):")
    hoy = dt.date.today()
    for dias, horas, g, _m in o3:
        if dias >= 40:
            nota = "margen cómodo"
        elif dias >= MIN:
            nota = "posible, pero beta=%.2f es estrecho" % (24 / dias)
        else:
            nota = "40 días el %s" % (hoy + dt.timedelta(days=40 - dias)).strftime("%d/%m")
        print("      %-8s %3d días  %5d horas   %s" % (g, dias, horas, nota))
listas = d.get("tpbd_ready") or []
print("\n   Listas ya: %s" % (", ".join(listas) if listas else "ninguna"))
PY
fi

# ─────────────────────────────────────────────────────────────────────
paso "Acústica"
curl -s --max-time 120 "$API/acustica/$ZONA" > "$J" 2>/dev/null
if [ ! -s "$J" ]; then avi "sin respuesta de /acustica/$ZONA"; else
python3 - "$J" "$ZONA" <<'PY'
import sys, json, textwrap
d = json.load(open(sys.argv[1], encoding="utf-8"))
if "error" in d:
    print("   ERROR:", d["error"]); sys.exit(0)
inf = (d.get("zones") or {}).get(sys.argv[2])
if not inf:
    print("   sin informe para esta zona."); sys.exit(0)
if not inf.get("disponible"):
    print("   no disponible:", (inf.get("nota") or "")[:150]); sys.exit(0)
print("   campos detectados:", inf.get("campos_detectados"))
print("\n   %-18s %6s %6s %6s %6s %6s  %-20s %s" % (
    "nodo", "Lden", "Ld", "Le", "Ln", "L90", "RD 1367", "OMS"))
for _mac, n in sorted((inf.get("nodos") or {}).items(),
                      key=lambda x: x[1].get("name") or ""):
    i = n.get("indicadores") or {}
    g = lambda k: (i.get(k) or {}).get("valor")
    v = [("%6.1f" % g(k)) if g(k) is not None else "     -"
         for k in ("lden", "ld", "le", "ln", "l90")]
    print("   %-18s %s %s %s %s %s  %-20s %s" % (
        n.get("name"), *v, (n.get("rd_1367") or {}).get("estado"),
        (n.get("oms_2018") or {}).get("estado")))
dg = inf.get("diagnostico") or {}
print("\n   recorridos entre nodos (dB): %s" % dg.get("recorridos_db"))
if dg.get("recorridos_db_comunes"):
    print("   solo nodos con L90 y Lden a la vez: %s" % dg["recorridos_db_comunes"])
if dg.get("sin_l90"):
    print("   sin L90: %s" % ", ".join(dg["sin_l90"]))
print("   CAUSA: %s" % dg.get("causa"))
for l in textwrap.wrap(dg.get("nota", ""), 76):
    print("     " + l)
PY
fi

# ─────────────────────────────────────────────────────────────────────
paso "Alertas"
V="$(curl -s --max-time 10 "$API/health" >/dev/null 2>&1; \
     printf '%s' "${ALERT_PERSIST_H:-}")"
echo "   La persistencia exige ALERT_PERSIST_H > 1 para entrar."
echo "   Compruébalo dentro del contenedor:"
echo "       docker compose exec -T ai-bridge sh -c 'echo \$ALERT_PERSIST_H'"
echo "   Si sale 1 o vacío, mira el .env y el docker-compose.yml."

paso "Hecho"
echo "   Todo lo de arriba es de solo lectura."
echo "   Si tienes parche-*.sh en el home, bórralos: sus payloads son de"
echo "   septiembre y ejecutar uno ahora degradaría el servidor."
exit 0
