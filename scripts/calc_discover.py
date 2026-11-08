#!/usr/bin/env python3
"""
AWS Pricing Calculator service schema discovery.

Fetches live service definitions and prints, for every input, the exact JSON
to put in calculationComponents: id, valid option ids, unit ids, default,
and when the input is visible.

Usage:
  python calc_discover.py --list [keyword]
  python calc_discover.py amazonElasticBlockStore ec2Enhancement
  python calc_discover.py amazonSimpleStorageServiceGroup      # group: lists sub-services
  python calc_discover.py amazonElasticBlockStore --skeleton spec.json
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from calc_utils import console_safe, load_manifest, load_selector_combos, load_service
    from calc_validate import (blank_entry, default_row_entry, get_template, is_group, template_inputs,
                               unit_options, value_options, visibility)
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from calc_utils import console_safe, load_manifest, load_selector_combos, load_service
    from calc_validate import (blank_entry, default_row_entry, get_template, is_group, template_inputs,
                               unit_options, value_options, visibility)


def strip_pricing(cond):
    """Drop "exists" (regional pricing availability) terms; None if nothing is left.

    and(exists, x) -> x; or(exists, x) -> None (pricing almost always exists).
    """
    if not isinstance(cond, dict) or len(cond) != 1:
        return cond
    op, args = next(iter(cond.items()))
    if op == "exists":
        return None
    if op in ("and", "or"):
        kids = [strip_pricing(k) for k in args]
        if op == "or" and None in kids:
            return None
        kids = [k for k in kids if k is not None]
        return None if not kids else kids[0] if len(kids) == 1 else {op: kids}
    if op == "not":
        kid = strip_pricing(args)
        return None if kid is None else {"not": kid}
    return cond


def render_condition(cond) -> str:
    """Human-readable displayIf."""
    if not isinstance(cond, dict) or len(cond) != 1:
        return json.dumps(cond)
    op, args = next(iter(cond.items()))

    def operand(o):
        return o["id"] if isinstance(o, dict) and o.get("type") == "component" else repr(o)

    if op in ("and", "or"):
        return "(" + f" {op} ".join(render_condition(c) for c in args) + ")"
    if op == "not":
        return f"not {render_condition(args)}"
    if op in ("checkboxChecked", "checkboxNotChecked"):
        return f"{operand(args)} {'checked' if op == 'checkboxChecked' else 'unchecked'}"
    if isinstance(args, list) and len(args) == 2:
        return f"{operand(args[0])} {op} {operand(args[1])}"
    return json.dumps(cond)


def describe_input(card: dict, comp: dict) -> dict:
    vals = comp.get("validations", {})
    conds = [render_condition(c) for c in (strip_pricing(x.get("displayIf")) for x in (card, comp)) if c]
    return {
        "id": comp["id"],
        "subType": comp.get("subType"),
        "label": comp.get("label") or comp.get("fieldLabel", ""),
        "required": bool(vals.get("required")),
        "min": vals.get("minValue"),
        "max": vals.get("maxValue"),
        "visibleIf": " and ".join(conds) or None,
        "default": blank_entry(comp),
        "valueOptions": value_options(comp),
        "unitOptions": unit_options(comp),
        "columns": [r.get("selectorId", "undefined") for r in comp.get("row", [])],
    }


def discover_service(code: str) -> dict:
    defn = load_service(code)
    info = {"serviceCode": code, "serviceName": defn.get("serviceName", code),
            "version": defn.get("version")}
    if is_group(defn):
        manifest = load_manifest()
        info.update(group=True, estimateFor=defn.get("templateId"),
                    defaultSubServices=defn.get("defaultTemplates", []),
                    subServices=[{"serviceCode": c, "name": manifest.get(c, {}).get("name", "").strip()}
                                 for c in defn.get("templates", [])])
        return info
    info["templates"] = [
        {"estimateFor": t["id"], "title": t.get("title", ""),
         "inputs": [describe_input(card, comp) for card, comp in template_inputs(t)]}
        for t in defn.get("templates", []) if isinstance(t, dict)
    ]
    return info


def print_service(info: dict) -> None:
    print(f"\n=== {info['serviceName']}  serviceCode={info['serviceCode']}  version={info['version']}")
    if info.get("group"):
        print(f"GROUP service: estimateFor={info['estimateFor']!r}; put the real config in "
              f'"subServices". Discover each sub-service code below. '
              f"Calculator defaults: {info['defaultSubServices']}")
        for s in info["subServices"]:
            print(f"    {s['serviceCode']:<45} {s['name']}")
        return
    for t in info["templates"]:
        print(f"\n  template estimateFor={t['estimateFor']!r}  {t['title']}")
        for i in t["inputs"]:
            flags = " [REQUIRED]" if i["required"] else ""
            rng = f" range {i['min']}..{i['max']}" if i["min"] is not None or i["max"] is not None else ""
            print(f"    {i['id']}  ({i['subType']}) {i['label']}{flags}{rng}")
            if i["visibleIf"]:
                print(f"        visible if: {i['visibleIf']}")
            if i["default"] is not None:
                print(f"        write: {json.dumps(i['default'])}")
            elif i["columns"]:
                print(f'        write: {{"value": [{{"<column>": {{"value": "..."}}, ...}}]}}  '
                      f"columns: {i['columns']}  (examples in service_formats.md)")
            else:
                print(f"        no documented format for subType {i['subType']!r}: configure it in the "
                      "calculator UI and copy it with calc_fetch.py")
            for key, label in (("valueOptions", "value ids"), ("unitOptions", "unit ids")):
                if i[key]:
                    opts = ", ".join(f"{v!r}={lbl}" if lbl and lbl != v else repr(v) for v, lbl in i[key])
                    print(f"        {label}: {opts}")


def skeleton(info: dict, region: str) -> dict:
    """Spec stub: every input visible with default settings, set to its UI default.

    Inputs left out of calculationComponents load as 0, so the stub spells them all out.
    """
    if info.get("group"):
        subs = []
        for code in info["defaultSubServices"]:
            try:
                sub = skeleton(discover_service(code), region)
            except Exception as e:  # e.g. a listed sub-service whose definition 403s
                print(f"WARNING: skipped sub-service {code}: {e}")
                continue
            sub.pop("region")  # inherited from the group
            subs.append(sub)
        return {"serviceCode": info["serviceCode"], "region": region, "subServices": subs}
    t = get_template(load_service(info["serviceCode"]))
    if t is None:
        print(f"WARNING: {info['serviceCode']} has no configurable template")
        return {"serviceCode": info["serviceCode"], "region": region, "calculationComponents": {}}
    pairs = template_inputs(t)
    inputs = {comp["id"]: (card, comp) for card, comp in pairs}
    defaults = {comp["id"]: d for _, comp in pairs if (d := blank_entry(comp)) is not None}
    for _, comp in pairs:  # table inputs: the calculator's default row, completed from pricing data
        if comp.get("subType") == "columnFormIPM":
            combos = load_selector_combos(info["serviceCode"], comp.get("mappingDefinitionName"), region)
            if (row := default_row_entry(comp, combos)) is not None:
                defaults[comp["id"]] = row
    # Judge visibility against the complete default configuration, not a partial one.
    cc = {cid: d for cid, d in defaults.items() if visibility(*inputs[cid], defaults, inputs) is not False}
    return {"serviceCode": info["serviceCode"], "region": region, "monthlyCost": 0,
            "calculationComponents": cc}


def list_services(keyword: str | None) -> None:
    rows = sorted(
        (s["name"].strip(), code) for code, s in load_manifest().items()
        if s.get("isActive") == "true" and not s.get("disableConfigure")
        and (not keyword or keyword.lower() in (s["name"] + code + " ".join(s.get("searchKeywords", []))).lower())
    )
    print(f"{len(rows)} active services\n")
    for name, code in rows:
        print(f"  {code:<50} {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover AWS Pricing Calculator service schemas")
    parser.add_argument("services", nargs="*", help="Service codes to discover")
    parser.add_argument("--list", nargs="?", const="", metavar="KEYWORD", help="List active services")
    parser.add_argument("--schema", help="Save the discovered schema as JSON")
    parser.add_argument("--skeleton", help="Write a spec stub with calculator defaults")
    parser.add_argument("--region", default="us-east-1", help="Region for --skeleton")
    args = parser.parse_args()
    console_safe()

    if args.list is not None:
        list_services(args.list)
        return
    if not args.services:
        parser.print_help()
        sys.exit(1)

    infos, failed = {}, False
    for code in args.services:
        try:
            infos[code] = discover_service(code)
        except Exception as e:
            print(f"ERROR discovering {code}: {e} (check the code with --list)")
            failed = True
            continue
        print_service(infos[code])

    if args.schema and infos:
        with open(args.schema, "w", encoding="utf-8") as f:
            json.dump(infos, f, indent=2)
        print(f"\nSchema saved to: {args.schema}")
    if args.skeleton and infos:
        spec = {"name": "Estimate", "groups": [
            {"name": "Default", "services": [skeleton(i, args.region) for i in infos.values()]}]}
        with open(args.skeleton, "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2)
        print(f"\nSkeleton spec saved to: {args.skeleton}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
