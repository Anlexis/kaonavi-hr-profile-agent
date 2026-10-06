"""AgentCore Platform v1.0 - CMN-C2-273 Kaonavi HR Profile Agent state."""

# State contract: State must be a flat TypedDict - never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects (and
# nested dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). kaonavi_payload /
# kaonavi_config / redaction_flags are dicts/lists at the point of use but are
# stored in State as JSON strings via to_json/from_json below. Do NOT add
# credentials, secrets, or Pydantic models (PB-2 / PB-5). The Kaonavi
# integration token is NEVER stored here - it is read via ctx.secrets in
# CallKaonaviApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Kaonavi HR Profile agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Kaonavi-workflow fields are added below, all NotRequired (the flat
    state contract). All values are JSON/msgpack-serializable primitives -
    the Kaonavi integration token is NEVER stored here (accessed via
    ctx.secrets).
    """

    # Caller-supplied target hint (employee code or name fragment from
    # input_context / the request envelope). Never inferred; resolution to a
    # Kaonavi member code is explicit-only (v1: pass-through when the hint or
    # request text already carries a code).
    employee_hint: NotRequired[str]
    employee_id: NotRequired[str]  # resolved Kaonavi member (employee) code

    # ValidateInput (deterministic redaction scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferKaonaviFields
    profile_name: NotRequired[str]  # employee display name / record label
    # JSON - assembled Kaonavi REST API v2 request body (stored as a JSON
    # string, not a native dict; (de)serialize via to_json/from_json).
    kaonavi_payload: NotRequired[Optional[str]]

    # Declared `kaonavi:` config section forwarded by _parent_config() and
    # injected by the inner graph's _extra_initial_state() (JSON string).
    kaonavi_config: NotRequired[Optional[str]]

    # CallKaonaviApi
    record_id: NotRequired[str]  # member code / record id returned by Kaonavi
    record_ref: NotRequired[str]  # human-readable reference (kaonavi://members/<code>)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
