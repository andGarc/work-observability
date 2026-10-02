# Daily data observability

The daily GitHub Actions run validates `data/vw_indicator_final.csv` and `data/vw_driver_final.csv`, commits `docs/results.json`, a draft inventory, and the most recent 30 entries in `docs/history.json`, then publishes `docs/` to GitHub Pages. Data quality findings are recorded in JSON and do not stop deployment. The upstream CSV refresh is separate. The monitor reads committed extracts; it does not query the live database views.

Run locally with `python3 observability.py`. Open `docs/index.html` through a local HTTP server to view the dashboard, for example `python3 -m http.server --directory docs 8000`.

Enable **GitHub Actions** as the Pages build source in repository settings. The dashboard and JSON are public when the Pages site is public.

The dashboard combines data quality with the public [GitHub workflow-runs API](https://docs.github.com/en/rest/actions/workflow-runs). A failed run, or a successful run without a matching `run_id` in published JSON, is `FAIL`. An unavailable API, unreadable JSON, or scheduled run still missing at the following day's 12:17 UTC run time is `UNKNOWN`. Data quality errors are `FAIL`; warnings are `WARN`; otherwise the status is `PASS`. Job health and data quality are displayed separately. API rate limits can temporarily make job health `UNKNOWN`.

## Check behavior

- Latest indicator records are selected for each `(indicator_id, org_level, org, Site)`. A missing `is_max_date=1` falls back to the greatest reporting date with a warning. Multiple flags and stale flags are reported.
- Identical file hashes compared with the previous `results.json` produce a missed-refresh warning. This detects an unchanged daily export, including an unchanged file that was recommitted.
- Outlier checks compare the selected value with at least six earlier monthly values for the same indicator and entity. A robust median/MAD threshold with a relative floor flags large deviations. The dashboard separately counts entities lacking enough prior months. This is a screening rule, not a business threshold.
- Sector and division values appear together by indicator and reporting date; site rows are included as detail. Numeric rollup reconciliation is `unverified` until formulas and weights are configured.
- Freshness is evaluated per driver/organization and indicator/organization/level/site, plus extract level. The prior reporting month is due at 00:00 UTC on day 15 of the current month. Before that day, the month before the prior month is due. `config/freshness.json` can set `due_day` (1–28) and monthly lag overrides, for example `{"due_day": 15, "drivers": {"1": 1}, "indicators": {"6": 2}}`.
- `config/metrics.json` can approve value ranges and thresholds by indicator ID, for example `{"indicators": {"6": {"min": 0, "max": 1, "thresholds": {"threshold_green_min": 0.96}}}}`. Direction-aware green-threshold comparisons produce provisional warnings. Driver score/status rules and numeric sector/division rollups remain unverified.
- History stores grouped current-period counts, status mix, nulls, zero denominators, active indicators, thresholds, mappings, schema, and organization levels. Large changes and cycle count drops generate warnings.

## Required inventory

The monitor writes `config/inventory.draft.json` from the current CSVs for review. Its `approval: draft` marker means it is **not** an approved baseline. The presence check is `unconfigured` until a reviewed `config/inventory.json` is committed. It must be version 1 and enumerate required indicator/entity and driver/entity combinations:

```json
{
  "version": 1,
  "approval": "approved",
  "indicators": [
    {"indicator_id": "6", "org_level": "Sector", "org": "E - Engineering & Science", "site": null}
  ],
  "drivers": [
    {"driver_id": "1", "org": "E - Engineering & Science"}
  ]
}
```

An empty list explicitly requires no entries of that type. Leave the file absent until the inventory is approved. Approved inventory checks flag missing and unexpected combinations and organization-level changes.

Run `python3 -m unittest discover -s tests` and `node tests/test_health.js` for monitor and dashboard health tests.
