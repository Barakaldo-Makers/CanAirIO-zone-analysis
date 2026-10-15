# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/).
Versionado semántico.

## [1.3.0] — 2026-10-15

### Añadido
- **`acustica.py`: indicadores acústicos de largo plazo.** Lee los Ld/Le/Ln/Lden
  y el L90 que ya publican los nodos —no los recalcula— y los contrasta con el
  RD 1367/2007 (objetivos por periodo y tipo de área) y con la guía de la OMS
  2018 (Lden y Lnight). Autodetecta el nombre de los campos en InfluxDB en vez
  de darlo por supuesto. Endpoint `/acustica[/<geo3>]`.
  El L90 sirve para separar dos cosas que el Lden no distingue: si los nodos
  concuerdan en el ruido de FONDO y discrepan en Lden, están bien calibrados y
  miden calles distintas; si discrepan ya en el fondo, es calibración o
  emplazamiento. Los veredictos se publican como ORIENTATIVOS: el RD se refiere
  a valores anuales y una evaluación formal exige el procedimiento de la
  ISO 1996-2 con equipo calibrado.
- **`narrador.py`: redacción en castellano vía API remota** (cualquier endpoint
  compatible con OpenAI). El modelo no calcula: recibe las cifras hechas y solo
  las redacta. Después se extrae **cada número** del texto y se comprueba que
  esté en los datos de entrada; si aparece una cifra inventada, el texto se
  descarta y el sistema sigue con el motor de reglas. Apagado por defecto
  (`NARRA_ENABLE=0`): sin activarlo no se llama a ninguna API.
  Endpoint `/narrativa/<geo3>`.
- Endpoints `/ref-history[/<geo3>]`, que se quedó sin portar en la 1.2.0.

### Corregido
- **El cotejo entre sensores medía el ruido en porcentaje.** `bias_pct` es
  `100·(sensor−consenso)/consenso`, y eso no significa nada en una escala
  logarítmica con cero arbitrario. Con los umbrales de concentración
  (150 % dudoso, 300 % divergente) y un consenso de 40 dB, salir `divergente`
  exigía una diferencia de 120 dB: leer 160 dB, por encima del rango físico.
  **Ningún nodo de ruido podía marcarse como divergente.** Y la rho de Spearman
  es ciega a un desplazamiento constante: el mismo patrón diario desplazado
  42 dB da rho = 1,000 exacto, que es justo el fallo típico de un micrófono.
  Ahora `LOG_METRICS` desvía esas métricas a umbrales acústicos: ≤3 dB
  coherente (3 dB es un doblaje de energía; 1,5 dB la tolerancia de un
  sonómetro de clase 2), >6 dB divergente, y el sesgo se reporta en dB.
  En el despliegue real, cuatro nodos que salían `coherente` con 13–27 dB de
  discrepancia pasaron a `divergente`.
- **`ai-bridge/app.py` tenía 1165 líneas duplicadas verbatim.** Un bloque
  contiguo de 32 funciones —`clean_series`, `detect_alerts`,
  `cross_sensor_validation`, `pm_corrections`, `detect_episodes`…— aparecía dos
  veces, desde el commit `b184175`. Las dos copias eran idénticas, así que no
  había fallo de comportamiento (en Python gana la última definición), pero era
  un tercio del fichero y una trampa: quien editara la primera copia no vería
  ningún efecto. Verificado antes de limpiar que las dos regiones eran iguales
  línea a línea, y después que las 97 funciones efectivas, las 27 rutas y las
  79 constantes son byte a byte las de antes.
- `.gitignore` vuelve a excluir `*.code-workspace` y `parche-*.sh`: las reglas
  se perdieron en un merge y por eso se colaron al repo un script de parche con
  payload embebido y el workspace del editor.

## [1.2.0] — 2026-09-24

### Añadido
- **Detector de deriva del factor de corrección** (regla α-de-W, de Allka 2025,
  cap. 4): hay deriva cuando al menos `DRIFT_ALPHA` de los `DRIFT_W` días
  recientes salen de la banda del historial anterior. Se agrega por día, no por
  ciclo, y la banda lleva suelo relativo para que un factor muy estable no dé
  falsos positivos. Endpoint `/drift[/<geo3>]` y bloque `factor_drift`.
- **Repositorio de referencia oficial** (`ref_history`): guarda la serie horaria
  de cabina que antes se descartaba. Es el requisito previo de TPB-D, que
  necesita M ≥ 24 días completos porque β = 24/M debe ser ≤ 1. Endpoint
  `/ref-history[/<geo3>]` con los días acumulados y la β de Gavish-Donoho.

### Corregido
- **La persistencia y la histéresis de avisos eran código muerto.**
  `detect_alerts()` se llamaba sin `series_recent` ni `prev_alerts`, así que la
  comprobación de persistencia nunca entraba y la histéresis partía siempre de
  vacío; `prev_alerts` además no tenía fuente en ninguna parte del proyecto.
  Ahora hay un almacén `_last_alerts` por zona/sensor y `ALERT_PERSIST_H` pasa
  a 2. Un pico de una sola hora ya no dispara aviso.
- **`pm_factor_history` mezclaba dos referencias.** Se escribe dos veces por
  ciclo, una con el factor derivado de CAMS y otra con el de la cabina, y sin
  tag que las distinguiera la mediana diaria combinaba ×0,664 con ×1,233 y
  reportaba ×0,732. El measurement gana el tag `basis` y el detector juzga una
  sola base: la oficial si existe, CAMS como respaldo advirtiéndolo, nunca la
  mezcla.
- Un cambio grande dentro de la banda ya no se etiqueta «estable»: pasa a
  `sin_concluir`, con la banda a la vista.
- `ref_history_state()` recorría dos veces el generador de puntos que devuelve
  `ResultSet.items()` de influxdb-python, y el segundo cómputo salía a 0
  («1 día, 0 horas»).

## [1.1.0] — 2026-09-24

### Añadido
- **ESOD-WH: repositorio histórico junto a la ventana deslizante.** Adaptado de
  Allka, X. (UPC, 2025), *Enhancing Data Quality in IoT Monitoring Sensor
  Networks*, cap. 6. Un punto que el MAD de la ventana marcaría como atípico se
  conserva si cabe en la distribución histórica de su hora del día (`fH`) o si
  forma parte de una excursión sostenida (`run`). Solo se rescatan excursiones
  por arriba. Una racha de `ESOD_STUCK_H` horas con valores idénticos se sigue
  descartando: es un sensor atascado, no un episodio.
- Measurement `esod_history` en la BD de análisis (tags `geo3`/`mac`/`metric`/
  `hod`), alimentado con lo que pasa el rango físico, antes del MAD.
- Endpoint `/esod-debug/<geo3>`: antes/después del filtrado en la misma llamada
  y estado del repositorio histórico.
- `cleaning[<métrica>]` publica `rescued_history`, `rescued_run` y `rescue_note`.

### Corregido
- **El limpiador borraba los episodios antes de detectarlos.** `clean_series()`
  filtraba con MAD sobre la ventana de 168 h y nada más. Con la mediana real de
  la zona de referencia (5,4 µg/m³) el techo quedaba en ~13,5 µg/m³, por debajo
  del umbral de episodio de PM2.5 (25) y muy por debajo del crítico (75). Como
  `detect_episodes()` trabaja sobre `series_clean` y `detect_alerts()` sobre
  `last_value`, **ningún episodio de PM podía dispararse en una zona tranquila**.
  Mismo caso en PM10: techo ~24, umbral 50. Detalle y cifras en
  [`docs/VALIDACION.md`](docs/VALIDACION.md#7-el-limpiador-borraba-los-episodios-antes-de-detectarlos).

## [1.0.0] — 2026-09-19

Primera publicación. Motor de análisis extraído del despliegue de Barakaldo
Makers, con las correcciones de validación documentadas en
[`docs/VALIDACION.md`](docs/VALIDACION.md).

### Añadido
- Cadena de referencia oficial automática: red local → OpenAQ → CAMS, con la
  posición real de los sensores en vez del centro de la celda geohash.
- `openaq.py`: estaciones oficiales de referencia en ~100 países, con filtro de
  monitores regulatorios y conversión de unidades a la T y P reales.
- `euskadi.py` reescrito: descubre la red desde el portal de datos abiertos y
  selecciona estaciones por cercanía **y cobertura de métricas**.
- Bloques `aggregation` y `discarded_metrics` en el análisis: ninguna métrica
  desaparece sin motivo.
- Endpoints `/official-sources`, `/openaq-debug`, `/euskadi-debug`.
- `scripts/check.sh`: verificación de solo lectura.

### Corregido
- **Agregación de zona.** Era un único `mean()` sobre todos los puntos, lo que
  ponderaba por frecuencia de publicación y dejaba que los nodos sin sensor de
  PM (que publican 0) hundieran la zona: PM2.5 pasó de 0,33 a 5,36 µg/m³.
  Ahora media por sensor y mediana entre sensores, excluyendo a quien lee 0 en
  toda la ventana.
- **Elección de referencia.** Se tomaba la de mayor correlación aunque su
  veredicto fuera peor; ahora manda la calidad del veredicto.
- **Factores de corrección de PM.** Se derivaban de CAMS aunque hubiera cabina
  oficial, con el resultado de reducir lecturas que ya subestimaban.
  Jerarquía nueva: cabina > Malm (humedad) > nada.
- **Sellado horario** de los CSV horarios: la etiqueta «H» cubre `[H-1, H)`.
- **Episodios** de humedad y temperatura, que no son episodios de contaminación.
- La tabla web marcaba los valores fuera de rango con el rojo de «crítico», que
  se lee como contaminación alta cuando significa sensor averiado.