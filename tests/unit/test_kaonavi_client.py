# CMN-C2-273 - Unit tests: KaonaviClient service (Kaonavi REST API v2 shape)
# Adapted from an established tool-calling golden pattern (notion_client -> kaonavi_client).
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.kaonavi_client import KaonaviApiError, KaonaviClient


def test_find_member_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"updated_at": "2026-01-01 00:00:00", "member_data": [{"code": "1001", "name": "Member 1001"}]}

    client = KaonaviClient("https://kaonavi.example.test/api/v2.0/", get=get)
    resp = client.find_member("1001", "tok123")
    assert resp["member_data"][0]["code"] == "1001"
    assert captured["url"] == "https://kaonavi.example.test/api/v2.0/members"
    # Kaonavi REST API v2 auth: the per-call token travels in Kaonavi-Token.
    assert captured["headers"]["Kaonavi-Token"] == "tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["member_code"] == "1001"


def test_create_member_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"task_id": 42, "member_code": "9002"}

    client = KaonaviClient("https://kaonavi.example.test/api/v2.0", post=post)
    payload = {"member_data": [{"code": "9002", "name": "Alice"}]}
    resp = client.create_member(payload, "tok")
    assert resp["member_code"] == "9002"
    assert captured["url"] == "https://kaonavi.example.test/api/v2.0/members"
    assert captured["body"] == payload


def test_update_member_success_with_injected_patch():
    captured = {}

    def patch(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"task_id": 7, "member_code": "1001"}

    client = KaonaviClient("https://kaonavi.example.test/api/v2.0", patch=patch)
    payload = {"member_data": [{"code": "1001"}]}
    resp = client.update_member(payload, "tok")
    assert resp["member_code"] == "1001"
    assert captured["body"] == payload


def test_non_2xx_raises_kaonavi_api_error():
    def post(url, headers, body):
        return 400, {"errors": ["member_data is malformed"]}

    client = KaonaviClient("https://kaonavi.example.test/api/v2.0", post=post)
    with pytest.raises(KaonaviApiError) as exc:
        client.create_member({"member_data": [{}]}, "tok")
    assert exc.value.status_code == 400
    assert "member_data is malformed" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = KaonaviClient()
    assert client.uses_stub_transport is True
    resp = client.find_member("1001", "tok")
    assert resp.get("_stub") is True
    record = resp["member_data"][0]
    assert record["code"] == "1001"
    assert record["name"] == "Member 1001"


def test_default_stub_transport_create_echoes_member_code():
    client = KaonaviClient()
    resp = client.create_member({"member_data": [{"code": "9002", "name": "Alice"}]}, "tok")
    assert resp.get("_stub") is True
    assert resp["member_code"] == "9002"
    assert isinstance(resp["task_id"], int)


def test_injected_transport_disables_stub_flag():
    client = KaonaviClient(get=lambda url, headers, body: (200, {"member_data": []}))
    assert client.uses_stub_transport is False
