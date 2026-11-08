"""
Re-validate every JSON block in references/service_formats.md against the
live calculator definitions (network). Run after calculator releases:

  python tests/check_formats.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from calc_build import build_service  # noqa: E402
from calc_validate import report, validate_service  # noqa: E402

doc = (ROOT / "references" / "service_formats.md").read_text(encoding="utf-8")
blocks = re.findall(r"```json\n(.*?)```", doc, re.S)
failed = 0
for block in blocks:
    entry = build_service(json.loads(block))
    print(f"=== {entry['serviceName']} ({entry['serviceCode']})")
    errors = report(validate_service(entry), show_notes=False)
    failed += bool(errors)
print(f"\n{len(blocks) - failed}/{len(blocks)} formats valid")
sys.exit(1 if failed else 0)
