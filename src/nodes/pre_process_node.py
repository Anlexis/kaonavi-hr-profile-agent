"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: validate the caller's request (raw NL text + the
structured caller fields) and serialize it into `validated_input` for the
inner Kaonavi workflow graph. This node OWNS the caller-data contract: every
field a caller can supply is checked here, against explicit bounds, before
any of it reaches the workflow. Business validation (intent, entity
extraction) lives in the inner graph.

Caller contract (`input_context`), every field optional:

    employee_id / employee_hint / employee_code
        the target employee: a Kaonavi member code (short alphanumeric
        identifier). The first supplied alias wins.

Rules applied to it:
  - the value must be a STRING. A number, boolean, mapping or list is refused
    outright, never coerced: str(float("nan")) is "nan", which would pass a
    code-shape check, so coercion would let a non-finite value target the
    lookup.
  - the value must match the member-code shape (inert identifier alphabet,
    length-bounded). It renders into the confirmation and the record
    reference, so free text here would be caller-controlled output.
  - instruction-override text (directives aimed at the MODEL: role
    reassignment, system-prompt manipulation, chat-template control tokens)
    is REFUSED, fail closed, on BOTH caller text channels - the raw
    instruction text and every decoded string in input_context, keys
    included, at any depth - before anything is carried forward. The screen
    is the template's own (src/services/security.py), never delegated to a
    framework gate.
  - a refusal names the FIELD and never echoes the offending value; a hostile
    field NAME is masked, never echoed either.
  - absent fields are simply absent: the workflow falls back to what it can
    read out of the instruction text.
"""

import json
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.security import contains_instruction_override, sanitize_query

# A Kaonavi member code: short alphanumeric identifier (no spaces). It renders
# into the confirmation and the record reference, so it is locked to an inert
# identifier alphabet - the same shape the inner field-extraction step
# enforces on codes it reads out of the request text.
_CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")

_EMPLOYEE_ALIASES = ("employee_id", "employee_hint", "employee_code")

# Contract field names that may be echoed into a refusal message. Any other
# input_context key is caller-controlled text, so its spot in the reported
# path shows a placeholder - a hostile field NAME is never echoed either.
_KNOWN_CONTEXT_FIELDS = frozenset(_EMPLOYEE_ALIASES)


class CallerFieldError(ValueError):
    """A caller-supplied field failed its contract. Carries the field name only."""


def _find_instruction_override(value: object, path: str = "input_context") -> "str | None":
    """Depth-first scan of every decoded string in the mapping - keys included.

    Returns the path of the first string carrying an instruction-override
    directive, or None. The walk runs on the PARSED mapping, so JSON escaping
    cannot smuggle a phrase past it, and it covers undeclared keys too: the
    screen must hold on what the caller SENT, not only on what the contract
    keeps. Path components outside the declared contract are masked, so the
    returned path is always safe to name in an error message.
    """
    if isinstance(value, str):
        return path if contains_instruction_override(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in _KNOWN_CONTEXT_FIELDS else "<unrecognised-field>"
            key_path = f"{path}.{safe_key}"
            if isinstance(key, str) and contains_instruction_override(key):
                return key_path
            found = _find_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _validate_employee_ref(raw: object, field: str) -> str:
    """Validate an employee reference: a member-code-shaped inert identifier.

    Raises CallerFieldError naming the field (never the value) on any breach.
    Rejects every non-string type, `bool` included, so numeric input cannot be
    stringified into something that satisfies the shape check.
    """
    if not isinstance(raw, str):
        raise CallerFieldError(f"'{field}' must be a string")
    text = raw.strip()
    if not text:
        raise CallerFieldError(f"'{field}' must not be empty")
    if not _CODE_SHAPE_RE.match(text):
        raise CallerFieldError(f"'{field}' is not a valid member code")
    return text


def validate_caller_fields(input_context: object) -> "dict[str, str]":
    """Validate the caller contract. Raises CallerFieldError on any breach."""
    if input_context in (None, {}):
        return {}
    if not isinstance(input_context, dict):
        raise CallerFieldError("'input_context' must be a mapping")

    fields: dict[str, str] = {}
    supplied = [k for k in _EMPLOYEE_ALIASES if input_context.get(k) is not None]
    if supplied:
        fields["employee_hint"] = _validate_employee_ref(input_context[supplied[0]], supplied[0])
    return fields


class PreProcessNode(FunctionNode):
    """Validate the caller contract and shape the request for the inner graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Kaonavi call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # -- Instruction-override screen (template-owned, fail CLOSED) --------
        # Runs on BOTH caller text channels before anything is carried
        # forward: a refusal leaves no validated_input and no employee_hint
        # for any downstream node. The screen lives in this node's own
        # execute() path - calling execute() directly still refuses, so the
        # guarantee does not depend on any framework gate being present or
        # configured on.
        if contains_instruction_override(user_input):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": "user_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request refused - instruction-override content in user_input"],
            }

        override_path = _find_instruction_override(input_context) if isinstance(input_context, (dict, list)) else None
        if override_path is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": override_path},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: request refused - instruction-override content in {override_path}"],
            }

        try:
            caller_fields = validate_caller_fields(input_context)
        except CallerFieldError as exc:
            # Fail closed, naming the field only - the rejected value is never
            # echoed into the log.
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "caller_field", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: rejected caller input - {exc}"],
            }

        # Strip HTML markup + cap length before JSON serialization.
        sanitized_input = sanitize_query(user_input.strip())

        # Target employee is caller-supplied and never inferred here; the
        # validated (inert, length-bounded) code is the only context field
        # that crosses into the workflow.
        employee_hint = caller_fields.get("employee_hint", "")

        validated_input = json.dumps({"text": sanitized_input, "employee_hint": employee_hint})

        # Audit the shaped request - hint presence only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {"has_employee_hint": bool(employee_hint)},
            state,
        )

        return {
            "validated_input": validated_input,
            "employee_hint": employee_hint,
            "status": AgentStatus.SUCCESS.value,
        }
