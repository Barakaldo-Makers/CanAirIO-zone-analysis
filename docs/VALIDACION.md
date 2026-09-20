# Por qué hace falta validar: seis fallos que solo aparecen al contrastar

**CanAirIO-zone-analysis** · caso real, septiembre de 2026

> Este documento es el argumento del proyecto. Todos los fallos que describe
> estuvieron activos durante semanas en una red real sin que nada los
> señalara, y solo salieron a la luz al comparar contra cabinas oficiales.
> Las cifras son medidas, no estimaciones.

Lo que empezó como «no veo el NH3 en la web» destapó seis fallos de fondo, dos
de ellos silenciosos desde hacía semanas. Este documento recoge qué pasaba, cómo
se detectó y qué se cambió, por orden de descubrimiento.

---

## Resumen

| | antes | después |
|---|---|---|
| Zonas validadas contra estaciones reales | 1 de 5 | **5 de 6** |
| PM2.5 de la zona `ezt` | 0,33 µg/m³ | **5,36 µg/m³** |
| PM2.5 vs cabina oficial | rho 0,39 · sesgo −95,4 % | **rho 0,64 · sesgo −19,0 %** |
| PM10 vs cabina oficial | rho 0,13 · sesgo −97,0 % | **rho 0,58 · sesgo −51,8 %** |
| Etiqueta de PM2.5 | `dudoso` (vs CAMS) | **`fiable`** (vs cabina) |
| Factor de corrección PM2.5 | x0,664 (empeoraba) | **x1,233** |
| Episodios detectados | 2, de humedad | **0** |
| Métricas que desaparecían sin avisar | sí | no |

Las cifras de arriba son medidas reales de la zona `ezt`, no estimaciones.

---

## 1. Referencia oficial automática (red vasca + OpenAQ)

**Antes:** tres estaciones escritas a mano en el compose
(`EUSKADI_STATIONS=BARAKALDO,UNIDAD_MOVIL_10,UNIDAD_MOVIL_2`) y una lista de
zonas con referencia (`ZONE_HAS_OFFICIAL=ezt`). Las otras cuatro zonas activas
—Berlín, Calgary, Cali, Bogotá— se validaban **solo contra CAMS, que es un
modelo**, no estaciones.

**Ahora:** cadena de referencia automática.

```
zone_sensor_center(geo3[, mac])      posición REAL de los sensores
        ▼
fetch_official_any()
        ├─ 1. euskadi.py   ¿cabina vasca a menos de 25 km?   → la usa
        ├─ 2. openaq.py    ¿cabina oficial a menos de 25 km? → la usa
        └─ 3. (ninguna)    → CAMS, como antes
```

### Por qué el centroide y no el centro del geohash

Una celda geohash de 3 caracteres mide ~156 km de lado. Su centro geométrico
puede quedar a 100 km de los sensores, así que buscar cabinas «en 25 km
alrededor del centro de la celda» daba resultados sin sentido.
`zone_sensor_center()` promedia las posiciones del geohash fino de 7 caracteres
de los sensores que están emitiendo.

**Y con `mac`, la posición es la de ESE sensor.** El análisis por sensor usaba
el centroide de la zona: en Bilbao/Barakaldo ese punto cae entre ambas
localidades, así que un sensor de Barakaldo se comparaba contra una media que
incluía Mazarredo (Guggenheim), a 7 km. Ahora cada sensor busca sus cabinas.

### Por qué `url.txt` y no construir el nombre de fichero

El slug **no se deduce del nombre** de forma consistente:

| Estación | Fichero |
|---|---|
| `AÑORGA` | `ANORGA.csv` |
| `ALGORTA (BBIZI2)` | `ALGORTA_BBIZI2.csv` |
| `ZIERBENA (Puerto)` | `ZIERBENA_Puerto.csv` |
| `BANDERAS (meteo)` | `BANDERAS_meteo.csv` |
| `ZUBIETA METEO` | `ZUBIETA_METEO.csv` |
| `LASARTE-ORIA` | `LASARTE-ORIA.csv` |

Mayúsculas y minúsculas cambian según el caso. `url.txt` es la fuente de verdad
(61 estaciones con CSV publicado, de 62 en `estaciones.csv`).

### Selección por cobertura, no solo por distancia

Por pura distancia, Barakaldo elegía BARAKALDO, ERANDIO, SESTAO y MUNOA — y
**dejaba fuera las unidades móviles de Zorroza y Elorrieta, las únicas que
miden NH3 y O3**. Habríamos perdido esa validación justo después de añadirla.

La solución: recorrer las candidatas por distancia y seguir descargando
mientras falten métricas de `EUSKADI_TARGET_METRICS`, con tope
`EUSKADI_MAX_FETCH`. Medido: 6 CSV y las 7 métricas cubiertas.

### OpenAQ: dos decisiones que importan

- **`monitor=true`** — solo monitores de referencia regulatorios. Sin ese filtro
  entrarían sensores de bajo coste de la propia red OpenAQ: validaríamos un
  sensor barato contra otro sensor barato.
- **Conversión de unidades con T y P reales.** OpenAQ devuelve cada parámetro en
  la unidad del proveedor (µg/m³, ppm o ppb según el país). Verificado contra
  los valores de referencia a 25 °C: NO2 20 ppb → 37,61 µg/m³; O3 50 ppb →
  98,09; CO 1 ppm → 1,145 mg/m³. Si la unidad no se reconoce, el punto se
  descarta en vez de inventarlo.

### Sellado horario del CSV vasco

La etiqueta «H» del CSV es el promedio del intervalo `[H-1, H)`. El código
usaba `h0 = hh` (todo desplazado **+1 h**) y `24 → 0` del mismo día
(**−23 h**). Ahora `EUSKADI_HOUR_OFFSET`, por defecto `-1`. También se cambió
`time.mktime` por `calendar.timegm`, que no depende de la zona horaria del
servidor. Verificado: el rho de PM2.5 subió, así que el ajuste era correcto.

---

## 2. Los nodos sin sensor de PM hundían la zona

**El hallazgo más grave.** Medido en `ezt` sobre 48 h:

| sensor | media PM2.5 | puntos |
|---|---|---|
| EZTTTGOTD0BBBA | **0** | 5.724 |
| EZTTTGOTD0CC4E | **0** | 5.724 |
| EZTTTGOTDFF196 | **0** | 5.724 |
| EZTXIAOS355532 | **0** | 5.723 |
| EZTESP32C3071A6 | **0** | 2.862 |
| EZTXIAOS361222 | **0** | 2.862 |
| EZTTTGOT7ECCA2 | 5,81 | 804 |
| EZTTTGOT7D5CFE | 4,45 | 790 |
| EZTTTGOT770076 | 4,08 | 492 |

Seis nodos dan **exactamente 0** y acumulan 28.619 de 30.697 puntos (93 %).
La consulta agregaba con un solo `mean()` sobre todos los puntos:

```
(5,81×804 + 4,45×790 + 4,08×492) / 30.697 = 0,332 µg/m³
```

Exactamente el valor que devolvía InfluxDB.

**No eran sensores averiados leyendo bajo: son nodos que no llevan sensor de
PM** — el módulo de ruido C3 y los nodos XIAO de gases. El firmware de CanAirIO
publica 0 en las métricas cuyo sensor no está conectado, en vez de omitir el
campo. Y explica por qué los números empeoraron con el tiempo: cuantos más
nodos de gases y ruido se añadían, más se diluía el PM.

**Importante:** la mediana sola no lo arregla. Con 6 ceros de 9 valores, la
mediana también es 0. Hay que **excluir**, no promediar de otra forma.

El arreglo, en tres partes:

1. La consulta agrupa por `mac`: media horaria de **cada** sensor primero. Antes
   ponderaba por frecuencia de publicación — un nodo que publica cada 30 s pesaba
   diez veces más que uno de cada 5 min.
2. Si un sensor da 0 exacto en **todas** sus lecturas de una métrica, no lleva
   ese sensor: se excluye (`ZERO_MEANS_ABSENT`).
3. Entre sensores, **mediana** (`ZONE_AGG`), para que un sensor descalibrado pero
   con lecturas reales no arrastre la zona.

Resultado: 0,332 → **5,36 µg/m³**. La temperatura sigue agregándose con los 9,
porque esa sí la miden todos.

El análisis expone ahora un bloque `aggregation` con quién se usó y a quién se
excluyó por métrica, para que esto no vuelva a pasar en silencio.

### El mismo fallo en la tabla de la web

`collect_sensors()` usaba `last()` con comprobación de rango, y el rango de
`pm25`, `pm10`, `no2`, `o3`, `nh3`, `co` y `db` **empieza en 0**, así que un 0
pasaba como medida buena: nueve filas con PM2.5 = 0 y CO = 0. Ahora la consulta
trae también la media de la ventana y, si es 0 exacto, la celda queda vacía.

---

## 3. Las métricas desaparecían sin decir por qué

`zone_statistics` hacía `if len(vc) < 6: continue` **sin dejar registro**. Caso
real: el nodo de gases lee NH3 entre 1.419 y 1.651 µg/m³ (mediana **1.521**),
el filtro de rango (0-400) descartaba los 48 puntos y el NH3 se esfumaba del
análisis sin que nada lo explicara.

**El rango no se ha tocado.** 1.521 µg/m³ no es un valor ambiental posible —la
cabina oficial mide ~2,4. El filtro hace bien su trabajo; el sensor está
descalibrado. Ampliarlo solo metería basura en el análisis.

Lo que se añadió es visibilidad: `discarded_metrics` con el motivo y las cifras.

```json
"nh3": { "reason": "fuera_de_rango_fisico", "raw_points": 48, "kept": 0,
         "dropped_range": 48, "raw_median": 1520.93, "valid_range": [0, 400],
         "note": "el sensor lee ~1520.93 µg/m³, por encima del rango físico…" }
```

Motivos: `fuera_de_rango_fisico`, `pocos_datos_tras_limpieza`, `sin_datos`.

---

## 4. La referencia se elegía por rho, no por veredicto

`merge_confidence` tomaba la referencia con mayor rho. Con PM2.5, la cabina
oficial decía `fiable` (rho 0,64, sesgo −19 %) pero ganaba CAMS con `dudoso`
por tener rho 0,70 y un sesgo mucho peor. Se publicaba el veredicto **peor**
teniendo el mejor a mano.

Ahora compara la **calidad del veredicto** (`fiable > dudoso > no_fiable`) y
solo desempata por rho. En empate gana la oficial: es aire real del sitio.

### Y el umbral de sesgo era demasiado laxo

`CONF_BIAS_FAIR` pasa de 100 a **50**. Con 100, el O3 con −85 % de sesgo salía
`fiable` solo porque correlacionaba. Un sensor que lee el 15 % del valor real no
es fiable por bien que siga la forma de la curva. `dudoso` **no silencia las
alertas** (eso solo lo hace `no_fiable`).

---

## 5. Los factores de corrección iban al revés

`pm_corrections` derivaba el factor de CAMS aunque hubiera cabina oficial.
Medido: PM2.5 factor **x0,664**, o sea **reduciendo** un sensor que ya
subestima un 19 %: 5,47 → 3,63 µg/m³, alejándolo de los 6,6 de la cabina.
Contra la cabina sale **x1,235** → 6,76. En PM10, x1,04 cuando el −51,8 % pide
~x2,07.

Ahora el factor se recalcula con la referencia oficial cuando existe, y `basis`
dice cuál se usó.

### Jerarquía de corrección: una sola cifra

Quedaban **dos correcciones contradictorias** publicadas a la vez:

```
pm25: factor x1.233 -> 6.74     Malm(HR) -> 3.47
pm10: factor x2.073 -> 14.57    Malm(HR) -> 4.46
```

Una multiplica por 1,23 y la otra divide por 1,575 (con HR 70,56 % y k=0,24:
1 + 0,24 × 70,56/29,44 = 1,575; 5,47/1,575 = 3,47 exacto). La web mostraba las
dos sin decir cuál creer.

La causa es conceptual: **la fórmula EPA/Malm asume que el óptico sobreestima
con humedad alta** (las partículas absorben agua). Contra la cabina, estos
sensores **subestiman**. La hipótesis de la fórmula ya está desmentida por los
datos de este emplazamiento.

Jerarquía implementada:

| Prioridad | Corrección | Cuándo |
|---|---|---|
| 1 | factor contra cabina oficial | siempre que haya cabina |
| 2 | fórmula EPA/Malm por humedad | solo si **no** hay cabina (p. ej. Cali) |
| 3 | factor contra CAMS | **nunca se aplica**, queda informativo |

El análisis publica `applied`, `applied_value`, `applied_source` y
`applied_note`: una sola cifra, sin ambigüedad. Malm se conserva como dato y,
cuando el signo no cuadra, se marca con `malm_consistent: false` — que eso pase
es información útil: dice que la humedad no explica el sesgo de estos equipos.

---

## 6. Episodios de humedad

`detect_episodes` recorría toda métrica con umbral, y `hum` tiene umbral 80 %.
En Bilbao eso es un día normal: salían «2 episodios sostenidos» de humedad
(82 % de media durante 9 h) que no dicen nada de la calidad del aire. Ahora
solo contaminantes y ruido, vía `EPISODE_METRICS`.

---

## Cambios en la web (`index.html`)

- **Una sola corrección por métrica**, la que el análisis marca como aplicada,
  con su base en letra pequeña. Antes mostraba la de humedad aunque
  contradijera a la cabina.
- **Aviso cuando Malm y la cabina discrepan** en el sentido.
- **Fuente de referencia por zona.** La leyenda decía siempre «vs
  CAMS·Copernicus», y eso ya es falso en 5 de 6 zonas. Ahora: «Validado contra
  red oficial vasca: CASTREJANA, ARRAIZ (Monte), UNIDAD MÓVIL 10».
- **Métricas descartadas** con su motivo en la ficha del sensor, en vez de
  desaparecer.

---

## Variables nuevas

```yaml
# Referencia oficial
ZONE_HAS_OFFICIAL=              # vacío = automático en todas las zonas
EUSKADI_RADIUS_KM=25
EUSKADI_MAX_STATIONS=12         # candidatas del índice
EUSKADI_MAX_FETCH=8             # tope de CSV por zona
EUSKADI_MIN_STATIONS=3
EUSKADI_TARGET_METRICS=pm25,pm10,no2,o3,nh3,co,so2
EUSKADI_HOUR_OFFSET=-1          # la etiqueta "H" cubre [H-1,H)
OPENAQ_API_KEY=${OPENAQ_API_KEY}
OPENAQ_RADIUS_KM=25             # máximo de la API
OPENAQ_MONITOR_ONLY=1           # solo monitores de referencia
OPENAQ_EXCLUDE_MOBILE=1
OPENAQ_MIN_COVERAGE=50
OPENAQ_LOC_GRID=2               # agrupa la caché a ~1,1 km

# Agregación
ZONE_AGG=median
ZERO_MEANS_ABSENT=1

# Etiquetas y episodios
CONF_BIAS_FAIR=50
EPISODE_METRICS=pm25,pm10,pm1,no2,o3,nh3,co,co2,so2,db
```

---

## Endpoints nuevos

```
/official-sources[?max_tier=N]   qué referencia usa cada zona activa
/euskadi-debug[/<geo3>]          índice de la red vasca, selección y cobertura
/openaq-debug[/<geo3>]           estaciones OpenAQ del radio y métricas
```

`/analysis/<geo3>` añade: `aggregation`, `discarded_metrics`,
`official_provider`, `official_meta`, y `applied_*` dentro de
`pm_corrections`.

> **Ojo al leer la API:** las estadísticas por métrica se publican como
> **`statistics`**, no `metrics` (`result["statistics"] = zs["metrics"]`).
> Confundirlas hace que un verificador informe «0 métricas analizadas» con el
> análisis perfectamente correcto.

---

## Verificación

```bash
bash ~/comprobar.sh          # solo lectura: estado completo
bash ~/comprobar.sh u33      # otra zona
```

---

## Pendiente (no es software)

1. **Recalibrar el cero de los tres canales de gas** (`z1`, `z2`, `z3`) en
   exterior y a temperatura real de operación. NH3 lee ~1.521 µg/m³ contra 2,4
   de la cabina; NO2 va +585 %. Antes de recalibrar, comprobar en el log serie
   si el sensor dice ~2,18 ppm: si sí, es calibración; si no, es el firmware.
2. **Revisar `EZTTTGOTD0CC4E`**: su óptico de PM ya no publica nada y la
   validación cruzada lo marcó por temperatura anómala.
3. **Revocar el token de Telegram** en @BotFather (estuvo en texto plano).
4. **Desplegar el firmware corregido del módulo de ruido C3** (desbordamiento de
   `micros()` cada 71,58 min + watchdog).
5. `d29` (Cali) sigue sin cabina a menos de 25 km, que es el máximo de la API de
   OpenAQ. Ahí la corrección aplicada será la de humedad.
