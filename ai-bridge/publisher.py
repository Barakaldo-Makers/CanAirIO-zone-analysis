# SPDX-License-Identifier: GPL-3.0-or-later
#
# canairio-zone-analysis — exports analysis results as JSON and publishes them to a git repo
# Copyright (C) 2026 Barakaldo Makers
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
publisher.py — Exporta el estado y el histórico de análisis a JSON y los
publica en un repo de GitHub Pages (git commit + push).

Lo usa el ai-bridge tras cada ciclo. No añade carga de servir tráfico:
el servidor solo escribe archivos y hace push; GitHub Pages los sirve.

Variables de entorno relevantes:
  PUBLISH_ENABLED   "1" para activar (por defecto "0")
  PUBLISH_DIR       carpeta del repo git montada (p.ej. /site)
  GIT_REMOTE        url remota (https con token o ssh) — opcional si ya está
  GIT_AUTHOR        "Nombre <email>" para los commits
"""
import os
import json
import time
import logging
import subprocess
from datetime import datetime, timezone

log = logging.getLogger("publisher")

PUBLISH_ENABLED = os.getenv("PUBLISH_ENABLED", "0") == "1"
PUBLISH_DIR = os.getenv("PUBLISH_DIR", "/site")
GIT_AUTHOR = os.getenv("GIT_AUTHOR", "ai-bridge <bot@localhost>")
HISTORY_DAYS = int(os.getenv("PUBLISH_HISTORY_DAYS", "30"))


def _run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        log.warning("git %s -> %s", " ".join(cmd[1:]), p.stderr.strip()[:200])
    return p.returncode == 0


def export_json(state_zones, history_rows, meta=None):
    """
    Escribe data/state.json y data/history.json en PUBLISH_DIR.
    state_zones: lista de dicts (estado actual por zona).
    history_rows: lista de dicts (puntos del histórico para el timeline).
    """
    if not PUBLISH_ENABLED:
        return False
    data_dir = os.path.join(PUBLISH_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    state = {"generated_at": now, "zones": state_zones, "meta": meta or {}}
    history = {"generated_at": now, "days": HISTORY_DAYS, "points": history_rows}
    try:
        with open(os.path.join(data_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(data_dir, "history.json"), "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
        return True
    except OSError as e:
        log.error("no pude escribir JSON: %s", e)
        return False


def export_sensors(sensor_rows, ranges=None):
    """
    Escribe data/sensors.json en PUBLISH_DIR: lista completa de estaciones
    CanAirIO con su última lectura reciente.
    sensor_rows: lista de dicts (una por estación).
    ranges: {metrica: [min, max]} con los rangos físicos válidos. La web los
    usa para explicar por qué una lectura está marcada como fuera de rango;
    se exportan en vez de escribirlos a mano en el HTML para que no puedan
    desfasarse de METRICS en app.py.
    """
    if not PUBLISH_ENABLED:
        return False
    data_dir = os.path.join(PUBLISH_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    payload = {"generated_at": now, "count": len(sensor_rows),
               "ranges": ranges or {}, "sensors": sensor_rows}
    try:
        with open(os.path.join(data_dir, "sensors.json"), "w",
                  encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        return True
    except OSError as e:
        log.error("no pude escribir sensors.json: %s", e)
        return False


def git_publish():
    """git add + commit + push en PUBLISH_DIR. Silencioso si no hay cambios."""
    if not PUBLISH_ENABLED:
        return False
    d = PUBLISH_DIR
    if not os.path.isdir(os.path.join(d, ".git")):
        log.warning("PUBLISH_DIR %s no es un repo git; omito push", d)
        return False
    name, _, email = GIT_AUTHOR.partition(" <")
    email = email.rstrip(">") or "bot@localhost"
    _run(["git", "config", "user.name", name.strip() or "ai-bridge"], d)
    _run(["git", "config", "user.email", email], d)
    _run(["git", "add", "-A"], d)
    # commit solo si hay cambios staged
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=d).returncode == 0:
        return True  # nada que publicar
    msg = "update air-quality data " + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    _run(["git", "commit", "-m", msg], d)
    # reconcilia con el remoto ANTES de empujar, para evitar rechazos
    # 'fetch first' cuando el repo remoto tiene commits que el local no
    # (p.ej. ediciones del index.html hechas directamente en GitHub).
    # -X ours: ante conflicto, prioriza la versión local (datos recién generados).
    _run(["git", "pull", "--no-rebase", "-X", "ours", "origin", "main"], d)
    ok = _run(["git", "push"], d)
    if not ok:
        # último intento: re-reconcilia y reempuja una vez más
        _run(["git", "pull", "--no-rebase", "-X", "ours", "origin", "main"], d)
        ok = _run(["git", "push"], d)
    if ok:
        log.info("[publisher] datos publicados en GitHub Pages")
    else:
        log.warning("[publisher] push fallido tras reconciliar; reintentara "
                    "en el proximo ciclo")
    return ok
