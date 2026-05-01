#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    ROOT / "contracts" / "snapshot.v1.json",
    ROOT / "contracts" / "budget.v1.json",
    ROOT / "contracts" / "ledger.v1.json",
    ROOT / "contracts" / "routing-policy.v1.json",
    ROOT / "contracts" / "breaker.v1.json",
    ROOT / "docs" / "adr" / "0001-control-plane-contracts.md",
    ROOT / "docs" / "policies" / "control-plane-versioning-policy.md",
    ROOT / "docs" / "policies" / "control-plane-rollback-policy.md",
    ROOT / "docs" / "policies" / "control-plane-no-bypass-policy.md",
]


def main() -> int:
    errors: list[str] = []

    for p in REQUIRED_FILES:
        if not p.exists():
            errors.append(f"Missing required file: {p.relative_to(ROOT)}")

    for p in (ROOT / "contracts").glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Invalid JSON in {p.name}: {exc}")
            continue

        schema_version = data.get("properties", {}).get("schema_version", {}).get("const")
        expected = p.name.replace(".json", "")
        if schema_version != expected:
            errors.append(f"{p.name}: schema_version const '{schema_version}' != '{expected}'")

    if errors:
        print("Phase 0 compliance: FAIL")
        for err in errors:
            print(f"- {err}")
        return 1

    print("Phase 0 compliance: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
