from __future__ import annotations

from typing import Any


class ReplayFirewallViolation(RuntimeError):
    """Raised when replay attempts to access non-snapshot/live data."""


def enforce_replay_data_firewall(*, snapshot_only: bool, live_external_call: bool) -> None:
    """Enforce Phase 1 replay firewall policy.

    Policy:
    - replay must be snapshot-only
    - no live external calls are allowed
    """
    if not snapshot_only:
        raise ReplayFirewallViolation("Replay must be snapshot-only")
    if live_external_call:
        raise ReplayFirewallViolation("Live external calls are blocked during replay")


def filtered_replay_context(snapshot_context: dict[str, Any], live_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return replay context restricted to snapshot data only."""
    _ = live_context  # explicitly ignored by firewall design
    return snapshot_context.copy()
