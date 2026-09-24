# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/).
Versionado semántico.

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
