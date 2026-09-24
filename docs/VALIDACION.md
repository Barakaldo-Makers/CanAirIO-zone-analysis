# Por qué hace falta validar: ocho fallos que solo aparecen al contrastar

**CanAirIO-zone-analysis** · caso real, septiembre de 2026

> Este documento es el argumento del proyecto. Todos los fallos que describe
> estuvieron activos durante semanas en una red real sin que nada los
> señalara, y solo salieron a la luz al comparar contra cabinas oficiales.
> Las cifras son medidas, no estimaciones.

Lo que empezó como «no veo el NH3 en la web» destapó ocho fallos de fondo, cuatro
de ellos silenciosos desde hacía semanas. El séptimo no lo encontró una queja
sino una lectura: una tesis doctoral sobre calidad de datos en redes IoT
describía exactamente el fallo que teníamos. Este documento recoge qué pasaba, cómo
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
| Episodios de PM detectables en una zona tranquila | **ninguno** | todos |
| Persistencia e histéresis de avisos | código muerto | activas |
| Historial de factores | mezclaba CAMS y cabina | separado por base |

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


## 7. El limpiador borraba los episodios antes de detectarlos

Este no lo destapó un síntoma, sino la lectura de una tesis: Allka, X. (UPC,
2025), *Enhancing Data Quality in IoT Monitoring Sensor Networks*, capítulo 6.
Su figura 6.1 describe un fallo concreto de los detectores de atípicos que
trabajan solo con una ventana deslizante (que la tesis llama **ESOD-W**): marcan
un punto como atípico **porque es extremo en esa ventana**, cuando es
perfectamente normal **en la distribución histórica**.

Es exactamente lo que hacía `clean_series()`: MAD sobre las 168 h, `MAD_FACTOR=5`,
y nada más. Con la mediana real de la zona `ezt` eso pone el techo aquí:

| | valor |
|---|---|
| Mediana de PM2.5 en la ventana | 5,4 µg/m³ |
| Techo que impone el MAD | **~13,5 µg/m³** |
| Umbral de episodio de PM2.5 (`THRESHOLDS`) | 25 |
| Umbral crítico de PM2.5 | 75 |

Y el orden de las operaciones remata el problema: `detect_episodes()` recibe
`series_clean`, y `detect_alerts()` usa `last_value`, que también sale de la
serie ya limpia. Es decir, **toda hora que superara el umbral se borraba antes
de que el detector pudiera verla**. Medido sobre una ventana sintética con la
mediana real de la zona:

```
episodio  6 h a 25 ug/m3 -> techo MAD 13.5 -> BORRADO por clean_series
episodio  6 h a 50 ug/m3 -> techo MAD 13.5 -> BORRADO por clean_series
episodio  6 h a 75 ug/m3 -> techo MAD 13.5 -> BORRADO por clean_series
pm10, zona mediana 11: episodio a 150 -> techo 24.4 -> BORRADO
```

Ni los episodios de la web ni los avisos de Telegram podían dispararse por PM en
una zona tranquila. No es que no hubiera episodios: es que no había forma de
verlos. Y el fallo es invisible desde dentro, porque cada pieza por separado
funciona bien.

### Lo que se cambió: ESOD-WH

La tesis propone añadir un **segundo repositorio** con el histórico y descartar
el punto solo si es atípico en **ambos** (mide 10–30 % de mejora en precisión por
ese añadido). Aquí se implementa con dos rescates:

- **`fH`** — el valor cabe en la distribución histórica de su hora del día
  (≤ p95 × `ESOD_HIST_K`, exigiendo al menos `ESOD_HIST_MIN_N` muestras de esa
  hora antes de fiarse de ella).
- **`run`** — el valor forma parte de una excursión sostenida de al menos
  `ESOD_RUN_H` horas seguidas. Un fallo puntual de lectura no dura horas.

Solo se rescatan excursiones **por arriba**: un valor anormalmente bajo en un
sensor de contaminación es avería (óptica sucia, sensor ausente), no un episodio.

El histórico vive en el measurement `esod_history` de la BD de análisis, con tags
`geo3`/`mac`/`metric`/`hod` (hora del día). Se alimenta con lo que pasa el rango
físico, **antes** del MAD: si se alimentara de la serie limpia sería circular y
nunca aprendería que un episodio es posible. Se graba con el timestamp de la
hora, así que reescribir las últimas horas en cada ciclo es idempotente.

**Guard anti-avería.** Una racha de `ESOD_STUCK_H` horas o más con **todos** los
valores idénticos es un sensor atascado y no se rescata. La primera versión
rechazaba cualquier racha de valor constante, y el test lo tumbó: CanAirIO
publica con poca resolución y un episodio corto puede dar cifras repetidas de
forma legítima. Seis horas clavadas en la misma cifra, no.

Comportamiento verificado sobre el `clean_series` real:

| caso | antes | ahora |
|---|---|---|
| Episodio 6 h ~45 µg/m³ | 6 h borradas | **conservadas** (`run`) |
| Episodio 12 h ~75 µg/m³ (crítico) | 12 h borradas | **conservadas** |
| Episodio 2 h ~30 µg/m³ (duración mínima) | 2 h borradas | **conservadas** |
| Episodio 3 h a 45,0 exactos (cuantizado) | 3 h borradas | **conservadas** |
| Pico suelto de 1 h a 60 / 120 / 300 | filtrado | filtrado |
| Pico de 1 h a 60 con histórico p95 = 80 | filtrado | **conservado** (`fH`) |
| Pico de 1 h a 300 con histórico p95 = 80 | filtrado | filtrado |
| Sensor atascado 8 h y 24 h a 40,0 exactos | filtrado | filtrado |
| Ventana normal sin episodio | sin cambios | sin cambios |

El filtro no se ha aflojado: lo que era basura sigue cayendo.

**Limitación honesta:** el repositorio histórico empieza vacío y necesita unas
dos semanas de datos antes de que `fH` aporte algo. Hasta entonces trabaja solo
el rescate por racha, que es el que corrige el fallo desde el primer ciclo. La
fecha en que `fH` entra en juego se ve en `history_hours_ready` de
`/esod-debug/<geo3>`.

---


## 8. Avisos sin persistencia ni histéresis, y un historial que mezclaba referencias

Dos fallos del mismo tipo que el de `statistics` contra `metrics`: el código
existe, parece correcto y no hace lo que dice.

### 8a. `detect_alerts` se llamaba con la mitad de los argumentos

```python
result["alerts"] = detect_alerts(stats_by_metric, confidence)
```

La función acepta además `prev_alerts` y `series_recent`, y son justo los que
activan las dos mejoras documentadas como P1.3:

- **Persistencia**: la comprobación es `if series_recent and m in series_recent
  and PERSIST_H > 1`. Con `series_recent=None` nunca entraba. Y `ALERT_PERSIST_H`
  venía a `1`, que la desactiva por sí solo, así que había dos capas de nada.
- **Histéresis**: `prev_set` se construye de `prev_alerts or []`, luego salía
  vacío y `was_active` era siempre `None`. Peor: **`prev_alerts` no tenía fuente
  en ninguna parte del proyecto**. No era que se olvidara pasarlo; es que nadie
  guardaba las alertas del ciclo anterior.

Resultado: un pico de una sola hora disparaba aviso, y un valor oscilando en el
umbral lo encendía y apagaba en ciclos alternos.

Arreglado: hay un almacén `_last_alerts` por zona y sensor, se pasan los dos
argumentos y `ALERT_PERSIST_H` pasa a 2.

| | antes | ahora |
|---|---|---|
| Pico de **1 hora** a 30 µg/m³ | `warning` | **ninguna** |
| 2 horas seguidas a 30 | `warning` | `warning` |
| Aviso activo, el valor baja a 24 | ninguna (parpadeo) | **`warning`** |
| Aviso activo, el valor baja a 22,4 | ninguna | ninguna |

El límite de histéresis es 25 × 0,9 = 22,5, tal como decía el docstring.

> Si despliegas esto, comprueba que `ALERT_PERSIST_H` no esté fijado a `1` en tu
> `.env` o tu `docker-compose.yml`: el entorno pisa el valor por defecto del
> código, y con `1` la persistencia sigue apagada. Pasó en el despliegue original.

### 8b. `pm_factor_history` mezclaba dos referencias distintas

`_save_pm_factor_history()` se llama **dos veces por ciclo**: una con el factor
derivado de CAMS y otra con el derivado de la cabina oficial. Las dos escribían
con los mismos tags (`{"geo3": geo3}`) y sin timestamp explícito, así que
quedaban como dos puntos distintos y ambos se conservaban.

El detector de deriva hacía la mediana diaria **sobre la mezcla**. Para PM2.5 de
la zona de referencia eso es la mediana de una distribución con dos modas:

| | factor |
|---|---|
| Derivado de CAMS | ×0,664 |
| Derivado de la cabina oficial | ×1,233 |
| **Lo que reportaba el detector** | **×0,732** |

Ni el valor ni su variabilidad significan nada. Y como la mezcla es muy
dispersa, la banda salía tan ancha que se tragaba cualquier cambio: el informe
daba «−17,2 %» y «estable» en la misma línea.

Arreglado: el measurement gana el tag `basis` y el detector juzga **una sola
base** — la oficial si existe, CAMS como respaldo advirtiendo de que entonces
mide deriva frente a un modelo y no frente a una medida, y nunca la mezcla. Los
puntos anteriores al tag no se pueden atribuir, así que quedan fuera del
veredicto y el detector lo dice con esas palabras en vez de dar una cifra.

Y un cambio grande que cae dentro de la banda ya no se llama «estable»: pasa a
`sin_concluir` con la banda a la vista y la explicación de que es ruido.

### El detector, y por qué α-de-W

Viene del capítulo 4 de la tesis, que declara deriva cuando **α** muestras de una
ventana **W** salen de banda, en vez de reaccionar a una sola. Su mecanismo de
recalibración exige re-coubicar el sensor junto a una cabina; aquí no hace falta,
porque la referencia llega por API y el factor se recalcula en cada ciclo. Lo que
faltaba era la señal: `pm_factor_history` se escribía y **no lo leía nadie**.

Se agrega por día, no por ciclo: el envejecimiento de un sensor pasa en semanas,
y con ciclos de 30 minutos una ventana de 6 muestras serían 3 horas. La banda se
calcula sobre el historial anterior a la ventana reciente, con suelo relativo del
10 %: si el factor lleva doce días clavado el MAD es 0, y sin suelo cualquier
variación sería «deriva».

`factor = referencia / sensor`. Si **sube**, el sensor lee cada vez más bajo
frente a la cabina: óptica sucia o sensor envejecido. Si **baja**, conviene mirar
si cambió la estación de referencia antes de culpar al sensor.

| historial simulado | veredicto |
|---|---|
| 8 días | `sin_historial_suficiente` (8/12) |
| 30 días estables | `estable`, 0/6 fuera |
| 30 días clavados en la misma cifra | `estable` (el suelo de banda hace su trabajo) |
| **1 día anómalo suelto** | **`estable`**, 1/6 fuera |
| ×1,233 → ×1,660 progresivo | `deriva`, 5/6, +34,2 % |
| Ruidoso, −17,5 %, banda [0,731, 1,767] | `sin_concluir`: es ruido |

El cuarto caso es lo que compra la regla: un día anómalo no es deriva.

### El retardo δ: no implementado, a propósito

El δ de dESOD-WH (capítulo 6) existe porque ESOD es un algoritmo de streaming en
el borde: decide una vez, de forma irrevocable, conforme llega cada muestra. Este
proyecto recalcula la ventana entera de 168 h en cada ciclo, así que un punto que
hoy está en el borde mañana está en mitad de ella y se juzga con contexto por los
dos lados. **El δ ya existe, y se llama recálculo.**

Donde sí faltaba contexto era en los avisos, que se deciden sobre `last_value`.
Eso es el apartado 8a.

### Historial de cabina: el requisito de TPB-D

TPB-D (capítulo 3) construye un subespacio con los perfiles **diarios** de
cabinas cercanas y proyecta sobre él el día del sensor. El §3.2.4 demuestra que
**no necesita instrumento co-ubicado**, lo que encaja con este proyecto (O3 MOX:
RMSE 15,24 → 13,1; R² 0,57 → 0,68).

Lo que sí necesita es historial. El umbral de κ de Gavish-Donoho usa β = D/M con
D = 24 h, y β debe ser ≤ 1: hacen falta **M ≥ 24 días completos**, y de 40 a 60
para ir cómodo. El proyecto pedía 168 h de cabina en cada ciclo y las tiraba.

Ahora se guardan en el measurement `ref_history` (tags `geo3`/`provider`/
`metric`), y `/ref-history` calcula los días completos y la β. Un día cuenta si
trae al menos 20 de sus 24 horas, porque TPB-D trabaja con vectores diarios de
dimensión 24.

**TPB-D no está implementado**, y es una decisión, no un olvido: hasta que haya
24 días completos no hay nada que valorar, y cuando los haya será solo para
**O3**. La tesis dice sin rodeos que PM2.5 y NO son demasiado irregulares para
este método, y PM2.5 es precisamente la métrica fiable de este despliegue.

---

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

# ESOD-WH: ventana deslizante + repositorio histórico
ESOD_ENABLE=1          # 0 = comportamiento anterior (solo ventana)
ESOD_RUN_H=2           # horas seguidas para considerarlo excursión real
ESOD_STUCK_H=6         # horas clavadas en la misma cifra = avería
ESOD_HIST_DAYS=90      # ventana del repositorio histórico
ESOD_HIST_MIN_N=14     # muestras mínimas de una hora para fiarse de ella
ESOD_HIST_K=1.5        # margen sobre el p95 histórico
ESOD_HIST_WRITE_H=48   # horas que se regraban en cada ciclo

# Deriva del factor (regla alpha-de-W)
DRIFT_ENABLE=1
DRIFT_W=6              # días recientes de la ventana
DRIFT_ALPHA=4          # cuántos deben salir de banda
DRIFT_MIN_DAYS=12      # historial mínimo para opinar
DRIFT_K=3.0            # anchura de la banda, en sigmas
DRIFT_BAND_MIN_PCT=10  # suelo relativo de la banda
DRIFT_HIST_DAYS=120

# Historial de cabina (requisito previo de TPB-D)
REF_HIST_ENABLE=1
REF_HIST_DAYS=180

# Avisos
ALERT_PERSIST_H=2      # antes 1, que desactivaba la persistencia
```

---

## Endpoints nuevos

```
/official-sources[?max_tier=N]   qué referencia usa cada zona activa
/euskadi-debug[/<geo3>]          índice de la red vasca, selección y cobertura
/openaq-debug[/<geo3>]           estaciones OpenAQ del radio y métricas
/esod-debug/<geo3>               antes/después del filtrado, estado del histórico
/drift[/<geo3>]                  deriva del factor, con la base que la sustenta
/ref-history[/<geo3>]            días de cabina acumulados y si dan para TPB-D
```

`/analysis/<geo3>` añade: `aggregation`, `discarded_metrics`,
`official_provider`, `official_meta`, `applied_*` dentro de `pm_corrections`, y
`rescued_history` / `rescued_run` / `rescue_note` dentro de `cleaning`, y
`factor_drift` con la base de la que sale cada factor.

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
