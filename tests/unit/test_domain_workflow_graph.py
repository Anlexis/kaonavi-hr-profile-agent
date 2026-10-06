# CMN-C2-273 - Unit tests: inner KaonaviWorkflowGraph (BaseGraph) contract.
# Adapted from an established tool-calling golden pattern. The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free v1 stub.


from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import KaonaviWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return KaonaviWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "kaonavi_hr_profile_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_kaonavi_config_as_json():
    g = _graph({"configurable": {"kaonavi": {"base_url": "https://kaonavi.example.test/api/v2.0"}}})
    extra = g._extra_initial_state()
    # State contract: forwarded as a JSON string, not a native dict.
    assert isinstance(extra["kaonavi_config"], str)
    assert from_json(extra["kaonavi_config"], {}) == {"base_url": "https://kaonavi.example.test/api/v2.0"}


def test_extra_initial_state_empty_without_kaonavi_section():
    assert _graph()._extra_initial_state() == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "1001", "record_ref": "kaonavi://members/1001", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_profile",
            "employee_id": "1001",
            "record_id": "1001",
            "record_ref": "kaonavi://members/1001",
            "profile_name": "Member 1001",
            "confirmation": "ok",
            "kaonavi_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_profile"
    assert out["record_ref"] == "kaonavi://members/1001"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "1001", "record_ref": "kaonavi://members/1001", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"kaonavi": {"base_url": "https://api.kaonavi.jp/api/v2.0"}}})
    g.compile()
    result = g.invoke(user_input="Look up the HR profile for employee code 1001 and summarize the record on file.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "1001"
    assert result["record_ref"] == "kaonavi://members/1001"
    assert result["intent"] == "lookup_profile"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferKaonaviFieldsNode",
        "CallKaonaviApiNode",
        "ConfirmNode",
    ]
