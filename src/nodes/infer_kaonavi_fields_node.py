"""AgentCore Platform v1.0 - inner workflow Step 3: InferKaonaviFields.

Extracts the employee code, display name, and profile fields from the
(redacted) request and assembles a validated Kaonavi REST API v2 request body
for the classified intent.

Employee code resolution is 100% deterministic, LLM-untouched, in every
build: taken only from an explicit code in the text or the caller-supplied
employee_hint - an unresolved code is left empty rather than invented (risk
mitigation: never touch the wrong employee record; the executor surfaces the
miss as status=error). This invariant is deliberately out of scope for the
LLM enhancement below - a hallucinated employee code is the one failure mode
this node exists to prevent, so it is never a decision an LLM call gets to make.

Display name and free-text "Key: value" profile fields are LLM-enhanced: an
Azure OpenAI call (built fresh per invocation from ctx.secrets, never cached
on self) attempts to extract name/fields from prose that the deterministic
"Key: value" line parser misses (e.g. "her department is Sales, starting
Monday"); any failure (missing secret, API error, malformed/wrong-shape
response) falls back to the original deterministic parser unchanged.
"""

import re
from typing import Any, cast

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.audit_logger import emit_trace_event
from shared.utils.llm_json import extract_json_object

from src.schemas.state import to_json

# A Kaonavi member code: short alphanumeric identifier (no spaces).
_CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit code mention in the request text, EN or JA
# ("employee code 1001" / "member code: A123" / "社員番号 1001").
_CODE_IN_TEXT_RE = re.compile(
    r"(?:employee|member|staff)\s+(?:code|id|number)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:社員番号|社員コード|従業員番号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Quoted display name: named "Foo" / called "Foo". Curly quotes as \u escapes so
# the source stays pure ASCII (push-safe).
_NAME_QUOTED_RE = re.compile(r'(?:named|called|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" profile-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Structural bounds on the parsed field table: entry cap + per-value length
# cap, so a pathological request cannot balloon the assembled API payload.
_MAX_FIELDS = 50
_MAX_VALUE_LEN = 500

# Keys that are the code/name themselves, not custom profile fields.
_CODE_KEYS = ("code", "employee code", "member code", "id")
_NAME_KEYS = ("name", "employee name", "member name")

_SYSTEM_PROMPT = """\
You extract HR-profile fields from a request about a Kaonavi employee record.

Respond with a JSON object of exactly two keys:
- "profile_name": the employee's display name mentioned in the text, or null if none
- "fields": an object of additional profile field name -> value pairs mentioned in
  the text (e.g. department, title, start date) — {} if none

Do NOT include an employee code, employee ID, or member code in either field —
that is resolved separately and any code you return will be ignored. Do not
invent values not present in the text. Respond with only the JSON object.
"""

# Bound the LLM-returned field-name length the same way a heuristic-parsed
# "Key: value" line is bounded (_KV_RE's own key-length cap), so an LLM
# response cannot smuggle an oversized field name into the payload.
_MAX_FIELD_NAME_LEN = 41


class InferKaonaviFieldsNode(FunctionNode):
    """Extract entities and assemble the Kaonavi REST API v2 request body."""

    # Inner domain node - derives fields from already-validated text; the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any = None) -> None:
        # `llm` is a unit-test seam only (FakeLLM double) - production wiring
        # (register_nodes()) never passes one; a real client is built fresh
        # per invocation in _extract_via_llm(), never cached on self.
        super().__init__()
        self._llm = llm

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_profile") or "lookup_profile"
        employee_hint = state.get("employee_hint", "") or ""

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferKaonaviFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        # employee_id resolution is deterministic-only, always - the LLM
        # enhancement below never participates in this decision (see module
        # docstring: a hallucinated code is the exact failure this guards).
        employee_id = self._resolve_code(text, employee_hint, fields)
        profile_name = self._resolve_name(text, fields)

        llm_extra = self._extract_via_llm(text, state)
        via_llm = llm_extra is not None
        if llm_extra is not None:
            if not profile_name and llm_extra.get("profile_name"):
                profile_name = llm_extra["profile_name"]
            fields = self._merge_fields(fields, llm_extra.get("fields", {}))

        if intent == "create_profile":
            payload = self._build_member_data(employee_id, profile_name, fields)
        elif intent == "update_profile":
            payload = self._build_member_data(employee_id, profile_name, fields)
        else:  # lookup_profile (read-only default)
            payload = {"member_code": employee_id}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_kaonavi_fields_complete",
            {"intent": intent, "has_employee_id": bool(employee_id), "n_fields": len(fields), "via_llm": via_llm},
            state,
        )

        return {
            "employee_id": employee_id,
            "profile_name": profile_name,
            "kaonavi_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- LLM path (enhances name/fields only, never employee_id) --------------

    def _extract_via_llm(self, text: str, state: dict[str, Any]) -> "dict[str, Any] | None":
        """Return {"profile_name": str|None, "fields": dict} from the LLM, or None on failure.

        Broad by design: a missing/invalid secret, an LLM API failure, a
        malformed response, or a wrong-shape parse must all degrade the same
        way - fall through to the deterministic-only result, never a node
        error. Never asked about, and never trusted for, the employee code.
        """
        try:
            if self._llm is not None:
                raw = cast(str, self._llm.generate(system_prompt=_SYSTEM_PROMPT, user_prompt=text))
            else:
                ctx = InvocationContext.from_state(state)
                llm = AzureOpenAIClient(
                    {
                        "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                        "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                        "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
                    }
                )
                response = llm.complete(
                    [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ]
                )
                raw = cast(str, response.get("content", ""))
            parsed = extract_json_object(raw)
            if not parsed:
                return None

            profile_name = parsed.get("profile_name")
            if not isinstance(profile_name, str) or not profile_name.strip():
                profile_name = None
            else:
                profile_name = profile_name.strip()[:100]

            raw_fields = parsed.get("fields")
            fields: dict[str, str] = {}
            if isinstance(raw_fields, dict):
                for key, value in raw_fields.items():
                    if not isinstance(key, str) or not isinstance(value, (str, int, float)):
                        continue
                    key = key.strip()[:_MAX_FIELD_NAME_LEN]
                    if not key or key.lower() in _CODE_KEYS or key.lower() in _NAME_KEYS:
                        continue  # never let the LLM re-introduce a code/name as a "field"
                    fields[key] = str(value).strip()[:_MAX_VALUE_LEN]
                    if len(fields) >= _MAX_FIELDS:
                        break

            return {"profile_name": profile_name, "fields": fields}
        except Exception:
            return None

    def _merge_fields(
        self, heuristic_fields: "list[tuple[str, str]]", llm_fields: "dict[str, str]"
    ) -> "list[tuple[str, str]]":
        """Add LLM-found fields the heuristic parser missed; never overwrite an existing key."""
        if not llm_fields:
            return heuristic_fields
        existing_keys = {key.strip().lower() for key, _ in heuristic_fields}
        merged = list(heuristic_fields)
        for key, value in llm_fields.items():
            if key.strip().lower() in existing_keys:
                continue  # heuristic already found this field - keep its value
            if len(merged) >= _MAX_FIELDS:
                break
            merged.append((key, value))
        return merged

    # -- extraction -----------------------------------------------------------

    def _resolve_code(self, text: str, employee_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit code only: text mention > code-shaped hint > 'Code:' field. Never invented."""
        m = _CODE_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = employee_hint.strip()
        if hint and _CODE_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS and _CODE_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_name(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _NAME_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:100]
        for key, value in fields:
            if key.strip().lower() in _NAME_KEYS:
                return value.strip()[:100]
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] profile fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            if len(fields) >= _MAX_FIELDS:
                break
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()[:_MAX_VALUE_LEN]))
        return fields

    # -- payload assembly (Kaonavi REST API v2 member_data shape) --------------

    def _build_member_data(
        self, employee_id: str, profile_name: str, fields: "list[tuple[str, str]]"
    ) -> dict[str, Any]:
        record: dict[str, Any] = {"code": employee_id}
        if profile_name:
            record["name"] = profile_name
        custom_fields = []
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS + _NAME_KEYS:
                continue
            custom_fields.append({"name": key, "values": [value]})
        if custom_fields:
            record["custom_fields"] = custom_fields
        return {"member_data": [record]}
