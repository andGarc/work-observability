#!/usr/bin/env python3
"""Validate the two dashboard exports and publish a static JSON snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

INDICATOR_FIELDS = {"bucket_id", "driver_id", "indicator_id", "metric_name", "indicator_status", "current_value_num", "org", "org_level", "reporting_date", "Site", "is_max_date"}
DRIVER_FIELDS = {"driver_name", "bucket_id", "display_order", "driver_id", "org", "reporting_date", "numerator", "denominator", "driver_score", "driver_status"}
INDICATOR_STATUSES = {"Healthy", "At-Risk", "Warning", "Not Available", "unk"}
DRIVER_STATUSES = {"Healthy", "At-Risk", "Warning", "Not Available"}
LEVELS = {"Sector", "Division", "Site"}
NULLS = {"", "NULL", "null", "None"}


def missing(value):
    return value is None or value.strip() in NULLS


def number(value):
    if missing(value):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def valid_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def read_csv(path, required, findings):
    name = path.name
    if not path.exists():
        findings.append(issue("error", "missing_file", f"{name} is missing", source=name))
        return [], None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        absent = sorted(required - set(reader.fieldnames or []))
        if absent:
            findings.append(issue("error", "missing_columns", f"Missing columns: {', '.join(absent)}", source=name))
            return [], digest
        rows = list(reader)
    if not rows:
        findings.append(issue("error", "empty_file", f"{name} has no data rows", source=name))
    return rows, digest


def issue(severity, code, message, **context):
    return {"severity": severity, "code": code, "message": message, **{k: v for k, v in context.items() if v is not None}}


def context(row, source):
    return {"source": source, "driver_id": row.get("driver_id"), "indicator_id": row.get("indicator_id"), "org": row.get("org"), "org_level": row.get("org_level"), "site": row.get("Site"), "reporting_date": row.get("reporting_date")}


def validate_rows(rows, kind, findings):
    source = f"vw_{kind}_final.csv"
    key_fields = ("indicator_id", "org_level", "org", "Site", "reporting_date") if kind == "indicator" else ("driver_id", "org", "reporting_date")
    counts = Counter(tuple(row.get(k) for k in key_fields) for row in rows)
    seen = set()
    for row in rows:
        ctx = context(row, source)
        key = tuple(row.get(k) for k in key_fields)
        if counts[key] > 1 and key not in seen:
            findings.append(issue("error", "duplicate_key", "Duplicate reporting key", **ctx))
            seen.add(key)
        required_values = ("bucket_id", "driver_id", "indicator_id", "metric_name", "org", "org_level", "reporting_date") if kind == "indicator" else ("bucket_id", "driver_id", "driver_name", "org", "reporting_date")
        for field in required_values:
            if missing(row.get(field)):
                findings.append(issue("error", "missing_value", f"{field} is missing", field=field, **ctx))
        if not valid_date(row.get("reporting_date")):
            findings.append(issue("error", "invalid_date", "Invalid reporting date", **ctx))
        status_field = "indicator_status" if kind == "indicator" else "driver_status"
        if row.get(status_field) not in (INDICATOR_STATUSES if kind == "indicator" else DRIVER_STATUSES):
            findings.append(issue("error", "invalid_status", f"Invalid {status_field}: {row.get(status_field)}", **ctx))
        if kind == "indicator":
            if row.get("org_level") not in LEVELS:
                findings.append(issue("error", "invalid_level", "Invalid org_level", **ctx))
            if row.get("org_level") == "Site" and missing(row.get("Site")):
                findings.append(issue("error", "missing_site", "Site-level record has no Site", **ctx))
            if row.get("is_max_date") not in {"0", "1"}:
                findings.append(issue("error", "invalid_flag", "is_max_date must be 0 or 1", **ctx))
            for field in ("current_value_num", "FTE_Percent", "threshold_blue_min", "threshold_green_min", "threshold_yellow_min", "threshold_red_min"):
                if field in row and not missing(row[field]) and number(row[field]) is None:
                    findings.append(issue("error", "invalid_number", f"Invalid {field}", field=field, **ctx))
            if missing(row.get("current_value_num")) and row.get("indicator_status") not in {"Not Available", "unk"}:
                findings.append(issue("warning", "missing_metric_value", "Available indicator has no numeric value", **ctx))
        else:
            values = {field: number(row.get(field)) for field in ("numerator", "denominator", "driver_score")}
            for field, value in values.items():
                if value is None:
                    findings.append(issue("error", "invalid_number", f"Invalid {field}", field=field, **ctx))
            n, d, score = (values[f] for f in ("numerator", "denominator", "driver_score"))
            if n is not None and d is not None and score is not None:
                if d == 0 and n != 0:
                    findings.append(issue("error", "invalid_driver_denominator", "Nonzero numerator with zero denominator", **ctx))
                expected = n / d if d else 0.0
                if not math.isclose(score, expected, rel_tol=1e-8, abs_tol=1e-8):
                    findings.append(issue("error", "driver_score_mismatch", f"driver_score {score:g} differs from numerator/denominator {expected:g}", **ctx))


def latest_indicators(rows, findings):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["indicator_id"], row["org_level"], row["org"], row["Site"])].append(row)
    latest = []
    for group in groups.values():
        dated = [r for r in group if valid_date(r["reporting_date"])]
        if not dated:
            continue
        flagged = [r for r in dated if r["is_max_date"] == "1"]
        ctx = context(max(dated, key=lambda r: r["reporting_date"]), "vw_indicator_final.csv")
        if not flagged:
            findings.append(issue("warning", "missing_max_flag", "No is_max_date=1; using greatest reporting date", **ctx))
        if len(flagged) > 1:
            findings.append(issue("error", "multiple_max_flags", f"{len(flagged)} rows have is_max_date=1", **ctx))
        selected = max(flagged or dated, key=lambda r: r["reporting_date"])
        if selected["reporting_date"] < max(r["reporting_date"] for r in dated):
            findings.append(issue("warning", "stale_max_flag", "Flagged row predates another reporting row", **ctx))
        latest.append(selected)
    return latest


def validate_mapping(indicators, drivers, findings):
    driver_map = defaultdict(set)
    for row in drivers:
        driver_map[row["driver_id"]].add((row["bucket_id"], row["driver_name"]))
    indicator_map = defaultdict(set)
    for row in indicators:
        indicator_map[row["indicator_id"]].add((row["driver_id"], row["bucket_id"], (row["metric_name"] or "").strip()))
    for indicator_id, mappings in indicator_map.items():
        if len(mappings) > 1:
            findings.append(issue("error", "indicator_mapping_conflict", f"Indicator {indicator_id} has conflicting driver, bucket, or metric mappings", indicator_id=indicator_id))
        for driver_id, bucket, _ in mappings:
            if driver_id not in driver_map:
                findings.append(issue("error", "unknown_driver", f"Indicator {indicator_id} refers to absent driver {driver_id}", driver_id=driver_id, indicator_id=indicator_id))
            elif not any(b == bucket for b, _ in driver_map[driver_id]):
                findings.append(issue("error", "bucket_mismatch", f"Indicator {indicator_id} bucket differs from driver {driver_id}", driver_id=driver_id, indicator_id=indicator_id))
    for driver_id, mappings in driver_map.items():
        if len(mappings) > 1:
            findings.append(issue("error", "driver_mapping_conflict", f"Driver {driver_id} has conflicting name or bucket", driver_id=driver_id))


def check_inventory(latest, drivers, path, findings):
    if not path.exists():
        return "unconfigured"
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
        if inventory.get("version") != 1 or not isinstance(inventory.get("indicators"), list) or not isinstance(inventory.get("drivers"), list):
            raise ValueError("expected version 1 with indicators and drivers arrays")
        for item in inventory["indicators"]:
            if not all(k in item for k in ("indicator_id", "org_level", "org", "site")):
                raise ValueError("indicator entries need indicator_id, org_level, org, site")
        for item in inventory["drivers"]:
            if not all(k in item for k in ("driver_id", "org")):
                raise ValueError("driver entries need driver_id, org")
    except (OSError, ValueError, TypeError) as exc:
        findings.append(issue("error", "invalid_inventory", f"Cannot use inventory: {exc}"))
        return "invalid"
    present_i = {(r["indicator_id"], r["org_level"], r["org"], None if missing(r["Site"]) else r["Site"]) for r in latest}
    present_d = {(r["driver_id"], r["org"]) for r in drivers}
    for item in inventory["indicators"]:
        key = (str(item["indicator_id"]), item["org_level"], item["org"], item["site"])
        if key not in present_i:
            findings.append(issue("error", "missing_required_indicator", "Required indicator/entity absent", indicator_id=key[0], org_level=key[1], org=key[2], site=key[3]))
    for item in inventory["drivers"]:
        key = (str(item["driver_id"]), item["org"])
        if key not in present_d:
            findings.append(issue("error", "missing_required_driver", "Required driver/entity absent", driver_id=key[0], org=key[1]))
    return "configured"


def check_outliers(rows, latest, findings):
    groups = defaultdict(dict)
    for row in rows:
        day = valid_date(row["reporting_date"])
        value = number(row["current_value_num"])
        if day and value is not None:
            key = (row["indicator_id"], row["org_level"], row["org"], row["Site"])
            month = (day.year, day.month)
            previous = groups[key].get(month)
            if previous is None or day > previous[0]:
                groups[key][month] = (day, value)
    insufficient = 0
    evaluated = 0
    for row in latest:
        key = (row["indicator_id"], row["org_level"], row["org"], row["Site"])
        current = number(row["current_value_num"])
        if current is None:
            continue
        selected_date = valid_date(row["reporting_date"])
        history = [value for month, (_, value) in sorted(groups[key].items()) if month < (selected_date.year, selected_date.month)]
        if len(history) < 6:
            insufficient += 1
            continue
        evaluated += 1
        median = statistics.median(history)
        mad = statistics.median(abs(v - median) for v in history)
        # When the baseline is flat, use a small relative floor to catch any material jump.
        threshold = max(5 * 1.4826 * mad, 0.2 * max(abs(median), 0.01))
        if abs(current - median) > threshold:
            findings.append(issue("warning", "historical_outlier", f"Current {current:g} differs from historical median {median:g} (n={len(history)})", **context(row, "vw_indicator_final.csv")))
    return {"evaluated": evaluated, "insufficient_history": insufficient, "minimum_prior_months": 6}


def build_drivers(drivers, latest):
    by_driver = defaultdict(list)
    for row in latest:
        by_driver[row["driver_id"]].append(row)
    result = []
    for driver_id, rows in sorted(grouped(drivers, "driver_id").items(), key=lambda pair: (pair[1][0]["bucket_id"] or "", pair[1][0]["display_order"] or "", pair[1][0]["driver_name"] or "")):
        exemplar = rows[0]
        indicators = []
        for indicator_id, records in sorted(grouped(by_driver[driver_id], "indicator_id").items(), key=lambda pair: pair[1][0]["metric_name"] or ""):
            ordered = sorted(records, key=lambda r: (r["reporting_date"] or "", r["org_level"] or "", r["org"] or "", r["Site"] or ""), reverse=True)
            indicators.append({"indicator_id": indicator_id, "metric_name": (ordered[0]["metric_name"] or "").strip(), "records": [{"reporting_date": r["reporting_date"], "org": r["org"], "org_level": r["org_level"], "site": None if missing(r["Site"]) else r["Site"], "value": number(r["current_value_num"]), "display": r.get("Display"), "status": r["indicator_status"]} for r in ordered]})
        result.append({"driver_id": driver_id, "driver_name": exemplar["driver_name"], "bucket_id": exemplar["bucket_id"], "records": [{"org": r["org"], "reporting_date": r["reporting_date"], "score": number(r["driver_score"]), "status": r["driver_status"]} for r in sorted(rows, key=lambda r: r["org"] or "")], "indicators": indicators})
    return result


def grouped(rows, key):
    result = defaultdict(list)
    for row in rows:
        result[row[key]].append(row)
    return result


def run(indicator_path, driver_path, output_dir, inventory_path, now=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    prior_path = output_dir / "results.json"
    try:
        previous = json.loads(prior_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    findings = []
    indicators, ihash = read_csv(indicator_path, INDICATOR_FIELDS, findings)
    drivers, dhash = read_csv(driver_path, DRIVER_FIELDS, findings)
    current_hashes = {"indicator": ihash, "driver": dhash}
    for name, digest in current_hashes.items():
        if digest and previous.get("source_hashes", {}).get(name) == digest:
            findings.append(issue("warning", "missed_refresh", f"{name} CSV is byte-identical to the prior run", source=f"vw_{name}_final.csv"))
    validate_rows(indicators, "indicator", findings)
    validate_rows(drivers, "driver", findings)
    validate_mapping(indicators, drivers, findings)
    latest = latest_indicators(indicators, findings)
    inventory_status = check_inventory(latest, drivers, inventory_path, findings)
    history_stats = check_outliers(indicators, latest, findings)
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    summary = dict(Counter(f["severity"] for f in findings))
    results = {"schema_version": 1, "generated_at": timestamp, "source_hashes": current_hashes, "source_rows": {"indicator": len(indicators), "driver": len(drivers)}, "summary": {"error": summary.get("error", 0), "warning": summary.get("warning", 0)}, "checks": {"inventory": inventory_status, "numeric_reconciliation": "unverified", "history": history_stats}, "findings": findings, "drivers": build_drivers(drivers, latest)}
    prior_history_path = output_dir / "history.json"
    try:
        history = json.loads(prior_history_path.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            history = []
    except (OSError, ValueError):
        history = []
    history.append({"generated_at": timestamp, "summary": results["summary"], "source_hashes": current_hashes, "source_rows": results["source_rows"]})
    (output_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output_dir / "history.json").write_text(json.dumps(history[-30:], indent=2) + "\n", encoding="utf-8")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--indicator", type=Path, default=Path("data/vw_indicator_final.csv"))
    parser.add_argument("--driver", type=Path, default=Path("data/vw_driver_final.csv"))
    parser.add_argument("--inventory", type=Path, default=Path("config/inventory.json"))
    parser.add_argument("--output", type=Path, default=Path("docs"))
    args = parser.parse_args()
    results = run(args.indicator, args.driver, args.output, args.inventory)
    print(f"Wrote {args.output / 'results.json'}: {results['summary']['error']} errors, {results['summary']['warning']} warnings")


if __name__ == "__main__":
    main()
