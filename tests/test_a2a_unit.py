"""A2A unit tests: envelope validation, canonical serialization, signing,
replay windows, rate limiting, SSRF validation, disclosure shaping.

Pure in-memory; no database. Uses throwaway Ed25519 keypairs.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from app.a2a import signing
from app.a2a.disclosure import build_disclosure
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.rate_limit import SlidingWindowRateLimiter
from app.a2a.replay import validate_time_window
from app.a2a.schemas import (
    A2AEnvelope,
    new_message_id,
    new_task_id,
    parse_iso,
    utc_now_iso,
    utc_iso_in,
)
from app.a2a.transport import validate_endpoint
from app.identity import crypto
from app.policy.models import DisclosureScope


def _make_keypair():
    private_key, public_key = crypto.generate_keypair()
    public_b64 = base64.b64encode(crypto.public_key_bytes(public_key)).decode()
    agent_id = crypto.agent_id_from_public_key(
        crypto.public_key_bytes(public_key)
    )
    return private_key, public_b64, agent_id


def _envelope(
    *,
    sender: str,
    recipient: str,
    purpose: str = "scheduling",
    payload: dict | None = None,
    timestamp: str | None = None,
    expires_at: str | None = None,
) -> A2AEnvelope:
    return A2AEnvelope(
        message_id=new_message_id(),
        task_id=new_task_id(),
        sender=sender,
        recipient=recipient,
        timestamp=timestamp or utc_now_iso(),
        expires_at=expires_at or utc_iso_in(60),
        message_type="request",
        purpose=purpose,
        payload=payload
        or {"action": "disclose_information", "data_category": "availability"},
    )


# --- Envelope validation -------------------------------------------------------


def test_envelope_rejects_bad_agent_id() -> None:
    _, _, agent_id = _make_keypair()
    with pytest.raises(Exception):
        _envelope(sender="not-an-agent-id", recipient=agent_id)


def test_envelope_rejects_bad_timestamp() -> None:
    _, _, agent_id = _make_keypair()
    with pytest.raises(Exception):
        _envelope(sender=agent_id, recipient=agent_id, timestamp="2026-09-14 10:00:00")


def test_envelope_rejects_floats_in_payload() -> None:
    _, _, agent_id = _make_keypair()
    with pytest.raises(Exception):
        _envelope(
            sender=agent_id,
            recipient=agent_id,
            payload={"price": 10.5},
        )


def test_envelope_rejects_unknown_fields() -> None:
    _, _, agent_id = _make_keypair()
    data = _envelope(sender=agent_id, recipient=agent_id).model_dump()
    data["extra_field"] = "nope"
    with pytest.raises(Exception):
        A2AEnvelope.model_validate(data)


def test_parse_iso_roundtrip() -> None:
    value = "2026-09-14T10:00:00Z"
    assert parse_iso(value) == datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)


# --- Canonical serialization + signing -------------------------------------------


def test_canonical_bytes_are_deterministic() -> None:
    _, _, agent_id = _make_keypair()
    first = _envelope(sender=agent_id, recipient=agent_id)
    second = A2AEnvelope.model_validate(first.model_dump())
    assert first.canonical_bytes() == second.canonical_bytes()


def test_canonical_bytes_exclude_signature() -> None:
    _, _, agent_id = _make_keypair()
    envelope = _envelope(sender=agent_id, recipient=agent_id)
    signed = envelope.model_copy(update={"signature": "sig-value"})
    assert b"signature" not in signed.canonical_bytes()
    assert b"sig-value" not in signed.canonical_bytes()


def test_payload_change_changes_canonical_bytes() -> None:
    _, _, agent_id = _make_keypair()
    base = _envelope(sender=agent_id, recipient=agent_id)
    modified = _envelope(
        sender=agent_id,
        recipient=agent_id,
        payload={"action": "disclose_information", "data_category": "location"},
    )
    assert base.canonical_bytes() != modified.canonical_bytes()


def test_sign_and_verify_roundtrip() -> None:
    """Sign with a stub identity service; verify with the public key."""
    import asyncio

    private_key, pub_b64, aid = _make_keypair()
    envelope = _envelope(sender=aid, recipient="nexus:ed25519:" + "a" * 32)

    class StubIdentity:
        async def sign(self, data: bytes) -> bytes:
            return crypto.sign_bytes(private_key, data)

    signed = asyncio.run(signing.sign_envelope(StubIdentity(), envelope))  # type: ignore[arg-type]
    assert signed.signature is not None
    assert signing.verify_envelope_signature(signed, pub_b64) is True


def test_tampered_payload_fails_verification() -> None:
    import asyncio

    private_key, pub_b64, aid = _make_keypair()
    envelope = _envelope(sender=aid, recipient="nexus:ed25519:" + "a" * 32)

    class StubIdentity:
        async def sign(self, data: bytes) -> bytes:
            return crypto.sign_bytes(private_key, data)

    signed = asyncio.run(signing.sign_envelope(StubIdentity(), envelope))  # type: ignore[arg-type]
    tampered = signed.model_copy(
        update={
            "payload": {
                "action": "disclose_information",
                "data_category": "financial",
            }
        }
    )
    assert signing.verify_envelope_signature(tampered, pub_b64) is False


def test_wrong_key_fails_verification() -> None:
    import asyncio

    private_key, _, aid = _make_keypair()
    _, other_pub, _ = _make_keypair()
    envelope = _envelope(sender=aid, recipient="nexus:ed25519:" + "a" * 32)

    class StubIdentity:
        async def sign(self, data: bytes) -> bytes:
            return crypto.sign_bytes(private_key, data)

    signed = asyncio.run(signing.sign_envelope(StubIdentity(), envelope))  # type: ignore[arg-type]
    assert signing.verify_envelope_signature(signed, other_pub) is False


def test_agent_id_matches_key() -> None:
    _, pub_b64, agent_id = _make_keypair()
    assert signing.agent_id_matches_key(agent_id, pub_b64) is True
    assert signing.agent_id_matches_key("nexus:ed25519:" + "0" * 32, pub_b64) is False
    assert signing.agent_id_matches_key(agent_id, "not-base64!!!") is False


# --- Replay / time windows ---------------------------------------------------------


def test_expired_message_rejected() -> None:
    _, _, agent_id = _make_keypair()
    envelope = _envelope(
        sender=agent_id,
        recipient=agent_id,
        expires_at=utc_now_iso(),
    )
    with pytest.raises(A2AError) as excinfo:
        validate_time_window(envelope, max_clock_skew_seconds=30)
    assert excinfo.value.code is A2AErrorCode.EXPIRED


def test_future_message_beyond_skew_rejected() -> None:
    _, _, agent_id = _make_keypair()
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    envelope = _envelope(
        sender=agent_id,
        recipient=agent_id,
        timestamp=future.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    with pytest.raises(A2AError) as excinfo:
        validate_time_window(envelope, max_clock_skew_seconds=30)
    assert excinfo.value.code is A2AErrorCode.CLOCK_SKEW


def test_future_message_within_skew_accepted() -> None:
    _, _, agent_id = _make_keypair()
    near_future = datetime.now(timezone.utc) + timedelta(seconds=5)
    envelope = _envelope(
        sender=agent_id,
        recipient=agent_id,
        timestamp=near_future.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    validate_time_window(envelope, max_clock_skew_seconds=30)  # no raise


def test_expires_before_timestamp_rejected() -> None:
    _, _, agent_id = _make_keypair()
    now = datetime.now(timezone.utc)
    # Timestamp within the skew window, but expires_at earlier than it.
    envelope = _envelope(
        sender=agent_id,
        recipient=agent_id,
        timestamp=(now + timedelta(seconds=200)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    with pytest.raises(A2AError) as excinfo:
        validate_time_window(envelope, max_clock_skew_seconds=300)
    assert excinfo.value.code is A2AErrorCode.INVALID_ENVELOPE


# --- Rate limiting -------------------------------------------------------------------


def test_rate_limiter_allows_then_blocks() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)
    assert all(limiter.check("agent-x") for _ in range(3))
    assert limiter.check("agent-x") is False
    # Independent keys are unaffected.
    assert limiter.check("agent-y") is True


# --- SSRF / endpoint validation ----------------------------------------------------------


def test_endpoint_schemes_restricted() -> None:
    for bad in [
        "file:///etc/passwd",
        "ftp://host/messages",
        "javascript:alert(1)",
        "data:text/html,hi",
        "gopher://host",
    ]:
        with pytest.raises(A2AError) as excinfo:
            validate_endpoint(bad, allow_local=True)
        assert excinfo.value.code is A2AErrorCode.INVALID_ENDPOINT


def test_endpoint_local_rejected_when_not_allowed() -> None:
    for local in [
        "http://localhost:8000/a2a/messages",
        "http://127.0.0.1:8000/a2a/messages",
        "http://192.168.1.10/a2a/messages",
        "http://10.0.0.5/a2a/messages",
        "http://169.254.169.254/latest/meta-data",
    ]:
        with pytest.raises(A2AError):
            validate_endpoint(local, allow_local=False)


def test_endpoint_local_allowed_in_dev_mode() -> None:
    validate_endpoint(
        "http://127.0.0.1:8001/a2a/messages", allow_local=True
    )  # no raise


def test_endpoint_public_https_accepted() -> None:
    validate_endpoint(
        "https://agent.example.com/a2a/messages", allow_local=False
    )  # no raise


def test_endpoint_credentials_rejected() -> None:
    with pytest.raises(A2AError):
        validate_endpoint(
            "https://user:pass@agent.example.com/a2a/messages",
            allow_local=False,
        )


# --- Disclosure shaping ----------------------------------------------------------------------


def test_disclosure_none_returns_nothing() -> None:
    disclosure = build_disclosure(
        scope=DisclosureScope.NONE,
        data_category="availability",
        memories=["Boss prefers meetings after 6 PM."],
    )
    payload = disclosure.to_payload()
    assert "memories" not in payload
    assert "summary" not in payload
    assert "category_available" not in payload


def test_disclosure_category_only_acknowledges_category() -> None:
    disclosure = build_disclosure(
        scope=DisclosureScope.CATEGORY,
        data_category="availability",
        memories=["secret detail"],
    )
    payload = disclosure.to_payload()
    assert payload.get("category_available") is True
    assert "memories" not in payload
    assert "secret detail" not in str(payload)


def test_disclosure_summary_never_leaks_content() -> None:
    disclosure = build_disclosure(
        scope=DisclosureScope.SUMMARY,
        data_category="availability",
        memories=["Boss prefers meetings after 6 PM.", "Boss is in Goa."],
    )
    payload = disclosure.to_payload()
    assert payload["summary"] == ["2 memories regarding availability"]
    assert "6 PM" not in str(payload)
    assert "Goa" not in str(payload)


def test_disclosure_exact_is_bounded() -> None:
    memories = [f"memory {i} " + "x" * 600 for i in range(10)]
    disclosure = build_disclosure(
        scope=DisclosureScope.EXACT,
        data_category="availability",
        memories=memories,
    )
    payload = disclosure.to_payload()
    assert len(payload["memories"]) == 3  # MAX_EXACT_ITEMS
    assert all(len(m) <= 500 for m in payload["memories"])  # MAX_ITEM_CHARS
