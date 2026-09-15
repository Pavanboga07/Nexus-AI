"""IdentityService and identity API tests (require PostgreSQL; skip if down).

Covers spec §26: persistence, restart behaviour, first-run, repeated
initialisation, encryption integration, public API safety, owner isolation.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest

from app.database.models import Owner
from app.identity import crypto
from app.identity.service import (
    IdentityCorruptionError,
    IdentityNotReadyError,
    IdentityService,
)

pytestmark = pytest.mark.asyncio

TEST_SECRET = "unit-test-identity-secret-DO-NOT-USE-IN-PROD"


def _make_service(db_session_factory, owner_id, secret=TEST_SECRET) -> IdentityService:
    return IdentityService(
        session_factory=db_session_factory,
        encryption_secret=secret,
        owner_id=owner_id,
    )


async def _make_owner(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as session:
        owner = Owner(name=f"owner-{uuid.uuid4().hex[:8]}")
        session.add(owner)
        await session.commit()
        return owner.id


# --- First-run / persistence / restart ---------------------------------------


async def test_first_run_creates_exactly_one_identity(db_session_factory) -> None:
    owner_id = await _make_owner(db_session_factory)
    service = _make_service(db_session_factory, owner_id)
    public = await service.initialize_identity()

    assert public.agent_id.startswith("nexus:ed25519:")
    assert public.key_algorithm == "Ed25519"

    # Repeated initialisation must NOT create a second identity.
    again = _make_service(db_session_factory, owner_id)
    second = await again.initialize_identity()
    assert second.agent_id == public.agent_id

    from sqlalchemy import func, select

    from app.identity.models import AgentIdentity

    async with db_session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(AgentIdentity)
        )
        assert count == 1


async def test_identity_survives_restart(db_session_factory) -> None:
    """New service instance = simulated process restart; identity unchanged."""
    owner_id = await _make_owner(db_session_factory)
    first = await _make_service(db_session_factory, owner_id).initialize_identity()

    restarted = _make_service(db_session_factory, owner_id)
    second = await restarted.initialize_identity()

    assert second.agent_id == first.agent_id
    assert second.public_key == first.public_key
    assert second.fingerprint == first.fingerprint


async def test_stored_private_key_is_encrypted_at_rest(db_session_factory) -> None:
    owner_id = await _make_owner(db_session_factory)
    service = _make_service(db_session_factory, owner_id)
    public = await service.initialize_identity()

    from sqlalchemy import select

    from app.identity.models import AgentIdentity

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(AgentIdentity).where(
                    AgentIdentity.agent_id == public.agent_id
                )
            )
        ).scalar_one()
        stored = row.encrypted_private_key

    # The stored blob must not contain the raw private key material.
    private_raw = crypto.private_key_bytes(service._private_key)  # type: ignore[arg-type]
    assert private_raw not in base64.b64decode(stored)
    assert private_raw.hex() not in stored
    # And it must decrypt correctly with the right secret.
    assert crypto.decrypt_private_key(stored, TEST_SECRET) == private_raw


async def test_wrong_secret_fails_startup_hard(db_session_factory) -> None:
    owner_id = await _make_owner(db_session_factory)
    await _make_service(db_session_factory, owner_id).initialize_identity()

    wrong = _make_service(db_session_factory, owner_id, secret="attacker-guess")
    with pytest.raises(IdentityCorruptionError):
        await wrong.initialize_identity()


async def test_missing_secret_fails_startup_hard(db_session_factory) -> None:
    owner_id = await _make_owner(db_session_factory)
    await _make_service(db_session_factory, owner_id).initialize_identity()

    missing = _make_service(db_session_factory, owner_id, secret=None)
    with pytest.raises(IdentityCorruptionError):
        await missing.initialize_identity()


async def test_no_secret_and_empty_db_cannot_create_identity(db_session_factory) -> None:
    owner_id = await _make_owner(db_session_factory)
    service = _make_service(db_session_factory, owner_id, secret=None)
    with pytest.raises(IdentityCorruptionError):
        await service.initialize_identity()


# --- Signing through the service ------------------------------------------------


async def test_service_sign_and_external_verify(db_session_factory) -> None:
    owner_id = await _make_owner(db_session_factory)
    service = _make_service(db_session_factory, owner_id)
    await service.initialize_identity()

    message = b"Hello from Nexus"
    signature = await service.sign(message)

    public_raw = base64.b64decode(service.get_public_identity().public_key)
    assert await service.verify(public_raw, message, signature) is True
    # Tampered message fails.
    assert await service.verify(public_raw, b"Hello from Nexus!", signature) is False
    # Wrong key fails.
    other = crypto.public_key_bytes(crypto.generate_keypair()[1])
    assert await service.verify(other, message, signature) is False


async def test_service_verify_does_not_need_private_key(db_session_factory) -> None:
    """Verification is a pure function: works even on a fresh, uninitialised
    service instance (Part 6 A2A will rely on this)."""
    owner_id = await _make_owner(db_session_factory)
    service = _make_service(db_session_factory, owner_id)
    await service.initialize_identity()

    private, public = crypto.generate_keypair()
    signature = crypto.sign_bytes(private, b"independent")

    fresh = _make_service(db_session_factory, owner_id)  # never initialised
    assert fresh._private_key is None
    assert await fresh.verify(crypto.public_key_bytes(public), b"independent", signature)


async def test_sign_before_init_raises(db_session_factory) -> None:
    owner_id = await _make_owner(db_session_factory)
    service = _make_service(db_session_factory, owner_id)
    with pytest.raises(IdentityNotReadyError):
        await service.sign(b"msg")


# --- Owner isolation ---------------------------------------------------------------


async def test_identity_is_owner_scoped(db_session_factory) -> None:
    owner_a = await _make_owner(db_session_factory)
    owner_b = await _make_owner(db_session_factory)

    identity_a = await _make_service(db_session_factory, owner_a).initialize_identity()
    identity_b = await _make_service(db_session_factory, owner_b).initialize_identity()

    assert identity_a.agent_id != identity_b.agent_id
    assert identity_a.public_key != identity_b.public_key

    # Loading for owner B never returns A's identity.
    reloaded_b = await _make_service(db_session_factory, owner_b).initialize_identity()
    assert reloaded_b.agent_id == identity_b.agent_id

    from sqlalchemy import select

    from app.identity.models import AgentIdentity

    async with db_session_factory() as session:
        rows = (
            (await session.execute(select(AgentIdentity))).scalars().all()
        )
        assert {(r.owner_id, r.agent_id) for r in rows} == {
            (owner_a, identity_a.agent_id),
            (owner_b, identity_b.agent_id),
        }
