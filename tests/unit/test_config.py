# CMN-C2-273 - Unit tests: configuration contract.
#
# Two files, two roles:
#   config/agent.yaml   - the static registry manifest (identity, entry point,
#                         trust level, compile-time requires). Flat, root-level
#                         keys - the registry does not read nested blocks.
#   config/config.yaml  - the runtime parameters (max_retry, timeout_s, the
#                         kaonavi integration section).
#
# Declaring a value is not the same as it arriving: these tests also assert
# each declared value REACHES its consumer - the framework retry config on the
# outer graph, and the kaonavi base_url on the inner graph's state - so a
# dead declaration cannot sit in the shipped config unnoticed.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_ROOT = pathlib.Path(__file__).parents[2]
_MANIFEST_PATH = _ROOT / "config" / "agent.yaml"
_RUNTIME_PATH = _ROOT / "config" / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


class TestManifest:
    def test_identity(self):
        data = _manifest()
        assert data["id"] == "CMN-C2-273"
        assert data["name"] == "KaonaviHRProfileAgent"
        assert data["category"] == "Cat 2"
        assert data["industry"] == "CMN"
        assert data["base_type"] == "ToolCallingAgent"
        assert data["enabled"] is True

    def test_entry_point_is_flat_dotted_path(self):
        data = _manifest()
        assert data["class"] == "src.graph.graph.KaonaviHRProfileAgent"
        # The registry reads root-level keys only - no nested agent: block.
        assert "agent" not in data

    def test_generation_mode_matches_the_code(self):
        # ClassifyIntentNode and InferKaonaviFieldsNode call an Azure OpenAI
        # LLM to enhance intent classification and name/field extraction -
        # any failure falls back to the original keyword/regex heuristic
        # unchanged, so the declared mode is llm (LLM-enhanced, not LLM-only).
        assert _manifest()["generation_mode"] == "llm"

    def test_security(self):
        data = _manifest()
        # Agent-level entry trust, enforced by the outer backbone pre_process
        # gate (VERIFIED_EXTERNAL); inner domain nodes stay ANONYMOUS.
        assert data["required_trust_level"] == "VERIFIED_EXTERNAL"

    def test_requires_matches_the_code(self):
        data = _manifest()
        # KAONAVI_TOKEN is read via ctx.secrets.get() - an OPTIONAL lookup
        # (the stub transport runs without it), so it is deliberately NOT a
        # compile-time required secret: declaring an unprovisioned secret
        # fails the agent at compile. No client extra is constructed for it.
        #
        # The three Azure OpenAI secrets ARE compile-time required: they back
        # the LLM enhancement in ClassifyIntentNode / InferKaonaviFieldsNode.
        # An LLM failure at runtime degrades gracefully to the heuristic (see
        # those nodes' own tests), but declaring the secret at all means it
        # must be resolvable at compile time if provisioned - consistent with
        # every other LLM-calling template in the fleet.
        assert sorted(data["requires"]["secrets"]) == [
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_ENDPOINT",
        ]
        assert data["requires"]["extras"] == ["openai"]


class TestRuntimeConfig:
    def test_runtime_values_declared(self):
        cfg = _runtime()
        assert isinstance(cfg["max_retry"], int)
        assert isinstance(cfg["timeout_s"], int)
        assert cfg["kaonavi"]["base_url"].startswith("https://")

    def test_declared_values_reach_the_graph_config(self):
        """agent_runtime_config() forwards the validated declared values -
        the framework consumes max_retry from exactly this mapping."""
        from src.graph.graph import agent_runtime_config

        declared = agent_runtime_config()
        cfg = _runtime()
        assert declared["max_retry"] == cfg["max_retry"]
        assert declared["timeout_s"] == cfg["timeout_s"]

    def test_kaonavi_section_reaches_the_inner_graph(self):
        """_parent_config() forwards the declared kaonavi section, and the
        inner graph injects it into State for the API-call node - the full
        declared-value path, not just the file parse."""
        from src.graph.graph import KaonaviWorkflowGraphNode
        from src.schemas.state import from_json

        node = KaonaviWorkflowGraphNode()
        forwarded = node._parent_config()["configurable"]["kaonavi"]
        assert forwarded["base_url"] == _runtime()["kaonavi"]["base_url"]

        subgraph = node.get_subgraph()
        injected = subgraph._extra_initial_state()
        assert from_json(injected["kaonavi_config"], {})["base_url"] == forwarded["base_url"]

    def test_malformed_runtime_values_are_not_forwarded(self, monkeypatch):
        """A malformed declaration falls back to the framework defaults -
        never crashes, never forwards a non-finite or out-of-range value."""
        import src.graph.graph as graph_mod

        monkeypatch.setattr(
            graph_mod,
            "_runtime_config",
            lambda: {
                "max_retry": float("nan"),
                "timeout_s": "soon",
                "kaonavi": {"base_url": "http://insecure.example"},
            },
        )
        assert graph_mod.agent_runtime_config() == {}
        node = graph_mod.KaonaviWorkflowGraphNode()
        assert node._parent_config() == {"configurable": {}}
