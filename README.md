# Daily data observability

The daily GitHub Actions run validates `data/vw_indicator_final.csv` and `data/vw_driver_final.csv`, commits `docs/results.json` and the most recent 30 entries in `docs/history.json`, then publishes `docs/` to GitHub Pages. Data quality findings are recorded in JSON and do not stop deployment. The upstream CSV refresh is separate.

Run locally with `python3 observability.py`. Open `docs/index.html` through a local HTTP server to view the dashboard, for example `python3 -m http.server --directory docs 8000`.

Enable **GitHub Actions** as the Pages build source in repository settings. The dashboard and JSON are public when the Pages site is public.

## Check behavior

- Latest indicator records are selected for each `(indicator_id, org_level, org, Site)`. A missing `is_max_date=1` falls back to the greatest reporting date with a warning. Multiple flags and stale flags are reported.
- Identical file hashes compared with the previous `results.json` produce a missed-refresh warning. This detects an unchanged daily export, including an unchanged file that was recommitted.
- Outlier checks compare the selected value with at least six earlier monthly values for the same indicator and entity. A robust median/MAD threshold with a relative floor flags large deviations. The dashboard separately counts entities lacking enough prior months. This is a screening rule, not a business threshold.
- Sector and division values appear together by indicator and reporting date; site rows are included as detail. Numeric rollup reconciliation is `unverified` until formulas and weights are configured.

## Required inventory

The presence check is `unconfigured` until `config/inventory.json` is committed. It must be version 1 and enumerate required indicator/entity and driver/entity combinations:

```json
{
  "version": 1,
  "indicators": [
    {"indicator_id": "6", "org_level": "Sector", "org": "E - Engineering & Science", "site": null}
  ],
  "drivers": [
    {"driver_id": "1", "org": "E - Engineering & Science"}
  ]
}
```

An empty list explicitly requires no entries of that type. Leave the file absent until the inventory is approved.
