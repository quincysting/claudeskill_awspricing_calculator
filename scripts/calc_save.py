#!/usr/bin/env python3
"""
Save an AWS Pricing Calculator estimate via the Save API, then read it back
to confirm the stored copy matches what was sent.

Usage:
  python calc_save.py estimate.json
  python calc_save.py estimate.json --costs readback.jsonl  # use the calculator's own costs (SKILL.md Steps 5-6)
  python calc_save.py estimate.json --local-only     # validate only, no upload
"""

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from calc_utils import (CALCULATOR_URL, SAVE_API, console_safe, curl_post, fetch_estimate,
                            iter_services, parse_estimate_key, read_json, unwrap_body)
    from calc_validate import report, validate_estimate
    from calc_build import _total
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from calc_utils import (CALCULATOR_URL, SAVE_API, console_safe, curl_post, fetch_estimate,
                            iter_services, parse_estimate_key, read_json, unwrap_body)
    from calc_validate import report, validate_estimate
    from calc_build import _total


def _cost(p: dict) -> dict:
    """serviceCost exactly as the calculator stored it: it marks a service "Cost updated"
    when the saved object differs, even by an extra "upfront": 0 or rounding."""
    cost = {k: p[k] for k in ("monthly", "upfront")
            if isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool)}
    cost.setdefault("monthly", 0)
    return cost


def load_readback(path: str) -> tuple[dict, list[dict]]:
    """browser_readback.js output: the full JSON result, or its `lines` saved as JSON Lines.
    Returns (priced, mismatches)."""
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "priced" in data:  # full result (a one-line .jsonl is a record, not this)
            return data["priced"], data.get("mismatches", [])
    except json.JSONDecodeError:
        pass
    priced, mismatches = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} line {n} is not valid JSON ({e}); copy that line again") from None
        if "mismatch" in rec:
            mismatches.append(rec["mismatch"])
        elif "summary" in rec:
            p = priced.setdefault(rec["id"], {"subServices": []})
            if rec.get("sub") is None:
                p["configSummary"] = p.get("configSummary", "") + rec["summary"]
            else:
                subs = p.setdefault("subServices", [])
                while len(subs) <= rec["sub"]:
                    subs.append({})
                subs[rec["sub"]]["configSummary"] = subs[rec["sub"]].get("configSummary", "") + rec["summary"]
        else:
            p = priced.setdefault(rec["id"], {"subServices": []})
            p.update(monthly=rec.get("monthly"), upfront=rec.get("upfront"), status=rec.get("status"))
            subs = p.setdefault("subServices", [])
            for i, (m, u) in enumerate(rec.get("sub", [])):
                while len(subs) <= i:
                    subs.append({})
                subs[i].update(monthly=m, upfront=u)
    return priced, mismatches


def apply_costs(estimate: dict, priced: dict) -> tuple[dict, list[str], list[str]]:
    """Copy the calculator's own costs and config summaries (browser_readback.js `priced`,
    keyed by service id) into a copy of the estimate and recompute the totals.
    Returns (estimate, errors, warnings)."""
    est = copy.deepcopy(estimate)
    errors, warnings = [], []

    def node(n: dict) -> None:
        for sid, svc in n.get("services", {}).items():
            p = priced.get(sid)
            name = svc.get("serviceName", sid)
            if not isinstance(p, dict):
                errors.append(f"{name}: not in the read-back (was the same estimate.json loaded?)")
                continue
            if p.get("status") == "inError":
                errors.append(f"{name}: the calculator could not price it (Required input); fix and rebuild")
            elif p.get("status") not in (None, "priceUpdated"):
                warnings.append(f"{name}: calculator status {p.get('status')!r}")
            if not isinstance(p.get("monthly"), (int, float)):
                errors.append(f"{name}: no calculator cost in the read-back (taken before pricing finished?)")
            subs, psubs = svc.get("subServices", []), p.get("subServices") or []
            if len(subs) != len(psubs):
                errors.append(f"{name}: {len(subs)} sub-services here, {len(psubs)} in the read-back")
            svc["serviceCost"] = _cost(p)
            if p.get("configSummary"):
                svc["configSummary"] = p["configSummary"]
            for sub, ps in zip(subs, psubs):
                sub["serviceCost"] = _cost(ps)
                if ps.get("configSummary"):
                    sub["configSummary"] = ps["configSummary"]
        for g in n.get("groups", {}).values():
            node(g)
            g["totalCost"] = _total(g.get("services", {}), g.get("groups", {}))

    node(est)
    est["totalCost"] = _total(est.get("services", {}), est.get("groups", {}))
    return est, errors, warnings


def _fingerprint(estimate: dict) -> list:
    # Compare values, not JSON text: the API stores 0.0 as 0.
    items = [[s.get("serviceCode"), s.get("calculationComponents"), s.get("subServices")]
             for s in iter_services(estimate)]
    return sorted(items, key=lambda i: json.dumps(i, sort_keys=True))


def save_estimate(estimate: dict) -> dict:
    """POST estimate. Returns {"savedKey", "url"}; raises if the API did not return a key."""
    resp = curl_post(SAVE_API, estimate)
    body = unwrap_body(resp)
    key = body.get("savedKey") if isinstance(body, dict) else None
    status = resp.get("statusCode", 200) if isinstance(resp, dict) else 200
    if not key or str(status) != "200":
        raise RuntimeError(f"Save API returned no savedKey: {json.dumps(resp)[:500]}")
    key = parse_estimate_key(key)
    return {"savedKey": key, "url": CALCULATOR_URL.format(key=key)}


def verify_saved(key: str, sent: dict) -> bool:
    """Read the saved estimate back and compare services/components."""
    stored = fetch_estimate(key)
    return _fingerprint(stored) == _fingerprint(sent)


def main() -> None:
    parser = argparse.ArgumentParser(description="Save estimate to AWS Pricing Calculator")
    parser.add_argument("estimate_json", help="Path to estimate JSON file")
    parser.add_argument("--local-only", action="store_true", help="Validate only, don't upload")
    parser.add_argument("--no-validate", action="store_true", help="Skip validation")
    parser.add_argument("--costs", metavar="READBACK",
                        help="browser_readback.js result (JSON, or its `lines` as JSON Lines): "
                             "use the calculator's own costs, then save")
    args = parser.parse_args()
    console_safe()
    estimate = read_json(args.estimate_json)

    if args.costs:
        try:
            priced, mismatches = load_readback(args.costs)
        except ValueError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        estimate, errors, warnings = apply_costs(estimate, priced)
        for m in mismatches:
            warnings.append(f"{m.get('service')}: estimate page ${m.get('estimatePage')} vs its edit page "
                            f"${m.get('editPage')} (calculator inconsistency; the link shows the estimate page)")
        for w in warnings:
            print(f"  [WARNING] {w}")
        for e in errors:
            print(f"  [ERROR] {e}")
        if errors:
            print("\nNot saving: the calculator could not price every service.")
            sys.exit(1)
        Path(args.estimate_json).write_text(json.dumps(estimate, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Applied the calculator's costs to {args.estimate_json}")

    services = list(iter_services(estimate))
    total = estimate.get("totalCost", {}).get("monthly", 0)
    print(f"Estimate: {estimate.get('name', 'Unknown')}  ({len(services)} services, "
          f"fallback total ${total:,.2f}/mo)")

    if not args.no_validate:
        errors = report(validate_estimate(estimate))  # notes too: inputs left at 0 change the price
        if errors:
            print(f"\n{errors} validation error(s); not uploading. Use --no-validate to override.")
            sys.exit(1)
        print("Validation OK")
    if args.local_only:
        return

    print("\nSaving to AWS Pricing Calculator...")
    result = save_estimate(estimate)
    print(f"Calculator URL:\n  {result['url']}")

    try:
        ok = verify_saved(result["savedKey"], estimate)
        print("Round-trip check: stored estimate matches" if ok
              else "WARNING: stored estimate differs from what was sent; open the URL and check")
    except Exception as e:  # the URL is already valid; don't fail the save over the check
        print(f"WARNING: could not read the estimate back ({e})")

    url_path = Path(args.estimate_json).with_name(Path(args.estimate_json).stem + "_url.txt")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url_path.write_text(
        f"{estimate.get('name', 'Estimate')}\nGenerated: {ts}\nKey: {result['savedKey']}\n"
        f"URL: {result['url']}\n", encoding="utf-8")
    print(f"Saved URL: {url_path}")


if __name__ == "__main__":
    main()
