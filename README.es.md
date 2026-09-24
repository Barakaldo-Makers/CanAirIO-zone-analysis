# canairio-zone-analysis

**Análisis estadístico riguroso de datos de [CanAirIO](https://canair.io),
validado contra estaciones oficiales de referencia.**

[![Licencia: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

*[Read this in English](README.md)*

---

Los sensores de bajo coste son baratos como para ponerlos en todas partes, y
ese es justo su valor. Pero un número sin margen de error no es una medida.
Este proyecto coge el flujo crudo de una red CanAirIO y responde a una pregunta
más difícil que «¿qué dice el sensor?»:

> **¿Cuánto hay que creerle, y comparado con qué?**

Lee los datos horarios de InfluxDB, los limpia, les aplica estadística no
paramétrica y valida cada métrica contra la mejor referencia disponible en esa
posición: una **cabina oficial cercana** si la hay, y el modelo CAMS/Copernicus
si no. Cada métrica sale etiquetada `fiable` / `dudoso` / `no_fiable` con la
correlación y el sesgo que le han dado esa etiqueta.

## Por qué existe

Lo construimos para Barakaldo Makers y encontró cosas que no sabíamos de
nuestra propia red. El ejemplo más claro:

| | antes | después |
|---|---|---|
| PM2.5 de la zona | **0,33 µg/m³** | **5,36 µg/m³** |
| vs cabina oficial | rho 0,39, sesgo −95 % | rho 0,64, sesgo −19 % |

Seis de nuestros nueve nodos no llevaban sensor de PM —un módulo de ruido y
varios nodos de gases— y el firmware de CanAirIO publica `0` en las métricas
cuyo sensor no está conectado, en vez de omitir el campo. Esos ceros
acumulaban el **93 % de los puntos**, y un `mean()` normal sobre la zona ahogaba
a los tres nodos que sí medían. La zona llevaba semanas publicando la
dieciseisava parte del valor real, sin que nada lo indicara.

Ese tipo de fallo es invisible si no tienes una referencia contra la que
contrastar. Es la razón de ser del proyecto, y `docs/VALIDACION.md` documenta
otros cinco del mismo estilo.

## Qué hace

- **Agregación de zona en dos pasos.** Primero la media horaria de *cada
  sensor*, después la **mediana entre sensores** — así la frecuencia de
  publicación no pondera el resultado y un nodo descalibrado no puede arrastrar
  la zona. Los sensores que leen exactamente `0` en toda la ventana se
  excluyen: no llevan ese sensor.
- **Cadena de referencia oficial.** Para cada zona (o cada sensor individual)
  se calcula su posición real y se busca una cabina de referencia a menos de
  25 km: primero una red local, luego [OpenAQ](https://openaq.org) a escala
  mundial, y CAMS/Copernicus solo como último recurso.
- **Etiquetas de confianza por métrica,** a partir de la correlación de
  Spearman y el sesgo relativo contra esa referencia, tomando el *mejor
  veredicto* y no simplemente la mejor correlación.
- **Factores de corrección** derivados de la cabina oficial, con una jerarquía
  clara para que solo se publique una cifra corregida.
- **Estadística no paramétrica:** Mann-Kendall, pendiente de Theil-Sen,
  intervalos de confianza del 95 %, descarte de atípicos por MAD. Sin suponer
  normalidad.
- **Consenso entre sensores:** cada nodo contra la mediana de sus vecinos, que
  es como se detecta una unidad que ha derivado en silencio.
- **Rosa de contaminación, CAQI, episodios sostenidos, perfiles horarios** y
  correlación con lluvia y tendencia de presión.
- **Nada desaparece en silencio.** Cualquier métrica que no llegue al análisis
  se reporta en `discarded_metrics` con el motivo y las cifras.
- **Avisos por Telegram** opcionales, con persistencia e histéresis, y salida
  en **JSON estático** para una web.

Los informes los genera un **motor de reglas determinista**, no un LLM. En el
hardware para el que se construyó (una CPU antigua sin AVX) un modelo local
tardaba 4,4 segundos por token: entre 30 y 45 minutos por informe. El motor de
reglas es instantáneo, reproducible y no puede inventarse una medida.

## Arquitectura

```
  red CanAirIO ──► InfluxDB ──► motor de análisis ──┬──► JSON  (web estática)
                                                     ├──► InfluxDB (histórico)
                                                     └──► Telegram (avisos)
                                        ▲
                estaciones oficiales ───┤
                CAMS / Copernicus ──────┤
                Open-Meteo (meteo) ─────┘
```

Un único servicio Flask (`ai-bridge`) hace el análisis y expone una API HTTP de
solo lectura. `docs/ARQUITECTURA.md` tiene el pipeline completo, función a
función.

## Puesta en marcha

Necesitas Docker y un InfluxDB 1.x con datos de CanAirIO (measurement
`fixed_stations_01`, tags `geo3` y `mac`).

```bash
git clone https://github.com/Barakaldo-Makers/canairio-zone-analysis.git
cd canairio-zone-analysis

cp .env.example .env
$EDITOR .env          # contraseñas, SITE_DIR y tu UID/GID

docker compose up -d --build

curl -s localhost:5000/health
curl -s localhost:5000/zones
```

Después, un ciclo completo y a mirar el resultado:

```bash
curl -s localhost:5000/run-cycle >/dev/null
bash scripts/check.sh            # resumen legible de la primera zona activa
```

Para tener cabinas reales fuera de una red local soportada, saca una
[clave gratuita de OpenAQ](https://explore.openaq.org/register) y ponla en
`.env` como `OPENAQ_API_KEY`.

## API HTTP

| Endpoint | Qué devuelve |
|---|---|
| `GET /health` | estado del servicio |
| `GET /zones` · `/zones/all` | zonas activas · todas las zonas con su tier |
| `GET /analysis/<geo3>` | análisis completo de una zona |
| `GET /analysis-sensor/<geo3>/<mac>` | análisis completo de un sensor |
| `GET /analysis-sensors/<geo3>` | todos los sensores de una zona, uno a uno |
| `GET /sensors` | todos los sensores con su última lectura |
| `GET /cross-validation/<geo3>` | consenso entre sensores vecinos |
| `GET /pollution-rose/<geo3>` | concentración por sector de viento |
| `GET /compare/<geo3>` · `/compare-official/<geo3>` | vs CAMS · vs cabinas oficiales |
| `GET /official-sources` | qué referencia usa cada zona activa |
| `GET /openaq-debug[/<geo3>]` · `/euskadi-debug[/<geo3>]` | diagnóstico de referencias |
| `GET /run-cycle` · `/publish` | fuerza un ciclo · fuerza la publicación |

> Las estadísticas por métrica se publican bajo **`statistics`**, no `metrics`.

## Añadir tu propia red oficial

`euskadi.py` es el ejemplo trabajado: descubre la red del Gobierno Vasco desde
su portal de datos abiertos, elige estaciones por distancia **y por cobertura
de métricas**, y las promedia. Unas 200 líneas.

Para añadir otra red, escribe un módulo que exponga:

```python
fetch_official_series(hours, lat=None, lon=None) -> (series, sources)
# series[metric]  = {epoch_hora_utc: valor}   µg/m³, salvo CO en mg/m³
# sources[metric] = ["Nombre de la estación", ...]
```

y añádelo a la cadena de `fetch_official_any()` en `app.py`. `openaq.py` sigue
el mismo contrato. Las aportaciones que añadan redes son muy bienvenidas: es la
forma más útil de ampliar esto.

## Limitaciones, sin adornos

- Construido y probado contra **InfluxDB 1.x con InfluxQL**. No está portado a
  la 2.x.
- OpenAQ limita el radio de búsqueda a **25 km**; las zonas sin cabina dentro de
  ese radio caen al modelo CAMS, que es un modelo y no una medida.
- Los factores de corrección son **un único factor multiplicativo** por
  métrica. No modelan la dependencia de la humedad, la temperatura ni el tamaño
  de partícula.
- La corrección de humedad EPA/Malm solo se aplica donde no hay cabina, porque
  en nuestro despliegue apuntaba al lado *contrario* que la estación oficial:
  señal de que la humedad no era lo que sesgaba esas unidades.
- Datos de sensores comunitarios. **Esto no es una fuente oficial de calidad
  del aire** y no debe usarse como tal.

## Créditos

Construido sobre [CanAirIO](https://canair.io), cuya red y firmware hacen
posible todo esto. Datos de referencia de [OpenAQ](https://openaq.org),
[Open-Meteo](https://open-meteo.com),
[Copernicus CAMS](https://atmosphere.copernicus.eu) y
[Open Data Euskadi](https://opendata.euskadi.eus).

Desarrollado por [Barakaldo Makers](https://github.com/Barakaldo-Makers).

## Licencia

GPL-3.0-or-later. Ver [LICENSE](LICENSE).

La misma que usa CanAirIO: si mejoras esto, la mejora vuelve a la comunidad.