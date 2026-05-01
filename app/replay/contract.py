from __future__ import annotations

import platform
import sys
from hashlib import sha256
from typing import Any, Dict


def build_environment_fingerprint(config: Dict[str, Any]) -> str:
    material = f"py={sys.version};platform={platform.platform()};config={sorted(config.items())}"
    return sha256(material.encode("utf-8")).hexdigest()


def assert_replay_inputs(snapshot_id: str | None = None, snapshot_uri: str | None = None) -> None:
    if not snapshot_id and not snapshot_uri:
        raise ValueError("Replay requires snapshot_id or snapshot_uri")


def assert_replay_isolated_runtime(allow_network_egress: bool, allow_k8s_api: bool, allow_external_tools: bool) -> None:
    if allow_network_egress or allow_k8s_api or allow_external_tools:
        raise ValueError("Replay runtime must be isolated: no egress, no k8s API, no external tool calls")
