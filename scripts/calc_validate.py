#!/usr/bin/env python3
"""
Validate estimate services against the live calculator service definitions.

The calculator *recomputes* every service from `calculationComponents` when a
saved estimate is opened. Its loader (calculator.aws bundle, "rehydrate"):
  - unknown component ids          -> ignored, so the intended value is lost
  - dropdown value not an option id -> silently falls back to the default option
  - unit not an option id          -> value dropped (fileSize/frequency/...)
  - required input missing         -> service flagged "Required input"
  - table (columnFormIPM) cell not in the pricing data -> "Invalid Selection",
                                      another row or none priced
  - table input missing            -> the calculator's default row is priced
  - anything that throws           -> service becomes read-only and shows the
                                      pre-calculated serviceCost instead
These checks catch those cases before the estimate is saved.

Usage:
  python calc_validate.py estimate.json
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

try:
    from calc_utils import (console_safe, iter_services, load_manifest, load_regions, load_selector_combos,
                            load_service, read_json)
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from calc_utils import (console_safe, iter_services, load_manifest, load_regions, load_selector_combos,
                            load_service, read_json)

NUMERIC_SUBTYPES = {"numericInput", "percentInput", "frequency", "fileSize",
                    "throughput", "durationInput", "utilization", "sqlPassiveNode"}
# "utilization" input units (calculator bundle)
UTILIZATION_UNITS = ["percentagePerMonth", "hoursPerDay", "hoursPerWeek", "hoursPerMonth",
                     "minutesPerDay", "minutesPerMonth"]
NON_INPUT_SUBTYPES = {"alert", "headerText", "bodyText", "condition"}
INSTANCE_PLACEHOLDER = "SET-INSTANCE-TYPE"  # skeleton value that validation rejects
# EC2 inputs that leave the service at $0 when missing, though not flagged required
ESSENTIAL_SUBTYPES = {"ec2InstanceSearch", "workload", "ec2AdvPricingStrategyV2"}
INSTANCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$")
# EC2 on-demand utilization units (calculator bundle) and their maximum values
UTILIZATION_LIMITS = {"%Utilized/Month": 100, "Hours/Day": 24, "Hours/Week": 168, "Hours/Month": 730}
# EC2 Savings Plans / Reserved Instances, as the calculator UI saves them (captured 2026-11-08):
# {"selectedOption": "standard", "term": "1 Year", "upfrontPayment": "Partial", "model": "standard"}
COMMITMENT_MODELS = {"compute-savings": "computeSavings", "instance-savings": "instanceSavings",
                     "standard": "standard", "convertible": "convertible"}
COMMITMENT_TERMS = ["1 Year", "3 Year"]
COMMITMENT_PAYMENTS = ["None", "Partial", "All"]
# utilization cell of a columnFormIPM row (the column has no selectorId, so its key is "undefined")
ROW_UTILIZATION = {"value": {"unit": "100", "selectedId": "%Utilized/Month"}}


class Issue(NamedTuple):
    level: str  # "error" | "warning" | "note"
    where: str
    message: str


# ---------------------------------------------------------------------------
# Schema helpers (shared with calc_discover.py)
# ---------------------------------------------------------------------------
def is_group(defn: dict) -> bool:
    """Group services (S3, DynamoDB, ...) hold their real config in subServices."""
    return defn.get("subType") == "subServiceSelector"


def get_template(defn: dict, template_id: str | None = None) -> dict | None:
    templates = [t for t in defn.get("templates", []) if isinstance(t, dict)]
    if template_id is None:
        return templates[0] if templates else None
    return next((t for t in templates if t.get("id") == template_id), None)


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def template_inputs(template: dict) -> list[tuple[dict, dict]]:
    """Return [(card, component)] for every user-input component."""
    return [
        (card, comp)
        for card in template.get("cards", [])
        for comp in card.get("inputSection", {}).get("components", [])
        if comp.get("type") == "input" and comp.get("subType") not in NON_INPUT_SUBTYPES
    ]


def template_component_ids(template: dict) -> set[str]:
    """Ids of every component (input, pricing, maths) the template knows about."""
    return {o["id"] for o in _walk(template.get("cards", [])) if "id" in o and "type" in o}


def unit_options(comp: dict) -> list[tuple[str, str]] | None:
    """[(unit_id, label)] accepted in the saved `unit` field, or None if no unit."""
    st = comp.get("subType")
    if st == "fileSize":
        sizes = [s.get("id", s.get("value")) for s in comp.get("dropDownSize", [])]
        freqs = [f.get("id", f.get("value")) for f in comp.get("dropDownFrequency") or []]
        if not freqs:
            return [(f"{s}|NA", s.upper()) for s in sizes]
        return [(f"{s}|{f}", f"{s.upper()} {f}") for s in sizes for f in freqs]
    if st == "frequency":
        return [(o["id"], o.get("label", "")) for o in comp.get("options", [])]
    if st == "throughput":
        return [(o["value"], o.get("label", "")) for o in comp.get("options", [])]
    if st == "durationInput":
        return [(o["value"], o.get("label", "")) for o in comp.get("dropDownDuration", [])]
    if st == "utilization":
        return [(u, u) for u in UTILIZATION_UNITS]
    return None


def value_options(comp: dict) -> list[tuple[str, str]] | None:
    """[(value, label)] accepted in the saved `value` field, or None if free-form."""
    st = comp.get("subType")
    if st == "dropdown":
        return [(o["id"], o.get("label", "")) for o in comp.get("options", [])]
    if st == "radioTiles":
        return [(o["value"], o.get("label", "")) for o in comp.get("radioOptions", [])]
    return None


def _default_option(comp: dict, declared: Any) -> str | None:
    """Resolve a declared default (id, label or index) to a valid option id.

    Some definitions declare a label or an index; the calculator form then
    falls back to the first option, so we do the same.
    """
    opts = value_options(comp) or []
    ids = [v for v, _ in opts]
    if declared in ids:
        return declared
    by_label = {lbl: v for v, lbl in opts}
    if isinstance(declared, str) and declared in by_label:
        return by_label[declared]
    if isinstance(declared, int) and not isinstance(declared, bool) and 0 <= declared < len(ids):
        return ids[declared]
    return ids[0] if ids else None


def _default_unit(comp: dict, declared: Any) -> str | None:
    """The declared default unit if it is accepted, else the first accepted unit."""
    units = [u for u, _ in unit_options(comp) or []]
    return declared if declared in units else (units[0] if units else None)


def default_entry(comp: dict) -> dict | None:
    """The entry for an input left at its UI default (None: no usable default)."""
    st = comp.get("subType")
    dv = comp.get("defaultValue")
    num = None if dv in (None, "") else str(dv)
    if st == "dropdown":
        v = _default_option(comp, comp.get("defaultDropDownItem", comp.get("defaultOption")))
        return {"value": v} if v is not None else None
    if st == "radioTiles":
        v = _default_option(comp, comp.get("defaultSelection"))
        return {"value": v} if v is not None else None
    if st == "checkbox":
        return {"value": bool(dv)}
    if st in ("numericInput", "percentInput"):
        if num is None:
            return None
        lo = comp.get("validations", {}).get("minValue")
        if _is_number(num) and lo is not None and float(num) < lo:  # some definitions default below their own minimum
            num = str(lo)
        return {"value": num}
    opt = comp.get("defaultOption")
    declared_unit = {
        "fileSize": f"{opt.get('size')}|{opt.get('frequency')}" if isinstance(opt, dict) else None,
        "frequency": comp.get("defaultFrequency"),
        "throughput": comp.get("defaultThroughput"),
        "durationInput": comp.get("defaultDuration", comp.get("outputDurationUnit")),
        "utilization": comp.get("defaultDuration"),
    }
    if st in declared_unit:
        return {"value": num or "0", "unit": _default_unit(comp, declared_unit[st])}
    # EC2-specific inputs (ec2Enhancement), shapes as the calculator saves them
    if st == "ec2InstanceSearch":
        return {"value": INSTANCE_PLACEHOLDER}
    if st == "workload":
        return {"value": {"workloadType": "consistent", "data": "1"}}
    if st == "ec2AdvPricingStrategyV2":
        return {"value": {"selectedOption": "on-demand", "utilizationValue": "100",
                          "utilizationUnit": "%Utilized/Month"}}
    if st == "ec2AdvPricingMetrics":
        return {"value": 1}
    if st == "dataTransferV2":
        return {"value": []}
    return None


def blank_entry(comp: dict) -> dict | None:
    """Default entry, or a zero (minimum, if required) entry of the right shape."""
    d = default_entry(comp)
    if d is not None:
        return d
    if comp.get("subType") in ("numericInput", "percentInput"):
        vals = comp.get("validations", {})
        lo = vals.get("minValue") or 0
        return {"value": str(lo) if vals.get("required") and lo > 0 else "0"}
    return None


def visibility(card: dict, comp: dict, cc: dict, inputs: dict) -> bool | None:
    """False if hidden by the other inputs, None if it depends on pricing data."""
    results = [eval_display_if(c["displayIf"], cc, inputs) for c in (card, comp) if "displayIf" in c]
    if False in results:
        return False
    return None if None in results else True


def _same(a: Any, b: Any) -> bool:
    """displayIf equality: numeric when both parse as numbers, booleans as true/false."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        pass

    def norm(x: Any) -> str:
        return str(x).lower() if isinstance(x, bool) else str(x)

    return norm(a) == norm(b)


def eval_display_if(cond: Any, cc: dict, inputs: dict) -> bool | None:
    """Evaluate a displayIf condition. None = depends on pricing data, can't tell."""
    def operand(o: Any) -> Any:
        if isinstance(o, dict) and o.get("type") == "component":
            if o.get("id") in cc and isinstance(cc[o["id"]], dict):
                return cc[o["id"]].get("value")
            # Missing input, as the loader sees it: required ones get their default, others 0.
            comp = inputs.get(o.get("id"), (None, None))[1]
            if comp is None:
                return None
            d = default_entry(comp) if comp.get("validations", {}).get("required") else None
            return d["value"] if d else 0
        return o

    def compare(args, fn):
        a, b = (operand(x) for x in args)
        if a is None or b is None:
            return None
        try:
            return fn(float(a), float(b))
        except (TypeError, ValueError):
            return None

    if not isinstance(cond, dict) or len(cond) != 1:
        return None
    op, args = next(iter(cond.items()))
    if op in ("==", "!="):
        a, b = (operand(x) for x in args)
        if a is None or b is None:
            return None
        return _same(a, b) == (op == "==")
    if op in (">", ">=", "<", "<="):
        fns = {">": float.__gt__, ">=": float.__ge__, "<": float.__lt__, "<=": float.__le__}
        return compare(args, fns[op])
    if op in ("and", "or"):
        results = [eval_display_if(c, cc, inputs) for c in args]
        decisive = op == "or"  # a True decides "or", a False decides "and"
        if decisive in results:
            return decisive
        return None if None in results else not decisive
    if op == "not":
        r = eval_display_if(args, cc, inputs)
        return None if r is None else not r
    if op in ("checkboxChecked", "checkboxNotChecked"):
        v = operand(args)
        return None if v is None else bool(v) == (op == "checkboxChecked")
    return None  # "exists" (pricing data), unknown operators


# ---------------------------------------------------------------------------
# Component checks
# ---------------------------------------------------------------------------
def _is_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _fmt_opts(opts: list[tuple[str, str]], limit: int = 12) -> str:
    shown = ", ".join(f"{v!r}" + (f" ({lbl})" if lbl and lbl != v else "") for v, lbl in opts[:limit])
    return shown + (f", ... {len(opts) - limit} more" if len(opts) > limit else "")


def check_component(comp: dict, entry: Any, where: str) -> list[Issue]:
    """Validate one saved calculationComponents entry against its definition."""
    st = comp.get("subType")
    if not isinstance(entry, dict) or "value" not in entry:
        return [Issue("error", where, f'must be an object with a "value" key, got {entry!r}')]
    value = entry["value"]
    issues = []

    if st in NUMERIC_SUBTYPES:
        if not _is_number(value):
            issues.append(Issue("error", where, f"value must be a number, got {value!r}"))
        elif st in ("numericInput", "percentInput") and not (
                float(value) == 0 and not comp.get("validations", {}).get("required")):
            # 0 on an optional input means "not set" to the calculator; unit-bearing
            # inputs are skipped because their limits are in the output unit.
            v = float(value)
            vals = comp.get("validations", {})
            lo, hi = vals.get("minValue"), vals.get("maxValue")
            if st == "percentInput":
                lo, hi = 0 if lo is None else lo, 100 if hi is None else hi
            if lo is not None and v < lo:
                issues.append(Issue("error", where, f"value {value} is below minimum {lo}"))
            if hi is not None and v > hi:
                issues.append(Issue("error", where, f"value {value} is above maximum {hi}"))

    units = unit_options(comp)
    if units is not None:
        unit = entry.get("unit")
        ids = [u for u, _ in units]
        if unit not in ids:
            hint = ""
            if st == "fileSize" and isinstance(unit, str) and f"{unit}|NA" in ids:
                hint = f' (use "{unit}|NA")'
            issues.append(Issue("error", where,
                                f"unit {unit!r} is not accepted{hint}; valid: {_fmt_opts(units)}"))

    opts = value_options(comp)
    if opts is not None:
        ids = [v for v, _ in opts]
        if value not in ids:
            by_label = {lbl.lower(): v for v, lbl in opts}
            if str(value) in ids:
                hint = f' (must be the string "{value}")'
            elif isinstance(value, str) and value.lower() in by_label:
                hint = f" (that is a label, use id {by_label[value.lower()]!r})"
            else:
                hint = ""
            fallback = "silently falls back to the default option" if st == "dropdown" else "fails to load"
            issues.append(Issue("error", where,
                                f"value {value!r} is not an option{hint}; the calculator {fallback}. "
                                f"Valid: {_fmt_opts(opts)}"))

    if st == "checkbox" and not isinstance(value, bool):
        issues.append(Issue("error", where, f"value must be true/false, got {value!r}"))

    if st == "dataTransferV2":
        allowed = set(comp.get("dataTransferTypes", ["INBOUND", "OUTBOUND", "INTRA_REGION"]))
        if not isinstance(value, list):
            issues.append(Issue("error", where, "value must be a list of transfer entries"))
        else:
            for i, e in enumerate(value):
                if not isinstance(e, dict) or e.get("entryType") not in allowed:
                    issues.append(Issue("error", f"{where}[{i}]",
                                        f"entryType must be one of {sorted(allowed)}"))
                elif e.get("value") not in ("", None) and not _is_number(e["value"]):
                    issues.append(Issue("error", f"{where}[{i}]", f"value must be a number, got {e['value']!r}"))

    if st == "columnFormIPM":
        rows = comp.get("row", [])
        allowed = {r.get("selectorId", "undefined") for r in rows}
        if not (isinstance(value, list) and value and isinstance(value[0], dict)):
            issues.append(Issue("error", where, "value must be a list containing one row object"))
        else:
            row = value[0]
            for key, cell in row.items():
                if key not in allowed:
                    issues.append(Issue("error", f"{where}.{key}",
                                        f"not a column of this form; valid: {sorted(allowed)}"))
                elif not isinstance(cell, dict) or "value" not in cell:
                    issues.append(Issue("error", f"{where}.{key}", 'must be {"value": ...}'))
                elif cell["value"] == INSTANCE_PLACEHOLDER:
                    issues.append(Issue("error", f"{where}.{key}", f"replace the skeleton placeholder {INSTANCE_PLACEHOLDER!r} with a real value"))
            for r in rows:
                key = r.get("selectorId", "undefined")
                if r.get("validations", {}).get("required") and key not in row:
                    issues.append(Issue("error", f"{where}.{key}", "required column is missing"))

    if st == "ec2AdvPricingStrategyV2":
        allowed = [o.get("value", o.get("id")) for o in comp.get("options", [])]
        pv: dict = value if isinstance(value, dict) else {}
        sel = pv.get("selectedOption")
        if sel not in allowed:
            issues.append(Issue("error", where,
                                f"value.selectedOption {sel!r} is not valid; use one of {allowed} "
                                f"(anything else makes EC2 read-only)"))
        elif sel == "on-demand":
            unit = pv.get("utilizationUnit")
            if unit is None and "utilizationValue" not in pv:
                # The calculator then bills the full workload hours; only the summary text breaks.
                issues.append(Issue("warning", where, 'add "utilizationValue": "100", "utilizationUnit": '
                                                      '"%Utilized/Month" (or the real utilization)'))
            elif unit not in UTILIZATION_LIMITS:
                issues.append(Issue("error", where, f"value.utilizationUnit must be one of "
                                                    f"{list(UTILIZATION_LIMITS)}, got {unit!r}"))
            elif not (_is_number(pv.get("utilizationValue")) and
                      0 <= float(pv["utilizationValue"]) <= UTILIZATION_LIMITS[unit]):
                issues.append(Issue("error", where, f"value.utilizationValue must be 0..{UTILIZATION_LIMITS[unit]} "
                                                    f"{unit}, got {pv.get('utilizationValue')!r}"))
        elif sel in COMMITMENT_MODELS:
            want = {"selectedOption": sel, "term": pv.get("term"), "upfrontPayment": pv.get("upfrontPayment"),
                    "model": COMMITMENT_MODELS[sel]}
            if pv.get("term") not in COMMITMENT_TERMS:
                want["term"] = COMMITMENT_TERMS[0]
            if pv.get("upfrontPayment") not in COMMITMENT_PAYMENTS:
                want["upfrontPayment"] = COMMITMENT_PAYMENTS[0]
            if {k: pv.get(k) for k in want} != want:
                issues.append(Issue("error", where, f"{sel} must look like {json.dumps(want)} "
                                                    f"(term {COMMITMENT_TERMS}, upfrontPayment {COMMITMENT_PAYMENTS})"))
        elif sel == "spot":
            pct = pv.get("spotDiscountPercentage")
            if not (_is_number(pct) and 0 <= float(str(pct)) <= 100):
                issues.append(Issue("error", where, 'spot must look like {"selectedOption": "spot", '
                                                    f'"spotDiscountPercentage": "60"}} (0-100), got {pct!r}'))

    if st == "ec2InstanceSearch" and not (isinstance(value, str) and INSTANCE_TYPE_RE.match(value)):
        issues.append(Issue("error", where, f'value must be an instance type such as "m5.xlarge", got {value!r}'))

    if st == "workload" and not (isinstance(value, dict) and value.get("workloadType") and
                                 _is_number(value.get("data"))):
        issues.append(Issue("error", where,
                            'value must look like {"workloadType": "consistent", "data": "<instance count>"}'))

    return issues


def check_row_pricing(comp: dict, entry: Any, combos: list[dict], where: str) -> list[Issue]:
    """A columnFormIPM row must match a combination the calculator has prices for
    (e.g. MSK "Compute Family" is "m5.large", not "kafka.m5.large"). Otherwise the
    row fails to load and the service shows "Required input"."""
    value = entry.get("value") if isinstance(entry, dict) else None
    if not (isinstance(value, list) and value and isinstance(value[0], dict)):
        return []  # shape errors come from check_component
    combos = table_combos(comp, combos)
    columns = {k for c in combos for k in c}
    picked = {k: str(cell["value"]) for k, cell in value[0].items()  # placeholder: check_component reports it
              if k in columns and isinstance(cell, dict) and cell.get("value") not in (None, "", INSTANCE_PLACEHOLDER)}
    # case-insensitive, like the calculator (AppStream "True" matches pricing "true")
    if not picked or any(all(str(c.get(k, "")).casefold() == v.casefold() for k, v in picked.items())
                         for c in combos):
        return []
    for k, v in picked.items():
        valid = sorted({c[k] for c in combos if c.get(k)})
        if v.casefold() not in {x.casefold() for x in valid}:
            return [Issue("error", f"{where}.{k}", f"{v!r} is not in the calculator's pricing data for this "
                                                   f"region; the calculator rejects it (\"Invalid Selection\") and prices another row or none. Valid: {_fmt_opts([(x, x) for x in valid])}")]
    key = next((k for k in primary_keys(comp) if k in picked), next(iter(picked)))
    same = [c for c in combos if str(c.get(key, "")).casefold() == picked[key].casefold()]
    hints = "; ".join(f"{k}: {sorted({c[k] for c in same if k in c})}" for k in picked if k != key)
    return [Issue("error", where, f"no priced combination for {picked}; the calculator rejects the row. "
                                  f"With {key}={picked[key]!r} use {hints}")]


def primary_keys(comp: dict) -> list[str]:
    """columnFormIPM primaryKey is a dict ({"Instance Type": [tags]}) or a plain string."""
    pk = comp.get("primaryKey")
    return [pk] if isinstance(pk, str) else list(pk or {})


def table_combos(comp: dict, combos: list[dict] | None) -> list[dict]:
    """Pricing combinations this table can use: remap.constant pins columns that aren't
    in the row (Directory Service: {"Directory Type": "Microsoft AD"}). If the pin matches
    nothing, the unfiltered list is kept so the check still runs."""
    combos = [c for c in combos or [] if isinstance(c, dict)]
    const = (comp.get("remap") or {}).get("constant") or {}
    pinned = [c for c in combos if all(str(c.get(k, "")).casefold() == str(v).casefold() for k, v in const.items())]
    return pinned or combos


def default_row(comp: dict) -> dict:
    """The columnFormIPM columns that have a default, as the calculator fills them in."""
    return {r["selectorId"]: r["defaultValue"] for r in comp.get("row", [])
            if r.get("selectorId") and r.get("defaultValue") not in (None, "")}


def default_row_entry(comp: dict, combos: list[dict] | None) -> dict | None:
    """A columnFormIPM entry for the calculator's default row, with the columns that have
    no default (instance family, pricing model, storage, ...) taken from the first
    on-demand pricing combination that matches. None if the defaults aren't priced.
    An instance-type column without a default (RDS: the UI starts blank) gets
    INSTANCE_PLACEHOLDER, so validation fails until a real type is chosen."""
    row = {k: str(v) for k, v in default_row(comp).items()}
    for col in comp.get("row", []):  # required counts without a default: 1
        vals = col.get("validations") or {}
        if (col.get("type") == "textInput" and col.get("selectorId") and col["selectorId"] not in row
                and vals.get("required")):
            lo = float(vals["minValue"]) if _is_number(vals.get("minValue")) else 0
            row[col["selectorId"]] = str(max(int(lo), 1) if lo == lo and abs(lo) < 1e12 else 1)  # nan/inf -> 1
    combos = sorted(table_combos(comp, combos), key=lambda c: c.get("TermType", "OnDemand") != "OnDemand")
    match = next((c for c in combos if all(str(c.get(k, "")).casefold() == v.casefold()
                                           for k, v in row.items() if k in c)), None)
    if combos and match is None:
        return None
    primary = next(iter(primary_keys(comp)), None)
    counts = [row.get(c.get("selectorId")) for c in comp.get("row", []) if c.get("type") == "textInput"]
    if counts and all(_is_number(n) and float(str(n)) == 0 for n in counts):
        primary = None  # a 0-node row: any priced instance type will do
    cells = {}
    for col in comp.get("row", []):
        key = col.get("selectorId")
        if col.get("type") == "utilization":
            cells[key or "undefined"] = ROW_UTILIZATION
        elif key in row:
            cells[key] = {"value": row[key]}
        elif key and key == primary and combos:
            cells[key] = {"value": INSTANCE_PLACEHOLDER}
        elif match and match.get(key):
            cells[key] = {"value": match[key]}
    return {"value": [cells]} if cells else None


# ---------------------------------------------------------------------------
# Service checks
# ---------------------------------------------------------------------------
def _check_header(svc: dict, defn: dict, where: str) -> list[Issue]:
    issues = []
    code = svc.get("serviceCode")
    entry = load_manifest().get(code)
    if entry is None:
        return [Issue("error", where, f"unknown serviceCode {code!r} (see calc_discover.py --list)")]
    if entry.get("isActive") != "true" or entry.get("disableConfigure"):
        issues.append(Issue("error", where, f"{code} is inactive in the calculator and will not load"))
    regions = load_regions(code)
    if regions and svc.get("region") not in regions:
        issues.append(Issue("error", where, f"region {svc.get('region')!r} not offered for {code}"))
    if svc.get("version") != defn.get("version"):
        issues.append(Issue("warning", where,
                            f"version {svc.get('version')!r} != current {defn.get('version')!r}"))
    return issues


def validate_service(svc: dict, where: str = "") -> list[Issue]:
    """Validate one estimate service entry (regular or group)."""
    code = svc.get("serviceCode")
    where = where or str(code)
    try:
        defn = load_service(code)
    except Exception as e:  # network/404: report, don't crash the whole run
        return [Issue("error", where, f"cannot load definition for {code!r}: {e}")]

    try:
        issues = _check_header(svc, defn, where)
    except Exception as e:  # manifest/region list unavailable: say so, keep validating components
        issues = [Issue("warning", where, f"could not check service code/region: {e}")]

    if is_group(defn):
        if svc.get("estimateFor") != defn.get("templateId"):
            issues.append(Issue("error", where,
                                f"estimateFor must be {defn.get('templateId')!r} for this group service"))
        subs = svc.get("subServices")
        if not isinstance(subs, list) or not subs:
            issues.append(Issue("error", where,
                                f"group service needs subServices; choose from {defn.get('templates')}"))
            return issues
        for i, sub in enumerate(subs):
            sub_where = f"{where}.subServices[{i}]({sub.get('serviceCode')})"
            if sub.get("serviceCode") not in defn.get("templates", []):
                issues.append(Issue("error", sub_where, f"not a sub-service of {code}"))
                continue
            issues.extend(validate_service(sub, sub_where))
        return issues

    template = get_template(defn, svc.get("estimateFor"))
    if template is None:
        ids = [t.get("id") for t in defn.get("templates", []) if isinstance(t, dict)]
        return issues + [Issue("error", where, f"estimateFor {svc.get('estimateFor')!r} not in {ids}")]

    cc = svc.get("calculationComponents")
    if not isinstance(cc, dict) or not cc:
        return issues + [Issue("error", where, "calculationComponents is empty")]

    known = template_component_ids(template)
    inputs = {comp["id"]: (card, comp) for card, comp in template_inputs(template)}
    others = {t["id"]: template_component_ids(t) for t in defn["templates"]
              if isinstance(t, dict) and t is not template}
    for cid in cc:
        if cid not in known:
            owner = next((tid for tid, ids in others.items() if cid in ids), None)
            hint = (f'; it belongs to template "{owner}", add a separate service with estimateFor "{owner}"'
                    if owner else "")
            issues.append(Issue("error", f"{where}.{cid}",
                                f"unknown component id (ignored by the calculator, value lost){hint}"))
    # Missing inputs, as the calculator loads them: required ones get their default,
    # required ones with no default (outside conditional cards) fail, the rest become 0.
    defaulted, zeroed, hidden = [], [], []
    for cid, (card, comp) in inputs.items():
        if visibility(card, comp, cc, inputs) is False:
            if cid in cc:
                hidden.append(cid)
            continue
        if cid in cc:
            issues.extend(check_component(comp, cc[cid], f"{where}.{cid}"))
            combos = (load_selector_combos(code, comp.get("mappingDefinitionName"), svc.get("region"))
                      if comp.get("subType") == "columnFormIPM" else None)
            if combos:
                issues.extend(check_row_pricing(comp, cc[cid], combos, f"{where}.{cid}"))
            continue
        d = default_entry(comp)
        if comp.get("subType") == "columnFormIPM":
            # Not 0: the calculator prices its default row (OpenSearch: 3 masters, 2 UltraWarm nodes)
            issues.append(Issue("warning", f"{where}.{cid}",
                                f"missing, so the calculator prices its default row {default_row(comp) or ''} "
                                f"({comp.get('label', '')}); set it explicitly, with a node count of 0 if unused"))
        elif comp.get("subType") in ESSENTIAL_SUBTYPES:
            issues.append(Issue("error", f"{where}.{cid}",
                                f"missing; without it the calculator prices nothing ({comp.get('label', '')}). "
                                f"Shape: {json.dumps(d)}"))
        elif comp.get("validations", {}).get("required"):
            if d is not None:
                defaulted.append(f"{cid}={d['value']}")
            elif "displayIf" not in card and "displayIf" not in comp:
                issues.append(Issue("error", f"{where}.{cid}",
                                    f"required input missing ({comp.get('label', '')})"))
        else:
            zeroed.append(cid)
    if defaulted:
        issues.append(Issue("note", where, "required inputs left at their default: " + ", ".join(defaulted)))
    if zeroed:
        issues.append(Issue("note", where, "not set, so the calculator uses 0: " + ", ".join(zeroed)))
    if hidden:
        issues.append(Issue("warning", where,
                            "hidden by the other settings, so ignored by the calculator: " + ", ".join(hidden)))
    return issues


def validate_estimate(estimate: dict) -> list[Issue]:
    issues = []
    for svc in iter_services(estimate):
        issues.extend(validate_service(svc))
    return issues


def report(issues: list[Issue], show_notes: bool = True) -> int:
    """Print issues, return the number of errors."""
    for i in issues:
        if i.level == "note" and not show_notes:
            continue
        print(f"  [{i.level.upper()}] {i.where}: {i.message}")
    return sum(1 for i in issues if i.level == "error")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate an estimate JSON against live service definitions")
    parser.add_argument("estimate_json")
    args = parser.parse_args()
    console_safe()
    errors = report(validate_estimate(read_json(args.estimate_json)))
    print(f"\n{errors} error(s)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
