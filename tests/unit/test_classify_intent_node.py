# CMN-C2-273 - Unit tests: ClassifyIntentNode (inner Step 2)
# Adapted from an established tool-calling golden pattern (Notion -> Kaonavi).
# Intents: lookup_profile / create_profile / update_profile (deterministic
# keyword heuristic, v1 - no LLM; unknown falls back to the read-only lookup).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the trust gate,
# input mask, execute() and output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_profile(self):
        result = self.node(_state("Look up the HR profile for employee code 1001."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_profile"

    def test_keyword_create_profile(self):
        result = self.node(_state("Register a new employee with code A123"))
        assert result["intent"] == "create_profile"

    def test_keyword_update_profile(self):
        result = self.node(_state("Update the record for employee code 1001 with the new grade"))
        assert result["intent"] == "update_profile"

    def test_write_keyword_wins_over_lookup(self):
        # Priority order is writes-first: an "update ... then show it" style
        # request classifies as the write, never the read.
        result = self.node(_state("Update the grade for employee code 1001 and show the record"))
        assert result["intent"] == "update_profile"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_profile"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_profile" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the HR profile for employee code 1001."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_profile"
        assert payloads["classify_intent_complete"]["defaulted"] is False
        assert payloads["classify_intent_complete"]["via_llm"] is False


class _FakeLLM:
    """Test double matching AzureOpenAIClient's .complete()-derived .generate() shape."""

    def __init__(self, response: str = "", raises: "Exception | None" = None):
        self.response = response
        self.raises = raises
        self.calls = 0

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.response


class TestClassifyIntentNodeLLM:
    def test_llm_response_overrides_the_heuristic(self):
        # Text has no keyword signal at all - heuristic would default to
        # lookup_profile; a well-formed LLM reply wins instead.
        llm = _FakeLLM(response='{"intent": "update_profile"}')
        node = ClassifyIntentNode(llm=llm)
        result = node(_state("please handle this for the team"))
        assert llm.calls == 1
        assert result["intent"] == "update_profile"
        assert not result.get("error_log")  # not a "defaulted" note - a real LLM classification

    def test_llm_response_wrapped_in_markdown_fence_still_parses(self):
        llm = _FakeLLM(response='Sure:\n```json\n{"intent": "create_profile"}\n```')
        node = ClassifyIntentNode(llm=llm)
        result = node(_state("please handle this for the team"))
        assert result["intent"] == "create_profile"

    def test_malformed_llm_response_falls_back_to_heuristic(self):
        llm = _FakeLLM(response="not json at all")
        node = ClassifyIntentNode(llm=llm)
        result = node(_state("Register a new employee with code A123"))
        assert result["intent"] == "create_profile"  # heuristic keyword match

    def test_out_of_enum_llm_value_falls_back_to_heuristic(self):
        llm = _FakeLLM(response='{"intent": "delete_profile"}')
        node = ClassifyIntentNode(llm=llm)
        result = node(_state("Update the record for employee code 1001"))
        assert result["intent"] == "update_profile"  # heuristic keyword match, not the invalid value

    def test_llm_raising_falls_back_to_heuristic(self):
        llm = _FakeLLM(raises=RuntimeError("simulated API failure"))
        node = ClassifyIntentNode(llm=llm)
        result = node(_state("Register a new employee with code A123"))
        assert result["intent"] == "create_profile"

    def test_no_input_never_calls_the_llm(self):
        llm = _FakeLLM(response='{"intent": "update_profile"}')
        node = ClassifyIntentNode(llm=llm)
        node(_state(""))
        assert llm.calls == 0

    def test_no_llm_injected_and_no_secret_bound_falls_back_to_heuristic(self):
        # The real production shape in any env without a configured secret -
        # ctx.secrets.require() raises MissingSecret, caught, falls back.
        node = ClassifyIntentNode()  # no llm= double, no secret bound in test state
        result = node(_state("Register a new employee with code A123"))
        assert result["intent"] == "create_profile"
