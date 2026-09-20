# Documentación

Los documentos están en español; el README del proyecto está en
[inglés](../README.md) y [español](../README.es.md).

| Documento | Qué contiene |
|---|---|
| [VALIDACION.md](VALIDACION.md) | **Empieza aquí.** Seis fallos reales que solo aparecieron al contrastar contra cabinas oficiales, con las cifras medidas. Es el argumento del proyecto. |
| [ARQUITECTURA.md](ARQUITECTURA.md) | El pipeline completo función a función, el esquema de InfluxDB, las fuentes externas y los endpoints. |
| [REFERENCIAS_OFICIALES.md](REFERENCIAS_OFICIALES.md) | Cómo funciona la cadena de referencia: descubrimiento de la red vasca, OpenAQ, selección por cobertura de métricas y conversión de unidades. |

## Atajos

Qué referencia usa cada zona:

```bash
curl -s localhost:5000/official-sources | python3 -m json.tool
```

Por qué una métrica no aparece en el análisis:

```bash
curl -s localhost:5000/analysis/<geo3> \
  | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('discarded_metrics'), indent=2, ensure_ascii=False))"
```

Resumen legible de todo:

```bash
bash scripts/check.sh <geo3>
```
