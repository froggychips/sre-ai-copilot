from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts"

SCHEMA_FILES = {
    "snapshot.v1": "snapshot.v1.json",
    "budget.v1": "budget.v1.json",
    "ledger.v1": "ledger.v1.json",
    "routing-policy.v1": "routing-policy.v1.json",
    "breaker.v1": "breaker.v1.json",
}


@lru_cache(maxsize=None)
def _load_schema(schema_version: str) -> dict[str, Any]:
    if schema_version not in SCHEMA_FILES:
        raise ValueError(f"Unknown schema version: {schema_version}")

    schema_path = CONTRACT_DIR / SCHEMA_FILES[schema_version]
    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def validate_contract_payload(schema_version: str, payload: dict[str, Any]) -> None:
    """Validate payload against the named control-plane schema.

    Raises:
        ValueError: if schema_version is unknown.
        jsonschema.ValidationError: if payload does not conform.
    """
    schema = _load_schema(schema_version)
    jsonschema.validate(instance=payload, schema=schema)
