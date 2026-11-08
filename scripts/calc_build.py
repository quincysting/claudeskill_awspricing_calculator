#!/usr/bin/env python3
"""
Build an AWS Pricing Calculator estimate JSON from a spec file, then validate it
against the live service definitions.

Spec format (JSON). version / estimateFor / serviceName are filled in from the
live definition; set estimateFor only to pick a non-default template.
{
  "name": "My Estimate",
  "services": [ ... ],                      # optional, services outside any group
  "groups": [
    {
      "name": "Production",
      "services": [
        {
          "serviceCode": "amazonElasticBlockStore",
          "region": "us-east-1",
          "monthlyCost": 8.00,              # fallback only, see SKILL.md
          "configSummary": "100 GB gp3",
          "calculationComponents": { ... }  # from calc_discover.py
        },
        {
          "serviceCode": "amazonSimpleStorageServiceGroup",   # group service
          "region": "us-east-1",
          "subServices": [
            {"serviceCode": "amazonS3Standard", "monthlyCost": 2.30,
             "calculationComponents": { ... }}
          ]
        }
      ],
      "groups": [ ... ]                     # optional nested groups
    }
  ]
}

Usage:
  python calc_build.py spec.json -o estimate.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from calc_utils import console_safe, iter_services, load_service, make_uuid, read_json, region_name
    from calc_validate import get_template, is_group, report, validate_estimate
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from calc_utils import console_safe, iter_services, load_service, make_uuid, read_json, region_name
    from calc_validate import get_template, is_group, report, validate_estimate


def build_service(spec: dict, default_region: str = "us-east-1") -> dict:
    """Build one saved-format service entry (regular or group)."""
    code = spec.get("serviceCode")
    try:
        defn = load_service(code)
    except RuntimeError as e:
        raise ValueError(f"cannot load serviceCode {code!r}; find valid codes with "
                         f"`calc_discover.py --list <keyword>` ({e})") from None
    region = spec.get("region", default_region)
    entry = {
        "calculationComponents": spec.get("calculationComponents", {}),
        "serviceCode": code,
        "region": region,
        "estimateFor": spec.get("estimateFor"),
        "version": defn.get("version"),
        "description": spec.get("description"),
        "serviceCost": {"monthly": round(float(spec.get("monthlyCost", 0)), 2),
                        "upfront": round(float(spec.get("upfrontCost", 0)), 2)},
        "serviceName": (spec.get("serviceName") or defn.get("serviceName", code)).strip(),
        "regionName": region_name(region),
        "configSummary": spec.get("configSummary", ""),
    }
    if is_group(defn):
        subs = [build_service(s, region) for s in spec.get("subServices", [])]
        entry["subServices"] = subs
        entry["estimateFor"] = defn.get("templateId")
        del entry["calculationComponents"]
        if "monthlyCost" not in spec:
            entry["serviceCost"]["monthly"] = round(sum(s["serviceCost"]["monthly"] for s in subs), 2)
        if "upfrontCost" not in spec:
            entry["serviceCost"]["upfront"] = round(sum(s["serviceCost"]["upfront"] for s in subs), 2)
    elif entry["estimateFor"] is None:
        template = get_template(defn)
        entry["estimateFor"] = template.get("id") if template else None
    return entry


def _total(services: dict, groups: dict) -> dict:
    items = [s["serviceCost"] for s in services.values()] + [g["totalCost"] for g in groups.values()]
    return {k: round(sum(c.get(k, 0) for c in items), 2) for k in ("monthly", "upfront")}


def build_node(spec: dict) -> tuple[dict, dict]:
    """Build services + nested groups for the estimate root or one group."""
    services = {f"{s['serviceCode']}-{make_uuid()}": build_service(s) for s in spec.get("services", [])}
    groups = {}
    for g in spec.get("groups", []):
        g_services, g_groups = build_node(g)
        groups[f"grp-{make_uuid()}"] = {
            "name": g["name"],
            "services": g_services,
            "groups": g_groups,
            "groupSubtotal": {},
            "totalCost": _total(g_services, g_groups),
        }
    return services, groups


def build_estimate(spec: dict) -> dict:
    services, groups = build_node(spec)
    return {
        "name": spec.get("name", "Estimate"),
        "services": services,
        "groups": groups,
        "groupSubtotal": {},
        "totalCost": _total(services, groups),
        "support": {},
        "metaData": {
            "locale": "en_US",
            "currency": "USD",
            "createdOn": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "source": "calculator-platform",
        },
    }


def print_summary(estimate: dict) -> None:
    total = estimate["totalCost"]
    print(f"Estimate: {estimate['name']}")
    for svc in iter_services(estimate):
        print(f"  - {svc['serviceName']} ({svc['region']}): ${svc['serviceCost']['monthly']:,.2f}/mo")
        for sub in svc.get("subServices", []):
            print(f"      - {sub['serviceName']}: ${sub['serviceCost']['monthly']:,.2f}/mo")
    print(f"Fallback total (your monthlyCost values): ${total['monthly']:,.2f}/mo, "
          f"${total['upfront']:,.2f} upfront")


SESSION_KEY = "awspc-root-estimate-v1"  # sessionStorage key of the calculator's in-progress estimate


def browser_load_js(estimate: dict) -> str:
    """JavaScript that opens `estimate` as the in-progress estimate of a calculator.aws
    tab, where the calculator prices it. Nothing is saved or shared.

    Switching the page to #/estimate makes the calculator store a fresh empty estimate,
    so after the switch it waits for that write (up to 5 s), writes the estimate again
    and reloads."""
    data = json.dumps({"estimate": estimate}, separators=(",", ":"))
    return (f"var k = {json.dumps(SESSION_KEY)}, v = {json.dumps(data)}, t0 = Date.now();\n"
            "sessionStorage.setItem(k, v);\n"
            "if (location.hash === '#/estimate') { location.reload(); } else {\n"
            "  location.hash = '#/estimate';\n"
            "  (function wait() {\n"
            "    if (sessionStorage.getItem(k) === v && Date.now() - t0 < 5000) return setTimeout(wait, 100);\n"
            "    setTimeout(function () { sessionStorage.setItem(k, v); location.reload(); }, 300);\n"
            "  })();\n"
            "}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an AWS Pricing Calculator estimate from a spec")
    parser.add_argument("spec", help="Path to spec JSON file")
    parser.add_argument("-o", "--output", help="Output estimate JSON path")
    parser.add_argument("--force", action="store_true", help="Write the estimate even if validation fails")
    parser.add_argument("--js", metavar="FILE",
                        help="Also write JavaScript that loads the estimate into a calculator.aws tab (SKILL.md Step 5)")
    args = parser.parse_args()
    console_safe()

    try:
        estimate = build_estimate(read_json(args.spec))
    except (ValueError, KeyError) as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print_summary(estimate)

    print("\nValidating against live service definitions...")
    errors = report(validate_estimate(estimate))
    if errors and not args.force:
        print(f"\n{errors} error(s). Fix the spec (or pass --force). Nothing written.")
        sys.exit(1)
    print("OK" if not errors else f"{errors} error(s), writing anyway (--force)")

    output = args.output or str(Path(args.spec).with_name(Path(args.spec).stem + "_estimate.json"))
    with open(output, "w", encoding="utf-8") as f:
        json.dump(estimate, f, indent=2, ensure_ascii=False)
    print(f"\nSaved: {output}")
    if args.js:
        Path(args.js).write_text(browser_load_js(estimate), encoding="utf-8")
        print(f"Browser loader: {args.js}")


if __name__ == "__main__":
    main()
