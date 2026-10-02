import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from observability import run


INDICATOR_FIELDS = ["bucket_id", "driver_id", "indicator_id", "metric_name", "indicator_status", "current_value_num", "org", "org_level", "reporting_date", "Site", "is_max_date"]
DRIVER_FIELDS = ["driver_name", "bucket_id", "display_order", "driver_id", "org", "reporting_date", "numerator", "denominator", "driver_score", "driver_status"]


def indicator(indicator_id="10", date="2026-07-01", org="Sector A", level="Sector", site="NULL", flag="1", value="1", driver_id="1"):
    return dict(bucket_id="1", driver_id=driver_id, indicator_id=indicator_id, metric_name=f"Metric {indicator_id}", indicator_status="Healthy", current_value_num=value, org=org, org_level=level, reporting_date=date, Site=site, is_max_date=flag)


def driver(driver_id="1", date="2026-07-01", org="Sector A", numerator="2", denominator="1", score="2"):
    return dict(driver_name=f"Driver {driver_id}", bucket_id="1", display_order="1", driver_id=driver_id, org=org, reporting_date=date, numerator=numerator, denominator=denominator, driver_score=score, driver_status="Healthy")


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class ObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.indicator_path = self.root / "vw_indicator_final.csv"
        self.driver_path = self.root / "vw_driver_final.csv"
        self.output = self.root / "docs"
        self.inventory = self.root / "inventory.json"

    def tearDown(self):
        self.temp.cleanup()

    def check(self, indicators, drivers):
        write_csv(self.indicator_path, INDICATOR_FIELDS, indicators)
        write_csv(self.driver_path, DRIVER_FIELDS, drivers)
        return run(self.indicator_path, self.driver_path, self.output, self.inventory, datetime(2026, 10, 1, tzinfo=timezone.utc))

    def test_independent_dates_flags_and_site_detail(self):
        rows = [
            indicator(date="2026-06-01", flag="1"),
            indicator(date="2026-07-01", flag="0"),
            indicator("11", "2026-05-01", flag="0"),
            indicator("11", "2026-07-01", flag="0"),
            indicator("12", "2026-06-01", flag="1"),
            indicator("12", "2026-07-01", flag="1"),
            indicator("10", "2026-06-01", org="Division B", level="Division"),
            indicator("10", "2026-06-01", org="Division B", level="Site", site="WH"),
        ]
        result = self.check(rows, [driver()])
        codes = {finding["code"] for finding in result["findings"]}
        self.assertTrue({"stale_max_flag", "missing_max_flag", "multiple_max_flags"} <= codes)
        records = {i["indicator_id"]: i["records"] for i in result["drivers"][0]["indicators"]}
        self.assertEqual({r["reporting_date"] for r in records["10"]}, {"2026-06-01"})
        self.assertEqual({r["reporting_date"] for r in records["11"]}, {"2026-07-01"})
        self.assertIn("WH", {r["site"] for r in records["10"]})
        self.assertEqual(result["checks"]["numeric_reconciliation"], "unverified")

    def test_refresh_inventory_invalid_values_and_score(self):
        rows = [indicator(value="broken"), indicator("11", value="NaN")]
        drivers = [driver(score="99")]
        first = self.check(rows, drivers)
        self.assertEqual(first["checks"]["inventory"], "unconfigured")
        self.assertTrue((self.output / "results.json").exists())
        self.assertGreater(first["summary"]["error"], 0)
        self.assertTrue({"invalid_number", "driver_score_mismatch"} <= {f["code"] for f in first["findings"]})
        self.inventory.write_text(json.dumps({"version": 1, "approval": "approved", "indicators": [{"indicator_id": "404", "org_level": "Sector", "org": "Sector A", "site": None}], "drivers": [{"driver_id": "404", "org": "Sector A"}]}))
        second = self.check(rows, drivers)
        self.assertTrue({"missed_refresh", "missing_required_indicator", "missing_required_driver"} <= {f["code"] for f in second["findings"]})
        self.assertEqual(second["checks"]["inventory"], "configured")
        self.assertEqual(len(json.loads((self.output / "history.json").read_text())), 2)

    def test_history_outlier_and_mapping(self):
        dates = [f"2026-{month:02d}-01" for month in range(1, 8)]
        rows = [indicator(date=day, flag="1" if index == 6 else "0", value="10" if index == 6 else "1") for index, day in enumerate(dates)]
        rows.append(indicator("20", driver_id="404"))
        result = self.check(rows, [driver()])
        self.assertTrue({"historical_outlier", "unknown_driver"} <= {f["code"] for f in result["findings"]})
        self.assertEqual(result["checks"]["history"]["evaluated"], 1)
        self.assertEqual(result["checks"]["history"]["insufficient_history"], 1)

    def test_duplicate_keys_and_bad_date_still_publish_json(self):
        rows = [indicator(), indicator(), indicator("12", date="bad-date")]
        result = self.check(rows, [driver()])
        self.assertTrue({"duplicate_key", "invalid_date"} <= {f["code"] for f in result["findings"]})
        self.assertEqual(json.loads((self.output / "results.json").read_text())["summary"], result["summary"])

    def test_empty_and_unreadable_sources_publish(self):
        self.indicator_path.write_bytes(b"\xff\xfe")
        write_csv(self.driver_path, DRIVER_FIELDS, [])
        result = run(self.indicator_path, self.driver_path, self.output, self.inventory, datetime(2026, 10, 1, tzinfo=timezone.utc), run_id="123")
        self.assertEqual(result["run_id"], "123")
        self.assertEqual(result["data_status"], "FAIL")
        self.assertTrue({"unreadable_file", "empty_file"} <= {f["code"] for f in result["findings"]})
        self.assertTrue((self.output / "results.json").exists())

    def test_monthly_deadline_and_lag(self):
        fresh = self.root / "freshness.json"
        fresh.write_text(json.dumps({"due_day": 15, "drivers": {"1": 1}, "indicators": {"10": 1}}))
        write_csv(self.indicator_path, INDICATOR_FIELDS, [indicator(date="2026-07-01")])
        write_csv(self.driver_path, DRIVER_FIELDS, [driver(date="2026-07-01")])
        before = run(self.indicator_path, self.driver_path, self.output, self.inventory, datetime(2026, 9, 14, tzinfo=timezone.utc), freshness_path=fresh)
        self.assertEqual(before["freshness"]["indicator"]["required_month"], "2026-07-01")
        self.assertFalse(before["freshness"]["indicator"]["entities"][0]["stale"])
        after = run(self.indicator_path, self.driver_path, self.output, self.inventory, datetime(2026, 10, 15, tzinfo=timezone.utc), freshness_path=fresh)
        self.assertEqual(after["freshness"]["driver"]["required_month"], "2026-09-01")
        self.assertTrue(after["freshness"]["driver"]["entities"][0]["stale"])

    def test_inventory_additions_zero_denominator_and_flags(self):
        self.inventory.write_text(json.dumps({"version": 1, "approval": "approved", "indicators": [{"indicator_id": "404", "org_level": "Sector", "org": "Sector A", "site": None}], "drivers": []}))
        rows = [indicator(), indicator(date="2026-06-01", flag="1")]
        result = self.check(rows, [driver(numerator="0", denominator="0", score="1")])
        codes = {f["code"] for f in result["findings"]}
        self.assertTrue({"multiple_max_flags", "missing_required_indicator", "unexpected_indicator", "unexpected_driver", "zero_denominator_convention"} <= codes)
        self.assertEqual(json.loads((self.root / "inventory.draft.json").read_text())["approval"], "draft")

    def test_composition_drift_and_approved_metric(self):
        metric = self.root / "metrics.json"
        metric.write_text(json.dumps({"indicators": {"10": {"min": 0, "max": 1, "thresholds": {"threshold_green_min": 0.8}}}}))
        first = [indicator(value="0.7") for _ in range(4)]
        for index, row in enumerate(first):
            row["org"] = f"Sector {index}"
        write_csv(self.indicator_path, INDICATOR_FIELDS, first)
        write_csv(self.driver_path, DRIVER_FIELDS, [driver()])
        run(self.indicator_path, self.driver_path, self.output, self.inventory, datetime(2026, 10, 1, tzinfo=timezone.utc), metric_path=metric)
        changed = indicator(value="2")
        changed["threshold_green_min"] = "0.9"
        write_csv(self.indicator_path, INDICATOR_FIELDS + ["threshold_green_min", "is_higher_better"], [{**changed, "is_higher_better": "1"}])
        result = run(self.indicator_path, self.driver_path, self.output, self.inventory, datetime(2026, 10, 1, tzinfo=timezone.utc), metric_path=metric)
        codes = {f["code"] for f in result["findings"]}
        self.assertTrue({"indicator_out_of_range", "threshold_change", "schema_change", "driver_volume_drop"} <= codes)

    def test_mixed_driver_dates_negative_values_and_provisional_status(self):
        metric = self.root / "metrics.json"
        metric.write_text(json.dumps({"indicators": {"10": {"min": 0, "max": 1}}}))
        first = {**indicator(value="0.2"), "threshold_green_min": "0.8", "is_higher_better": "1"}
        write_csv(self.indicator_path, INDICATOR_FIELDS + ["threshold_green_min", "is_higher_better"], [first])
        write_csv(self.driver_path, DRIVER_FIELDS, [driver(date="2026-09-01"), driver(driver_id="2", date="2026-06-01", numerator="-1", denominator="1", score="-1")])
        result = run(self.indicator_path, self.driver_path, self.output, self.inventory, datetime(2026, 10, 1, tzinfo=timezone.utc), metric_path=metric)
        entities = result["freshness"]["driver"]["entities"]
        self.assertEqual({e["driver_id"]: e["stale"] for e in entities}, {"1": False, "2": True})
        self.assertTrue({"negative_driver_component", "provisional_status_mismatch"} <= {f["code"] for f in result["findings"]})

    def test_non_site_field_does_not_distinguish_reporting_key(self):
        result = self.check([indicator(site="North"), indicator(site="South")], [driver()])
        self.assertIn("duplicate_key", {f["code"] for f in result["findings"]})


if __name__ == "__main__":
    unittest.main()
