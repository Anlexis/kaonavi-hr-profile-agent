# CMN-C2-273 - Unit tests: CallKaonaviApiNode (inner Step 4, tool side-effect)
# Adapted from an established tool-calling golden pattern (Notion -> Kaonavi).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the trust gate,
# input mask, execute() and output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, the trust gate is unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's KaonaviClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_kaonavi_api_node import CallKaonaviApiNode
from src.services.kaonavi_client import KaonaviApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_kaonavi_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "kaonavi_payload": to_json({"member_code": "1001"}),
        "intent": "lookup_profile",
        "employee_id": "1001",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-kaonavi-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for KaonaviClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_member(self, member_code, api_token):
        raise KaonaviApiError(403, "forbidden by integration permissions")


class _FakeLiveClient:
    """Stands in for KaonaviClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def find_member(self, member_code, api_token):
        _FakeLiveClient.captured = {"member_code": member_code, "api_token": api_token}
        return {"member_data": [{"code": member_code, "name": f"Member {member_code}"}]}


class TestCallKaonaviApiNode:
    def setup_method(self):
        self.node = CallKaonaviApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"
        assert result["record_ref"] == "kaonavi://members/1001"
        assert result["employee_id"] == "1001"
        assert result["profile_name"] == "Member 1001"

    def test_create_success_via_default_v1_stub(self):
        state = _state(
            intent="create_profile",
            employee_id="9002",
            kaonavi_payload=to_json({"member_data": [{"code": "9002", "name": "Alice"}]}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "9002"
        assert result["record_ref"] == "kaonavi://members/9002"

    def test_update_success_via_default_v1_stub(self):
        state = _state(
            intent="update_profile",
            employee_id="1001",
            kaonavi_payload=to_json({"member_data": [{"code": "1001"}]}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"

    def test_kaonavi_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `kaonavi:` section as the JSON
        # kaonavi_config state field; the stub transport still serves the call.
        state = _state(kaonavi_config=to_json({"base_url": "https://kaonavi.example.test/api/v2.0"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "kaonavi://members/1001"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"kaonavi": {"base_url": "https://kaonavi.example.test/api/v2.0"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "1001"

    def test_missing_payload_errors(self):
        result = self.node(_state(kaonavi_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_lookup_with_unresolved_code_errors(self):
        state = _state(employee_id="", kaonavi_payload=to_json({"member_code": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved employee code" in entry for entry in result["error_log"])

    def test_update_with_unresolved_code_errors(self):
        state = _state(
            intent="update_profile",
            employee_id="",
            kaonavi_payload=to_json({"member_data": [{"code": ""}]}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_profile"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error_with_the_status_only(self, monkeypatch):
        """A remote error body is upstream content - it can carry identifiers,
        names or echoed request fields - so the log line carries the HTTP
        status only, never the API's own message."""
        monkeypatch.setattr("src.nodes.call_kaonavi_api_node.KaonaviClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        entry = next(e for e in result["error_log"] if "403" in e)
        assert "Kaonavi API error 403" in entry
        assert "forbidden by integration permissions" not in entry
        assert "forbidden" not in entry

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing KAONAVI_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_kaonavi_api_node.KaonaviClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_kaonavi_api_node.KaonaviClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"KAONAVI_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["member_code"] == "1001"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_kaonavi_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_kaonavi_api_complete"]
        assert payload["intent"] == "lookup_profile"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True
