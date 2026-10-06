# CMN-C2-273 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline around execute()); this backbone formatter declares ANONYMOUS -> the
# state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The
# domain output gate is the MODULE-LEVEL _security_gate_output() helper (the
# framework gate methods are final and auto-wrapped hooks break the invoke
# chain), so the helper is also unit-tested directly as a plain function.
# The gate walks EVERY representation - nested mappings, list entries and
# mapping keys - so a credential one level down is caught exactly like a
# top-level one.
#
# The errored-state branch is driven through execute() DIRECTLY where noted:
# BaseNode.__call__ short-circuits on an incoming errored state and the
# backbone routes an error straight to finalize, so that branch is only
# reachable from inside - and it must still be a closed set.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    ERROR_REASONS,
    _CLEARED_OUTPUT_STATE,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
    _security_gate_output,
)
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "1001",
        "record_ref": "kaonavi://members/1001",
        "profile_name": "Member 1001",
        "intent": "lookup_profile",
        "confirmation": "Retrieved HR profile 'Member 1001' - ref=kaonavi://members/1001 - id=1001",
        "kaonavi_payload": to_json({"member_code": "1001"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _bearer(fill: str = "a") -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + fill * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must survive nowhere in a returned mapping.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the enum .value STRING, never the
        # bare AgentStatus enum member (a str-Enum passes a plain isinstance,
        # so the member is excluded explicitly).
        assert isinstance(result["status"], str)
        assert not isinstance(result["status"], AgentStatus)
        out = result["formatted_output"]
        assert out["record_id"] == "1001"
        assert out["record_ref"] == "kaonavi://members/1001"
        assert out["intent"] == "lookup_profile"
        assert out["confirmation"].startswith("Retrieved HR profile")
        # State-contract round-trip: the JSON kaonavi_payload string surfaces parsed.
        assert out["kaonavi_payload"] == {"member_code": "1001"}

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallKaonaviApiNode: Kaonavi API error 403"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Kaonavi API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "1001", "record_ref": "kaonavi://members/1001", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        violations = _security_gate_output(
            {"record_id": "1001", "note": _bearer()},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []


class TestGateWalksEveryRepresentation:
    """The gate scans nested structures, list entries and mapping KEYS - a
    scan limited to top-level strings would step past a credential riding in
    the payload mapping."""

    def test_top_level_control_proves_the_scan_fires(self):
        # The control case: without it, a clean nested probe cannot tell
        # "gate is blind" from "probe is wrong".
        violations = _security_gate_output({"record_id": "1001", "note": _bearer()}, is_success=True)
        assert violations

    def test_credential_nested_in_payload_mapping_is_caught(self):
        bearer_like = _bearer("b")
        formatted = {
            "record_id": "1001",
            "kaonavi_payload": {
                "member_data": [{"code": "1001", "custom_fields": [{"name": "notes", "values": [bearer_like]}]}]
            },
        }
        violations = _security_gate_output(formatted, is_success=True)
        assert any("kaonavi_payload" in v for v in violations)
        # The violation names the PATH, never the value.
        assert all(bearer_like not in v for v in violations)

    def test_credential_shaped_mapping_key_is_caught_and_never_repeated_in_the_label(self):
        """The label is built from mapping keys, so a credential-shaped key
        would otherwise be quoted by the very violation that reports it - and
        that label travels in error_log, where the framework's credential scan
        raises and discards the cleared result. The key is withheld instead."""
        jwt_like = "eyJ" + "c" * 40
        violations = _security_gate_output({"record_id": "1001", "kaonavi_payload": {jwt_like: "x"}}, is_success=True)
        assert violations
        assert all(jwt_like not in v for v in violations)
        assert any("<withheld>" in v for v in violations)

    def test_clean_nested_payload_passes(self):
        formatted = {
            "record_id": "1001",
            "kaonavi_payload": {"member_data": [{"code": "1001", "name": "Member 1001"}]},
        }
        assert _security_gate_output(formatted, is_success=True) == []

    def test_credential_in_error_log_never_reaches_the_caller(self):
        """A remote 4xx body would arrive in error_log. The error path
        publishes the reason code only, so the body cannot reach the caller
        through it - and the inner entries are not re-emitted either.

        execute() is called DIRECTLY: BaseNode.__call__ short-circuits on an
        already-errored state, so this branch is only reachable from inside."""
        node = PostProcessNode()
        bearer_like = _bearer("d")
        result = node.execute(
            _state(
                status=AgentStatus.ERROR.value,
                record_id="",
                record_ref="",
                error_log=[f"upstream failure: {bearer_like}"],
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert bearer_like not in json.dumps(result, default=str)
        assert "error_log" not in result


class TestViolationContainment:
    """A violating output is CLEARED, not just error-flagged: the response
    envelope reads formatted_output falling back to result with no status
    check, so a bare error status would still ship the un-gated answer."""

    def _violating_state(self, bearer_like):
        return _state(
            profile_name=bearer_like,
            confirmation=f"Retrieved '{bearer_like}'",
            result={"confirmation": f"Retrieved '{bearer_like}'"},
        )

    def test_success_path_violation_clears_every_output_field(self):
        node = PostProcessNode()
        bearer_like = _bearer("e")
        result = node.execute(self._violating_state(bearer_like))
        assert result["status"] == AgentStatus.ERROR.value
        # Every output-bearing field is cleared in the returned delta.
        assert result["result"] is None
        assert result["confirmation"] == ""
        assert result["kaonavi_payload"] is None
        assert result["profile_name"] == ""
        assert result["record_id"] == "" and result["record_ref"] == ""
        # The released text is nowhere in the delta.
        assert bearer_like not in json.dumps(result, default=str)

    def test_withheld_envelope_is_truthy_so_the_fallback_never_fires(self):
        """formatted_output must be TRUTHY: an empty replacement would
        activate the envelope's `or result` fallback - the exact leak the
        clearing exists to prevent. It is the reason code and nothing else."""
        node = PostProcessNode()
        result = node.execute(self._violating_state(_bearer("f")))
        assert result["formatted_output"]
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}


class TestExistingErrorPathContainment:
    """The pre-existing-ERROR branch must contain, not re-publish.

    molt source review (wave-7, 2026-09-04): "existing-ERROR path includes
    Kaonavi record evidence and retains output-bearing state."

    record_id / record_ref are the WRITE EVIDENCE for this agent - the success
    gate REFUSES a SUCCESS that lacks them. Returning them in an envelope whose
    status is ERROR tells a caller being informed of failure that a Kaonavi
    record was nonetheless touched, and which one. Kaonavi is an HR system, so
    the record identifiers and the profile name are personal data.

    Two properties are asserted, and they are different properties:
      1. the shipped envelope carries none of it, AND stays TRUTHY - a falsy
         formatted_output re-opens the framework's `formatted_output or result`
         projection (get_output(), no status check) onto whatever survived;
      2. the returned delta CLEARS the output-bearing state fields, so a
         checkpoint or a downstream reader cannot pick them up either.
    """

    _RECORD_ID = "1001"
    _RECORD_REF = "kaonavi://members/1001"
    _PROFILE_NAME = "Member 1001"
    _EMPLOYEE_ID = "EMP-1001"

    # Every output-bearing State field: the envelope composes them and
    # downstream formatting/checkpointing reads them.
    _OUTPUT_BEARING = (
        "result",
        "record_id",
        "record_ref",
        "employee_id",
        "profile_name",
        "confirmation",
        "kaonavi_payload",
        "intent",
    )

    def _errored_state(self) -> dict:
        """An error raised AFTER the Kaonavi call resolved a record - the
        realistic shape (call succeeded, a later step failed), and the only
        shape in which record evidence is present on an error at all."""
        return _state(
            status=AgentStatus.ERROR.value,
            record_id=self._RECORD_ID,
            record_ref=self._RECORD_REF,
            profile_name=self._PROFILE_NAME,
            employee_id=self._EMPLOYEE_ID,
            confirmation=f"Retrieved HR profile '{self._PROFILE_NAME}' - ref={self._RECORD_REF}",
            result={
                "record_id": self._RECORD_ID,
                "record_ref": self._RECORD_REF,
                "confirmation": f"Retrieved HR profile '{self._PROFILE_NAME}'",
            },
            error_log=["ConfirmNode: downstream failure after the Kaonavi call"],
        )

    def test_error_envelope_is_present_and_truthy(self):
        """Containment must not be achieved by emptying the envelope: the
        framework projects `formatted_output or result` with NO status check,
        so a falsy value hands the caller `result` instead."""
        result = PostProcessNode().execute(self._errored_state())
        assert "formatted_output" in result, "error path must ship an envelope"
        assert result["formatted_output"], (
            "error envelope must be TRUTHY - a falsy one re-opens the "
            "`formatted_output or result` fallback in AgentBaseGraph.get_output()"
        )

    def test_error_envelope_carries_no_record_evidence(self):
        """The leak itself: no record identifier or personal field may ride
        the failure envelope back to the caller."""
        shipped = json.dumps(
            PostProcessNode().execute(self._errored_state())["formatted_output"],
            default=str,
            ensure_ascii=False,
        )
        leaked = [
            field
            for field, value in (
                ("record_id", self._RECORD_ID),
                ("record_ref", self._RECORD_REF),
                ("profile_name", self._PROFILE_NAME),
                ("employee_id", self._EMPLOYEE_ID),
            )
            if value in shipped
        ]
        # The failure names the FIELD, never the value.
        assert not leaked, f"error envelope leaked Kaonavi record evidence: {leaked}"

    def test_error_return_clears_output_bearing_state(self):
        """ "Retains output-bearing state": omitting a field from ONE envelope
        is not clearing it. The delta must blank every output-bearing field so
        no checkpoint or downstream reader can recover it."""
        result = PostProcessNode().execute(self._errored_state())
        retained = [field for field in self._OUTPUT_BEARING if field not in result or result[field]]
        assert not retained, f"output-bearing state not cleared on the error path: {retained}"

    def test_error_status_is_still_reported(self):
        """Containment must not mask the failure."""
        assert PostProcessNode().execute(self._errored_state())["status"] == AgentStatus.ERROR.value

    def test_clean_path_control_still_returns_the_answer(self):
        """CONTROL. Without it every containment assertion above passes
        vacuously - a node that returned an empty envelope would satisfy them
        all. The success path must still carry the record evidence."""
        result = PostProcessNode().execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "1001"
        assert out["record_ref"] == "kaonavi://members/1001"
        assert out["profile_name"] == "Member 1001"
        assert out["confirmation"].startswith("Retrieved HR profile")
        assert out["kaonavi_payload"] == {"member_code": "1001"}
        assert "reason" not in out


# Every non-success path this node has. `via` says how the path is reached:
# node(state) runs the framework pipeline; execute() is used for an errored
# state because BaseNode.__call__ short-circuits on it (and the backbone routes
# an error straight to finalize), so that branch is only reachable from inside.
_ERROR_PATHS = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "record_id": "", "record_ref": ""},
        "execute",
        id="inner-workflow-error",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "result": {"record_id": "1001", "confirmation": "Retrieved HR profile 'Member 1001'"},
        },
        "execute",
        id="inner-workflow-error-with-answer-in-result",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "error_log": [_sentinel(), "CallKaonaviApiNode: rejected " + _bearer()],
        },
        "execute",
        id="inner-workflow-error-with-credential-in-error-log",
    ),
    pytest.param({"record_id": "", "record_ref": ""}, "call", id="gate-missing-record-evidence"),
    pytest.param(
        {
            "kaonavi_payload": to_json(
                {"member_data": [{"code": "1001", "custom_fields": [{"name": "notes", "values": [_bearer()]}]}]}
            )
        },
        "call",
        id="gate-credential-nested-in-payload",
    ),
    pytest.param(
        {"kaonavi_payload": to_json({"member_data": [{"code": "1001", "sk-" + "a" * 20: _bearer()}]})},
        "call",
        id="gate-credential-shaped-mapping-key",
    ),
]


class TestErrorEnvelopeIsClosedSet:
    """Whatever the non-success path, the caller-visible envelope is made of
    this module's own constants - never of node-authored text.

    error_log is seeded with a recognisable sentinel on every path: a name and
    a credential-shaped token inside an echoed upstream body, which is exactly
    what an API-call failure can put there. Truncating or redacting such a line
    is not a closed set, so it must appear nowhere in what the node returns.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _drive(self, overrides: dict, via: str) -> dict:
        state = _state(**{"error_log": [_sentinel()], **overrides})
        return self.node.execute(state) if via == "execute" else self.node(state)

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_values_are_declared_constants(self, overrides, via):
        result = self._drive(overrides, via)

        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}, envelope
        assert set(envelope.values()) <= ERROR_REASONS, envelope

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_stays_truthy_and_the_answer_is_cleared(self, overrides, via):
        """`formatted_output or result`: a falsy envelope re-opens the fallback,
        and a surviving `result` is what it would fall back onto."""
        result = self._drive(overrides, via)

        assert result["formatted_output"], "a falsy formatted_output re-opens the `result` fallback"
        assert result["result"] is None
        cleared = {field: result[field] for field in _CLEARED_OUTPUT_STATE if field != "result"}
        assert not any(cleared.values()), cleared

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_seeded_error_text_appears_nowhere_in_the_returned_mapping(self, overrides, via):
        result = self._drive(overrides, via)

        leaves = list(_leaves(result))
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), (fragment, result)
        rendered = json.dumps(result, default=str)
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered

    def test_the_reason_names_the_path_taken(self):
        errored = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        withheld = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert errored["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert withheld["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer appends error_log; re-emitting the inner entries
        would duplicate every line, and the caller never sees them anyway."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))

        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        result = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert any("output gate" in entry for entry in result["error_log"])
        assert "output gate" not in json.dumps(result["formatted_output"])

    def test_a_caller_field_name_lands_in_a_value_and_the_label_names_fixed_keys_only(self):
        """The assembled payload's mapping keys are fixed by the field-inference
        step; a caller's field NAME becomes the `name` VALUE of a custom-field
        entry. So the violation label is built from fixed keys and indices and
        cannot carry caller text - and the caller receives the reason code
        only, so it could not reach them through the envelope anyway."""
        field_name = "A. Tanaka <a.tanaka@" + "example.com>"
        payload = {"member_data": [{"code": "1001", "custom_fields": [{"name": field_name, "values": [_bearer()]}]}]}
        result = self.node(_state(kaonavi_payload=to_json(payload), error_log=[_sentinel()]))

        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        label = " ".join(result["error_log"])
        assert "formatted_output.kaonavi_payload.member_data[0].custom_fields[0].values[0]" in label
        assert "Tanaka" not in label
        assert _bearer() not in label

    def test_a_credential_shaped_mapping_key_is_withheld_from_the_label(self):
        """A key that is itself credential-shaped is refused AND kept out of
        the label: quoted, it would travel in error_log, where the framework
        credential scan raises on the node result and replaces the cleared
        result - restoring the leak the clearing just closed. The clearing
        surviving is the proof that nothing raised."""
        credential_shaped_key = "sk-" + "a" * 20
        payload = {"member_data": [{"code": "1001", credential_shaped_key: "x"}]}
        result = self.node(_state(kaonavi_payload=to_json(payload)))

        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert result["result"] is None
        assert credential_shaped_key not in json.dumps(result, default=str)
        assert any("<withheld>" in entry for entry in result["error_log"])
