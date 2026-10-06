"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_profile / create_profile
/ update_profile. Primary path is an LLM classification call (Azure OpenAI,
built fresh per invocation from ctx.secrets - never cached on self); any
failure (missing secret, API error, malformed/wrong-shape response) falls
back to the original deterministic keyword heuristic, so the template stays
testable and runnable without a live LLM (see the design document's
implementation note). Low-confidence / unknown / unavailable-LLM all fall
back to the read-only "lookup_profile" default with a note - never a write.
"""

from typing import Any, cast

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.audit_logger import emit_trace_event
from shared.utils.llm_json import extract_json_object

_VALID_INTENTS = ("lookup_profile", "create_profile", "update_profile")

_SYSTEM_PROMPT = """\
You classify an HR-profile request into exactly one intent.

Valid intents:
- lookup_profile: read-only request to find, show, or summarize an employee profile
- create_profile: request to register/onboard a new employee
- update_profile: request to change/correct/amend an existing employee's record

If the request asks to both update and then show a record, classify it as
update_profile (writes take priority over reads). If the intent is unclear,
respond with "lookup_profile" (the safe, read-only default).

Respond with a JSON object of exactly one key: {"intent": "<one of the three values above>"}.
Do not include any other text.
"""

# Deterministic keyword signals (checked in priority order, writes first so a
# "update the record then show it" style request classifies as the write).
_KEYWORDS = (
    (
        "update_profile",
        (
            "update",
            "change",
            "edit",
            "correct",
            "amend",
            "revise",
            "transfer",
            "promote",
            "promotion",
            "set the",
            "set her",
            "set his",
            "更新",
            "変更",
            "修正",
            "異動",
            "昇進",
        ),
    ),
    (
        "create_profile",
        (
            "create",
            "register",
            "onboard",
            "new employee",
            "new hire",
            "add a profile",
            "add an employee",
            "add a new",
            "登録",
            "追加",
            "新規",
            "入社",
            "作成",
        ),
    ),
    (
        "lookup_profile",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "what is",
            "who is",
            "summarize",
            "profile for",
            "profile of",
            "照会",
            "検索",
            "参照",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a Kaonavi HR-profile operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any = None) -> None:
        # `llm` is a unit-test seam only (FakeLLM double) - production wiring
        # (register_nodes()) never passes one; a real client is built fresh
        # per invocation in _classify_via_llm(), never cached on self.
        super().__init__()
        self._llm = llm

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_llm(text, state)
        via_llm = intent is not None
        if intent is None:
            intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_profile (read-only)"]
            intent = "lookup_profile"

        # Audit the classification decision - intent label + source, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note), "via_llm": via_llm},
            state,
        )

        result = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- LLM path (enhances, never the sole path) ------------------------------

    def _classify_via_llm(self, text: str, state: dict[str, Any]) -> "str | None":
        """Return a valid intent from the LLM, or None on any failure/invalid shape.

        Broad by design: a missing/invalid secret, an LLM API failure, a
        malformed response, or an out-of-enum value must all degrade the same
        way - fall through to the deterministic keyword heuristic, never a
        node error.
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
            intent = parsed.get("intent")
            if intent in _VALID_INTENTS:
                return cast(str, intent)
            return None
        except Exception:
            return None

    # -- deterministic fallback -------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
