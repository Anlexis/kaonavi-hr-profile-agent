"""AgentCore Platform v1.0 - inner Kaonavi workflow graph (Cat 2 domain workflow).

Instantiated by KaonaviWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_kaonavi_fields
          -> call_kaonavi_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    kaonavi  - the declared integration section from config/config.yaml
               (base_url); injected into State as the JSON `kaonavi_config`
               field via _extra_initial_state() so the no-arg nodes can read it

Nodes are registered WITHOUT constructor arguments (nodes are no-arg by
contract; ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_kaonavi_fields_node import InferKaonaviFieldsNode
from src.nodes.call_kaonavi_api_node import CallKaonaviApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json


class KaonaviWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "kaonavi_hr_profile_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the kaonavi section is optional (the client
        # falls back to the documented default base_url + the network-free v1
        # stub transport), and a missing/unusable setting is handled at
        # CallKaonaviApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_kaonavi_fields"] = InferKaonaviFieldsNode()
        self._nodes["call_kaonavi_api"] = CallKaonaviApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_kaonavi_fields")
        self._sg.add_edge("infer_kaonavi_fields", "call_kaonavi_api")
        self._sg.add_edge("call_kaonavi_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it. Annotated with this graph's
        # OWN State on purpose: a conditional-edge path callable's annotation
        # is read as its input schema, and a narrower base annotation would
        # project domain fields away if this were ever wired.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> dict[str, Any]:
        # Forward the declared `kaonavi` section (arriving under
        # config["configurable"] from _parent_config()) into State as a JSON
        # string (msgpack-safe) so the no-arg CallKaonaviApiNode can
        # read it via state.get("kaonavi_config").
        configurable = self.config.get("configurable") or {}
        kaonavi = configurable.get("kaonavi") or {}
        if not kaonavi:
            return {}
        return {"kaonavi_config": to_json(kaonavi)}

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "employee_id": state.get("employee_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "profile_name": state.get("profile_name", ""),
            "confirmation": state.get("confirmation", ""),
            "kaonavi_payload": state.get("kaonavi_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
