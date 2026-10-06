# CMN-C2-273 - Unit tests: ConfirmNode (inner Step 5)
# Adapted from an established tool-calling golden pattern (Notion -> Kaonavi).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the trust gate,
# input mask, execute() and output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "1001",
        "record_ref": "kaonavi://members/1001",
        "profile_name": "Member 1001",
        "intent": "lookup_profile",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved HR profile" in result["confirmation"]
        assert "Member 1001" in result["confirmation"]
        assert "ref=kaonavi://members/1001" in result["confirmation"]
        assert "id=1001" in result["confirmation"]
        assert result["result"]["record_id"] == "1001"
        assert result["result"]["record_ref"] == "kaonavi://members/1001"

    def test_create_verb(self):
        result = self.node(
            _state(intent="create_profile", record_id="9002", record_ref="kaonavi://members/9002", profile_name="Alice")
        )
        assert "Created HR profile" in result["confirmation"]

    def test_update_verb(self):
        result = self.node(_state(intent="update_profile"))
        assert "Updated HR profile" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed HR profile" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=1001" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_name_missing(self):
        result = self.node(_state(profile_name=""))
        assert "'1001'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
