"""
Offline tests for validation, build and save helpers (no network).

Run:  python tests/test_calc.py     (or: pytest tests/)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import calc_build  # noqa: E402
import calc_save  # noqa: E402
import calc_validate as v  # noqa: E402
from calc_utils import parse_estimate_key  # noqa: E402

DEFN = {
    "version": "0.0.9",
    "serviceName": "Fake Volume ",
    "templates": [{
        "id": "fakeTemplate",
        "cards": [{
            "inputSection": {"components": [
                {"type": "input", "subType": "alert", "id": "note"},
                {"type": "input", "subType": "numericInput", "id": "count",
                 "validations": {"required": True, "minValue": 1, "maxValue": 10}},
                {"type": "input", "subType": "dropdown", "id": "kind", "defaultDropDownItem": "ssd",
                 "options": [{"id": "ssd", "label": "SSD"}, {"id": "1000", "label": "thousands"}]},
                {"type": "input", "subType": "fileSize", "id": "size", "defaultValue": 30,
                 "dropDownSize": [{"id": "gb"}, {"id": "tb"}], "defaultOption": {"size": "gb", "frequency": "NA"}},
                {"type": "input", "subType": "frequency", "id": "requests", "defaultFrequency": "perMonth",
                 "options": [{"id": "perMonth"}, {"id": "perDay"}]},
                {"type": "input", "subType": "numericInput", "id": "ssdIops",
                 "displayIf": {"==": [{"type": "component", "id": "kind"}, "ssd"]}},
                {"type": "pricing", "subType": "singlePricePoint", "id": "price"},
            ]},
        }],
    }, {"id": "otherTemplate", "cards": [{"inputSection": {"components": [
        {"type": "input", "subType": "numericInput", "id": "otherOnly"}]}}]}],
}
GROUP = {"version": "0.0.2", "serviceName": "Fake Group", "subType": "subServiceSelector",
         "templateId": "fakeGroupTemplate", "templates": ["fakeVolume"], "defaultTemplates": ["fakeVolume"]}
DEFS = {"fakeVolume": DEFN, "fakeGroup": GROUP}
MANIFEST = {"fakeVolume": {"isActive": "true"}, "fakeGroup": {"isActive": "true"},
            "oldThing": {"isActive": "false"}}

_REAL = {}


def setup_module(module=None):
    """Swap network loaders for fixtures (restored in teardown_module)."""
    for mod, name, fake in ((v, "load_service", DEFS.__getitem__), (calc_build, "load_service", DEFS.__getitem__),
                            (v, "load_manifest", lambda: MANIFEST), (v, "load_regions", lambda code: {"us-east-1"})):
        _REAL[(mod, name)] = getattr(mod, name)
        setattr(mod, name, fake)


def teardown_module(module=None):
    for (mod, name), real in _REAL.items():
        setattr(mod, name, real)

GOOD = {"count": {"value": "2"}, "kind": {"value": "ssd"}, "size": {"value": "100", "unit": "gb|NA"},
        "requests": {"value": "5", "unit": "perMonth"}, "ssdIops": {"value": "3000"}}


def svc(cc=None, **over):
    base = {"serviceCode": "fakeVolume", "estimateFor": "fakeTemplate", "version": "0.0.9",
            "region": "us-east-1", "calculationComponents": dict(GOOD if cc is None else cc)}
    return {**base, **over}


def errors(service):
    return [i for i in v.validate_service(service) if i.level == "error"]


def test_valid_service_has_no_errors():
    assert errors(svc()) == []


def test_filesize_unit_without_frequency_is_rejected_with_hint():
    (e,) = errors(svc({**GOOD, "size": {"value": "100", "unit": "gb"}}))
    assert "gb|NA" in e.message


def test_dropdown_label_and_number_are_rejected():
    (e,) = errors(svc({**GOOD, "kind": {"value": "SSD"}}))
    assert "use id 'ssd'" in e.message
    (e,) = errors(svc({**GOOD, "kind": {"value": 1000}}))
    assert 'must be the string "1000"' in e.message


def test_range_and_type_checks():
    assert errors(svc({**GOOD, "count": {"value": "11"}}))[0].message.endswith("above maximum 10")
    assert "must be a number" in errors(svc({**GOOD, "count": {"value": "two"}}))[0].message


def test_unknown_id_names_owning_template():
    (e,) = errors(svc({**GOOD, "otherOnly": {"value": "1"}}))
    assert '"otherTemplate"' in e.message


def test_hidden_component_warns_and_missing_required_errors():
    issues = v.validate_service(svc({**GOOD, "kind": {"value": "1000"}}))
    assert any(i.level == "warning" and "ssdIops" in i.message for i in issues)
    cc = {k: val for k, val in GOOD.items() if k != "count"}
    assert "required input missing" in errors(svc(cc))[0].message


def test_missing_optional_input_is_reported_as_zero():
    cc = {k: val for k, val in GOOD.items() if k != "requests"}
    notes = [i for i in v.validate_service(svc(cc)) if i.level == "note"]
    assert any("uses 0" in n.message and "requests" in n.message for n in notes)


def test_inactive_service_and_bad_template():
    v.load_service = lambda code: DEFN  # oldThing reuses the fixture definition
    try:
        assert any("inactive" in e.message for e in errors(svc(serviceCode="oldThing")))
    finally:
        v.load_service = DEFS.__getitem__
    assert "not in" in errors(svc(estimateFor="nope"))[0].message


def test_display_if_evaluation():
    inputs = {}
    cc = {"a": {"value": "5"}, "b": {"value": "x"}}
    cid = lambda i: {"type": "component", "id": i}  # noqa: E731
    assert v.eval_display_if({">": [cid("a"), 2]}, cc, inputs) is True
    assert v.eval_display_if({"and": [{"==": [cid("b"), "x"]}, {"exists": {}}]}, cc, inputs) is None
    assert v.eval_display_if({"or": [{"==": [cid("b"), "x"]}, {"exists": {}}]}, cc, inputs) is True
    assert v.eval_display_if({"not": {"==": [cid("b"), "x"]}}, cc, inputs) is False


def test_build_group_fills_template_version_and_totals():
    spec = {"name": "T", "groups": [{"name": "G", "services": [
        {"serviceCode": "fakeGroup", "subServices": [
            {"serviceCode": "fakeVolume", "monthlyCost": 1.5, "calculationComponents": GOOD},
            {"serviceCode": "fakeVolume", "monthlyCost": 2.25, "upfrontCost": 10, "calculationComponents": GOOD}]},
        {"serviceCode": "fakeVolume", "monthlyCost": 4, "calculationComponents": GOOD}]}]}
    est = calc_build.build_estimate(spec)
    (group,) = est["groups"].values()
    grp_svc = next(s for s in group["services"].values() if s["serviceCode"] == "fakeGroup")
    assert grp_svc["estimateFor"] == "fakeGroupTemplate" and "calculationComponents" not in grp_svc
    assert grp_svc["serviceCost"] == {"monthly": 3.75, "upfront": 10}
    assert grp_svc["subServices"][0]["estimateFor"] == "fakeTemplate"
    assert grp_svc["subServices"][0]["version"] == "0.0.9"
    assert grp_svc["subServices"][0]["serviceName"] == "Fake Volume"
    assert est["totalCost"] == {"monthly": 7.75, "upfront": 10}
    assert [i for i in v.validate_estimate(est) if i.level == "error"] == []


def test_estimate_key_parsing_and_roundtrip_fingerprint():
    key = "dc44aeb24cb438bcd826d2b48694e3955bc69adc"
    assert parse_estimate_key(f"https://calculator.aws/#/estimate?id={key}") == key
    try:
        parse_estimate_key("Internal server error")
        raise AssertionError("bad key accepted")
    except ValueError:
        pass
    a = {"services": {"x": {"serviceCode": "s", "calculationComponents": {"c": {"value": 0.0}}}}}
    b = {"services": {"y": {"serviceCode": "s", "calculationComponents": {"c": {"value": 0}}}}}
    assert calc_save._fingerprint(a) == calc_save._fingerprint(b)


def test_defaults_resolve_to_valid_ids_and_units():
    dd = {"subType": "dropdown", "options": [{"id": "1", "label": "Yes"}, {"id": "0", "label": "No"}]}
    assert v.default_entry({**dd, "defaultDropDownItem": "Yes"}) == {"value": "1"}  # label
    assert v.default_entry({**dd, "defaultOption": 1}) == {"value": "0"}  # index
    assert v.default_entry({**dd, "defaultDropDownItem": "nope"}) == {"value": "1"}  # first option
    dur = {"subType": "durationInput", "outputDurationUnit": "hr",
           "dropDownDuration": [{"value": "min"}, {"value": "sec"}]}
    assert v.default_entry(dur)["unit"] == "min"
    fs = {"subType": "fileSize", "dropDownSize": [{"id": "gb"}], "defaultOption": {"size": "gb", "frequency": "month"}}
    assert v.default_entry(fs)["unit"] == "gb|NA"
    num = {"subType": "numericInput", "defaultValue": "", "validations": {"required": True, "minValue": 2}}
    assert v.default_entry(num) is None and v.blank_entry(num) == {"value": "2"}
    util = {"subType": "utilization", "defaultValue": 730, "defaultDuration": "hoursPerMonth"}
    assert v.default_entry(util) == {"value": "730", "unit": "hoursPerMonth"}


def test_display_if_uses_loader_view_of_missing_inputs():
    inputs = {c["id"]: (DEFN["templates"][0]["cards"][0], c)
              for c in DEFN["templates"][0]["cards"][0]["inputSection"]["components"] if "id" in c}
    cond = {"==": [{"type": "component", "id": "kind"}, "ssd"]}
    assert v.eval_display_if(cond, {}, inputs) is False  # optional "kind" missing loads as 0
    assert v.eval_display_if({"==": [{"type": "component", "id": "count"}, 1]}, {"count": {"value": "1.0"}}, inputs)
    assert v._same(True, "true") and not v._same("1", "2")


def test_ec2_core_inputs_missing_is_an_error():
    ec2 = {"version": "1", "serviceName": "EC2", "templates": [{"id": "template", "cards": [{"inputSection": {
        "components": [{"type": "input", "subType": "ec2InstanceSearch", "id": "instanceType"},
                       {"type": "input", "subType": "dropdown", "id": "tenancy", "defaultDropDownItem": "shared",
                        "options": [{"id": "shared"}]}]}}]}]}
    DEFS["fakeEc2"], MANIFEST["fakeEc2"] = ec2, {"isActive": "true"}
    try:
        errs = errors({"serviceCode": "fakeEc2", "estimateFor": "template", "version": "1",
                       "region": "us-east-1", "calculationComponents": {"tenancy": {"value": "shared"}}})
        assert any("instanceType" in e.where and "prices nothing" in e.message for e in errs)
    finally:
        del DEFS["fakeEc2"], MANIFEST["fakeEc2"]


TABLE = {"type": "input", "subType": "columnFormIPM", "id": "nodes", "mappingDefinitionName": "fake-calc",
         "primaryKey": {"Instance Type": ["vCPU"]}, "row": [
             {"selectorId": "Nodes", "type": "textInput", "defaultValue": 3, "validations": {"required": True}},
             {"selectorId": "Instance Type", "type": "autoSuggest", "defaultValue": "r5.large.search"},
             {"type": "utilization", "label": "Utilization"},
             {"selectorId": "Instance Family", "type": "dropDown"},
             {"selectorId": "TermType", "type": "dropDown"}]}
COMBOS = [{"Instance Type": "r5.large.search", "vCPU": "2", "Instance Family": "Memory optimized", "TermType": "Reserved"},
          {"Instance Type": "r5.large.search", "vCPU": "2", "Instance Family": "Memory optimized", "TermType": "OnDemand"},
          {"Instance Type": "c5.large.search", "vCPU": "2", "Instance Family": "Compute optimized", "TermType": "OnDemand"}]


def row(**cells):
    return {"value": [{k: {"value": val} for k, val in cells.items()}]}


def test_table_row_checked_against_pricing_combinations():
    ok = row(**{"Instance Type": "r5.large.search", "Instance Family": "memory OPTIMIZED", "TermType": "OnDemand"})
    assert v.check_row_pricing(TABLE, ok, COMBOS, "t") == []  # case-insensitive, like the calculator
    (e,) = v.check_row_pricing(TABLE, row(**{"Instance Type": "kafka.r5.large"}), COMBOS, "t")
    assert e.where == "t.Instance Type" and "'c5.large.search'" in e.message
    (e,) = v.check_row_pricing(TABLE, row(**{"Instance Type": "c5.large.search",
                                              "Instance Family": "Memory optimized"}), COMBOS, "t")
    assert "no priced combination" in e.message and "Compute optimized" in e.message


def test_default_row_completed_from_pricing_data():
    entry = v.default_row_entry(TABLE, COMBOS)
    assert entry == row(**{"Nodes": "3", "Instance Type": "r5.large.search",
                           "undefined": {"unit": "100", "selectedId": "%Utilized/Month"},
                           "Instance Family": "Memory optimized", "TermType": "OnDemand"})
    assert v.default_row_entry(TABLE, COMBOS[2:]) is None  # default instance type not priced


def test_missing_table_input_warns_about_default_row():
    DEFS["fakeTable"] = {"version": "1", "serviceName": "T", "templates": [
        {"id": "template", "cards": [{"inputSection": {"components": [TABLE]}}]}]}
    MANIFEST["fakeTable"] = {"isActive": "true"}
    try:
        base = {"serviceCode": "fakeTable", "estimateFor": "template", "version": "1", "region": "us-east-1"}
        issues = v.validate_service({**base, "calculationComponents": {"other": {"value": "1"}}})
        assert any(i.level == "warning" and "default row" in i.message and "'Nodes': 3" in i.message
                   for i in issues)
    finally:
        del DEFS["fakeTable"], MANIFEST["fakeTable"]


def test_ec2_commitment_and_spot_shapes():
    comp = {"subType": "ec2AdvPricingStrategyV2", "options": [
        {"value": o} for o in ("compute-savings", "instance-savings", "on-demand", "spot", "standard", "convertible")]}
    good = {"selectedOption": "standard", "term": "1 Year", "upfrontPayment": "Partial", "model": "standard"}
    assert v.check_component(comp, {"value": good}, "ps") == []
    (e,) = v.check_component(comp, {"value": {**good, "model": None}}, "ps")
    assert '"model": "standard"' in e.message
    (e,) = v.check_component(comp, {"value": {**good, "term": "1yr"}}, "ps")
    assert '"term": "1 Year"' in e.message
    assert v.check_component(comp, {"value": {"selectedOption": "spot", "spotDiscountPercentage": "60"}}, "ps") == []
    assert v.check_component(comp, {"value": {"selectedOption": "spot"}}, "ps")[0].level == "error"


def test_browser_loader_puts_estimate_in_session_storage():
    import json
    est = {"name": 'q"uote\\ back</script>  é', "groups": {}}
    js = calc_build.browser_load_js(est)
    assert js.isascii()  # every special character escaped
    decl = js.split("var k = ", 1)[1].split(", t0 = ", 1)[0]
    key, payload = json.loads("[" + decl.replace(", v = ", ", ", 1) + "]")
    assert key == "awspc-root-estimate-v1" and json.loads(payload) == {"estimate": est}


def test_table_edge_cases():
    # remap.constant narrows the combinations; primaryKey may be a plain string
    ad = {"subType": "columnFormIPM", "primaryKey": "Directory Type", "remap": {"constant": {"Directory Type": "Microsoft AD"}},
          "row": [{"selectorId": "Number of Directories", "type": "textInput", "defaultValue": 1},
                  {"selectorId": "Directory Size", "type": "dropDown"}]}
    combos = [{"Directory Type": "AD Connector", "Directory Size": "Large"},
              {"Directory Type": "Microsoft AD", "Directory Size": "Enterprise"}]
    assert v.default_row_entry(ad, combos) == row(**{"Number of Directories": "1", "Directory Size": "Enterprise"})
    (e,) = v.check_row_pricing(ad, row(**{"Directory Size": "Large"}), combos, "t")
    assert "'Enterprise'" in e.message
    assert v.check_row_pricing(TABLE, row(**{"Instance Type": "r5.large.search", "TermType": ""}), COMBOS, "t") == []
    # required count without a default, string minValue, required dropdown left to pricing data
    t2 = {**TABLE, "row": [{"selectorId": "Nodes", "type": "textInput", "validations": {"required": True, "minValue": "2"}},
                           {"selectorId": "Instance Family", "type": "dropDown", "validations": {"required": True}},
                           *TABLE["row"][1:2]]}
    entry = v.default_row_entry(t2, COMBOS)["value"][0]
    assert entry["Nodes"] == {"value": "2"} and entry["Instance Family"] == {"value": "Memory optimized"}
    # an instance type without a default becomes the placeholder, rejected even without pricing data
    t3 = {**TABLE, "row": [TABLE["row"][0], {"selectorId": "Instance Type", "type": "autoSuggest"}]}
    assert v.default_row_entry(t3, COMBOS)["value"][0]["Instance Type"] == {"value": v.INSTANCE_PLACEHOLDER}
    errs = v.check_component(t3, row(Nodes="1", **{"Instance Type": v.INSTANCE_PLACEHOLDER}), "t")
    assert any("placeholder" in e.message for e in errs)
    assert v.check_row_pricing(t3, row(Nodes="1", **{"Instance Type": v.INSTANCE_PLACEHOLDER}), COMBOS, "t") == []
    # no count column at all: still forced to choose; a pin matching nothing keeps the check on
    t4 = {**TABLE, "row": [{"selectorId": "Instance Type", "type": "autoSuggest"}]}
    assert v.default_row_entry(t4, COMBOS)["value"][0]["Instance Type"] == {"value": v.INSTANCE_PLACEHOLDER}
    pinned = {**TABLE, "remap": {"constant": {"Region Kind": "nope"}}}
    assert v.check_row_pricing(pinned, row(**{"Instance Type": "kafka.r5.large"}), COMBOS, "t")


def test_missing_region_does_not_crash_table_check():
    from calc_utils import load_selector_combos, pricing_location
    assert load_selector_combos("anything", "m", None) is None and load_selector_combos("x", None, "us-east-1") is None
    assert pricing_location("eu-west-1") == "EU (Ireland)" and pricing_location(None) == ""


def test_calculator_costs_applied_by_service_id():
    spec = {"name": "T", "groups": [{"name": "G", "services": [
        {"serviceCode": "fakeGroup", "subServices": [{"serviceCode": "fakeVolume", "calculationComponents": GOOD}]},
        {"serviceCode": "fakeVolume", "monthlyCost": 4, "calculationComponents": GOOD}]}]}
    est = calc_build.build_estimate(spec)
    (group,) = est["groups"].values()
    ids = {s["serviceCode"]: sid for sid, s in group["services"].items()}
    priced = {ids["fakeVolume"]: {"monthly": 8.004, "upfront": 0, "status": "priceUpdated", "configSummary": "100 GB"},
              ids["fakeGroup"]: {"monthly": 2.39, "upfront": 0, "status": None,
                                 "subServices": [{"monthly": 2.39, "upfront": 0, "configSummary": "S3"}]}}
    new, errs, warns = calc_save.apply_costs(est, priced)
    assert errs == warns == [] and new["totalCost"] == {"monthly": 10.39, "upfront": 0}
    (g,) = new["groups"].values()
    assert g["services"][ids["fakeVolume"]]["configSummary"] == "100 GB" and g["totalCost"]["monthly"] == 10.39
    assert g["services"][ids["fakeGroup"]]["subServices"][0]["serviceCost"]["monthly"] == 2.39
    assert g["services"][ids["fakeVolume"]]["serviceCost"] == {"monthly": 8.004, "upfront": 0}  # copied as stored
    assert est["totalCost"]["monthly"] == 4  # input not mutated
    new, _, _ = calc_save.apply_costs(est, {**priced, ids["fakeVolume"]: {"monthly": 8}})
    (g,) = new["groups"].values()
    assert g["services"][ids["fakeVolume"]]["serviceCost"] == {"monthly": 8}  # no invented "upfront"
    _, errs, _ = calc_save.apply_costs(est, {**priced, ids["fakeVolume"]: {"status": "inError"}})
    assert errs and "Required input" in errs[0]
    _, errs, _ = calc_save.apply_costs(est, {})
    assert len(errs) == 2
    _, errs, _ = calc_save.apply_costs(est, {**priced, ids["fakeVolume"]: {"status": None}})
    assert any("no calculator cost" in e for e in errs)
    _, errs, _ = calc_save.apply_costs(est, {**priced, ids["fakeGroup"]: {"monthly": 1, "subServices": []}})
    assert any("1 sub-services here, 0 in the read-back" in e for e in errs)


def test_readback_json_lines_reassemble():
    import json
    import tempfile
    lines = [{"id": "s3-1", "monthly": 2.39, "upfront": 0, "status": None, "sub": [[2.39, 0]]},
             {"id": "s3-1", "sub": None, "summary": "S3 Standard storage (100 GB"},
             {"id": "s3-1", "sub": None, "summary": " per month)"},
             {"id": "s3-1", "sub": 0, "summary": "sub summary"},
             {"id": "os-1", "monthly": 311.96, "upfront": 0, "status": "priceUpdated", "sub": []},
             {"mismatch": {"service": "Amazon OpenSearch Service", "estimatePage": 311.96, "editPage": 295.96}}]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "readback.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        priced, mism = calc_save.load_readback(str(p))
        assert priced["s3-1"]["configSummary"] == "S3 Standard storage (100 GB per month)"
        assert priced["s3-1"]["subServices"] == [{"monthly": 2.39, "upfront": 0, "configSummary": "sub summary"}]
        assert priced["os-1"]["monthly"] == 311.96 and mism[0]["editPage"] == 295.96
        p.write_text(json.dumps({"priced": {"x": {"monthly": 1}}, "mismatches": []}), encoding="utf-8")
        assert calc_save.load_readback(str(p)) == ({"x": {"monthly": 1}}, [])
        p.write_text('{"id": "x", "monthly": 1, "upfront": 0, "status": null, "sub": []}\n', encoding="utf-8")
        assert calc_save.load_readback(str(p))[0]["x"]["monthly"] == 1  # one-line .jsonl is a record
        p.write_text('{"id": "x", "monthly": 1}\n{"id": "y", "mon', encoding="utf-8")
        try:
            calc_save.load_readback(str(p))
            raise AssertionError("broken line accepted")
        except ValueError as e:
            assert "line 2" in str(e)


def test_save_body_that_is_not_json_is_returned_unparsed():
    from calc_utils import unwrap_body
    resp = {"statusCode": 500, "body": "Internal server error"}
    assert unwrap_body(resp) is resp
    assert unwrap_body({"body": '{"savedKey": "ab"}'}) == {"savedKey": "ab"}


if __name__ == "__main__":
    setup_module()
    try:
        tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
        for t in tests:
            t()
    finally:
        teardown_module()
    print(f"{len(tests)} tests passed")
