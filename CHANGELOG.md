# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/).
Versionado semántico.

## [1.5.0] — 2026-11-12

### Añadido
- **`eea.py`: referencia oficial europea** vía el Download Service de la
  Agencia Europea de Medio Ambiente (datos UTD/E2a horarios, parquet, sin
  clave). Tercer eslabón de la cadena: euskadi → OpenAQ → **EEA** → CAMS. La
  red vasca reporta a la EEA, así que es el respaldo automático de
  `euskadi.py` si Open Data Euskadi se cae, y cubre cualquier zona europea
  futura sin escribir un módulo por país. Los datos se bajan **fichero a
  fichero y solo los de las estaciones elegidas**: se pide la lista de URLs
  (`/ParquetFile/urls`, unos KB de texto), se eligen las de los puntos
  cercanos —por id, y por código nacional de estación como respaldo, porque
  el id del metadato no tiene por qué coincidir con el del fichero— y se
  descargan en streaming a disco, con topes `EEA_MAX_FILES`/`EEA_MAX_MB` y
  filtrado temporal al parsear. Los datos vuelven agrupados por estación,
  para que nadie tenga que volver a cruzar identificadores.
  El primer despliegue en el servidor (antes de publicarse aquí) usaba
  `POST /ParquetFile` a secas: un ZIP con TODO el país —2310 ficheros y
  ~220 MB para tres días de España— cargado en memoria con `r.content`, y
  **tumbó el contenedor** con el hilo de Flask bloqueado. Y el segundo
  incidente no era ni memoria ni timeout: los binarios de **pyarrow exigen
  SSE4.2** y en una CPU anterior (el servidor es un Core 2 Quad de 2007)
  matan el proceso con **SIGILL, exit 132**, al primer uso y sin traceback.
  Y fastparquet **también** murió con SIGILL: desde la **2026.5.0** su
  `setup.py` fuerza `-march=x86-64-v2` siempre que el compilador lo acepte,
  sin mirar la CPU, así que ni compilarlo del fuente lo arregla. Se fija
  **`fastparquet<2026.5`** (verificado bajo QEMU con CPU Core 2 *Conroe*:
  numpy, pandas, cramjam y fastparquet 2026.3.0 arrancan; 2026.5.0 en
  adelante y pyarrow mueren). Y como la protección no puede depender de un
  pin, **la lectura de parquet corre en un subproceso** — una instrucción
  ilegal mata al hijo (código −4), el padre veta ese motor, lo publica en
  `/eea-debug` (`lectores_vetados`) y el servicio sigue intacto pase lo que
  pase. Verificado también en producción: con el fastparquet malo dentro,
  `/eea-debug` respondió con `lectores_vetados: ["fastparquet"]` en vez de
  caerse. pyarrow es opcional y se prefiere cuando la CPU lo aguanta. Detalle de todo en
  [`docs/VALIDACION.md`](docs/VALIDACION.md#10-un-módulo-de-respaldo-tumbó-el-servicio). Las coordenadas de estación salen
  del metadato del AQViewer (la API de descargas no las da): es un **ZIP con
  un CSV dentro** y **rechaza con 403 las peticiones sin User-Agent**; la URL
  está verificada contra la implementación del paquete R `EEAaq` (CRAN). Se
  cachea en disco compactado. El **contaminante de cada punto lo pone el
  parquet de datos, no el metadato**: el parquet trae su propia columna
  `Pollutant` y es la autoritativa, así que un cambio de nombre en esa columna
  del metadato ya no puede tumbar el módulo. Y queda `EEA_FORCE_SPOS` para
  fijar sampling points a mano si el metadato se mueve de sitio: con eso el
  módulo funciona sin metadato y sin coordenadas. Unidades: las del sistema,
  igual que `openaq.py` y `euskadi.py` —µg/m³ para todo salvo **CO, en
  mg/m³**—; cualquier otra unidad descarta la fila y se cuenta.
  `Validity < 1` se descarta. **Zona horaria por estación**: la EEA no
  documenta en qué zona van `Start`/`End` (formato sin desfase), pero el
  metadato declara una por estación (`Timezone`, p. ej. `UTC+01`); si el
  parquet trae la hora sin zona se interpreta en la de su estación
  (`EEA_TZ_MODE=metadato`, por defecto; `utc` para leerla como UTC). Un
  desfase global no serviría en Europa: Portugal va a UTC+0, España a UTC+1
  y Grecia a UTC+2.
- Endpoint `/eea-debug[/<geo3>]` con **cotejo estación a estación contra
  Euskadi**: cada cabina vasca se empareja con la estación EEA en su mismo
  sitio (< 500 m) y se compara serie contra serie. Mide el desfase horario
  **relativo** entre las dos fuentes y no recomienda ajustes: que discrepen
  no dice cuál tiene razón. Una serie demasiado plana para fechar (SO₂ en
  enteros bajos, que empata en todos los desfases) se marca
  `fechable: false` y no vota. La primera versión comparaba los *promedios*
  de cada fuente, que mezclan cabinas distintas, y su nota recomendaba
  ajustar `EEA_HOUR_OFFSET`, lo que habría sido un error. Aun así cazó dos
  cosas reales: **el CO salía mil veces alto** (`eea.py` lo pasaba a µg/m³
  y el sistema lo usa en mg/m³; `mediana_dif` 169,86), y el O₃ coincidía
  *exactamente* con una hora de desfase. El cotejo estación a estación lo
  confirmó en seis cabinas y cinco métricas sin excepción: **la EEA etiqueta
  los mismos valores 1 h más tarde que `euskadi.py`**. No era la zona
  horaria: el metadato declara UTC para todas. `/official-sources` enseña
  `eea` como proveedor cuando es quien cubriría la zona.
- **Endpoint `/lag-scan[/<geo3>]`: los sensores arbitran la hora.** Correla
  la serie horaria de la zona con cada fuente oficial desplazada de −3 a
  +3 h, sobre niveles y sobre diferencias hora a hora (estas afilan el
  máximo seis veces en la prueba sintética). La incertidumbre se mide con
  **remuestreo por días**: una métrica solo vota si su mejor desfase gana en
  al menos el 80 % de 200 remuestreos, y hacen falta dos de acuerdo para
  dictaminar (`hora_buena` o `desfasada`, con el valor exacto del ajuste);
  con una sola es `indicio`. La primera versión decidía con un umbral de
  margen fijo y en producción dio `hora_buena` a partir de un voto de margen
  0,07, dentro del error típico (~0,055 con n = 330). `?mac=` barre un solo
  sensor. Solo lectura: no pasa por `zone_statistics`, que escribe en el
  histórico ESOD. Por defecto 14 días (`?hours=`).
- **`EEA_HOUR_OFFSET=-1` para la red vasca.** La EEA publica los mismos
  valores que Open Data Euskadi una hora más tarde (cotejo: 23
  comparaciones idénticas al dato, sin excepción). El CSV vasco etiqueta de
  01:00 a 24:00, así que su etiqueta es el final de la hora y el sellado
  −1 de `euskadi.py` es correcto; la EEA toma ese final como `Start`. Los
  sensores no lo deciden con 14 días (márgenes dentro del ruido), pero no
  hace falta: el formato lo resuelve, y como la EEA es el respaldo de
  Euskadi, alinearlas evita que la referencia salte una hora en silencio el
  día que tome el relevo.
- **El código de país se saca del código EoI, no del nombre.** El metadato
  trae una columna `Country` con el NOMBRE (`Spain`, `Andorra`), no el ISO, y
  la API de descargas exige códigos: recortar el nombre a dos letras daba
  `SP` y `AN`, ambos inválidos, y el fallo habría sido **silencioso** —
  descarga vacía y caída a CAMS sin un solo error en el log. El código EoI
  lleva el ISO delante por convenio (`ES0001A`, `AD0942A`), y el Sampling
  Point Id también (`SPO-ES0001A-0001`). Lo encontró `sonda-eea.sh` al
  mirar la primera fila real del fichero.
- **Se descartan los puntos dados de baja.** La tabla tiene una fila por punto
  **y año**; sin filtrar, el índice incluía sampling points retirados hace
  años, que podían desplazar a estaciones vivas al recortar a
  `EEA_MAX_STATIONS`. Ahora gana la fila del año más alto de cada punto y se
  conservan solo los dos últimos años.
- **Una respuesta HTML se detecta y se dice.** Sin User-Agent el AQViewer no
  responde 403: responde **200 con una página HTML**, que es peor, porque se
  intentaría parsear como CSV. Ahora se reconoce y el error lo explica.
- `sonda-eea.sh`: comprueba de solo lectura si el metadato de la EEA se puede
  descargar y si trae las columnas que el módulo necesita, con y sin
  User-Agent. Sirve para resolver en dos minutos un cambio de URL sin tocar
  código.

### Repositorio
- **`scripts/pre-publish-check.sh` comprueba que cada `import` tiene su
  fichero.** Dos veces se publicó un `app.py` que importaba un módulo nuevo
  sin subir el módulo (`acustica` en la 1.3.0, `eea` en la 1.5.0): el
  servidor funcionaba, porque allí los ficheros se copiaron a mano, y el
  repo no arrancaba. Ahora todo import que no es de la biblioteca estándar
  ni de `requirements.txt` tiene que ser un fichero de `ai-bridge/`, o el
  audit falla. Además compila **todos** los `.py` (la lista fija de cuatro
  dejaba fuera `eea.py`, `acustica.py` y `narrador.py`) y revisa también la
  cabecera SPDX de `tests/`.
- `docker-compose.yml`: `ALERT_PERSIST_H=2` (seguía a 1, que desactiva la
  persistencia de avisos) y `EEA_HOUR_OFFSET=-1`.
- `tests/test cambios.py` y `tests/esperimento estimadores.py` pasan a
  `tests/test_cambios.py` y `tests/experimento_estimadores.py`: llevaban
  espacios en el nombre, y el segundo una errata que rompía su enlace en
  `docs/VALIDACION.md`. Se borra un `validacion.md` suelto en la raíz, copia
  de la 1.4.0 subida fuera de `docs/`.
- README (inglés y castellano), `docs/ARQUITECTURA.md` y
  `docs/REFERENCIAS_OFICIALES.md` describen la cadena con la EEA y los
  endpoints que faltaban: `/eea-debug`, `/lag-scan`, `/acustica` y
  `/narrativa`.

## [1.4.0] — 2026-11-11

### Corregido
- **El factor de corrección de PM era un cociente de dos medias.**
  `reference_mean / sensor_mean` usaba las ~46 parejas horarias de la ventana
  para calcular dos números y dividirlos; con niveles de ~5 µg/m³ el factor
  diario invertía el signo en cuatro días (+23,5 % → −17,5 % en `ezt`) y la
  banda del detector de deriva salía 42 veces más ancha que el propio factor,
  porque medía el ruido del estimador. Ahora el factor es la **mediana de los
  cocientes horarios** referencia/sensor, con IC 95 % por estadísticos de
  orden: medido por simulación, empata con el cociente de medias con ruido
  limpio y tiene 2,3–2,7 veces menos ruido —y ningún sesgo— cuando hay
  episodios locales, que es cuando el cociente además se sesgaba a corregir de
  menos. Detalle y cifras en
  [`docs/VALIDACION.md`](docs/VALIDACION.md#9-el-factor-de-corrección-era-un-cociente-de-dos-medias).
  El estimador nuevo se guarda en su propio campo (`pm_factor_med_<m>`, con IC
  en `pm_fci_lo/hi_<m>`): cambiarlo dentro de `pm_factor_<m>` habría mezclado
  dos definiciones en una serie, igual que antes se mezclaban dos bases. El
  detector de deriva juzga la serie nueva cuando acumula `DRIFT_MIN_DAYS`
  días y hasta entonces sigue con la antigua, anunciándolo en `estimador`.
  Un día con IC más ancho que `PM_FACTOR_CI_MAX` se publica como `incierto`.

### Añadido
- **`en_el_limite` en los veredictos acústicos.** Un nodo real pasó de
  `no_saludable` (Lden 53,6) a `saludable` (52,5) en cuatro días con un cambio
  de 1,1 dB: a esa distancia del umbral, los dos veredictos eran igual de
  indefendibles. A menos de `ACU_LIMITE_DB` (2 dB por defecto; la tolerancia
  de un sonómetro de clase 2 ya es ±1,5 dB y estos nodos no llegan) del
  objetivo del RD 1367/2007 o de la recomendación OMS, el estado es
  `en_el_limite` y la nota dice que decidirlo exige una medición ISO 1996-2.
  Un exceso claro en un periodo sigue dominando sobre otro en la franja.
- **Emergencia Lden−L90 por nodo** en el diagnóstico acústico. Un error de
  ganancia desplaza Lden y L90 por igual y se **cancela en la resta**, así que
  es la única magnitud comparable entre nodos sin calibrador. En el despliegue
  real los Lden abarcaban 23,3 dB entre nodos y las emergencias solo 6,6 dB:
  buena parte de aquel recorrido era calibración, no calle. No se publica
  cuando el L90 es de firmware (`l90_no_utilizable`), porque entonces la resta
  es tan falsa como él.

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