# CMN-C2-273 - Unit tests: InferKaonaviFieldsNode (inner Step 3)
# Adapted from an established tool-calling golden pattern (Notion -> Kaonavi).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the trust gate,
# input mask, execute() and output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework input mask rewrites Title-Case
# bigrams in validated_input, so quoted display names use a single-word name.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_kaonavi_fields_node import InferKaonaviFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_kaonavi_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_profile", employee_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "employee_hint": employee_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferKaonaviFieldsNode:
    def setup_method(self):
        self.node = InferKaonaviFieldsNode()

    def test_lookup_extracts_code_from_text(self):
        result = self.node(_state("Look up the HR profile for employee code 1001 and summarize the record on file."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "1001"
        # State contract: kaonavi_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["kaonavi_payload"], str)
        assert from_json(result["kaonavi_payload"], {}) == {"member_code": "1001"}

    def test_code_shaped_hint_used_when_text_has_no_code(self):
        result = self.node(_state("Show the current profile summary", employee_hint="A123"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "A123"
        assert from_json(result["kaonavi_payload"], {}) == {"member_code": "A123"}

    def test_create_builds_member_data_payload(self):
        # Field values stay lower-case: the framework name mask rewrites
        # Title-Case word pairs even ACROSS newlines ("Sales\nGrade" would be
        # masked before execute() sees the text).
        text = 'Register a new employee named "Alice"\nDepartment: sales\nGrade: 3'
        result = self.node(_state(text, intent="create_profile", employee_hint="9002"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == "9002"
        assert result["profile_name"] == "Alice"
        payload = from_json(result["kaonavi_payload"], {})
        record = payload["member_data"][0]
        assert record["code"] == "9002"
        assert record["name"] == "Alice"
        assert {"name": "Department", "values": ["sales"]} in record["custom_fields"]
        assert {"name": "Grade", "values": ["3"]} in record["custom_fields"]

    def test_update_builds_member_data_payload(self):
        text = "Update the record for employee code 1001\nGrade: 4"
        result = self.node(_state(text, intent="update_profile"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["kaonavi_payload"], {})
        record = payload["member_data"][0]
        assert record["code"] == "1001"
        assert {"name": "Grade", "values": ["4"]} in record["custom_fields"]

    def test_unresolved_code_left_empty_never_invented(self):
        result = self.node(_state("Show the profile summary for the flagged record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["employee_id"] == ""
        assert from_json(result["kaonavi_payload"], {}) == {"member_code": ""}

    def test_non_code_shaped_hint_left_unresolved(self):
        result = self.node(_state("Show the profile summary", employee_hint="not a valid code!"))
        assert result["employee_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


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


class TestInferKaonaviFieldsNodeLLM:
    """LLM enhancement: profile_name/fields only - employee_id stays deterministic-only."""

    def test_llm_adds_field_the_heuristic_parser_misses(self):
        llm = _FakeLLM(response='{"profile_name": null, "fields": {"Department": "sales"}}')
        node = InferKaonaviFieldsNode(llm=llm)
        text = "Register a new employee, starting in the sales team"
        result = node(_state(text, intent="create_profile", employee_hint="9002"))
        assert llm.calls == 1
        payload = from_json(result["kaonavi_payload"], {})
        record = payload["member_data"][0]
        assert {"name": "Department", "values": ["sales"]} in record["custom_fields"]

    def test_llm_fills_profile_name_when_heuristic_found_none(self):
        llm = _FakeLLM(response='{"profile_name": "alice", "fields": {}}')
        node = InferKaonaviFieldsNode(llm=llm)
        result = node(_state("Register a new employee", intent="create_profile", employee_hint="9002"))
        assert result["profile_name"] == "alice"

    def test_heuristic_name_wins_over_llm_when_both_present(self):
        llm = _FakeLLM(response='{"profile_name": "bob", "fields": {}}')
        node = InferKaonaviFieldsNode(llm=llm)
        text = 'Register a new employee named "alice"'
        result = node(_state(text, intent="create_profile", employee_hint="9002"))
        assert result["profile_name"] == "alice"

    def test_llm_response_wrapped_in_markdown_fence_still_parses(self):
        llm = _FakeLLM(response='Sure, here it is:\n```json\n{"profile_name": null, "fields": {"Grade": "4"}}\n```')
        node = InferKaonaviFieldsNode(llm=llm)
        result = node(_state("Update the record for employee code 1001, bump the grade", intent="update_profile"))
        payload = from_json(result["kaonavi_payload"], {})
        assert {"name": "Grade", "values": ["4"]} in payload["member_data"][0]["custom_fields"]

    def test_malformed_llm_response_falls_back_to_heuristic(self):
        llm = _FakeLLM(response="not json at all")
        node = InferKaonaviFieldsNode(llm=llm)
        text = "Update the record for employee code 1001\nGrade: 4"
        result = node(_state(text, intent="update_profile"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["kaonavi_payload"], {})
        assert {"name": "Grade", "values": ["4"]} in payload["member_data"][0]["custom_fields"]

    def test_llm_raising_falls_back_to_heuristic(self):
        llm = _FakeLLM(raises=RuntimeError("simulated API failure"))
        node = InferKaonaviFieldsNode(llm=llm)
        text = "Update the record for employee code 1001\nGrade: 4"
        result = node(_state(text, intent="update_profile"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["kaonavi_payload"], {})
        assert {"name": "Grade", "values": ["4"]} in payload["member_data"][0]["custom_fields"]

    def test_llm_cannot_smuggle_a_code_field(self):
        # Even if the LLM ignores the prompt's instruction and returns a "code"
        # key, it must never reach the assembled payload's record["code"] -
        # employee_id is resolved from text/hint only, never from LLM output.
        llm = _FakeLLM(response='{"profile_name": null, "fields": {"code": "9999"}}')
        node = InferKaonaviFieldsNode(llm=llm)
        result = node(_state("Register a new employee", intent="create_profile", employee_hint="9002"))
        assert result["employee_id"] == "9002"
        payload = from_json(result["kaonavi_payload"], {})
        assert payload["member_data"][0]["code"] == "9002"
        assert not any(f["name"].lower() == "code" for f in payload["member_data"][0].get("custom_fields", []))

    def test_no_input_never_calls_the_llm(self):
        llm = _FakeLLM(response='{"profile_name": "should-not-be-used", "fields": {}}')
        node = InferKaonaviFieldsNode(llm=llm)
        node(_state(""))
        assert llm.calls == 0

    def test_no_llm_injected_and_no_secret_bound_falls_back_to_heuristic(self):
        # The real production shape in any env without a configured secret -
        # ctx.secrets.require() raises MissingSecret, caught, falls back.
        node = InferKaonaviFieldsNode()  # no llm= double, no secret bound in test state
        text = "Update the record for employee code 1001\nGrade: 4"
        result = node(_state(text, intent="update_profile"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["kaonavi_payload"], {})
        assert {"name": "Grade", "values": ["4"]} in payload["member_data"][0]["custom_fields"]
