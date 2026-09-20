# Referencias oficiales: red vasca automática + OpenAQ global

**canairio-zone-analysis** · septiembre de 2026

Antes el sistema validaba los sensores contra tres cosas, y solo una de ellas
eran estaciones reales:

| Zona | Referencia | Qué era |
|---|---|---|
| `ezt` Barakaldo | 3 estaciones vascas escritas a mano + CAMS | estaciones reales |
| `u33` Berlín | CAMS | **modelo** |
| `c3n` Calgary | CAMS | **modelo** |
| `d29` Cali | CAMS | **modelo** |
| `d2g` Bogotá | CAMS | **modelo** |

Ahora las cinco se validan contra estaciones físicas, y sin configurar nada por
zona.

---

## 1. Cadena de referencia

```
zone_sensor_center(geo3)          centroide REAL de los sensores de la zona
        │
        ▼
fetch_official_any(geo3, hours)
        │
        ├─ 1. euskadi.py   ¿hay estación vasca a menos de 25 km?  → la usa
        ├─ 2. openaq.py    ¿hay estación oficial a menos de 25 km? → la usa
        └─ 3. (ninguna)    → queda CAMS, como antes
```

La confianza final de cada métrica sigue siendo la mejor correlación entre CAMS
y la referencia oficial, y ahora anota cuál ganó: `source` vale `euskadi`,
`openaq` o `cams`.

### Por qué el centroide y no el centro del geohash

Una celda geohash de 3 caracteres mide unos **156 km de lado**. Su centro
geométrico puede quedar a 100 km de los sensores reales, así que buscar
estaciones "en 25 km alrededor del centro de la celda" habría dado resultados
absurdos. `zone_sensor_center()` promedia las posiciones decodificadas del
geohash fino de 7 caracteres de los sensores que están emitiendo de verdad, y
solo cae al centro de la celda si ninguno publica posición.

---

## 2. Red vasca: de lista fija a descubrimiento

**Antes:** `EUSKADI_STATIONS=BARAKALDO,UNIDAD_MOVIL_10,UNIDAD_MOVIL_2` y
`ZONE_HAS_OFFICIAL=ezt`. Cualquier zona nueva de Euskadi no se validaba, y si
el Gobierno Vasco movía una unidad móvil había que editar el compose.

**Ahora:** el módulo descubre la red entera en cada arranque:

1. `estaciones.csv` → las 62 estaciones con latitud y longitud.
2. `url.txt` → las URLs reales de cada CSV horario.
3. `nearest_stations(lat, lon)` → las candidatas del radio, por distancia.

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

Mayúsculas y minúsculas cambian según el caso. Una regla fallaría en la mitad;
`url.txt` es la fuente de verdad y el slug calculado solo sirve para emparejar.

### Selección por cobertura, no solo por distancia

Este era el punto delicado. Por pura distancia, Barakaldo elegía:

```
0.01 km  BARAKALDO
0.93 km  ERANDIO
1.27 km  SESTAO
1.57 km  MUNOA
```

…y **se dejaba fuera las unidades móviles de Zorroza y Elorrieta, que son las
únicas que miden NH3 y O3** — exactamente las que habíamos añadido a mano por
ese motivo. Habríamos perdido la validación de NH3.

La solución es no parar en las N más cercanas: recorrer las candidatas en orden
de distancia y seguir descargando mientras falten métricas de
`EUSKADI_TARGET_METRICS`, con un tope de `EUSKADI_MAX_FETCH` descargas.
Verificado en pruebas: baja 6 CSV y cubre las 7 métricas.

```
descargados: BARAKALDO, ERANDIO, SESTAO, MUNOA, UNIDAD_MOVIL_2, UNIDAD_MOVIL_10
pm25 OK  pm10 OK  no2 OK  o3 OK  nh3 OK  co OK  so2 OK
```

### Dos correcciones de paso

**Sellado horario.** El CSV etiqueta las horas 01:00…24:00 y el valor "H" es el
promedio del intervalo `[H-1, H)`. El código anterior usaba `h0 = hh` para las
horas 1-23 (desplazando todo **+1 hora**) y `24 → 0` del mismo día
(desplazando **−23 horas**). Ahora se aplica `EUSKADI_HOUR_OFFSET`, por defecto
`-1`, que sella al inicio del intervalo.

Esto debería **subir** las correlaciones, pero como afecta a los números ya
validados (PM2.5 rho 0,60-0,70) conviene comprobarlo empíricamente:

```bash
# con la corrección (por defecto)
curl -s localhost:5000/compare-official/ezt | python3 -m json.tool | grep -A2 spearman

# volviendo al comportamiento anterior, para comparar
docker compose exec -e EUSKADI_HOUR_OFFSET=0 ai-bridge \
  python -c "import euskadi; print(euskadi.EUSKADI_HOUR_OFFSET)"
```
Si el rho baja, pon `EUSKADI_HOUR_OFFSET=0` en el compose y avísame.

**Zona horaria.** `time.mktime(...) - time.timezone` interpretaba la hora GMT
pasando por la zona local del servidor, lo que era correcto solo por
casualidad. Ahora usa `calendar.timegm`, que no depende de la TZ.

---

## 3. OpenAQ: referencia global

`openaq.py` tiene el mismo contrato que `euskadi.py`, así que `app.py` los usa
indistintamente.

```
GET /v3/locations?coordinates=LAT,LON&radius=METROS&monitor=true&mobile=false
GET /v3/sensors/{id}/hours?datetime_from=..&datetime_to=..
Cabecera: X-API-Key
```

Decisiones que importan:

- **`monitor=true`** — solo monitores de referencia regulatorios. Sin este
  filtro entrarían sensores de bajo coste de la propia red OpenAQ, y
  estaríamos validando un sensor barato contra otro sensor barato.
- **`mobile=false`** — las estaciones móviles cambian de sitio y rompen la
  comparación.
- **Radio máximo 25 km**, que es el límite de la API. Se recorta si se pide más.
- **Conversión de unidades.** OpenAQ devuelve cada parámetro con la unidad del
  proveedor: µg/m³, ppm o ppb según el país. Se normaliza a la convención del
  sistema (µg/m³, salvo CO en mg/m³) con el volumen molar a la **T y P reales
  de la zona**, que se le pasan desde el análisis. Si la unidad no se reconoce,
  el punto se descarta en vez de inventarlo. Verificado contra los valores de
  referencia a 25 °C:

  | Entrada | Salida | Referencia |
  |---|---|---|
  | NO2 20 ppb | 37,61 µg/m³ | 1 ppb = 1,88 µg/m³ |
  | O3 50 ppb | 98,09 µg/m³ | 1 ppb = 1,96 µg/m³ |
  | CO 1 ppm | 1,145 mg/m³ | 1 ppm = 1,145 mg/m³ |

- **Cobertura horaria.** Se descartan las horas con `coverage.percentComplete`
  por debajo de `OPENAQ_MIN_COVERAGE` (50 %): una hora construida con dos
  muestras no es comparable con una hora completa.
- **Límite de peticiones.** `OPENAQ_MIN_INTERVAL` espacia las llamadas y
  `OPENAQ_MAX_SENSORS` acota el gasto por zona. Las estaciones se cachean 24 h
  y las series 1 h.

---

## 4. Endpoints nuevos

```
/official-sources              qué referencia le toca a cada zona activa
/official-sources?max_tier=3   incluyendo zonas dormidas
/euskadi-debug                 índice, selección por cercanía y cobertura (zona ezt)
/euskadi-debug/<geo3>          lo mismo para otra zona
/openaq-debug                  estaciones OpenAQ y métricas (zona u33)
/openaq-debug/<geo3>           lo mismo para otra zona
```

`/compare-official/<geo3>` ahora incluye `official_provider` y `official_meta`
(centro usado, estaciones y distancias).

---

## 5. Pendiente

- **`index.html`**: los JSON publicados ya llevan `ref_source` y `ref_stations`
  por zona, pero la web todavía no los muestra. Cuando quieras, le añado una
  etiqueta del tipo "validado contra: Berlin Mitte (OpenAQ)" en la tarjeta.
- **`telegraf.conf`**: sigo sin tenerlo.
- **Informes de Telegram**: aún dicen "estaciones oficiales" en genérico; se
  puede hacer que nombren la fuente por zona.
