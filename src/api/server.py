"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import KaonaviHRProfileAgent, agent_runtime_config

app = FastAPI(title="Agent")

# The declared runtime settings (config/config.yaml) are passed as the graph
# config so the framework's own consumers (max_retry retry routing) see them —
# a no-arg construction would silently run on built-in defaults.
agent = KaonaviHRProfileAgent(config=agent_runtime_config())
agent.compile()
# Replace namespace/agent_name to match the agent's manifest values.
agent.provision_secrets(secrets_factory(namespace="cmn-c2-273", agent_name="KaonaviHRProfileAgent"))

# Upper bound on the serialized input_context (bytes). The pre_process node
# validates each field's type, shape and length; this adapter-level cap bounds
# the envelope as a whole before it enters the graph.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024
# Upper bound on top-level input_context entries - the contract declares a
# handful of fields, so dozens of keys is a malformed request, not a bigger one.
_MAX_INPUT_CONTEXT_KEYS = 16


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured invocation parameters: the target employee reference
    # (employee_id / employee_hint / employee_code) — validated field-by-field
    # inside the graph (PreProcessNode).
    input_context: dict[str, Any] | None = None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone-deployment caller auth: when INVOKE_AUTH_TOKEN is set on the
    # server environment, callers that no upstream middleware vouched for
    # (still ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (standalone equivalent of
    # the platform's auth middleware) — a deployment-level caller credential,
    # not an agent secret, so ctx.secrets does not apply (no InvocationContext
    # exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    input_context = req.input_context or {}
    if input_context and len(input_context) > _MAX_INPUT_CONTEXT_KEYS:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum entry count.")
    if input_context and len(json.dumps(input_context, default=str)) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum allowed size.")
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "KaonaviHRProfileAgent"}
