"""AgentCore Platform v1.0 - inner workflow Step 4: CallKaonaviApi (tool side-effect).

Performs the lookup/create/update call against the Kaonavi REST API v2 member
endpoints via src/services/kaonavi_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs (a first-invoke failure on a real deployment). The node
       therefore stays ANONYMOUS.
  Secrets: the integration token is read via ctx.secrets.get("KAONAVI_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. Note: while the deterministic NETWORK-FREE stub transport is
       active a missing token is tolerated (a sentinel placeholder is used - it
       is never sent anywhere because no request leaves the process); with a
       LIVE transport injected, a missing token is a hard status=error - a real
       API is never called unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external HR system; HTTP 4xx/5xx surfaces as status=error +
       error_log (no silent pass). The log line carries the HTTP status only -
       never the API's own message, which is remote content - and a transport
       failure is reported by exception class, never by its text.

Configuration: this node takes NO constructor arguments (nodes are no-arg by
contract). Kaonavi settings (base_url) arrive as the JSON `kaonavi_config`
state field - injected by the inner graph's _extra_initial_state() from the
section forwarded by KaonaviWorkflowGraphNode._parent_config() - or via the
optional `config["configurable"]["kaonavi"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.kaonavi_client import KaonaviApiError, KaonaviClient

_SECRET_KEY = "KAONAVI_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"


class CallKaonaviApiNode(FunctionNode):
    """Look up / create / update a Kaonavi HR-profile record via REST API v2."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = from_json(state.get("kaonavi_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallKaonaviApiNode: missing kaonavi_payload"],
            }

        intent = state.get("intent", "lookup_profile") or "lookup_profile"

        # Settings: config section from state (graph-injected), overridable via
        # an explicit config["configurable"]["kaonavi"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("kaonavi_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("kaonavi") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, see the
        # design document).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = KaonaviClient(base_url=base_url) if base_url else KaonaviClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallKaonaviApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        employee_id = state.get("employee_id", "") or str(payload.get("member_code", "") or "")

        try:
            if intent == "lookup_profile":
                if not employee_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallKaonaviApiNode: unresolved employee code - cannot look up profile"],
                    }
                resp = client.find_member(employee_id, api_token) or {}
                members = resp.get("member_data") or []
                if not members:
                    # Names the condition, never the caller-supplied code.
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallKaonaviApiNode: no member record found for the requested employee code"],
                    }
                record = members[0]
                record_id = str(record.get("code", "")) or employee_id
                profile_name = state.get("profile_name", "") or str(record.get("name", ""))
            elif intent in ("create_profile", "update_profile"):
                if intent == "update_profile" and not employee_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallKaonaviApiNode: unresolved employee code - cannot update profile"],
                    }
                if intent == "create_profile":
                    resp = client.create_member(payload, api_token) or {}
                else:
                    resp = client.update_member(payload, api_token) or {}
                record_id = str(resp.get("member_code", "")) or employee_id
                profile_name = state.get("profile_name", "")
            else:
                # Names the field, never the caller-controlled value.
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["CallKaonaviApiNode: unknown intent"],
                }
        except KaonaviApiError as exc:
            # The HTTP status is the closed-set part of the failure and is all
            # that is logged. The API's message is remote content - an upstream
            # body can carry identifiers, names or echoed request fields - and
            # capping or stripping it would still leave arbitrary text in the
            # log line, so it is not echoed at all.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallKaonaviApiNode: Kaonavi API error {exc.status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Type name only: an arbitrary exception string could carry
            # request content into the caller-facing error log.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallKaonaviApiNode: Kaonavi call failed ({type(exc).__name__})"],
            }

        record_ref = f"kaonavi://members/{record_id}" if record_id else ""

        # Audit the tool side-effect - intent + presence signals only,
        # never profile content or credentials.
        emit_trace_event(
            "call_kaonavi_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "employee_id": employee_id or record_id,
            "profile_name": profile_name,
            "status": AgentStatus.SUCCESS.value,
        }
