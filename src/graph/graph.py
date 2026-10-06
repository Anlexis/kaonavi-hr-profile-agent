"""AgentCore Platform v1.0 - CMN-C2-273 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in KaonaviWorkflowGraphNode (`main`
slot), which wraps the inner KaonaviWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import KaonaviWorkflowGraph

# Runtime-parameter file: src/graph/graph.py -> parents[2] is the repo root.
# config/agent.yaml (the static manifest) holds only registration identity;
# every runtime parameter lives in config/config.yaml.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# The documented Kaonavi REST API v2 endpoint - the client's own default; kept
# here only to bound the declared override.
_MAX_BASE_URL_LEN = 200


def _runtime_config() -> dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the same file the platform registry loads and passes as
    Graph(config=...); the standalone server (src/api/server.py) reads it here
    so the deployed agent and a registry-loaded agent see identical
    configuration. Returns an empty dict - never raises - when the file is
    absent, unreadable, not valid YAML, or not a mapping (the graph then runs
    on its built-in defaults).
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _config_number(value: Any, lo: float, hi: float) -> Optional[float]:
    """Validate a declared numeric setting: a real number, finite, within [lo, hi].

    Bools, strings, non-numerics, NaN/Infinity, and out-of-range values return
    None (the caller then keeps the built-in default). A non-finite value is
    the dangerous case: NaN comparisons are always False, so it would silently
    disable whatever decision the setting bounds.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def agent_runtime_config() -> dict[str, Any]:
    """The validated Graph(config=...) mapping for KaonaviHRProfileAgent.

    AgentBaseGraph itself consumes max_retry from its config (retry routing),
    so the declared value is live in both deployments: the platform registry
    passes config/config.yaml as Graph(config=...), and the standalone server
    constructs the agent with this same mapping. Invalid or absent keys are
    simply not forwarded; the framework then falls back to its defaults.
    """
    cfg = _runtime_config()
    declared: dict[str, Any] = {}

    max_retry = _config_number(cfg.get("max_retry"), 0, 10)
    if max_retry is not None and max_retry == int(max_retry):
        declared["max_retry"] = int(max_retry)

    timeout_s = _config_number(cfg.get("timeout_s"), 1, 600)
    if timeout_s is not None and timeout_s == int(timeout_s):
        declared["timeout_s"] = int(timeout_s)

    return declared


class KaonaviWorkflowGraphNode(GraphNode):
    """Wraps the inner Kaonavi workflow graph; assigned to the `main` slot.

    No constructor arguments (nodes are no-arg by contract) - configuration
    reaches the subgraph via _parent_config(), which reads config/config.yaml.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "KaonaviWorkflowGraph":
        from src.graph.domain_workflow_graph import KaonaviWorkflowGraph

        return KaonaviWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON string);
        # structured params travel as JSON and the first inner node parses them back.
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "employee_id": sub_result.get("employee_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "profile_name": sub_result.get("profile_name", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "kaonavi_payload": sub_result.get("kaonavi_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Forward the declared runtime settings to the inner graph.

        Reads config/config.yaml (see _runtime_config) and forwards the
        `kaonavi:` integration section under config["configurable"], where the
        inner graph's _extra_initial_state() injects it into State for the
        no-arg CallKaonaviApiNode. Never returns a bare {} silently: an empty
        forward simply means nothing is declared, and every reader then falls
        back to its documented default (the client's built-in endpoint + the
        network-free stub transport).

        The forwarded value is validated here (type + length) so a malformed
        configuration file can neither crash graph construction nor smuggle an
        unbounded value into the client.
        """
        cfg = _runtime_config()
        kaonavi_raw = cfg.get("kaonavi")
        kaonavi: dict[str, Any] = kaonavi_raw if isinstance(kaonavi_raw, dict) else {}

        declared: dict[str, Any] = {}
        base_url = kaonavi.get("base_url")
        if isinstance(base_url, str) and 0 < len(base_url) <= _MAX_BASE_URL_LEN and base_url.startswith("https://"):
            declared["base_url"] = base_url

        configurable: dict[str, Any] = {}
        if declared:
            configurable["kaonavi"] = declared
        return {"configurable": configurable}


class KaonaviHRProfileAgent(AgentBaseGraph):
    """CMN-C2-273 outer graph - Kaonavi HR Profile Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in KaonaviWorkflowGraphNode (`main` slot); Kaonavi
    settings flow from config/config.yaml via _parent_config(), and the
    backbone's own runtime settings (max_retry, timeout_s) from the same file
    via agent_runtime_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_273"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = KaonaviWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
