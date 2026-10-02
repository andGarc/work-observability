#!/usr/bin/env python3
"""Validate the two dashboard exports and publish a static JSON snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
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
        return [], None, []
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream, restkey="__extra__")
            absent = sorted(required - set(reader.fieldnames or []))
            if absent:
                findings.append(issue("error", "missing_columns", f"Missing columns: {', '.join(absent)}", source=name))
                return [], digest, sorted(reader.fieldnames or [])
            rows = list(reader)
            if any("__extra__" in row for row in rows):
                findings.append(issue("error", "malformed_csv", "Rows contain more fields than the header", source=name))
    except (OSError, UnicodeError, csv.Error) as exc:
        findings.append(issue("error", "unreadable_file", f"Cannot read {name}: {exc}", source=name))
        return [], None, []
    if not rows:
        findings.append(issue("error", "empty_file", f"{name} has no data rows", source=name))
    return rows, digest, sorted(reader.fieldnames or [])


def issue(severity, code, message, **context):
    return {"severity": severity, "code": code, "message": message, **{k: v for k, v in context.items() if v is not None}}


def context(row, source):
    return {"source": source, "driver_id": row.get("driver_id"), "indicator_id": row.get("indicator_id"), "org": row.get("org"), "org_level": row.get("org_level"), "site": row.get("Site"), "reporting_date": row.get("reporting_date")}


def entity_key(row, kind):
    if kind == "indicator":
        site = row.get("Site") if row.get("org_level") == "Site" and not missing(row.get("Site")) else None
        return (row.get("bucket_id"), row.get("driver_id"), row.get("indicator_id"), row.get("org"), row.get("org_level"), site)
    return (row.get("bucket_id"), row.get("driver_id"), row.get("org"))


def validate_rows(rows, kind, findings):
    source = f"vw_{kind}_final.csv"
    counts = Counter((*entity_key(row, kind), row.get("reporting_date")) for row in rows)
    seen = set()
    for row in rows:
        ctx = context(row, source)
        key = (*entity_key(row, kind), row.get("reporting_date"))
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
            for field in ("created_date", "updated_date"):
                if field in row and not missing(row[field]):
                    try:
                        datetime.fromisoformat(row[field])
                    except ValueError:
                        findings.append(issue("error", "invalid_date", f"Invalid {field}", field=field, **ctx))
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
            if n is not None and n < 0 or d is not None and d < 0:
                findings.append(issue("error", "negative_driver_component", "Driver numerator and denominator must be nonnegative", **ctx))
            if n is not None and d is not None and score is not None:
                if d == 0 and n != 0:
                    findings.append(issue("error", "invalid_driver_denominator", "Nonzero numerator with zero denominator", **ctx))
                expected = n / d if d else 0.0
                if not math.isclose(score, expected, rel_tol=1e-8, abs_tol=1e-8):
                    findings.append(issue("error", "driver_score_mismatch", f"driver_score {score:g} differs from numerator/denominator {expected:g}", **ctx))
                if n == d == 0 and (score != 0 or row.get("driver_status") != "Not Available"):
                    findings.append(issue("error", "zero_denominator_convention", "0/0 requires score 0 and Not Available", **ctx))


def latest_indicators(rows, findings):
    groups = defaultdict(list)
    for row in rows:
        groups[entity_key(row, "indicator")].append(row)
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
        if selected.get("indicator_status") == "unk":
            findings.append(issue("warning", "unknown_indicator_status", "Current indicator status is unk", **context(selected, "vw_indicator_final.csv")))
    return latest


def deadline(now, due_day):
    today = now.astimezone(timezone.utc).date()
    month_start = today.replace(day=1)
    return (month_start - timedelta(days=1)) if today.day >= due_day else (month_start.replace(day=1) - timedelta(days=1)).replace(day=1) - timedelta(days=1)


def freshness(rows, kind, now, config, findings):
    due_day = config.get("due_day", 15)
    if not isinstance(due_day, int) or not 1 <= due_day <= 28:
        raise ValueError("freshness due_day must be an integer from 1 to 28")
    required_month = deadline(now, due_day).replace(day=1)
    groups = defaultdict(list)
    for row in rows:
        groups[entity_key(row, kind)].append(row)
    entities = []
    overrides = config.get("indicators" if kind == "indicator" else "drivers", {})
    for group in groups.values():
        dated = [r for r in group if valid_date(r.get("reporting_date"))]
        if not dated:
            continue
        row = max(dated, key=lambda r: r["reporting_date"])
        lag = overrides.get(row["indicator_id" if kind == "indicator" else "driver_id"], 0)
        if not isinstance(lag, int) or lag < 0:
            raise ValueError("freshness lag overrides must be nonnegative integers")
        expected = required_month
        for _ in range(lag):
            expected = (expected.replace(day=1) - timedelta(days=1)).replace(day=1)
        latest_day = valid_date(row["reporting_date"])
        item = {**context(row, f"vw_{kind}_final.csv"), "latest_reporting_date": row["reporting_date"], "required_month": expected.isoformat(), "stale": latest_day < expected}
        if kind == "indicator":
            item.update({"latest_created_date": row.get("created_date"), "latest_updated_date": row.get("updated_date")})
        entities.append(item)
        if item["stale"]:
            findings.append(issue("warning", "stale_entity", f"Latest reporting date {latest_day} precedes required month {expected}", **context(row, f"vw_{kind}_final.csv")))
    latest = max((valid_date(r.get("reporting_date")) for r in rows if valid_date(r.get("reporting_date"))), default=None)
    if latest and latest < required_month:
        findings.append(issue("warning", "stale_extract", f"Extract latest reporting date {latest} precedes required month {required_month}", source=f"vw_{kind}_final.csv"))
    result = {"readable": True, "row_count": len(rows), "latest_reporting_date": latest.isoformat() if latest else None, "required_month": required_month.isoformat(), "entities": entities}
    if kind == "indicator":
        for field in ("created_date", "updated_date"):
            dates = [r.get(field) for r in rows if r.get(field) and not missing(r[field])]
            result[f"latest_{field}"] = max(dates) if dates else None
    return result


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
        if inventory.get("approval") == "draft":
            return "unconfigured"
        if inventory.get("version") != 1 or inventory.get("approval") != "approved" or not isinstance(inventory.get("indicators"), list) or not isinstance(inventory.get("drivers"), list):
            raise ValueError("expected version 1, approval=approved, and indicators and drivers arrays")
        for item in inventory["indicators"]:
            if not all(k in item for k in ("indicator_id", "org_level", "org", "site")):
                raise ValueError("indicator entries need indicator_id, org_level, org, site")
        for item in inventory["drivers"]:
            if not all(k in item for k in ("driver_id", "org")):
                raise ValueError("driver entries need driver_id, org")
    except (OSError, ValueError, TypeError) as exc:
        findings.append(issue("error", "invalid_inventory", f"Cannot use inventory: {exc}"))
        return "invalid"
    present_i = {(r["indicator_id"], r["org_level"], r["org"], r["Site"] if r["org_level"] == "Site" and not missing(r["Site"]) else None) for r in latest}
    present_d = {(r["driver_id"], r["org"]) for r in drivers}
    required_i = {(str(i["indicator_id"]), i["org_level"], i["org"], i["site"]) for i in inventory["indicators"]}
    required_d = {(str(i["driver_id"]), i["org"]) for i in inventory["drivers"]}
    for item in inventory["indicators"]:
        key = (str(item["indicator_id"]), item["org_level"], item["org"], item["site"])
        if key not in present_i:
            findings.append(issue("error", "missing_required_indicator", "Required indicator/entity absent", indicator_id=key[0], org_level=key[1], org=key[2], site=key[3]))
    for item in inventory["drivers"]:
        key = (str(item["driver_id"]), item["org"])
        if key not in present_d:
            findings.append(issue("error", "missing_required_driver", "Required driver/entity absent", driver_id=key[0], org=key[1]))
    for indicator_id, level, org, site in sorted(present_i - required_i, key=str):
        findings.append(issue("warning", "unexpected_indicator", "Indicator/entity absent from approved inventory", indicator_id=indicator_id, org_level=level, org=org, site=site))
    for driver_id, org in sorted(present_d - required_d, key=str):
        findings.append(issue("warning", "unexpected_driver", "Driver/entity absent from approved inventory", driver_id=driver_id, org=org))
    expected_levels = defaultdict(set)
    for indicator_id, level, org, _ in required_i:
        expected_levels[(indicator_id, org)].add(level)
    for indicator_id, level, org, site in present_i - required_i:
        if (indicator_id, org) in expected_levels and level not in expected_levels[(indicator_id, org)]:
            findings.append(issue("warning", "hierarchy_change", "Organization level differs from approved inventory", indicator_id=indicator_id, org_level=level, org=org, site=site))
    return "configured"


def draft_inventory(indicators, drivers, path):
    draft = {"version": 1, "approval": "draft", "indicators": [], "drivers": []}
    seen = set()
    for row in indicators:
        key = (row.get("indicator_id"), row.get("org_level"), row.get("org"), row.get("Site") if row.get("org_level") == "Site" and not missing(row.get("Site")) else None)
        if key not in seen:
            draft["indicators"].append(dict(zip(("indicator_id", "org_level", "org", "site"), key)))
            seen.add(key)
    seen.clear()
    for row in drivers:
        key = (row.get("driver_id"), row.get("org"))
        if key not in seen:
            draft["drivers"].append(dict(zip(("driver_id", "org"), key)))
            seen.add(key)
    draft["indicators"].sort(key=lambda item: str(tuple(item.values())))
    draft["drivers"].sort(key=lambda item: str(tuple(item.values())))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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


def load_config(path, default, findings, code):
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("configuration must be an object")
        return value
    except (OSError, ValueError) as exc:
        findings.append(issue("error", code, f"Cannot use {path.name}: {exc}"))
        return default


def metric_checks(latest, config, findings):
    metrics = config.get("indicators", {})
    for row in latest:
        spec = metrics.get(row["indicator_id"])
        if not spec:
            continue
        ctx = context(row, "vw_indicator_final.csv")
        value = number(row.get("current_value_num"))
        if value is not None and ("min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]):
            findings.append(issue("error", "indicator_out_of_range", "Value outside approved metric range", **ctx))
        for field, approved in spec.get("thresholds", {}).items():
            observed = number(row.get(field))
            if observed is None or not math.isclose(observed, approved, rel_tol=1e-9, abs_tol=1e-9):
                findings.append(issue("warning", "threshold_change", f"{field} differs from approved value", field=field, **ctx))
        # The extract has no authoritative status formula. This comparison is advisory.
        green = number(row.get("threshold_green_min"))
        if value is not None and green is not None and row.get("is_higher_better") in {"0", "1"} and row.get("indicator_status") in {"Healthy", "At-Risk", "Warning"}:
            higher = row.get("is_higher_better") == "1"
            healthy = value >= green if higher else value <= green
            if healthy != (row["indicator_status"] == "Healthy"):
                findings.append(issue("warning", "provisional_status_mismatch", "Status differs from direction-aware green threshold; formula is unverified", **ctx))


def composition(indicators, drivers, indicator_fields, driver_fields):
    latest_month = max((r.get("reporting_date", "")[:7] for r in indicators + drivers if valid_date(r.get("reporting_date"))), default=None)
    groups = {}
    for kind, rows, fields in (("indicator", indicators, indicator_fields), ("driver", drivers, driver_fields)):
        current = [r for r in rows if latest_month and r.get("reporting_date", "").startswith(latest_month)]
        status = "indicator_status" if kind == "indicator" else "driver_status"
        by_driver_status = defaultdict(Counter)
        by_driver_nulls = Counter()
        by_driver_zeros = Counter()
        for row in current:
            driver_id = row.get("driver_id")
            by_driver_status[driver_id][row.get(status)] += 1
            if kind == "indicator" and missing(row.get("current_value_num")) or kind == "driver" and any(missing(row.get(k)) for k in ("numerator", "denominator", "driver_score")):
                by_driver_nulls[driver_id] += 1
            if kind == "driver" and number(row.get("denominator")) == 0:
                by_driver_zeros[driver_id] += 1
        groups[kind] = {
            "schema": fields,
            "row_count": len(rows),
            "current_month": latest_month,
            "current_count": len(current),
            "status_mix": dict(Counter(r.get(status) for r in current)),
            "by_driver": dict(Counter(r.get("driver_id") for r in current)),
            "by_driver_status": {key: dict(value) for key, value in by_driver_status.items()},
            "by_driver_nulls": dict(by_driver_nulls),
            "by_driver_zeros": dict(by_driver_zeros),
            "by_level": dict(Counter(r.get("org_level") for r in current)) if kind == "indicator" else {},
            "org_levels": sorted({(r.get("indicator_id"), r.get("org"), r.get("org_level")) for r in current}) if kind == "indicator" else [],
            "null_values": sum(missing(r.get("current_value_num")) for r in current) if kind == "indicator" else sum(any(missing(r.get(k)) for k in ("numerator", "denominator", "driver_score")) for r in current),
            "zero_denominators": sum(number(r.get("denominator")) == 0 for r in current) if kind == "driver" else 0,
            "active_indicators": sorted({r.get("indicator_id") for r in current if r.get("is_active") == "1"}) if kind == "indicator" else [],
            "thresholds": sorted({(r.get("indicator_id"), r.get("threshold_blue_min"), r.get("threshold_green_min"), r.get("threshold_yellow_min"), r.get("threshold_red_min")) for r in current}) if kind == "indicator" else [],
            "mappings": sorted({(r.get("indicator_id"), r.get("driver_id"), r.get("bucket_id")) for r in current}) if kind == "indicator" else sorted({(r.get("driver_id"), r.get("bucket_id")) for r in current}),
        }
    return groups


def compare_composition(current, previous, findings):
    for kind in ("indicator", "driver"):
        before = previous.get(kind, {})
        after = current[kind]
        if not before:
            continue
        for field in ("schema", "active_indicators", "thresholds", "mappings", "by_level", "org_levels"):
            if field not in before:
                continue
            if before.get(field) != after.get(field):
                before_values = {tuple(v) if isinstance(v, list) else v for v in before.get(field, [])} if field in {"active_indicators", "thresholds", "mappings", "org_levels"} else set()
                after_values = {tuple(v) if isinstance(v, list) else v for v in after.get(field, [])} if field in {"active_indicators", "thresholds", "mappings", "org_levels"} else set()
                changed = before_values ^ after_values if before_values or after_values else {None}
                for value in changed:
                    identifier = value[0] if isinstance(value, tuple) else value
                    details = {"indicator_id": identifier} if kind == "indicator" and field not in {"schema", "by_level"} else {"driver_id": identifier} if kind == "driver" and field == "mappings" else {}
                    if field == "org_levels" and isinstance(value, tuple):
                        details.update(org=value[1], org_level=value[2])
                    findings.append(issue("warning", f"{field}_change", f"{kind} {field.replace('_', ' ')} changed since prior run", source=f"vw_{kind}_final.csv", reporting_period=after["current_month"], **details))
        for field in ("current_count", "null_values", "zero_denominators"):
            old, new = before.get(field, 0), after[field]
            if old and abs(new - old) >= max(3, old * .25):
                findings.append(issue("warning", f"{field}_change", f"{kind} {field.replace('_', ' ')} changed from {old} to {new}", source=f"vw_{kind}_final.csv", reporting_period=after["current_month"]))
        for driver_id in set(before.get("by_driver", {})) | set(after["by_driver"]):
            old, new = before.get("by_driver", {}).get(driver_id, 0), after["by_driver"].get(driver_id, 0)
            if old and new < old * .75:
                findings.append(issue("warning", "driver_volume_drop", f"Current records dropped from {old} to {new}", driver_id=driver_id, reporting_period=after["current_month"]))
            for field in ("by_driver_nulls", "by_driver_zeros"):
                if field not in before:
                    continue
                old_value = before.get(field, {}).get(driver_id, 0)
                new_value = after[field].get(driver_id, 0)
                if abs(new_value - old_value) >= max(3, old_value * .25):
                    findings.append(issue("warning", f"{field}_change", f"{kind} {field.replace('_', ' ')} changed from {old_value} to {new_value}", driver_id=driver_id, reporting_period=after["current_month"]))
            if "by_driver_status" in before:
                old_mix = before["by_driver_status"].get(driver_id, {})
                new_mix = after["by_driver_status"].get(driver_id, {})
                for status in set(old_mix) | set(new_mix):
                    old_value, new_value = old_mix.get(status, 0), new_mix.get(status, 0)
                    if old_value and abs(new_value - old_value) >= max(3, old_value * .25):
                        findings.append(issue("warning", "driver_status_mix_change", f"{kind} {status} count changed from {old_value} to {new_value}", driver_id=driver_id, reporting_period=after["current_month"]))
        for status in set(before.get("status_mix", {})) | set(after["status_mix"]):
            old, new = before.get("status_mix", {}).get(status, 0), after["status_mix"].get(status, 0)
            if old and abs(new - old) >= max(3, old * .25):
                findings.append(issue("warning", "status_mix_change", f"{kind} {status} count changed from {old} to {new}", source=f"vw_{kind}_final.csv", reporting_period=after["current_month"]))


def run(indicator_path, driver_path, output_dir, inventory_path, now=None, freshness_path=None, metric_path=None, run_id=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    prior_path = output_dir / "results.json"
    try:
        previous = json.loads(prior_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    findings = []
    indicators, ihash, ifields = read_csv(indicator_path, INDICATOR_FIELDS, findings)
    drivers, dhash, dfields = read_csv(driver_path, DRIVER_FIELDS, findings)
    current_hashes = {"indicator": ihash, "driver": dhash}
    for name, digest in current_hashes.items():
        if digest and previous.get("source_hashes", {}).get(name) == digest:
            findings.append(issue("warning", "missed_refresh", f"{name} CSV is byte-identical to the prior run", source=f"vw_{name}_final.csv"))
    validate_rows(indicators, "indicator", findings)
    validate_rows(drivers, "driver", findings)
    validate_mapping(indicators, drivers, findings)
    latest = latest_indicators(indicators, findings)
    inventory_status = check_inventory(latest, drivers, inventory_path, findings)
    if indicators and drivers:
        draft_inventory(indicators, drivers, inventory_path.with_name("inventory.draft.json"))
    fresh_config = load_config(freshness_path or Path("config/freshness.json"), {}, findings, "invalid_freshness_config")
    metric_config = load_config(metric_path or Path("config/metrics.json"), {}, findings, "invalid_metric_config")
    try:
        fresh = {"indicator": freshness(indicators, "indicator", now or datetime.now(timezone.utc), fresh_config, findings), "driver": freshness(drivers, "driver", now or datetime.now(timezone.utc), fresh_config, findings)}
        fresh["indicator"]["readable"] = bool(ifields) and INDICATOR_FIELDS <= set(ifields)
        fresh["driver"]["readable"] = bool(dfields) and DRIVER_FIELDS <= set(dfields)
    except (ValueError, TypeError, AttributeError) as exc:
        findings.append(issue("error", "invalid_freshness_config", str(exc)))
        fresh = {}
    try:
        metric_checks(latest, metric_config, findings)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        findings.append(issue("error", "invalid_metric_config", str(exc)))
    history_stats = check_outliers(indicators, latest, findings)
    groups = composition(indicators, drivers, ifields, dfields)
    prior_history_path = output_dir / "history.json"
    try:
        history = json.loads(prior_history_path.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            history = []
    except (OSError, ValueError):
        history = []
    if history:
        compare_composition(groups, history[-1].get("groups", {}), findings)
    for kind, rows in (("indicator", indicators), ("driver", drivers)):
        by_period = defaultdict(Counter)
        for row in rows:
            if valid_date(row.get("reporting_date")):
                by_period[row["reporting_date"][:7]][row.get("driver_id")] += 1
        periods = sorted(by_period)
        if len(periods) >= 2:
            older, newer = by_period[periods[-2]], by_period[periods[-1]]
            for driver_id, count in older.items():
                if count >= 4 and newer[driver_id] < count * .75:
                    findings.append(issue("warning", "cycle_count_drop", f"{kind} count dropped from {count} to {newer[driver_id]}", driver_id=driver_id, reporting_period=periods[-1]))
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    summary = dict(Counter(f["severity"] for f in findings))
    results = {"schema_version": 2, "generated_at": timestamp, "run_id": str(run_id or os.environ.get("GITHUB_RUN_ID") or "") or None, "source_hashes": current_hashes, "source_rows": {"indicator": len(indicators), "driver": len(drivers)}, "freshness": fresh, "data_status": "FAIL" if summary.get("error") else "WARN" if summary.get("warning") else "PASS", "summary": {"error": summary.get("error", 0), "warning": summary.get("warning", 0)}, "checks": {"inventory": inventory_status, "metric_configuration": "configured" if metric_config.get("indicators") else "unconfigured", "driver_status_rules": "unverified", "numeric_reconciliation": "unverified", "history": history_stats}, "findings": findings, "drivers": build_drivers(drivers, latest)}
    history.append({"generated_at": timestamp, "run_id": results["run_id"], "summary": results["summary"], "source_hashes": current_hashes, "source_rows": results["source_rows"], "groups": groups})
    (output_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output_dir / "history.json").write_text(json.dumps(history[-30:], indent=2) + "\n", encoding="utf-8")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--indicator", type=Path, default=Path("data/vw_indicator_final.csv"))
    parser.add_argument("--driver", type=Path, default=Path("data/vw_driver_final.csv"))
    parser.add_argument("--inventory", type=Path, default=Path("config/inventory.json"))
    parser.add_argument("--output", type=Path, default=Path("docs"))
    parser.add_argument("--freshness", type=Path, default=Path("config/freshness.json"))
    parser.add_argument("--metrics", type=Path, default=Path("config/metrics.json"))
    args = parser.parse_args()
    results = run(args.indicator, args.driver, args.output, args.inventory, freshness_path=args.freshness, metric_path=args.metrics)
    print(f"Wrote {args.output / 'results.json'}: {results['summary']['error']} errors, {results['summary']['warning']} warnings")


if __name__ == "__main__":
    main()
