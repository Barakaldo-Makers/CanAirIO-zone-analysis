# Arquitectura del motor de análisis

**CanAirIO-zone-analysis** · septiembre de 2026

> Documento en español. El README está en
> [inglés](../README.md) y [español](../README.es.md).
>
> Describe el despliegue de referencia de Barakaldo Makers (Debian 12, CPU sin
> AVX, 8 GB de RAM). Las rutas concretas son ejemplos: en el repositorio todo
> se configura por variables de entorno.

---

## 1. Visión general en una frase

Un sincronizador replica cada hora los datos de la red mundial CanAirIO a un
InfluxDB local; un servicio de análisis los procesa con estadística rigurosa,
los valida contra referencias externas y publica los resultados en una web
estática de GitHub Pages y en avisos de Telegram.

---

## 2. Mapa de directorios

```
<repo>/                    ← stack Docker principal
├── docker-compose.yml           ← define influxdb, grafana, telegraf, ai-bridge
├── .env                         ← credenciales (NO subir a git)
├── ai-bridge/                   ← código del servicio de análisis
│   ├── app.py                   ← TODO el motor de análisis (~2.600 líneas)
│   ├── euskadi.py               ← red oficial vasca, selección automática
│   ├── openaq.py                ← referencia oficial GLOBAL (OpenAQ v3)
│   ├── publisher.py             ← exporta JSON y hace git push
│   ├── requirements.txt
│   └── Dockerfile
├── telegraf/telegraf.conf
└── grafana/provisioning/

<sincronizador>/                 ← replicación desde CanAirIO (compose aparte)
├── docker-compose.yml           ← GEO3_PREFIXES="" → importa TODO el mundo
├── sync.py                      ← sincronizador incremental
└── Dockerfile

<SITE_DIR>/             ← repo git de la web (montado como /site)
├── index.html                   ← la web completa (mapa, tarjetas, tabla)
├── data/state.json              ← estado actual por zona  (lo genera el ai-bridge)
├── data/history.json            ← histórico para gráficas
├── data/sensors.json            ← listado de todos los sensores
└── .gitconfig                   ← identidad git del contenedor
```

**Firmware** (proyectos PlatformIO, fuera del servidor):
- Nodo de gases NH3/NO2/O3 + SHT31 → XIAO ESP32-S3
- Módulo de ruido (`main.cpp`, `DSP_Engine.*`, `I2C_Comm.*`) → LOLIN C3 Mini

---

## 3. Contenedores

| Contenedor | Estado | Función |
|---|---|---|
| `influxdb` | activo | Base de datos de series temporales (v1.12) |
| `ai-bridge` | activo | Análisis, publicación web, Telegram |
| `canairio-sync` | externo | Replica CanAirIO → InfluxDB local. No forma parte de este repositorio |
| `grafana` | activo | Visualización interna |
| `telegraf` | externo | Ingesta directa de sensores locales, si la usas |

`influxdb`, `grafana` y `ai-bridge` son los que define el `docker-compose.yml`
de este repositorio. El resto pertenece al despliegue completo original.

---

## 4. Bases de datos InfluxDB

| Base | Measurement | Contenido |
|---|---|---|
| `canairio` | `fixed_stations_01` | Datos crudos replicados de CanAirIO (~26M+ puntos) |
| `canairio` | `_sync_state` | Checkpoint de progreso del sincronizador |
| `analysis` | `zone_analysis` | Histórico de cada análisis (estado, AQI, meteo, confianza) |
| `analysis` | `pm_factor_history` | Deriva semanal del factor de corrección PM |
| `air_quality` | (telegraf) | Sensores locales — **no tocar** |

**Tags** de `fixed_stations_01`: `geo3` (prefijo geohash de 3 chars), `mac`, `rev`.
**Fields clave**: `pm25`, `pm10`, `pm1`, `no2`, `o3`, `nh3`, `co`, `co2`, `db`,
`tmp`, `hum`, `prs`, `geo` (geohash fino de 7 chars), `name_1` (nombre del sensor).

> Ojo: `name` es palabra reservada en InfluxQL → siempre usar alias (`AS sname`).

---

## 5. Flujo de datos completo

```
┌─────────────────────────────┐
│  InfluxDB remoto CanAirIO   │  influxdb.canair.io:8086
└──────────────┬──────────────┘
               │ sync.py — cada hora, incremental, SIN filtro geográfico
               ▼
┌─────────────────────────────┐
│  InfluxDB local "canairio"  │
└──────────────┬──────────────┘
               │ app.py — cada 30 min (ANALYSIS_INTERVAL)
               ▼
   ┌───────────────────────────────────────────┐
   │  PIPELINE DE ANÁLISIS (ver sección 6)     │
   └────┬─────────────┬──────────────┬─────────┘
        ▼             ▼              ▼
  InfluxDB      Telegram        GitHub Pages
  "analysis"    (avisos)        (web pública)
```

---

## 6. Pipeline de análisis — paso a paso

Todo ocurre en `app.py`. Orden de ejecución dentro de `zone_statistics()` y
`analyze_zone_full()`:

| # | Función | Qué hace |
|---|---|---|
| 1 | `all_zones_tiered()` | Clasifica TODAS las zonas del mundo en 4 tiers según antigüedad del último dato |
| 2 | `fetch_zone_series()` | Media horaria **por sensor** (`GROUP BY mac`), excluye al que lee 0 exacto en toda la ventana (no lleva ese sensor) y combina con **mediana**. Descarta horas congeladas (`stddev = 0`) |
| 3 | `clean_series()` | Rango físico por métrica + outliers MAD + interpolación de huecos ≤2 h. Lo que se cae entero queda en `discarded_metrics` con el motivo |
| 4 | `convert_gases()` | ppm → µg/m³ usando la T y P reales de la estación |
| 5 | `analyze_series()` | Mann-Kendall, Theil-Sen, IC 95 %; `correlations()` para Spearman entre métricas |
| 6 | `fetch_weather()` | Open-Meteo: temperatura, humedad, viento, **weathercode**, nubosidad, visibilidad, UV |
| 7 | `fetch_reference()` | CAMS/Copernicus (cobertura global) |
| 8 | `zone_sensor_center()` | Posición real de los sensores (la celda geo3 mide 156 km). Con `mac`, la de ESE sensor |
| 8b | `fetch_official_any()` | Cadena de referencia oficial: red vasca (por cercanía + cobertura de métricas) → OpenAQ global → nada |
| 9 | `build_confidence()` + `merge_confidence()` | Etiqueta por métrica tomando el **mejor veredicto** (no el mejor rho); empate a favor de la cabina oficial |
| 10 | `pm_corrections()` / `apply_malm_correction()` | Corrección PM. Jerarquía: cabina oficial > Malm (humedad) > nada. CAMS nunca se aplica. Publica `applied_*` con una sola cifra |
| 11 | `cross_sensor_validation()` | Cada sensor vs la mediana de sus vecinos |
| 12 | `pollution_rose()` | Concentración por sector de viento (8 sectores + calma) |
| 13 | `compute_caqi()` | Índice europeo CAQI |
| 14 | `detect_episodes()` | Períodos sostenidos sobre umbral. Solo contaminantes y ruido (`EPISODE_METRICS`): la humedad a 80 % es tiempo normal, no un episodio |
| 15 | `hourly_profile()` / `env_correlations()` | Perfil por hora del día; correlación con lluvia y presión |
| 16 | `detect_alerts()` | Alertas con persistencia e histéresis; excluye `no_fiable` |
| 17 | `rules_summary()` | Informe con motor de reglas determinista (**sin LLM**) |
| 18 | `store_analysis()` | Guarda el resultado en la BD `analysis` |
| 19 | `maybe_alert_critical()` | Aviso a Telegram si entra/sale de crítico |
| 20 | `publish_site()` | Genera los 3 JSON y llama a `publisher.git_publish()` |

### Estratificación por tiers (análisis global)

| Tier | Último dato | Frecuencia | Análisis |
|---|---|---|---|
| 1 — activa | < 2 h (`TIER1_HOURS`) | cada ciclo | completo |
| 2 — reciente | < 24 h (`TIER2_HOURS`) | cada 2 ciclos | completo |
| 3 — dormida | < 7 días (`TIER3_HOURS`) | cada 6 ciclos | sin meteo ni rosa |
| 4 — histórica | > 7 días | una vez al día | solo estadística |

---

## 7. Fuentes externas (todas gratuitas; solo OpenAQ pide API key)

| Fuente | URL | Para qué |
|---|---|---|
| CanAirIO | `influxdb.canair.io:8086` | Datos crudos de sensores |
| Open-Meteo Forecast | `api.open-meteo.com/v1/forecast` | Meteo actual y previsión |
| Open-Meteo Historical Forecast | `historical-forecast-api.open-meteo.com` | Viento pasado (rosa de contaminación) |
| Open-Meteo Air Quality | `air-quality-api.open-meteo.com` | Referencia CAMS/Copernicus |
| Open Data Euskadi | `opendata.euskadi.eus/.../calidad_aire_2026/…` | Red vasca: 62 estaciones. `estaciones.csv` (coordenadas) + `url.txt` (URLs reales) + `datos_horarios/*.csv` |
| OpenAQ v3 | `api.openaq.org/v3` | Estaciones oficiales de ~100 países. Requiere API key gratuita (`OPENAQ_API_KEY`) |

> El CSV de Euskadi **exige cabecera User-Agent de navegador**; sin ella el
> servidor rechaza la petición. Formato: separador `;`, decimal con coma,
> hora 01:00–24:00 GMT (24:00 = medianoche).

---

## 8. Endpoints del ai-bridge (puerto 5000)

```
/health                          estado del servicio
/zones                           zonas activas
/zones/all                       TODAS las zonas con su tier
/stats/<geo3>                    estadística bruta
/weather/<geo3>                  meteo actual + previsión
/analysis/<geo3>?hours=48        análisis completo de una zona
/analysis/all                    análisis de todas las zonas
/analysis-sensor/<geo3>/<mac>    análisis de UN sensor
/analysis-sensors/<geo3>         análisis individual de todos los sensores
/zone-sensors/<geo3>             lista de sensores de una zona
/sensors                         listado global de sensores
/cross-validation/<geo3>         consenso entre sensores
/pollution-rose/<geo3>           rosa de contaminación
/compare/<geo3>                  cotejo vs CAMS
/compare-official/<geo3>         cotejo vs estaciones oficiales
/euskadi-debug[/<geo3>]          índice de la red vasca, selección y cobertura
/openaq-debug[/<geo3>]           estaciones OpenAQ del radio y métricas
/official-sources[?max_tier=N]   qué referencia oficial usa cada zona activa

> Las estadísticas por métrica se publican como **`statistics`**, no `metrics`
> (`result["statistics"] = zs["metrics"]`). Confundirlas hace que un verificador
> informe «0 métricas analizadas» con el análisis correcto.
/run-cycle                       fuerza un ciclo completo ahora
/publish                         fuerza la publicación web
/telegram/test                   prueba de envío
/summary/daily                   fuerza el resumen diario
```

---

## 9. Publicación web

`publisher.py` escribe tres JSON en `/site/data/` y hace commit + push:

- **`state.json`** — estado por zona, con el bloque `sensors[]` de cada una
  (métricas dinámicas, confianza, consenso, corrección Malm), CAQI, episodios,
  meteo y correlaciones ambientales.
- **`history.json`** — serie histórica para las gráficas.
- **`sensors.json`** — todos los sensores con posición (lat/lon del geohash fino),
  últimas lecturas, flags de fuera de rango y timestamp real por sensor.

Antes de cada push hace `git pull --no-rebase -X ours origin main` para
reconciliar automáticamente y evitar el rechazo *"fetch first"*.

La web (`index.html`) se sirve desde GitHub Pages:
**https://barakaldo-makers.github.io/CanAirIO-analysis/**

Contiene: mapa Leaflet con un marcador por sensor y flujo de viento animado,
tarjetas por zona expandibles con el análisis de cada sensor, gráficas de
evolución y tabla global de sensores con búsqueda y orden.

---

## 10. Decisiones de diseño importantes

**Sin LLM.** `ANALYSIS_MODE=rules`. Se probó Ollama con `tinyllama`, `phi4-mini`
y un modelo propio `airquality-ai`: la CPU **no tiene AVX** (`grep -c avx
/proc/cpuinfo` → 0), lo que da ~4,4 s por token. Un informe tardaría 30-45 min.
El motor de reglas es instantáneo, determinista y no puede alucinar.

**Sin filtro geográfico.** `GEO3_PREFIXES=""` en el sync: cualquier sensor nuevo
del mundo entra automáticamente sin tocar configuración.

**El contenedor escribe como el usuario.** `user: "1000:1000"` en `ai-bridge`
evita que `.git/objects` quede como root y bloquee los commits desde VS Code.

**Referencia oficial automática.** No hay lista de zonas con referencia:
`ZONE_HAS_OFFICIAL` vacío significa que se intenta en todas y cada fuente decide
si cubre la posición. La red vasca se descubre de `estaciones.csv` + `url.txt`
(el nombre de fichero no se deduce del de la estación) y se eligen estaciones
por cercanía **y por cobertura de métricas**: por pura distancia se quedaban
fuera las unidades móviles de Zorroza y Elorrieta, las únicas con NH3 y O3.
Detalle completo en `REFERENCIAS_OFICIALES.md`.

**La convención del viento.** `wind_direction_10m` de Open-Meteo indica de dónde
VIENE el viento. Para dibujar el flujo en el mapa hay que rotar `wdir + 90`.

---

## 11. Veredicto de fiabilidad de los sensores

Validado contra triple referencia (CAMS + 3 estaciones oficiales):

| Métrica | Confianza | Detalle |
|---|---|---|
| **PM2.5** | fiable | rho 0,64; subestima ~19 % (factor x1,23 aplicado) |
| **PM10** | dudoso | rho 0,58; subestima ~52 % (factor x2,07) |
| **NO2** | no fiable | rho −0,48 y sesgo +585 %; correlación negativa |
| **O3** | fiable (al límite) | rho 0,77, sesgo +48 %; un solo sensor lo mide |
| **NH3** | descartado | sensor ~1.521 vs oficial ~2,4 µg/m³: fuera del rango físico 0-400, no entra al análisis |

> Medido el 19/09/2026 contra la red vasca, con la agregación ya corregida.
> El detalle de cómo se llegó a estas cifras está en
> `CORRECCIONES_19SEP2026.md`.
| Ruido, CO2 | sin referencia | no hay con qué validar |

Las métricas `no_fiable` se excluyen automáticamente de las alertas.

### Hallazgos del consenso entre sensores
- `EZTXIAOS361222` — divergente en todo (sesgo −99 %): revisar o retirar.
- `EZTTTGOTD08C9E` — lee ~20× bajo: óptico degradado, limpiar.
- `EZTTTGOTD0CC4E` — temperatura +25-30 % sobre el consenso: ¿sol directo?

---

## 12. Bugs de firmware identificados

**Desbordamiento de `micros()`** en el módulo de ruido C3: `micros()` es uint32
y desborda cada 2³² µs = **71,58 minutos**, exactamente la duración de las
rachas de valor congelado observadas. Corregido con `esp_timer_get_time()`
(int64) más watchdog activo que reinicia si el muestreo muere 60 s.

**Congelaciones largas (14-15 h)**: afectan a *todas* las métricas del nodo
(PM, temperatura y ruido a la vez) mientras el `heap` sigue variando → la tarea
de adquisición del firmware CanAirIO se para y la de publicación sigue enviando
el último snapshot. Mitigado en el servidor descartando horas con `stddev = 0`.

---

## 13. Comandos habituales

```bash
# desplegar cambios en el análisis
cp app.py <repo>/ai-bridge/app.py
cd ~/envmonitor && docker compose up -d --build ai-bridge

# forzar ciclo y publicación
curl -s http://localhost:5000/run-cycle
curl -s http://localhost:5000/publish

# ver logs sin el ruido del health check
docker compose logs ai-bridge --tail 30 | grep -vi "GET /health"

# desplegar la web
sudo chown -R $(id -un):$(id -gn) ~/CanAirIO-analysis
cp index.html <SITE_DIR>/index.html
cd ~/CanAirIO-analysis && git add index.html && git commit -m "..." && git push

# consultar la base cruda
docker compose exec influxdb influx -username admin -password "$INFLUXDB_ADMIN_PASSWORD" \
  -database canairio -precision rfc3339 \
  -execute "SELECT last(\"pm25\") FROM \"fixed_stations_01\" WHERE \"geo3\"='ezt'"

# ver todas las zonas del mundo con su tier
curl -s http://localhost:5000/zones/all | python3 -m json.tool
```

---

## 14. Pendientes

1. **Seguridad**: regenerar el token de Telegram expuesto (`/revoke` en
   @BotFather) y dejarlo solo en `.env`.
2. Revisar físicamente los nodos señalados por el consenso (sección 11).
3. Desplegar el firmware corregido del módulo de ruido C3.
4. Recalibrar los ceros de los sensores de gas en exterior, a temperatura real
   de operación, y revalidar con `/compare-official/ezt`.
5. Investigar MeteoEuskadi como fuente de viento local (más precisa que el
   modelo de 9 km para la rosa de contaminación).
6. `d29` (Cali) sigue sin cabina oficial a menos de 25 km (máximo de la API de
   OpenAQ): ahí la corrección aplicada es la de humedad.

---

*Datos de sensores comunitarios CanAirIO. No constituye una fuente oficial de
calidad del aire.*
