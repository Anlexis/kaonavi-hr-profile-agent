# Test Specification - CMN-C2-273 Kaonavi HR Profile Agent

## Test Strategy
- Test types: Unit (per node + service + config + inner graph) / Proof-of-Boundary
  (full outer-graph invoke, HTTP end-to-end through the real ASGI entry point,
  import isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end backbone coverage lives in PB-6 and the /invoke
  end-to-end suite, which drive the real compiled outer graph).
- The Kaonavi call is exercised through the deterministic, network-free stub
  transport (default) and through monkeypatched fake clients; no live Kaonavi call.
- **Trust-gate routing canon**: per-node unit tests invoke the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> PII mask -> `execute()` -> credential scan) - never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external gate)
  and `TrustLevel.ANONYMOUS.value` for every other node. Documented exceptions:
  the `CallKaonaviApiNode.execute(state, config=...)` config-override test (a 2nd
  argument `__call__` cannot forward), and the refusal tests that call
  `execute()` DIRECTLY on purpose - they prove the caller-contract and
  instruction-override guarantees are the template's own, holding with no
  framework gate in front. The trust rejection test asserts on the RETURNED
  error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in
  `error_log`, execute-only keys absent) - `__call__` never raises for a
  trust-gate denial.
- Assertion contract (wheel `agenticstar-agentcore[anthropic]==1.0.1`, the
  version the CI pipeline installs): the invoke surface is
  `result["output"]` / `status` / `trace_id` / `correlation_id` / `node_history`
  (never `formatted_output` at the invoke surface); status is compared to
  `AgentStatus.SUCCESS`/`.value` (lowercase `success`/`error`); the outer graph is
  called as `invoke(user_input=..., ctx=...)`; identifiers may be masked
  (`[MASKED]`) so record evidence is asserted by presence, not raw repr; audit
  spies assert on `call.args[1]` (the event payload), never the whole-call repr.
- Real-SDK pipeline behaviors encoded by the suite: `__call__` short-circuits on an
  incoming errored state (execute() is skipped; error status/error_log pass through);
  the framework input mask rewrites Title-Case bigrams (across newlines), emails, and
  digit groups in `user_input`/`validated_input` to `[MASKED]` before `execute()`
  sees the text - positive payloads are PII-free, intentional-PII tests assert the
  `[MASKED]` path. Injection-refusal assertions are behavioural (error status,
  nothing carried forward) - never a gate's wording, which differs across
  framework versions.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | serialize NL request + employee hint into `validated_input` (JSON); HTML strip; hint priority employee_id > employee_hint > employee_code; caller-contract fail-closed matrix (non-string types incl. bool/NaN/Infinity, non-code shapes, non-mapping context - field named, value never echoed); instruction-override screen on execute() directly (attack forms refused raw + markup-spliced, phrases, chat-template control tokens, hostile/nested/masked field names; ordinary directive-verb HR prose passes) | hint resolved by priority; `<script>` stripped; empty/missing -> `status=error`; every contract breach -> `status=error` naming the field; every attack form -> `status=error` with nothing carried forward |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`); inner-channel override re-screen | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string per the state contract); empty/short -> error; instruction-override content refused; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_profile / create_profile / update_profile (keyword, writes-first priority, read-only default); LLM path (test-double `_FakeLLM`) overrides the heuristic on a well-formed/fenced JSON reply; malformed response, out-of-enum value, and a raised API error all fall back to the heuristic unchanged; no LLM call on empty input; no double injected + no secret bound falls back to the heuristic (real production shape) | correct intent per keyword; no-signal defaults to lookup_profile with non-fatal note; empty -> error; audit emits intent label + `via_llm` flag; LLM override/fallback behave exactly as specified above, never a node error |
| U-05 | test_infer_kaonavi_fields_node.py | employee-code resolution (text > code-shaped hint; never invented, LLM output never consulted); quoted name; `Key: value` custom fields (entry-capped, value-length-capped); Kaonavi REST v2 `member_data` payload per intent (JSON string per the state contract); LLM path (test-double `_FakeLLM`) adds a field/name the heuristic parser missed, a fenced JSON reply still parses, a malformed/raising LLM falls back to the heuristic unchanged, a code-shaped key returned by the LLM is filtered out before reaching the payload, no LLM call on empty input, no double injected + no secret bound falls back to the heuristic | lookup `{member_code}`; create/update `member_data[0]` with code/name/custom_fields; unresolved code left `""`; empty input -> error; LLM-added fields merge without overwriting a heuristic-found key; employee_id is identical whether or not the LLM path ran |
| U-06 | test_call_kaonavi_api_node.py | lookup/create/update via the network-free stub; `kaonavi_config` state field + `execute(state, config=...)` override (documented direct-execute exception); API error / unresolved code / unknown intent / missing payload; secret posture (live transport refuses to run unauthenticated; token read via `ctx.secrets`, never env/state) | record_id/record_ref on success; the HTTP status (403) surfaces in error_log and the remote error body is never echoed; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to client; audit emits presence signals with `stub_transport=True` |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id formatting; name fallback | "Retrieved/Created/Updated HR profile ... ref=... id=..."; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (state-contract payload round-trip); errored state passes through `__call__` un-masked (short-circuit); record-evidence gate (module-level `_security_gate_output()` helper, full-node path + direct function tests); nested-walk credential scan (top-level control, credential nested in the payload mapping, credential-shaped mapping key caught and withheld from the label, clean nested pass, credential in error_log never reaches the caller); **existing-ERROR path containment** (envelope present AND truthy; no record_id/record_ref/profile_name/employee_id in the shipped envelope; every output-bearing field cleared in the delta; error status still reported; clean-path control so the containment assertions cannot pass vacuously); **closed-set error envelope** (parameterised over every non-success path - inner error, inner error with the answer still in `result`, inner error with a credential in error_log, missing record evidence, credential nested in the payload, credential-shaped mapping key: every envelope value is one of the module's `ERROR_REASONS`; `error_log` seeded with a sentinel upstream line that must appear nowhere in the returned mapping; inner entries not re-emitted; gate violations travel in error_log only) | success shape with parsed `kaonavi_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without record_id/record_ref blocked ("output gate"); nested credential caught with the PATH named, never the value; error envelope is `{"reason": <constant>}` only — no error_log text, no gate violations, no Kaonavi record evidence, and none left in state |
| U-09 | test_kaonavi_client.py | Kaonavi REST API v2 client: find/create/update member; `Kaonavi-Token` header; `KaonaviApiError` on non-2xx; stub shapes (member_data echo / task receipt + member_code echo, `_stub` marker); `uses_stub_transport` | correct URLs/headers/bodies; 400 raises with joined `errors`; stub deterministic shapes |
| U-10 | test_config.py | configuration contract: `config/agent.yaml` (flat registry manifest) + `config/config.yaml` (runtime parameters) + declared-value arrival at each consumer | id CMN-C2-273, Cat 2, CMN, ToolCallingAgent, flat dotted `class:` entry point, VERIFIED_EXTERNAL, `generation_mode: llm`, `requires.secrets` = the three Azure OpenAI secrets (KAONAVI_TOKEN stays an optional `ctx.secrets.get()` lookup, not declared), `requires.extras = ["openai"]`; `max_retry`/`timeout_s` reach the graph config; `kaonavi.base_url` reaches the inner graph's state; malformed values are not forwarded |
| U-11 | test_domain_workflow_graph.py | inner `KaonaviWorkflowGraph`: identity, `_extra_initial_state()` kaonavi_config JSON injection, `route()` error short-circuit, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as JSON string; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: 0 platform-internal SDK imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external-trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, KaonaviWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (config/config.yaml has no `hitl.enabled: true`): module-level skipif; skip stubs fail loudly if HITL is enabled without implementing PB-7 |
| PB (E2E) | HTTP end-to-end through POST /invoke | test_invoke_e2e.py | Bearer auth boundary (401 without/with wrong token); the public path does real work (record evidence from the instruction text AND from `input_context` alone - proof the caller contract crosses both graph boundaries - with the negative half asserted); caller contract fails closed over the wire (type/shape matrix, raw NaN/Infinity request bodies, oversized/over-keyed context -> 413, rejected values never echoed); injection refused end-to-end (raw, JSON-escaped, and in a caller field) while ordinary wording passes; output boundary (no credential-shaped string at any depth, no stack traces, identifiers cross byte-identical) |
| PB (containment) | Caller-facing envelope through `get_output()` | test_output_envelope_containment.py | post_process driven through its real call path (execute() for an errored state - `__call__` short-circuits on it and the backbone routes an error to finalize), partial state merged the way the reducer merges it, then projected through `KaonaviHRProfileAgent.get_output()`: on every non-success path (credential nested in the payload, missing record evidence, inner workflow error with the answer still in `result` and a sentinel upstream line in `error_log`) `output == {"reason": <one of ERROR_REASONS>}` and stays truthy; no refused text, record evidence, confirmation, sentinel fragment or gate violation appears anywhere in the envelope; the violation label sits in `error_log` only; the inner entry is present once (not re-emitted); clean-path control ships the answer with no reason code |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy
> tests (validate / classify / call nodes assert on the event payload,
> `call.args[1]`). PB-3 (live external service) is exercised at first invoke on
> a live deployment, not in this suite - the bundled transport is the
> documented network-free stub.

## Test Execution Summary
- Execution date: 2026-08-28
- Runner: pytest under the real framework wheel `agenticstar-agentcore[anthropic]==1.0.1` (the version the CI pipeline installs)
- Total tests: 188
- Pass: 186 / Fail: 0 / Skip: 2 (PB-7 A/B - auto-waived, non-HITL)
