#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# CanAirIO-zone-analysis — pre-publication audit
# Copyright (C) 2026 Barakaldo Makers
#
# =====================================================================
#  Comprueba que el árbol está limpio ANTES del primer push público.
#  Un secreto subido a un repo público queda en el historial para
#  siempre, aunque borres el fichero después: hay que rotarlo.
#
#  Uso:  bash scripts/pre-publish-check.sh
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi
paso(){ printf '\n%s== %s ==%s\n' "$B" "$1" "$N"; }
ok(){ printf '   %s[ok]%s   %s\n' "$G" "$N" "$1"; }
avi(){ printf '   %s[aviso]%s %s\n' "$Y" "$N" "$1"; }
err(){ printf '   %s[FALLO]%s %s\n' "$R" "$N" "$1"; }
FALLOS=0; AVISOS=0

# qué ficheros se subirían de verdad
if [ -d .git ]; then
  FICHEROS="$(git ls-files 2>/dev/null)"
  MODO="seguimiento de git"
else
  FICHEROS="$(find . -type f -not -path './.git/*' | sed 's|^\./||')"
  MODO="todo el árbol (aún sin git init)"
fi
echo "Auditando: $MODO"
N_FICH="$(printf '%s\n' "$FICHEROS" | grep -c . || true)"
echo "Ficheros: $N_FICH"

paso "1. Secretos"
# patrones de credenciales reales, no de plantillas
PAT='([0-9]{9,10}:AA[A-Za-z0-9_-]{30,})|(ghp_[A-Za-z0-9]{30,})|(github_pat_[A-Za-z0-9_]{50,})|(sk-[A-Za-z0-9]{32,})|(AKIA[0-9A-Z]{16})|(-----BEGIN [A-Z ]*PRIVATE KEY-----)'
HIT=""
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in LICENSE) continue ;; esac
  if grep -qEI "$PAT" "$f" 2>/dev/null; then
    HIT="$HIT$f\n"
  fi
done <<< "$FICHEROS"
if [ -n "$HIT" ]; then
  err "posibles credenciales:"; printf "$HIT" | sed 's/^/         /'
  echo "         Si alguna es real: ROTALA, no basta con borrar el fichero."
  FALLOS=$((FALLOS+1))
else
  ok "sin tokens ni claves privadas"
fi

# contraseñas asignadas con valor literal (no placeholders ni variables)
# Busca asignaciones de credenciales con valor literal.
# El prefijo [A-Za-z_-]* captura db_password, api_token, mi-secret ('_' no es
# frontera de palabra); -i porque los nombres suelen ir en minúscula.
# Se excluyen siempre: plantillas (CHANGE_ME, your_, example) y lecturas de
# variables de entorno.
# Y SOLO en código fuente se excluyen además los valores que son un
# identificador sin comillas, porque ahí son referencias a una variable.
# En un fichero de configuración una palabra suelta SÍ es el valor,
# así que en esos no se excluye.
_busca() {
  grep -HnEIi '[A-Za-z_-]*(PASSWORD|PASSWD|PASS|SECRET|API_?KEY|TOKEN|CREDENTIALS?)[[:space:]]*[=:][[:space:]]*"?'"'"'?[^$#[:space:]"'"'"']{6,}' "$1" 2>/dev/null \
    | grep -vEi 'CHANGE_ME|CAMBIA|your_|example|placeholder|xxx|\$\{|<.*>|\.example' \
    | grep -vE 'getenv|environ|process\.env|os\.env'
}
PW="$(printf '%s\n' "$FICHEROS" | while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in
    *.py|*.js|*.ts|*.go|*.java|*.rb|*.rs|*.c|*.cpp|*.h)
      _busca "$f" | grep -vE '[=:][[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*($|[,)]|or |and )' ;;
    *) _busca "$f" ;;
  esac
done || true)"
if [ -n "$PW" ]; then
  err "valores que parecen credenciales literales:"
  printf '%s\n' "$PW" | sed 's/^/         /' | head -12
  FALLOS=$((FALLOS+1))
else
  ok "sin contraseñas literales"
fi

paso "2. Ficheros que nunca deben subirse"
MALOS=""
for p in .env .gitconfig; do
  printf '%s\n' "$FICHEROS" | grep -qx "$p" && MALOS="$MALOS $p"
done
printf '%s\n' "$FICHEROS" | grep -qE '(^|/)\.env\.[^e]' && MALOS="$MALOS .env.*"
printf '%s\n' "$FICHEROS" | grep -qE '(^|/)(site|backup-)' && MALOS="$MALOS datos-generados"
if [ -n "$MALOS" ]; then
  err "en el árbol:$MALOS"; FALLOS=$((FALLOS+1))
else
  ok ".env, .gitconfig y datos generados fuera del repo"
fi
if [ -f .gitignore ] && grep -q '^\.env$' .gitignore; then
  ok ".gitignore cubre .env"
else
  err ".gitignore no excluye .env"; FALLOS=$((FALLOS+1))
fi

paso "3. Rutas y datos del despliegue original"
PERS="$(printf '%s\n' "$FICHEROS" | while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in docs/*|*.md) continue ;; esac
  grep -HnE '/home/[a-z][a-z0-9_-]*|[a-z0-9._-]+@[a-z0-9.-]+\.[a-z]{2,}' "$f" 2>/dev/null
done | grep -vE 'bot@localhost|noreply@|example\.(com|org)|@BotFather' || true)"
if [ -n "$PERS" ]; then
  avi "rutas personales o correos en código:"
  printf '%s\n' "$PERS" | sed 's/^/         /' | head -8
  AVISOS=$((AVISOS+1))
else
  ok "sin rutas personales ni correos en el código"
fi

paso "4. Licencia"
if [ -f LICENSE ] && grep -q "GNU GENERAL PUBLIC LICENSE" LICENSE \
   && grep -q "Version 3" LICENSE; then
  ok "LICENSE es la GPL-3.0 completa ($(wc -l < LICENSE) líneas)"
else
  err "falta LICENSE o no es la GPL-3.0"; FALLOS=$((FALLOS+1))
fi
SIN=""
for f in ai-bridge/*.py scripts/*.sh; do
  [ -f "$f" ] || continue
  grep -q "SPDX-License-Identifier" "$f" || SIN="$SIN $f"
done
if [ -n "$SIN" ]; then
  avi "fuentes sin cabecera SPDX:$SIN"; AVISOS=$((AVISOS+1))
else
  ok "todas las fuentes llevan cabecera SPDX"
fi

paso "5. El proyecto arranca"
for f in ai-bridge/app.py ai-bridge/euskadi.py ai-bridge/openaq.py ai-bridge/publisher.py; do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$f" \
    || { err "$f no compila"; FALLOS=$((FALLOS+1)); }
done
ok "los módulos Python compilan"
for f in scripts/*.sh; do bash -n "$f" || { err "$f no compila"; FALLOS=$((FALLOS+1)); }; done
ok "los scripts compilan"
if python3 -c "import yaml,sys; yaml.safe_load(open('docker-compose.yml',encoding='utf-8'))" 2>/dev/null; then
  ok "docker-compose.yml es YAML válido"
else
  avi "no pude validar el YAML (¿falta pyyaml?)"; AVISOS=$((AVISOS+1))
fi
for f in README.md README.es.md CONTRIBUTING.md CHANGELOG.md .env.example docs/README.md; do
  [ -s "$f" ] || { err "falta $f"; FALLOS=$((FALLOS+1)); }
done
ok "documentación mínima presente"

paso "6. Enlaces internos del README"
ROTOS=""
for md in README.md README.es.md docs/README.md CONTRIBUTING.md; do
  [ -f "$md" ] || continue
  d="$(dirname "$md")"
  grep -oE '\]\(([^)#:]+\.md|LICENSE|[^)#:]+\.py)\)' "$md" 2>/dev/null \
    | sed 's/^](//;s/)$//' | while IFS= read -r t; do
      [ -e "$d/$t" ] || echo "$md -> $t"
    done
done > /tmp/_rotos 2>/dev/null
ROTOS="$(cat /tmp/_rotos 2>/dev/null)"; rm -f /tmp/_rotos
if [ -n "$ROTOS" ]; then
  err "enlaces rotos:"; printf '%s\n' "$ROTOS" | sed 's/^/         /'; FALLOS=$((FALLOS+1))
else
  ok "los enlaces internos resuelven"
fi

paso "Resultado"
if [ "$FALLOS" -eq 0 ] && [ "$AVISOS" -eq 0 ]; then
  ok "listo para publicar"
elif [ "$FALLOS" -eq 0 ]; then
  avi "$AVISOS aviso(s); revísalos, pero no bloquean"
  ok "sin fallos que impidan publicar"
else
  err "$FALLOS fallo(s) y $AVISOS aviso(s): NO publiques todavía"
fi
echo
echo "   Recuerda: en un repo público el historial es permanente."
echo "   Si alguna credencial llegó a subirse, rótala aunque la borres."
exit "$FALLOS"
