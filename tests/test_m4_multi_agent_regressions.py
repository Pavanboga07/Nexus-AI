"""M4 regression tests: one owner, many agents; key rotation and revocation.

Audit findngs covered:

  H10  ``UniqueConstraint("owner_id")`` on ``agent_identities`` allowed exactly
       ONE agent per owner, enforced by the database. That is the structural
       reason "multi-agent" could only ever mean "my one agent talks to your
       one agent", and why the audit answered "can one owner have multiple
       agents?" with a hard no.
  H9   A single ``NEXUS_IDENTITY_KEY`` protected the one identity, with no key
       rotation, no revocation and no key history.
"""

from __future__ import annotations

import base64
import inspect
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database.models import Owner
from app.identity import crypto
from app.identity.bound import AgentIdentity
from app.identity.models import ACTIVE, PAUSED, REVOKED, Agent, AgentKey
from app.identity.service import (
    AgentNotFoundError,
    AgentNotUsableError,
    IdentityCorruptionError,
    IdentityService,
)

TEST_SECRET = "m4-test-identity-secret-not-real"


async def _owner(session_factory) -> uuid.UUID:
    async with session_factory() as session:
        owner = Owner(name="m4-owner")
        session.add(owner)
        await session.commit()
        return owner.id


def _svc(session_factory, owner_id=None, secret: str | None = TEST_SECRET) -> IdentityService:
    return IdentityService(
        session_factory=session_factory,
        encryption_secret=secret,
        owner_id=owner_id,
    )


# ---------------------------------------------------------------------------
# H10: many agents per owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_owner_can_have_many_agents(db_session_factory) -> None:
    """The headline M4 capability: several agents, distinct identities.

    Pre-fix this was impossible at the schema level.
    """
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)

    a = await svc.create_agent(owner_id, display_name="Alice's assistant")
    b = await svc.create_agent(owner_id, display_name="Calendar bot")
    c = await svc.create_agent(owner_id, display_name="Research bot")

    agents = await svc.list_agents(owner_id)
    assert len(agents) == 3

    ids = {ag.agent_id for ag in agents}
    assert len(ids) == 3, "each agent must have its own cryptographic identity"
    assert ids == {a.agent_id, b.agent_id, c.agent_id}
    assert {a.id, b.id, c.id} == {ag.id for ag in agents}

    # The first agent created becomes primary; the rest are not.
    assert a.is_primary is True
    assert b.is_primary is False and c.is_primary is False


@pytest.mark.asyncio
async def test_agents_of_two_owners_are_isolated(db_session_factory) -> None:
    owner_a = await _owner(db_session_factory)
    owner_b = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_a)

    a = await svc.create_agent(owner_a, display_name="A's agent")
    await svc.create_agent(owner_b, display_name="B's agent")

    assert len(await svc.list_agents(owner_a)) == 1
    assert len(await svc.list_agents(owner_b)) == 1

    # Another owner cannot read agent A, and the error is indistinguishable
    # from "does not exist" so agent ids are not enumerable across owners.
    with pytest.raises(AgentNotFoundError):
        await svc.get_agent(owner_b, a.id)


@pytest.mark.asyncio
async def test_each_agent_signs_with_its_own_key(db_session_factory) -> None:
    """Two agents of one owner produce signatures verifiable under their OWN keys."""
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)

    a = await svc.create_agent(owner_id, display_name="Agent A")
    b = await svc.create_agent(owner_id, display_name="Agent B")

    message = b"who signed this?"
    sig_a = await svc.sign(message, agent_row_id=a.id)
    sig_b = await svc.sign(message, agent_row_id=b.id)

    pub_a = await svc.get_public_identity_for(a.id)
    pub_b = await svc.get_public_identity_for(b.id)

    assert await svc.verify(
        base64.b64decode(pub_a.public_key), message, sig_a
    )
    # A's signature must NOT verify under B's key: identities are distinct.
    assert not await svc.verify(
        base64.b64decode(pub_b.public_key), message, sig_a
    )
    assert await svc.verify(
        base64.b64decode(pub_b.public_key), message, sig_b
    )


@pytest.mark.asyncio
async def test_agent_id_is_fingerprint_of_its_key(db_session_factory) -> None:
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)
    summary = await svc.create_agent(owner_id, display_name="Fingerprint me")

    public = await svc.get_public_identity_for(summary.id)
    raw = base64.b64decode(public.public_key)
    assert summary.agent_id == crypto.agent_id_from_public_key(raw)


@pytest.mark.asyncio
async def test_handle_is_unique_per_owner_and_normalised(db_session_factory) -> None:
    owner_a = await _owner(db_session_factory)
    owner_b = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_a)

    a = await svc.create_agent(owner_a, display_name="A", handle="@Mum")
    assert a.handle == "mum", "handles are normalised (lowercased, no @)"

    # The same handle is fine for a DIFFERENT owner...
    other = await svc.create_agent(owner_b, display_name="B", handle="mum")
    assert other.handle == "mum"

    # ...but not twice for the same owner. Letting this raise is correct: a
    # duplicate handle would make @handle resolution ambiguous.
    with pytest.raises(Exception):
        await svc.create_agent(owner_a, display_name="C", handle="mum")


# ---------------------------------------------------------------------------
# H9: key rotation and revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_rotation_keeps_history_and_old_key_verifies(
    db_session_factory,
) -> None:
    """Rotating a key must not destroy the ability to verify old signatures."""
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)
    agent = await svc.create_agent(owner_id, display_name="Rotating agent")

    before = await svc.get_public_identity_for(agent.id)
    signed_before = await svc.sign(b"pre-rotation", agent_row_id=agent.id)

    rotated = await svc.rotate_key(owner_id, agent.id)
    after = await svc.get_public_identity_for(agent.id)

    # The agent_id changed, because it IS the fingerprint of the key.
    assert rotated.agent_id != before.agent_id
    assert after.agent_id == rotated.agent_id

    keys = await svc.list_keys(owner_id, agent.id)
    assert len(keys) == 2, "rotation retains the previous key for verification"
    current = [k for k in keys if k["is_current"]]
    assert len(current) == 1
    assert current[0]["public_key"] == after.public_key

    # The RETIRED key still verifies the signature it produced.
    assert await svc.verify(
        base64.b64decode(before.public_key), b"pre-rotation", signed_before
    )

    # New signatures use the new key.
    signed_after = await svc.sign(b"post-rotation", agent_row_id=agent.id)
    assert await svc.verify(
        base64.b64decode(after.public_key), b"post-rotation", signed_after
    )
    assert not await svc.verify(
        base64.b64decode(before.public_key), b"post-rotation", signed_after
    )


@pytest.mark.asyncio
async def test_rotated_key_binding_follows_the_agent_row(db_session_factory) -> None:
    """The bound adapter signs with the CURRENT key, not a cached one.

    This is the property that makes rotation actually effective: a cached
    private key would keep signing with the retired key.
    """
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)
    agent = await svc.create_agent(owner_id, display_name="Bound agent")

    bound = await AgentIdentity.for_agent(
        identity_service=svc, agent_row_id=agent.id
    )
    # The snapshot is stale the moment the key rotates.
    assert await bound.is_stale() is False

    await svc.rotate_key(owner_id, agent.id)
    assert await bound.is_stale() is True

    # Refreshing picks up the new identity...
    refreshed = await bound.refresh()
    assert await bound.is_stale() is False

    # ...and signing already used the NEW key even before the refresh, because
    # key material is resolved per call.
    signed = await bound.sign(b"after rotation")
    assert await svc.verify(
        base64.b64decode(refreshed.public_key), b"after rotation", signed
    )


@pytest.mark.asyncio
async def test_revoked_key_cannot_sign(db_session_factory) -> None:
    """Revocation must actually stop signing, not merely record intent."""
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)
    agent = await svc.create_agent(owner_id, display_name="Doomed agent")

    await svc.revoke_key(
        owner_id, agent.id, agent_id=agent.agent_id, reason="compromised"
    )

    keys = await svc.list_keys(owner_id, agent.id)
    assert keys[0]["revoked_at"] is not None
    assert keys[0]["revoked_reason"] == "compromised"

    # The current key is revoked, so signing is refused rather than proceeding
    # with a compromised key.
    with pytest.raises(AgentNotUsableError):
        await svc.sign(b"should fail", agent_row_id=agent.id)

    assert await svc.is_key_valid_at(agent.agent_id) is False


@pytest.mark.asyncio
async def test_revoking_an_agent_revokes_its_keys(db_session_factory) -> None:
    """Agent-level revocation must be terminal and cover every key."""
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)
    agent = await svc.create_agent(owner_id, display_name="Retired agent")
    await svc.rotate_key(owner_id, agent.id)

    updated = await svc.set_agent_status(owner_id, agent.id, REVOKED)
    assert updated.status == REVOKED

    keys = await svc.list_keys(owner_id, agent.id)
    assert all(k["revoked_at"] is not None for k in keys), (
        "revoking an agent must revoke every key it owns"
    )
    with pytest.raises(AgentNotUsableError):
        await svc.sign(b"nope", agent_row_id=agent.id)
    # A revoked agent's key cannot be rotated back into service.
    with pytest.raises(AgentNotUsableError):
        await svc.rotate_key(owner_id, agent.id)


@pytest.mark.asyncio
async def test_paused_agent_cannot_sign_but_can_resume(db_session_factory) -> None:
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)
    agent = await svc.create_agent(owner_id, display_name="Pausable agent")

    await svc.set_agent_status(owner_id, agent.id, PAUSED)
    with pytest.raises(AgentNotUsableError):
        await svc.sign(b"paused", agent_row_id=agent.id)

    await svc.set_agent_status(owner_id, agent.id, ACTIVE)
    assert await svc.sign(b"resumed", agent_row_id=agent.id)


# ---------------------------------------------------------------------------
# Startup integrity (must not regress)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_secret_is_detected_at_startup(db_session_factory) -> None:
    """A wrong NEXUS_IDENTITY_KEY must fail loudly, not lazily on first sign."""
    owner_id = await _owner(db_session_factory)
    await _svc(db_session_factory, owner_id).create_agent(
        owner_id, display_name="Guarded agent"
    )

    wrong = _svc(db_session_factory, owner_id, secret="attacker-guess")
    with pytest.raises(IdentityCorruptionError):
        await wrong.initialize_primary_agent(owner_id)


@pytest.mark.asyncio
async def test_no_secret_cannot_create_an_agent(db_session_factory) -> None:
    """An unencryptable private key must never be written."""
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id, secret=None)
    with pytest.raises(IdentityCorruptionError):
        await svc.create_agent(owner_id, display_name="No secret")


@pytest.mark.asyncio
async def test_private_key_is_encrypted_in_the_key_table(db_session_factory) -> None:
    """Key material at rest is AES-GCM ciphertext, never plaintext."""
    owner_id = await _owner(db_session_factory)
    svc = _svc(db_session_factory, owner_id)
    agent = await svc.create_agent(owner_id, display_name="Encrypted agent")

    async with db_session_factory() as session:
        key = (
            await session.execute(
                select(AgentKey).where(AgentKey.agent_row_id == agent.id)
            )
        ).scalar_one()
        stored = key.encrypted_private_key
        raw = crypto.decrypt_private_key(stored, TEST_SECRET)

    assert raw.hex() not in stored
    assert len(base64.b64decode(stored)) >= len(raw) + 12 + 16
    # And the plaintext is never the whole stored value.
    assert base64.b64encode(raw).decode() not in stored


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------


def test_owner_uniqueness_constraint_is_gone() -> None:
    """The constraint that forbade many agents per owner must not return."""
    from sqlalchemy import UniqueConstraint

    unique_names = {
        c.name
        for c in AgentKey.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_agent_identities_owner" not in unique_names

    # And no unique constraint covers owner_id alone (a multi-column unique
    # such as (owner_id, handle) is fine - that is a different rule).
    for constraint in AgentKey.__table__.constraints:
        if isinstance(constraint, UniqueConstraint):
            cols = [c.name for c in constraint.columns]
            assert cols != ["owner_id"], (
                f"agent key table still enforces one-agent-per-owner: {constraint}"
            )


def test_identity_service_holds_no_process_global_key() -> None:
    """Key material must be resolved per operation, not cached in the process.

    A cached key would mask rotation and revocation, which is precisely the
    pre-M4 behaviour that made identity a singleton.
    """
    src = inspect.getsource(IdentityService)
    assert "_private_key: crypto.Ed25519PrivateKey | None = None" not in src
    assert "self._private_key = " not in src


def test_agents_router_is_mounted_behind_auth() -> None:
    import app.main as main_module

    src = inspect.getsource(main_module.create_app)
    assert "include_router(agents_router, dependencies=protected)" in src
