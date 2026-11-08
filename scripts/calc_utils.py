#!/usr/bin/env python3
"""
Shared utilities for AWS Pricing Calculator automation.

HTTP goes through curl: it is everywhere, and it keeps the scripts stdlib-only
without depending on how the local Python install handles CA certificates.
"""

import functools
import json
import re
import subprocess
import sys
import urllib.parse
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# API Endpoints (from calculator.aws config.js)
# ---------------------------------------------------------------------------
MANIFEST_URL = "https://d1qsjq9pzbk1k6.cloudfront.net/manifest/en_US.json"
SERVICE_DEF_URL = "https://d1qsjq9pzbk1k6.cloudfront.net/data/{code}/en_US.json"
REGION_LIST_URL = "https://d1qsjq9pzbk1k6.cloudfront.net/data/{code}/regionList/publish.json"
SAVE_API = "https://dnd5zrqcec4or.cloudfront.net/Prod/v2/saveAs"
GET_ESTIMATE_API = "https://d3knqfixx3sbls.cloudfront.net/{key}"
CALCULATOR_URL = "https://calculator.aws/#/estimate?id={key}"
PRICING_BASE = "https://calculator.aws/"  # + mappingDefinitionURL from a service definition

# ---------------------------------------------------------------------------
# Region Mapping
# ---------------------------------------------------------------------------
REGION_NAMES = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-southeast-5": "Asia Pacific (Malaysia)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ca-central-1": "Canada (Central)",
    "ca-west-1": "Canada West (Calgary)",
    "eu-central-1": "Europe (Frankfurt)",
    "eu-central-2": "Europe (Zurich)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
    "eu-south-1": "Europe (Milan)",
    "eu-south-2": "Europe (Spain)",
    "eu-north-1": "Europe (Stockholm)",
    "il-central-1": "Israel (Tel Aviv)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "mx-central-1": "Mexico (Central)",
    "sa-east-1": "South America (Sao Paulo)",
    "af-south-1": "Africa (Cape Town)",
    "us-gov-east-1": "AWS GovCloud (US-East)",
    "us-gov-west-1": "AWS GovCloud (US-West)",
}

HOURS_PER_MONTH = 730


def region_name(region_code: str) -> str:
    """Map AWS region code to display name."""
    return REGION_NAMES.get(region_code, region_code)


def make_uuid():
    """Generate a random UUID string."""
    return str(uuid.uuid4())


def _curl(url, args=(), data=None, timeout=60):
    """Run curl, fail on transport errors and non-2xx status. Returns body text."""
    cmd = ["curl", "-sS", "-L", "--compressed", "-w", "\n%{http_code}", *args, url]
    if data is not None:
        # Body goes through stdin: no argv size limit, no shell/codepage mangling.
        cmd[1:1] = ["--data-binary", "@-"]
    try:
        result = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("curl not found on PATH; install curl") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {timeout}s: {url}") from None
    if result.returncode != 0:
        raise RuntimeError(f"curl failed for {url}: {result.stderr.decode('utf-8', 'replace').strip()}")
    body, _, status = result.stdout.decode("utf-8").rpartition("\n")
    if not status.startswith("2"):
        raise RuntimeError(f"HTTP {status} from {url}: {body[:300]}")
    return body


def curl_get(url, timeout=60):
    """GET a JSON document."""
    return json.loads(_curl(url, timeout=timeout))


def curl_post(url, data, timeout=60):
    """POST JSON with the headers the calculator's Save API expects."""
    payload = json.dumps(data).encode("ascii")  # ensure_ascii: every char escaped, encoding-proof
    headers = [
        "-X", "POST",
        "-H", "Content-Type: application/json",
        "-H", "Origin: https://calculator.aws",
        "-H", "Referer: https://calculator.aws/",
    ]
    return json.loads(_curl(url, headers, data=payload, timeout=timeout))


@functools.lru_cache(maxsize=None)
def load_manifest():
    """Return {serviceCode: manifest entry}."""
    return {s["serviceCode"]: s for s in curl_get(MANIFEST_URL).get("awsServices", [])}


@functools.lru_cache(maxsize=None)
def load_service(code):
    """Fetch a service definition (cached per process)."""
    return curl_get(SERVICE_DEF_URL.format(code=code))


@functools.lru_cache(maxsize=None)
def load_regions(code):
    """Region codes the calculator offers for a service, or None if unknown."""
    try:
        return set(curl_get(REGION_LIST_URL.format(code=code)).get("inclusionList", [])) or None
    except (RuntimeError, ValueError):
        return None


def pricing_location(region_code: str) -> str:
    """Region name as the pricing data spells it ("EU (Ireland)", not "Europe (Ireland)")."""
    name = region_name(region_code) or ""
    if name == "AWS GovCloud (US-West)":
        return "AWS GovCloud (US)"
    return name.replace("Europe (", "EU (", 1)


@functools.lru_cache(maxsize=None)
def _selector_aggregations(url: str) -> tuple:
    """Cached on success only: lru_cache doesn't store exceptions."""
    data = curl_get(url)
    return tuple({k: str(v) for k, v in a["selectors"].items()}
                 for a in data.get("aggregations", []) if isinstance(a, dict) and isinstance(a.get("selectors"), dict))


def load_selector_combos(code, mapping_name, region_code):
    """Valid column combinations ("selectors") the calculator can price for a
    columnFormIPM input in a region, or None when the data isn't available."""
    if not (code and mapping_name and region_code):
        return None
    try:
        mapping = next((m for m in load_service(code).get("mappingDefinitions", [])
                        if m.get("mappingDefinitionName") == mapping_name), {})
        url = mapping.get("mappingDefinitionURL", "")
        if mapping.get("mappingDefinitionVersion") != "PLC_2.0" or not url.endswith("/metadata.json"):
            return None
        loc = urllib.parse.quote(pricing_location(region_code))
        url = PRICING_BASE + url.replace("[currency]", "USD").replace(
            "metadata.json", f"{loc}/primary-selector-aggregations.json")
        return list(_selector_aggregations(url)) or None
    except (RuntimeError, ValueError, KeyError, AttributeError, TypeError):
        return None


def parse_estimate_key(url_or_key):
    """Accept a calculator URL or a bare saved key, return the key."""
    m = re.search(r"[?&]id=([0-9a-fA-F]+)", url_or_key)
    key = m.group(1) if m else url_or_key.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{20,64}", key):
        raise ValueError(f"Not a calculator estimate URL or key: {url_or_key!r}")
    return key


def unwrap_body(resp):
    """Save/Get APIs sometimes wrap the payload as {"body": "<json string>"}."""
    body = resp.get("body") if isinstance(resp, dict) else None
    if not isinstance(body, str):
        return resp
    try:
        return json.loads(body)
    except ValueError:  # e.g. {"statusCode": 500, "body": "Internal server error"}
        return resp


def console_safe():
    """Don't crash printing names/JSON the console codepage can't encode (Windows)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def read_json(path):
    """Read JSON written by any editor or shell (UTF-8 with or without BOM, or UTF-16)."""
    raw = Path(path).read_bytes()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    return json.loads(raw.decode(enc))


def fetch_estimate(url_or_key):
    """Download a saved estimate's JSON."""
    return unwrap_body(curl_get(GET_ESTIMATE_API.format(key=parse_estimate_key(url_or_key))))


def iter_services(node):
    """Yield every service entry in an estimate (root services + nested groups)."""
    yield from node.get("services", {}).values()
    for group in node.get("groups", {}).values():
        yield from iter_services(group)
