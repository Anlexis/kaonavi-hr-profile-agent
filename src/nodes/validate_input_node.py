"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input and runs a deterministic (regex, NOT LLM)
scan of the inbound text for email addresses / access-token-like strings,
which are flag-and-redacted before anything is logged. An HR-profile request
legitimately names an employee (the framework's PII mask in the node pipeline
additionally masks emails/phones/names in user_input / validated_input), so
this is flag-and-redact for safe logging, not a hard reject.

Hard rejects on this channel: the empty / non-request guard, and the
template's own instruction-override screen (src/services/security.py). The
outer pre_process already screens the raw request, but this node re-screens
the text it actually forwards - the guarantee must hold even when the inner
graph is driven directly, without the outer backbone in front.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import contains_instruction_override, redact_sensitive

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Validate + flag-and-redact the inbound HR-profile request."""

    # Inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        employee_hint = state.get("employee_hint", "")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                employee_hint = obj.get("employee_hint", employee_hint)
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Template-owned instruction-override screen (fail CLOSED). Screens the
        # text both raw and after markup strip - see security.py.
        if contains_instruction_override(text):
            emit_trace_event(
                "validate_input_refused",
                {"reason": "instruction_override"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: request refused - instruction-override content"],
            }

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        redacted, flags = redact_sensitive(text)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_employee_hint": bool(employee_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "employee_hint": employee_hint,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
