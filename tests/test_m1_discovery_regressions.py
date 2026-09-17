"""M1 regression tests: trustworthy discovery.

The audit finding (H2): the gateway directory path could materialize an agent
that had never been cryptographically verified. Three separate places did it:

    target_resolver.py  agent_id path    -> synthesized a card from directory
                                           metadata and cached it
    target_resolver.py  handle path      -> cached raw directory JSON as a
                                           "card" when no card was present
    target_resolver.py  name-search path -> same
    routes/discovery.py directory_search -> set verified=True on a public_key
                                           FINGERPRINT match alone, with no
                                           signature check

A directory entry is a hint, never a trust source. These tests pin the
resulting invariant: no card is cached or surfaced as discovered unless its
Ed25519 signature, agent_id<->public_key consistency and time window all
verify.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.a2a import signing
from app.a2a.cards import build_card
from app.a2a.discovery import DiscoveryService
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.models import TrustedAgentCard
from app.a2a.repository import TrustedAgentCardRepository
from app.identity import crypto
from app.identity.service import IdentityService, PublicIdentity
from app.orchestration.schemas import TargetResolutionStatus
from app.orchestration.target_resolver import TargetResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class Signer:
    """Real Ed25519 signer implementing the IdentityService surface."""

    __test__ = False

    def __init__(self, private_key) -> None:
        self._private_key = private_key

    def get_public_identity(self) -> PublicIdentity:
        raw = crypto.public_key_bytes(self._private_key.public_key())
        return PublicIdentity(
            agent_id=crypto.agent_id_from_public_key(raw),
            public_key=__import__("base64").b64encode(raw).decode("ascii"),
            key_algorithm=crypto.KEY_ALGORITHM,
            fingerprint=crypto.fingerprint_from_public_key(raw),
        )

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self._private_key, data)


def _identity(tag: str = "") -> dict:
    priv, pub = crypto.generate_keypair()
    raw = crypto.public_key_bytes(pub)
    return {
        "priv": priv,
        "raw": raw,
        "key_b64": __import__("base64").b64encode(raw).decode("ascii"),
        "agent_id": crypto.agent_id_from_public_key(raw),
        "signer": Signer(priv),
        "display_name": f"Peer {tag or uuid.uuid4().hex[:6]}",
    }


async def _signed_card(ident: dict, *, ttl: int = 3600, endpoint: str = "https://peer.example/a2a") -> dict:
    unsigned = build_card(
        agent_id=ident["agent_id"],
        public_key=ident["key_b64"],
        display_name=ident["display_name"],
        endpoint=endpoint,
        ttl_seconds=ttl,
    )
    return await signing.sign_card(ident["signer"], unsigned)


def _resolver_with_gateway(ident_payload: dict, session_factory) -> TargetResolver:
    """TargetResolver whose gateway lookup returns ident_payload.

    A REAL session factory is required: TargetResolver builds a session to load
    the trusted-agent list even when the gateway path is what we are testing.
    """
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(return_value=ident_payload)
    client.get = AsyncMock(return_value=response)
    return TargetResolver(
        session_factory=session_factory,
        gateway_url="https://gateway.example.com",
        http_client=client,
    )


# ---------------------------------------------------------------------------
# The bypass is gone: directory metadata cannot create an agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_directory_entry_without_card_is_not_discovered(db_session_factory):
    """A directory entry with NO card must not become a discovered agent.

    Pre-fix: an unsigned card was synthesized from the directory's own
    metadata and cached, so a compromised relay could inject agents.
    """
    ident = _identity("noCard")
    resolver = _resolver_with_gateway(
        {
            # Exactly what a directory returns for an agent with no card.
            "agent_id": ident["agent_id"],
            "public_key": ident["key_b64"],
            "display_name": ident["display_name"],
            "handle": "@noc",
            "is_online": True,
        },
        db_session_factory,
    )

    result = await resolver.resolve(uuid.uuid4(), ident["agent_id"])

    assert result.status is TargetResolutionStatus.UNKNOWN_AGENT
    assert result.agent_id is None
    assert result.card is None
    # Nothing was cached.
    assert resolver.get_discovered_card(ident["agent_id"]) is None


@pytest.mark.asyncio
async def test_directory_entry_with_bad_signature_is_not_discovered(db_session_factory):
    """A card whose signature does not verify must be rejected."""
    ident = _identity("badSig")
    card = await _signed_card(ident)
    card["signature"] = __import__("base64").b64encode(b"\x00" * 64).decode("ascii")

    resolver = _resolver_with_gateway(
        {"agent_id": ident["agent_id"], "agent_card": card},
        db_session_factory,
    )
    result = await resolver.resolve(uuid.uuid4(), ident["agent_id"])

    assert result.status is TargetResolutionStatus.UNKNOWN_AGENT
    assert resolver.get_discovered_card(ident["agent_id"]) is None


@pytest.mark.asyncio
async def test_directory_entry_with_mismatched_key_is_not_discovered(db_session_factory):
    """A card claiming an agent_id that does not match its public_key fails."""
    ident = _identity("mismatch")
    other = _identity("other")
    card = await _signed_card(ident)
    # Swap in a different key: agent_id no longer matches public_key.
    card["public_key"] = other["key_b64"]

    resolver = _resolver_with_gateway(
        {"agent_id": ident["agent_id"], "agent_card": card},
        db_session_factory,
    )
    result = await resolver.resolve(uuid.uuid4(), ident["agent_id"])

    assert result.status is TargetResolutionStatus.UNKNOWN_AGENT
    assert resolver.get_discovered_card(ident["agent_id"]) is None


@pytest.mark.asyncio
async def test_directory_entry_with_expired_card_is_not_discovered(db_session_factory):
    """An expired card must be rejected."""
    ident = _identity("expired")
    card = await _signed_card(ident, ttl=-60)  # expired one minute ago

    resolver = _resolver_with_gateway(
        {"agent_id": ident["agent_id"], "agent_card": card},
        db_session_factory,
    )
    result = await resolver.resolve(uuid.uuid4(), ident["agent_id"])

    assert result.status is TargetResolutionStatus.UNKNOWN_AGENT


@pytest.mark.asyncio
async def test_valid_signed_card_is_discovered(db_session_factory):
    """The positive path still works: a properly signed card is discovered."""
    ident = _identity("good")
    card = await _signed_card(ident)

    resolver = _resolver_with_gateway(
        {"agent_id": ident["agent_id"], "agent_card": card},
        db_session_factory,
    )
    result = await resolver.resolve(uuid.uuid4(), ident["agent_id"])

    assert result.status is TargetResolutionStatus.DISCOVERED_AGENT
    assert result.agent_id == ident["agent_id"]
    assert result.card is not None
    assert resolver.get_discovered_card(ident["agent_id"]) is not None


@pytest.mark.asyncio
async def test_handle_lookup_without_card_is_not_discovered(db_session_factory):
    """The @handle path had the same bypass; it must be closed too."""
    ident = _identity("handle")
    resolver = _resolver_with_gateway(
        {
            "agent_id": ident["agent_id"],
            "public_key": ident["key_b64"],
            "display_name": ident["display_name"],
            "handle": "somehandle",
        },
        db_session_factory,
    )

    result = await resolver.resolve(uuid.uuid4(), "@somehandle")

    assert result.status is TargetResolutionStatus.UNKNOWN_AGENT
    assert resolver.get_discovered_card(ident["agent_id"]) is None


@pytest.mark.asyncio
async def test_name_search_without_card_is_not_discovered(db_session_factory):
    """The display-name search path had the same bypass."""
    ident = _identity("search")
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(
        return_value={
            "agents": [
                {
                    "agent_id": ident["agent_id"],
                    "public_key": ident["key_b64"],
                    "display_name": "Rahul Sharma",
                }
            ]
        }
    )
    client.get = AsyncMock(return_value=response)
    resolver = TargetResolver(
        session_factory=db_session_factory,
        gateway_url="https://gateway.example.com",
        http_client=client,
    )

    result = await resolver.resolve(uuid.uuid4(), "Rahul Sharma")

    assert result.status is TargetResolutionStatus.UNKNOWN_AGENT
    assert resolver.get_discovered_card(ident["agent_id"]) is None


def test_resolver_never_caches_unverified_cards():
    """Structural guard: every cache write follows a successful verification."""
    src = inspect.getsource(TargetResolver)
    # No assignment of raw directory data into the cache.
    assert "_discovered_cards[agent_id] = agent_data" not in src
    assert "_discovered_cards[agent_id] = c" not in src
    # And no tamper with the discovery service's methods to bypass verification.
    assert "verified_card = agent_data" not in src


# ---------------------------------------------------------------------------
# routes/discovery.py: "verified" means signature-verified, nothing less
# ---------------------------------------------------------------------------


def test_directory_search_does_not_accept_fingerprint_as_verification():
    """A fingerprint match is not verification (H2)."""
    from app.api.routes import discovery as discovery_module

    src = inspect.getsource(discovery_module)
    assert 'signing.agent_id_matches_key(agent_id, r["public_key"])' not in src
    assert "verification_error" in src


# ---------------------------------------------------------------------------
# DiscoveryService is the single verified writer
# ---------------------------------------------------------------------------


def _discovery_service(session_factory=None) -> DiscoveryService:
    ident_service = MagicMock(spec=IdentityService)
    a2a = MagicMock()
    a2a.register_trusted_agent = AsyncMock(
        return_value=MagicMock(agent_id="x", display_name="d", endpoint="e", status="active")
    )
    return DiscoveryService(
        identity_service=ident_service,
        a2a_service=a2a,
        allow_local_endpoints=True,
        session_factory=session_factory,
    )


@pytest.mark.asyncio
async def test_register_verified_card_rejects_unsigned_card():
    """The single writer must refuse a card with no signature."""
    service = _discovery_service()
    ident = _identity("unsigned")
    unsigned = build_card(
        agent_id=ident["agent_id"],
        public_key=ident["key_b64"],
        display_name=ident["display_name"],
        endpoint="https://peer.example/a2a",
    )
    with pytest.raises(A2AError) as exc:
        await service.register_verified_card(uuid.uuid4(), unsigned)
    assert exc.value.code in {
        A2AErrorCode.CARD_SIGNATURE_INVALID,
        A2AErrorCode.INVALID_CARD,
    }


@pytest.mark.asyncio
async def test_register_verified_card_rejects_wrong_expected_agent_id():
    """The single writer honours expected_agent_id pinning."""
    service = _discovery_service()
    ident = _identity("pin")
    card = await _signed_card(ident)
    with pytest.raises(A2AError):
        await service.register_verified_card(
            uuid.uuid4(), card, expected_agent_id="nexus:ed25519:" + "f" * 32
        )


@pytest.mark.asyncio
async def test_register_verified_card_accepts_valid_card():
    service = _discovery_service()
    ident = _identity("valid")
    card = await _signed_card(ident)
    agent, verified = await service.register_verified_card(uuid.uuid4(), card)
    assert verified["agent_id"] == ident["agent_id"]


@pytest.mark.asyncio
async def test_list_known_cards_is_implemented_not_a_silent_noop():
    """``list_known_cards`` must exist and be callable (it previously was not).

    ``hasattr`` guard in TargetResolver meant a missing method silently yielded
    no results, so verified cards could never be reused for discovery.
    """
    service = _discovery_service()
    assert hasattr(service, "list_known_cards")
    # With no session factory it degrades to empty rather than raising.
    assert await service.list_known_cards(uuid.uuid4()) == []


# ---------------------------------------------------------------------------
# Verified-card persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verified_card_is_cached_and_retrievable(
    db_session_factory, db_owner_id
):
    """A verified card is persisted and readable without re-fetching."""
    service = _discovery_service(session_factory=db_session_factory)
    ident = _identity("cache")
    card = await _signed_card(ident)

    await service.register_verified_card(db_owner_id, card)

    cached = await service.get_known_card(db_owner_id, ident["agent_id"])
    assert cached is not None
    assert cached["agent_id"] == ident["agent_id"]
    assert cached["signature"] == card["signature"]

    listed = await service.list_known_cards(db_owner_id)
    assert [c["agent_id"] for c in listed] == [ident["agent_id"]]


@pytest.mark.asyncio
async def test_expired_cached_card_is_excluded_from_lookups(
    db_session_factory, db_owner_id
):
    """Expired cached cards must not be returned by list/get."""
    service = _discovery_service(session_factory=db_session_factory)
    ident = _identity("stalecache")
    # Card valid for 1 second; we then backdate card_expires_at in the DB to
    # simulate staleness without waiting.
    card = await _signed_card(ident, ttl=3600)
    await service.register_verified_card(db_owner_id, card)

    from sqlalchemy import update

    async with db_session_factory() as session:
        await session.execute(
            update(TrustedAgentCard)
            .where(TrustedAgentCard.agent_id == ident["agent_id"])
            .values(
                card_expires_at=datetime.now(timezone.utc) - timedelta(hours=1)
            )
        )
        await session.commit()

    assert await service.list_known_cards(db_owner_id) == []


@pytest.mark.asyncio
async def test_card_cache_upsert_updates_rather_than_duplicates(
    db_session_factory, db_owner_id
):
    """Re-registering the same agent refreshes its card row."""
    service = _discovery_service(session_factory=db_session_factory)
    ident = _identity("upsert")
    first = await _signed_card(ident, endpoint="https://one.example/a2a")
    second = await _signed_card(ident, endpoint="https://two.example/a2a")

    await service.register_verified_card(db_owner_id, first)
    await service.register_verified_card(db_owner_id, second)

    async with db_session_factory() as session:
        row = await TrustedAgentCardRepository().get(
            session, db_owner_id, ident["agent_id"]
        )
    assert row is not None
    assert row.card["endpoint"] == "https://two.example/a2a"
