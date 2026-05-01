import pytest

from app.replay.firewall import ReplayFirewallViolation, enforce_replay_data_firewall, filtered_replay_context


def test_firewall_allows_snapshot_only_without_live_calls():
    enforce_replay_data_firewall(snapshot_only=True, live_external_call=False)


def test_firewall_blocks_non_snapshot_mode():
    with pytest.raises(ReplayFirewallViolation):
        enforce_replay_data_firewall(snapshot_only=False, live_external_call=False)


def test_firewall_blocks_live_external_calls():
    with pytest.raises(ReplayFirewallViolation):
        enforce_replay_data_firewall(snapshot_only=True, live_external_call=True)


def test_filtered_replay_context_ignores_live_context():
    snapshot = {"k": "snapshot"}
    live = {"k": "live", "secret": "x"}
    result = filtered_replay_context(snapshot, live)
    assert result == {"k": "snapshot"}
