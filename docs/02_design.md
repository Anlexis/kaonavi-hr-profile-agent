# Template Design Specification — CMN-C2-273 Kaonavi HR Profile Agent

## Position in AgentCore Architecture

- **Agent Class**: `KaonaviHRProfileAgent` (`src/graph/graph.py`)
- **L1 Base (framework base class)**: `AgentBaseGraph` — direct framework inheritance
- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Pipeline type**: ToolCalling — classify intent -> extract HR-profile
  fields -> build a Kaonavi REST API v2 request -> call the tool -> format the
  confirmation. No RAG retrieval, no autonomous loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: framework base-class inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | validate the caller contract (`input_context` employee reference: type + shape + length, fail closed); instruction-override screen on both caller text channels; HTML/length sanitize; serialize NL request + employee hint into `validated_input` (JSON) | user_input, input_context | validated_input, employee_hint | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Kaonavi workflow subgraph | validated_input | result, intent, employee_id, record_id, record_ref, profile_name, confirmation, kaonavi_payload | GraphNode (caller ctx forwarded unchanged) | KaonaviWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; module-level `_security_gate_output()` scan (nested walk, error branch included) | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------------|
| validate_input | 1 ValidateInput | empty/non-request guard; instruction-override re-screen; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, employee_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | LLM classification (Azure OpenAI, degrades to a deterministic keyword heuristic on any failure) -> lookup_profile / create_profile / update_profile; low-confidence/unavailable-LLM -> lookup_profile (read-only default — never a write) | intent | ANONYMOUS |
| infer_kaonavi_fields | 3 InferKaonaviFields | extract display name / "Key: value" profile fields (LLM-enhanced, degrades to deterministic regex on any failure) and employee code (always deterministic — LLM output never consulted); assemble the Kaonavi REST API v2 request body per intent; an unresolved employee code is left empty (never invented) | profile_name, employee_id, kaonavi_payload | ANONYMOUS |
| call_kaonavi_api | 4 CallKaonaviApi | GET /members (lookup) / POST /members (create) / PATCH /members (update) via `KaonaviClient`; token via ctx.secrets; 4xx/5xx -> status=error | record_id, record_ref, employee_id, profile_name | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / KaonaviWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_kaonavi_fields
              -> call_kaonavi_api -> confirm -> END
```

Structured parameters travel as a JSON string: `pre_process` serializes
`{"text", "employee_hint"}` into `validated_input`,
`KaonaviWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
the first inner node (`validate_input`) parses it back. Inner nodes read
`state.get("validated_input") or state.get("user_input", "")`.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (flat state contract —
fields are absent until their producer node writes them). Dict/list payloads
are stored as JSON strings (`Optional[str]`) via the module helpers
`to_json` / `from_json`, used by every producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| employee_hint | NotRequired[str] | caller-supplied employee code/name hint; never inferred | pre_process / validate_input |
| employee_id | NotRequired[str] | resolved Kaonavi member code (pass-through when the hint/text already carries a code) | infer_kaonavi_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of pattern categories redacted before logging | validate_input |
| profile_name | NotRequired[str] | employee display name / record label | infer_kaonavi_fields / call_kaonavi_api |
| kaonavi_payload | NotRequired[Optional[str]] | JSON — assembled Kaonavi REST API v2 request body (msgpack-safe: stored as a JSON string via `to_json`/`from_json`) | infer_kaonavi_fields |
| kaonavi_config | NotRequired[Optional[str]] | JSON — declared `kaonavi:` section from `config/config.yaml`, forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| record_id | NotRequired[str] | member code / record id returned by Kaonavi | call_kaonavi_api |
| record_ref | NotRequired[str] | human-readable record reference (`kaonavi://members/<code>`) | call_kaonavi_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the Kaonavi token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration Forwarding (nested Cat-2)

Nodes take **no constructor arguments** (nodes are no-arg by contract;
configuration never rides on node instances). Configuration is split across
two files:

- `config/agent.yaml` — the static registry manifest (identity, entry point,
  trust level, compile-time requires). Flat root-level keys only.
- `config/config.yaml` — the runtime parameters. `max_retry` / `timeout_s`
  feed the outer graph config (`agent_runtime_config()` in
  `src/graph/graph.py` — the platform registry passes the same file as
  `Graph(config=...)`, and the standalone server constructs the agent with
  it, so the framework's retry routing sees the declared value in both
  deployments). The `kaonavi:` section is forwarded to the inner graph by
  `KaonaviWorkflowGraphNode._parent_config()` under `config["configurable"]`.

Every forwarded value is validated (type, finiteness, range, URL shape) —
a malformed configuration file degrades to the built-in defaults, never
crashes graph construction and never forwards a non-finite or unbounded
value. The inner graph's `_extra_initial_state()` then injects the `kaonavi`
section into State as a JSON string (`kaonavi_config`), where
`CallKaonaviApiNode.execute(state, config=None)` reads it (an explicit
`config["configurable"]["kaonavi"]` override is also honoured for direct/unit
invocation). The configuration tests assert each declared value ARRIVES at
its consumer, so a dead declaration cannot sit in the shipped config.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the write-capable `CallKaonaviApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would trust-deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation).
- **Caller-data contract (fail closed)** — `PreProcessNode` owns the
  `input_context` contract: the employee reference
  (`employee_id`/`employee_hint`/`employee_code`) must be a STRING (never
  coerced — `str(float("nan"))` is `"nan"`, which would pass a shape check)
  matching the inert member-code shape (`[A-Za-z0-9][A-Za-z0-9_-]{0,19}`),
  because it renders into the confirmation and the record reference. A breach
  is refused naming the FIELD, never echoing the value; a hostile field NAME
  is masked. The adapter (`src/api/server.py`) additionally caps the
  input_context envelope (bytes + entry count).
- **Instruction-override screen (template-owned, fail closed)** —
  `src/services/security.py` refuses directives aimed at the model
  (role reassignment, system-prompt manipulation) and chat-template control
  tokens (`<|...|>`, `[INST]`, `<<SYS>>`), screening each text BOTH raw and
  after the markup strip (a raw-only screen loses the control tokens the
  strip removes; a stripped-only screen misses a directive spliced with
  markup, `ig<b>nore`, that re-assembles). The screen runs depth-first over
  the parsed `input_context` — keys included — in `PreProcessNode`, and again
  on the forwarded text in `ValidateInputNode`, so the guarantee holds even
  when the inner graph is driven directly and never depends on a framework
  gate being present. Ordinary HR prose with directive verbs ("correct the
  previous entry", "override the previous salary entry") passes.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a
  deterministic (regex, NOT LLM) scan for email addresses and access-token-like
  strings (`eyJ...`, `secret_...`, `sk-...`) and redacts them before any
  logging. An HR-profile request legitimately names an employee, so this is
  flag-and-redact for safe logging, not a hard reject; the framework's PII
  mask in `BaseNode.__call__` additionally masks emails/phones/names in
  `user_input`/`validated_input`. The structured caller channel carries no
  free text at all — the only contract field is locked to the inert
  member-code alphabet — so no unredacted caller text can bypass this scan.
- **Secrets + output gate** — the integration token is read via
  `ctx.secrets.get("KAONAVI_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. The lookup is OPTIONAL by design
  (`requires.secrets` is empty): a missing token is tolerated **only** while
  the deterministic network-free stub transport is active (no live call is
  made); with a live transport injected, a missing token is a hard
  `status=error`. The domain output gate is the **module-level**
  `_security_gate_output()` in `src/nodes/post_process_node.py`, called from
  `PostProcessNode.execute()`: it blocks any SUCCESS response that lacks
  record evidence (record_id/record_ref) and any credential-shaped string
  ANYWHERE in the caller-facing output — the scan walks nested mappings, list
  entries and mapping keys (`kaonavi_payload` is a nested structure carrying
  caller-derived text, so a top-level-only scan would step past a credential
  one level down). A violation names the offending PATH, never the value
  (a credential-shaped mapping key is withheld from the label rather than
  quoted), and is written to `error_log` only — it never enters the caller's
  envelope.
- **Error-path containment (every error return, not just a gate violation)** —
  both error returns of `PostProcessNode.execute()` — a gate violation and a
  pre-existing inner-workflow error — CLEAR every output-bearing state field
  (`result`, `confirmation`, `kaonavi_payload`, `profile_name`, `employee_id`,
  `intent`, and the record evidence) and replace `formatted_output` with the
  closed-set envelope built by `_contain()`: a constant reason code
  (`kaonavi_workflow_failed` / `output_withheld_by_gate`) and nothing else —
  never `error_log`, never the gate's violation entries, nothing read out of
  the record. Node-authored error text can embed an upstream API error body,
  identifiers or names, and truncating or redacting it is not a closed set;
  `error_log` stays the internal channel (state reducer + audit trail) and is
  not projected to the caller.
  `record_id`/`record_ref` are this agent's WRITE EVIDENCE — the gate refuses a
  SUCCESS that lacks them — so returning them under an ERROR status would tell
  a caller being informed of failure that a Kaonavi record was nonetheless
  touched, and which one; Kaonavi is an HR system, so those identifiers and the
  profile name are personal data. Omitting a field from one envelope is not
  clearing it: the clearing is what stops a checkpoint or a downstream reader
  recovering it. The envelope is deliberately TRUTHY — the response envelope
  reads `formatted_output` falling back to `result` with no status check, so a
  bare error status would still ship the un-gated inner answer, and an
  empty/falsy replacement would activate that same fallback. No node defines
  `_extra_security_gate_input/_output` instance methods (framework hooks are
  final / auto-wrapped — domain checks live inline or in module-level helpers).
- **Output invariant note** — the response renders record identifiers, the
  intent label, and confirmation text only; it aggregates no monetary values,
  so no numeric rounding/precision rule applies to this template. The
  enforced output invariants are exactly the two above
  (record-evidence-on-success; no credential-shaped string at any depth), and
  the boundary tests pin that identifiers — including pure-digit member codes
  — cross the boundary byte-identical.
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (intent / presence signals only — never request text, profile
  content, or credentials), plus a refusal event on each fail-closed path
  (`pre_process_validation_failed`, `validate_input_refused`,
  `post_process_gate_blocked`, `post_process_error_contained` — reason codes
  and counts only, never record content). `__call__()` is
  never overridden; `_invoke_impl` is never defined on any node. Event names
  (documented for operations):

  | Node | Audit event |
  |------|-----------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_kaonavi_fields | `infer_kaonavi_fields_complete` |
  | call_kaonavi_api | `call_kaonavi_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` |

## Implementation Note — LLM synthesis

Intent classification (`ClassifyIntentNode`) and field extraction
(`InferKaonaviFieldsNode`) are LLM-enhanced: each node first attempts an Azure
OpenAI call (`AzureOpenAIClient`, built fresh per invocation from
`ctx.secrets`, never cached on the node instance — the canonical Cat 1/2 LLM
injection pattern, since nodes are constructed in `register_nodes()` before
secrets are provisioned), then falls back to the original deterministic
keyword/regex heuristic on any failure (missing secret, API error,
malformed/wrong-shape response, out-of-enum value). The pipeline still runs
and tests end-to-end without a live LLM — the heuristic is a complete,
independently correct implementation, not a stub — and the manifest declares
`generation_mode: llm` + the three Azure OpenAI secrets + the `openai` extra
accordingly.

**Employee code resolution is excluded from the LLM path in every build.**
`InferKaonaviFieldsNode._resolve_code()` never consults the LLM output — the
system prompt explicitly instructs the model not to return a code, and any
`code`/`id`-keyed field the model returns anyway is filtered out before it
can reach the assembled payload. This preserves the pre-existing safety
invariant unconditionally: a hallucinated employee code is the one failure
mode this node exists to prevent (see "Write target" in the Design Decision
Record below), so it is never a decision an LLM call gets to make, regardless
of whether the LLM enhancement is available or degraded.

`ClassifyIntentNode`'s LLM path can only select among the same three
enumerated intents the heuristic already validates against
(`lookup_profile`/`create_profile`/`update_profile`); an out-of-enum LLM
response is treated identically to a parse failure and falls through to the
heuristic.

## Limitation — Kaonavi client (documented)

`src/services/kaonavi_client.py` is a pure service layer (injectable
transport, `KaonaviApiError`, per-call token, no framework imports) that ships
a **deterministic, network-free stub** as its default transport: it returns the documented Kaonavi REST API v2 response
shapes (a `member_data` list for lookups; a task/receipt shape with a synthetic
member code echo for create/update, derived from the request) so the pipeline
is runnable and testable without a live Kaonavi tenant or the `requests`
package. It does **not** perform a live Kaonavi call. The rule it follows:
never fake a live call — document the limitation. To go live,
inject real `post`/`patch`/`get` transports at construction; the method
contracts and payload shapes are already Kaonavi REST API v2 exact, so no
business-logic change is required. (The stub also runs without a live
credential — see the secrets note above; a live transport requires `KAONAVI_TOKEN`.)

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallKaonaviApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallKaonaviApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("KAONAVI_TOKEN")` (optional lookup; see the secrets note); `ctx.secrets.require("AZURE_OPENAI_API_KEY"/"AZURE_OPENAI_ENDPOINT"/"AZURE_OPENAI_DEPLOYMENT")` in `ClassifyIntentNode`/`InferKaonaviFieldsNode` (compile-time required per `requires.secrets`; a missing secret degrades that node's LLM path to its heuristic at runtime, but still gates compile — see "Implementation Note — LLM synthesis"); entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] Audit `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events (node_start/node_complete/node_error/s1_denied) NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `KaonaviWorkflowGraph` (`BaseGraph`) via `KaonaviWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `KaonaviWorkflowGraphNode._parent_config()` reads
  `config/config.yaml` and forwards the validated `kaonavi` section under
  `config["configurable"]` to the subgraph.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no platform-internal SDK import anywhere
- [x] `src/services/kaonavi_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Framework base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat; the 5 domain steps live in the inner graph |
| LLM dependency | deterministic-only | LLM-enhanced with deterministic fallback | LLM-enhanced with fallback | richer NL understanding for intent + free-text fields; template still runs/tests without a live LLM since the heuristic is the fallback, not a stub; employee_id resolution stays LLM-excluded unconditionally; see "Implementation Note — LLM synthesis" |
| Kaonavi client | live `requests` call | injectable transport + documented stub default | injectable + stub default | no live network in the bundled build; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + config forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | nodes are no-arg by contract (ctor args TypeError at graph build); `config/config.yaml` stays the single runtime-config source |
| Write target | infer employee code from NL freely | caller-supplied/explicit code only; unresolved left empty | explicit only | never write to the wrong employee record; unresolved code -> status=error, not invented |
| Default intent | create_profile | lookup_profile | lookup_profile | low-confidence classification must never default to a write |
