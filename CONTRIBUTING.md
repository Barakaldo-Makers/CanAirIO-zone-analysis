# Contributing

Thanks for looking. This project is small and practical, and the bar for
contributing is low — but there is one rule that matters more than the others.

## The one rule

**Show the measurement.** This is a project about not trusting numbers you have
not checked. If you change how a value is computed, corrected or labelled, say
what it produced before and after on real data. A pull request that says
"improves the correction" without a number is a pull request we cannot review.

Every fix documented in [`docs/VALIDACION.md`](docs/VALIDACION.md) follows that
shape, and they are worth skimming as examples.

## The most useful contribution

**Adding an official reference network.** OpenAQ covers roughly 100 countries,
but national and regional open-data portals are often richer: more metrics,
longer history, finer time resolution.

[`ai-bridge/euskadi.py`](ai-bridge/euskadi.py) is a complete worked example in
about 200 lines. Write a module that exposes:

```python
fetch_official_series(hours, lat=None, lon=None) -> (series, sources)
# series[metric]  = {epoch_hour_utc: value}   µg/m³, except CO in mg/m³
# sources[metric] = ["Station name", ...]
```

and add it to the chain in `fetch_official_any()` in `app.py`.

Three things that example will save you from:

- **Do not build station filenames from station names.** In the Basque portal
  `AÑORGA` becomes `ANORGA.csv` but `ZIERBENA (Puerto)` becomes
  `ZIERBENA_Puerto.csv` — case and accents follow no consistent rule. Use the
  portal's own index if it publishes one.
- **Do not pick stations by distance alone.** The nearest four stations to our
  own zone did not include the two that measure NH3 and O3. Keep fetching until
  the metrics you need are covered, with a cap.
- **Check what the hour label means.** A value labelled `14:00` is usually the
  average of `13:00–14:00`, so it should be timestamped at the *start* of the
  interval. Getting this wrong costs you correlation and is easy to miss.

## Reporting a bug

Include the endpoint you called and its JSON output. `discarded_metrics`,
`aggregation` and `official_meta` are usually where the answer is. If it is
about a value looking wrong, `bash scripts/check.sh <geo3>` prints everything
relevant in one screen.

## Style

Python, standard library plus numpy/scipy/flask/requests. No new heavy
dependencies without a reason — this runs on modest hardware by design.

Comments explain **why**, not what. If a line exists because of a bug you hit,
say which bug: future readers cannot infer it, and will otherwise "simplify" it
back into existence.

Code and comments are in Spanish, the documentation in Spanish, the README in
both. Contributions in either language are fine; do not feel obliged to
translate existing text to submit a fix.

## Licence

By contributing you agree your work is released under **GPL-3.0-or-later**, the
same licence as the project. Keep the SPDX header on new source files.
