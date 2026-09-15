"""Policy & consent API tests: every endpoint, validation, security."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

RAHUL = "nexus:ed25519:rahul00000000000000000000000000000000"


def _policy_payload(**overrides) -> dict:
    payload = {
        "requester_agent_id": RAHUL,
        "data_category": "availability",
        "action": "disclose_information",
        "purpose": "scheduling",
        "decision": "ALLOW",
        "disclosure_scope": "summary",
    }
    payload.update(overrides)
    return payload


def _evaluate_payload(**overrides) -> dict:
    payload = {
        "requester_agent_id": RAHUL,
        "data_category": "availability",
        "action": "disclose_information",
        "purpose": "scheduling",
    }
    payload.update(overrides)
    return payload


async def _create_policy(client, **overrides) -> dict:
    response = await client.post("/policy", json=_policy_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


# --- Policy CRUD ---------------------------------------------------------------


async def test_create_and_list_policies(db_policy_client) -> None:
    created = await _create_policy(db_policy_client)
    assert created["decision"] == "ALLOW"
    assert created["disclosure_scope"] == "summary"

    listed = await db_policy_client.get("/policy")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["policies"][0]["id"] == created["id"]


async def test_create_policy_with_expiry(db_policy_client) -> None:
    created = await _create_policy(
        db_policy_client, expires_at="2027-01-01T00:00:00Z", priority=5
    )
    assert created["priority"] == 5
    assert created["expires_at"].startswith("2027-01-01")


async def test_delete_policy(db_policy_client) -> None:
    created = await _create_policy(db_policy_client)
    response = await db_policy_client.delete(f"/policy/{created['id']}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True

    again = await db_policy_client.delete(f"/policy/{created['id']}")
    assert again.status_code == 404


async def test_delete_policy_invalid_uuid(db_policy_client) -> None:
    response = await db_policy_client.delete("/policy/not-a-uuid")
    assert response.status_code == 404


async def test_policy_validation_errors(db_policy_client) -> None:
    cases = [
        {"decision": "MAYBE"},                                   # bad decision
        {"disclosure_scope": "everything"},                      # bad scope
        {"data_category": "DROP TABLE owners"},                  # injection
        {"action": "read_memory'; --"},                          # injection
        {"purpose": ""},                                         # empty
        {"requester_agent_id": "Bad Agent!"},                    # bad chars
        {"priority": -5},                                        # negative priority
    ]
    for override in cases:
        response = await db_policy_client.post(
            "/policy", json=_policy_payload(**override)
        )
        assert response.status_code == 422, f"case {override} passed wrongly"


# --- Consent endpoints -------------------------------------------------------------


async def test_create_and_list_consents(db_policy_client) -> None:
    payload = {
        "requester_agent_id": RAHUL,
        "data_category": "location",
        "action": "disclose_information",
        "purpose": "travel",
        "decision": "ALLOW",
        "single_use": True,
    }
    created = await db_policy_client.post("/consent", json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["single_use"] is True
    assert body["used_at"] is None

    listed = await db_policy_client.get("/consent")
    assert listed.json()["total"] == 1


async def test_consent_rejects_wildcard_requester(db_policy_client) -> None:
    payload = {
        "requester_agent_id": "*",
        "data_category": "location",
        "action": "disclose_information",
        "purpose": "travel",
        "decision": "ALLOW",
    }
    response = await db_policy_client.post("/consent", json=payload)
    assert response.status_code == 422


async def test_consent_rejects_ask_decision(db_policy_client) -> None:
    payload = {
        "requester_agent_id": RAHUL,
        "data_category": "location",
        "action": "disclose_information",
        "purpose": "travel",
        "decision": "ASK",
    }
    response = await db_policy_client.post("/consent", json=payload)
    assert response.status_code == 422


async def test_delete_consent(db_policy_client) -> None:
    payload = {
        "requester_agent_id": RAHUL,
        "data_category": "location",
        "action": "disclose_information",
        "purpose": "travel",
        "decision": "DENY",
    }
    created = await db_policy_client.post("/consent", json=payload)
    consent_id = created.json()["id"]

    response = await db_policy_client.delete(f"/consent/{consent_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True

    missing = await db_policy_client.delete(f"/consent/{consent_id}")
    assert missing.status_code == 404


# --- Evaluate endpoint ----------------------------------------------------------------


async def test_evaluate_allow(db_policy_client) -> None:
    await _create_policy(db_policy_client)
    response = await db_policy_client.post(
        "/policy/evaluate", json=_evaluate_payload()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "ALLOW"
    assert body["requires_user_approval"] is False
    assert body["disclosure_scope"] == "summary"
    assert body["matched_policy_id"] is not None


async def test_evaluate_ask_default(db_policy_client) -> None:
    response = await db_policy_client.post(
        "/policy/evaluate",
        json=_evaluate_payload(requester_agent_id="nexus:ed25519:stranger"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "ASK"
    assert body["requires_user_approval"] is True
    assert body["disclosure_scope"] is None


async def test_evaluate_deny_sensitive_default(db_policy_client) -> None:
    response = await db_policy_client.post(
        "/policy/evaluate",
        json=_evaluate_payload(data_category="financial"),
    )
    assert response.json()["decision"] == "DENY"


async def test_evaluate_purpose_limitation(db_policy_client) -> None:
    await _create_policy(db_policy_client, purpose="scheduling")
    marketing = await db_policy_client.post(
        "/policy/evaluate", json=_evaluate_payload(purpose="marketing")
    )
    body = marketing.json()
    assert body["decision"] == "ASK"
    assert body["matched_policy_id"] is None


async def test_evaluate_rejects_wildcard_requester(db_policy_client) -> None:
    response = await db_policy_client.post(
        "/policy/evaluate", json=_evaluate_payload(requester_agent_id="*")
    )
    assert response.status_code == 422


async def test_evaluate_rejects_unknown_category_format(db_policy_client) -> None:
    response = await db_policy_client.post(
        "/policy/evaluate",
        json=_evaluate_payload(data_category="Not A Category!"),
    )
    assert response.status_code == 422


async def test_one_time_consent_flow_via_api(db_policy_client) -> None:
    """Evaluate -> ASK -> owner grants one-time consent -> ALLOW -> consumed."""
    request = _evaluate_payload(data_category="location", purpose="travel")

    first = await db_policy_client.post("/policy/evaluate", json=request)
    assert first.json()["decision"] == "ASK"

    await db_policy_client.post(
        "/consent",
        json={
            "requester_agent_id": RAHUL,
            "data_category": "location",
            "action": "disclose_information",
            "purpose": "travel",
            "decision": "ALLOW",
            "single_use": True,
        },
    )

    allowed = await db_policy_client.post("/policy/evaluate", json=request)
    body = allowed.json()
    assert body["decision"] == "ALLOW"
    assert body["matched_consent_id"] is not None

    reused = await db_policy_client.post("/policy/evaluate", json=request)
    assert reused.json()["decision"] == "ASK"


# --- Audit endpoint --------------------------------------------------------------------


async def test_audit_records_every_decision(db_policy_client) -> None:
    await _create_policy(db_policy_client)
    await db_policy_client.post("/policy/evaluate", json=_evaluate_payload())
    await db_policy_client.post(
        "/policy/evaluate",
        json=_evaluate_payload(requester_agent_id="nexus:ed25519:other"),
    )

    response = await db_policy_client.get("/policy/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    decisions = {e["decision"] for e in body["decisions"]}
    assert decisions == {"ALLOW", "ASK"}
    # Audit entries carry decision metadata, never disclosed content.
    for entry in body["decisions"]:
        assert "reason" in entry
        assert "created_at" in entry


async def test_policy_service_unavailable_returns_503(db_client) -> None:
    """db_client has NO policy service attached (plain db_app)."""
    response = await db_client.get("/policy")
    assert response.status_code == 503
