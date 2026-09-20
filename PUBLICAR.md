# Cómo publicar este repositorio

Pasos para el primer push. Todo el contenido ya está preparado y auditado.

## 1. Auditoría (obligatoria antes del primer push)

```bash
bash scripts/pre-publish-check.sh
```

Debe terminar en `listo para publicar`. Busca tokens, claves privadas,
contraseñas literales, `.env` dentro del árbol, rutas personales, la licencia,
que todo compile y que los enlaces del README resuelvan.

> En un repositorio público el historial es **permanente**. Si una credencial
> llega a subirse, borrarla después no sirve: hay que rotarla.

## 2. Crear el repositorio en GitHub

En <https://github.com/organizations/Barakaldo-Makers/repositories/new>:

- **Nombre:** `canairio-zone-analysis`
- **Descripción:** *Rigorous statistical analysis of CanAirIO air-quality data,
  validated against official reference stations*
- **Visibilidad:** Público
- **No marques** «Add a README», «Add .gitignore» ni «Choose a license»: ya
  están en el árbol y chocarían con el primer push.

## 3. Primer push

```bash
cd <ruta de este árbol>

git init -b main
git add .
git status          # revisa la lista: no debe aparecer .env

git commit -m "canairio-zone-analysis 1.0.0

Motor de análisis estadístico para datos de CanAirIO, validado contra
estaciones oficiales de referencia (red vasca, OpenAQ, CAMS).

Extraído del despliegue de Barakaldo Makers. Las correcciones de
validación que motivan el proyecto están en docs/VALIDACION.md."

git remote add origin git@github.com:Barakaldo-Makers/canairio-zone-analysis.git
git push -u origin main
```

Si usas HTTPS en vez de SSH, la URL es
`https://github.com/Barakaldo-Makers/canairio-zone-analysis.git`.

## 4. Ajustes en GitHub, después del push

**Topics** (rueda dentada junto a «About»), que es como te encuentra la gente:

```
canairio  air-quality  air-pollution  iot  sensors  influxdb
openaq  environmental-monitoring  citizen-science  python  docker
```

**About:** pon la descripción y, si tienes web pública, su URL.

**Releases:** crea la `v1.0.0` desde `CHANGELOG.md`.

**Desactiva** lo que no vayas a atender: Wikis y Projects. Deja Issues abiertos.

## 5. Darlo a conocer

Lo más útil es contarlo donde está la comunidad que puede usarlo:

- Un issue en [CanAirIO](https://github.com/kike-canaries/canairio_firmware)
  presentando el proyecto. El hallazgo de los nodos sin sensor de PM
  publicando `0` les interesa directamente: afecta a cualquiera que agregue
  datos de varios nodos.
- El foro/Telegram de CanAirIO.
- [OpenAQ](https://openaq.org) recoge proyectos que usan su API.

Si mencionas el proyecto, el gancho no es «otro dashboard de calidad del aire»
—hay muchos— sino **la validación contra cabinas oficiales y lo que encontró**:
una zona publicando 0,33 µg/m³ cuando el valor real era 5,36.

## 6. Después

- `docs/VALIDACION.md` es lo que hace este proyecto distinto. Si encuentras más
  fallos de este tipo, añádelos: es el documento que convence.
- Las aportaciones más útiles son **redes oficiales nuevas**
  (`CONTRIBUTING.md` explica el contrato, unas 200 líneas por red).
- Si renombras el proyecto, cambia también el nombre en las cabeceras SPDX de
  `ai-bridge/*.py` y `scripts/*.sh`.
