#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# CanAirIO-zone-analysis — sonda del metadato de la EEA
# Copyright (C) 2026 Barakaldo Makers
#
# =====================================================================
#  SOLO LECTURA. No instala, no escribe en el proyecto, no toca Docker.
#  Descarga el metadato de sampling points de la EEA a /tmp y dice si
#  sirve: codigo HTTP, formato, cabecera y columnas que necesita eea.py.
#
#  Uso:  bash sonda-eea.sh
# =====================================================================
set -uo pipefail
URL="${EEA_METADATA_URL:-https://discomap.eea.europa.eu/App/AQViewer/download?fqn=Airquality_Dissem.b2g.measurements&f=csv}"
UA="${EEA_USER_AGENT:-CanAirIO-zone-analysis/1.5 (+https://github.com/Barakaldo-Makers)}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "URL: $URL"
echo

echo "== 1. Sin User-Agent (lo que fallo con 403) =="
curl -s -o /dev/null -A "" -w '   HTTP %{http_code}   %{content_type}\n' --max-time 60 "$URL"

echo
echo "== 2. Con User-Agent =="
COD=$(curl -sS -o "$TMP/m.bin" -A "$UA" -H 'Accept: */*' \
      -w '%{http_code}' --max-time 300 -L "$URL" 2>"$TMP/err" || true)
echo "   HTTP $COD   $(wc -c < "$TMP/m.bin" 2>/dev/null || echo 0) bytes"
[ -s "$TMP/err" ] && echo "   curl: $(tr -d '\n' < "$TMP/err")"
if [ "$COD" != "200" ] || [ ! -s "$TMP/m.bin" ]; then
  echo
  echo "   NO DESCARGA. Dos salidas, por orden:"
  echo "   a) prueba el visor en un navegador para ver la URL vigente:"
  echo "      https://discomap.eea.europa.eu/App/AQViewer/index.html?fqn=Airquality_Dissem.b2g.measurements"
  echo "      y pon la que salga en EEA_METADATA_URL del .env"
  echo "   b) si no aparece, fija los sampling points a mano en el .env:"
  echo "      EEA_FORCE_SPOS=ES:SP_xxxx,ES:SP_yyyy"
  echo "      (eea.py funciona sin metadato con esa opcion)"
  exit 1
fi

echo
echo "== 3. Formato =="
MAGIA=$(head -c 2 "$TMP/m.bin")
if [ "$MAGIA" = "PK" ]; then
  echo "   ZIP. Contenido:"
  python3 -c "
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for i in z.infolist()[:10]:
    print('      %-55s %9d bytes' % (i.filename, i.file_size))
csvs = [n for n in z.namelist() if n.lower().endswith('.csv')]
if not csvs:
    print('   SIN .csv DENTRO -> eea.py no lo entendera'); sys.exit(1)
open(sys.argv[2], 'wb').write(z.read(csvs[0]))
" "$TMP/m.bin" "$TMP/m.csv" || exit 1
else
  echo "   CSV pelado (no ZIP). eea.py tambien lo admite."
  cp "$TMP/m.bin" "$TMP/m.csv"
fi

echo
echo "== 4. Columnas que necesita eea.py =="
python3 -c "
import csv, sys
ruta = sys.argv[1]
with open(ruta, newline='', encoding='utf-8-sig', errors='replace') as f:
    pri = f.readline()
    delim = '\t' if pri.count('\t') >= pri.count(',') else ','
    f.seek(0)
    rd = csv.reader(f, delimiter=delim)
    cab = next(rd)
    fila = next(rd, [])
norm = lambda s: ''.join(c for c in (s or '').lower() if c.isalnum())
idx = {norm(c): c for c in cab}
print('   delimitador: %r   columnas: %d' % (delim, len(cab)))
pide = [('spo', ['SamplingPointId','SamplingPoint']),
        ('lat', ['Latitude']), ('lon', ['Longitude']),
        ('pais', ['ISO','Countrycode','Country']),
        ('eoi', ['AirQualityStationEoICode','EoICode']),
        ('contaminante (opcional)', ['AirPollutant','AirPollutantCode'])]
falta = []
for que, alias in pide:
    hit = next((idx[norm(a)] for a in alias if norm(a) in idx), None)
    print('   %-24s %s' % (que, hit or 'NO ESTA'))
    if not hit and 'opcional' not in que:
        falta.append(que)
print()
if fila:
    print('   primera fila:')
    for c, v in list(zip(cab, fila))[:10]:
        print('      %-30s %s' % (c, (v or '')[:40]))
print()
print('   VEREDICTO: ' + ('sirve tal cual' if not falta else
      'FALTAN columnas imprescindibles: ' + ', '.join(falta)))
sys.exit(1 if falta else 0)
" "$TMP/m.csv"
