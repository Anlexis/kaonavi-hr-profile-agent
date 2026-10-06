"""AgentCore Platform v1.0 - Kaonavi REST API v2 client.

Service layer: a thin wrapper around the Kaonavi (HR-profile SaaS) REST API v2
member endpoints. Contains NO business logic, NO routing, and NO credentials -
the integration token is passed in per call by the node (which reads it via
ctx.secrets). This module imports no framework/SDK internals - pure stdlib
(import-isolation boundary).

v1 LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Kaonavi response shapes (a ``member_data`` list for lookups; the
    task/receipt shape with a synthetic ``member_code`` echo for create/update,
    derived from the request) so the pipeline is runnable and testable without
    a live Kaonavi tenant or the ``requests`` package - it does NOT perform a
    live Kaonavi call. The rule it follows: never fake a live call; document
    the limitation.

    To perform real Kaonavi calls, inject live transports (requests-based
    ``post`` / ``patch`` / ``get``) at construction time; the method contracts
    and payload shapes are already Kaonavi REST API v2 exact, so no
    business-logic change is needed to go live. A live transport also requires
    a real integration token (see CallKaonaviApiNode - the stub runs without
    one because no request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.kaonavi.jp/api/v2.0"


class KaonaviApiError(Exception):
    """Raised when the Kaonavi REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Kaonavi API error {status_code}: {message}")


class KaonaviClient:
    """Kaonavi REST API v2 member-data client.

    Args:
        base_url: Kaonavi API base URL (default https://api.kaonavi.jp/api/v2.0).
        post/patch/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE v1 stub is used
            (see the module docstring - it returns the documented shape without
            a live Kaonavi call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
        patch: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post
        self._patch = patch
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._post is None and self._patch is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> dict[str, str]:
        """Build the Kaonavi REST API v2 auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Kaonavi-Token": api_token,
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free v1 stub - returns the documented Kaonavi shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the v1
        limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        if json_body.get("_kaonavi_op") == "lookup":
            code = str(json_body.get("member_code", "")) or f"m-{digest[:8]}"
            # Documented GET /members shape: {"updated_at": ..., "member_data": [...]}
            return 200, {
                "updated_at": "1970-01-01 00:00:00",
                "member_data": [
                    {
                        "code": code,
                        "name": f"Member {code}",
                        "sheets": [],
                        "custom_fields": [],
                    }
                ],
                "_stub": True,  # marks the network-free v1 stub response
            }
        # POST /members (create) / PATCH /members (update) - documented
        # task-receipt shape, plus a synthetic member_code echo so the caller
        # can reference the affected record without a follow-up lookup.
        member_data = json_body.get("member_data") or [{}]
        first = member_data[0] if isinstance(member_data, list) and member_data else {}
        code = str(first.get("code", "")) or f"m-{digest[:8]}"
        return 200, {
            "task_id": int(digest[:6], 16),
            "member_code": code,
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def find_member(self, member_code: str, api_token: str) -> "dict[str, Any]":
        """GET /members - look up a member's HR-profile record by member code.

        The live Kaonavi endpoint returns the full member list; a live ``get``
        transport adapter is expected to filter to ``member_code`` (the stub
        returns the matching record directly). Returns the parsed response dict
        (containing ``member_data``). Raises KaonaviApiError on non-2xx.
        """
        url = f"{self._base_url}/members"
        transport = self._resolve(self._get)
        status, body = transport(url, self._headers(api_token), {"_kaonavi_op": "lookup", "member_code": member_code})
        if not (200 <= status < 300):
            raise KaonaviApiError(status, _err_message(body))
        return body

    def create_member(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /members - register a new member (HR-profile record).

        ``payload`` is the documented ``{"member_data": [...]}`` request body.
        Returns the parsed response dict (task receipt). Raises KaonaviApiError
        on a non-2xx status.
        """
        url = f"{self._base_url}/members"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise KaonaviApiError(status, _err_message(body))
        return body

    def update_member(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """PATCH /members - partially update an existing member's record.

        ``payload`` is the documented ``{"member_data": [...]}`` request body
        (each entry keyed by ``code``). Returns the parsed response dict (task
        receipt). Raises KaonaviApiError on a non-2xx status.
        """
        url = f"{self._base_url}/members"
        transport = self._resolve(self._patch)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise KaonaviApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a Kaonavi error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
