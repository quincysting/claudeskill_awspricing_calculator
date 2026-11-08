#!/usr/bin/env python3
"""
Download a saved AWS Pricing Calculator estimate.

Use it to copy the exact calculationComponents the calculator UI writes for a
service (configure it by hand once, share, fetch), or to edit an existing
estimate: fetch --spec, change the spec, calc_build.py, calc_save.py.

Usage:
  python calc_fetch.py "https://calculator.aws/#/estimate?id=..." -o estimate.json
  python calc_fetch.py <id> --spec spec.json
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from calc_utils import console_safe, fetch_estimate
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from calc_utils import console_safe, fetch_estimate


def to_spec_service(svc: dict) -> dict:
    out = {k: svc[k] for k in ("serviceCode", "region", "estimateFor", "configSummary", "description")
           if svc.get(k) not in (None, "")}
    cost = svc.get("serviceCost") or {}
    out["monthlyCost"] = cost.get("monthly", 0)
    if cost.get("upfront"):
        out["upfrontCost"] = cost["upfront"]
    if "subServices" in svc:
        out.pop("estimateFor", None)  # group templateId is filled in by calc_build
        out["subServices"] = [to_spec_service(s) for s in svc["subServices"]]
    else:
        out["calculationComponents"] = svc.get("calculationComponents", {})
    return out


def to_spec(node: dict) -> dict:
    spec = {"services": [to_spec_service(s) for s in node.get("services", {}).values()],
            "groups": [dict(name=g.get("name", "Group"), **to_spec(g)) for g in node.get("groups", {}).values()]}
    return {k: v for k, v in spec.items() if v}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download a saved calculator estimate")
    parser.add_argument("estimate", help="Calculator URL or saved key")
    parser.add_argument("-o", "--output", help="Write the raw estimate JSON here")
    parser.add_argument("--spec", help="Write a calc_build.py spec here")
    args = parser.parse_args()
    console_safe()

    estimate = fetch_estimate(args.estimate)
    if args.output:
        Path(args.output).write_text(json.dumps(estimate, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved: {args.output}")
    if args.spec:
        spec = {"name": estimate.get("name", "Estimate"), **to_spec(estimate)}
        Path(args.spec).write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Spec: {args.spec}")
    if not (args.output or args.spec):
        print(json.dumps(estimate, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
