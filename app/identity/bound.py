"""An identity bound to ONE agent.

``IdentityService`` is agent-scoped by design (M4): every operation names the
agent it applies to. The A2A machinery, the gateway client and card signing,
however, were written against the pre-M4 interface, which was implicitly
"the one identity this process has" - ``get_public_identity()``, ``sign()``,
``verify()``.

Rather than thread an agent id through the whole protocol stack *and* the
protocol itself (that is M6's job, where the envelope starts carrying the
recipient's agent), this adapter supplies the old interface over a specific
agent row. The result is what M4 is actually for:

  * identity is no longer a process-global singleton,
  * key material is resolved per signing call, so a rotation or revocation
    takes effect immediately instead of being masked by a cached key, and
  * routing the protocol to a chosen agent becomes a matter of constructing a
    different adapter - no protocol change required.

The binding is to the agent ROW, not to the agent_id string, precisely so a key
rotation (which changes the agent_id, since the id is a fingerprint of the key)
does not silently break the binding.
"""

from __future__ import annotations

import logging
import uuid

from app.identity.service import IdentityService, PublicIdentity

logger = logging.getLogger("nexus.identity.bound")


class AgentIdentity:
    """Agent identity bound to one agent row.

    ``get_public_identity()`` is synchronous by design (a snapshot taken at
    bind time), while ``sign()`` always resolves the CURRENT key — so a
    rotation or revocation takes effect immediately. After rotating, rebind
    via ``AgentIdentity.for_agent``: signing would otherwise use the new key
    while advertising the old id (``is_stale`` makes that checkable).
    """

    def __init__(
        self,
        *,
        identity_service: IdentityService,
        agent_row_id: uuid.UUID,
        public_identity: PublicIdentity,
    ) -> None:
        self._service = identity_service
        self._agent_row_id = agent_row_id
        self._public_identity = public_identity

    @classmethod
    async def for_agent(
        cls,
        *,
        identity_service: IdentityService,
        agent_row_id: uuid.UUID,
    ) -> AgentIdentity:
        """Bind the adapter to an agent, reading its current identity."""
        public = await identity_service.get_public_identity_for(agent_row_id)
        return cls(
            identity_service=identity_service,
            agent_row_id=agent_row_id,
            public_identity=public,
        )

    @property
    def agent_row_id(self) -> uuid.UUID:
        return self._agent_row_id

    def get_public_identity(self) -> PublicIdentity:
        """The identity snapshot taken at bind time."""
        return self._public_identity

    async def refresh(self) -> PublicIdentity:
        """Re-read the current identity (after a rotation)."""
        self._public_identity = await self._service.get_public_identity_for(
            self._agent_row_id
        )
        return self._public_identity

    async def is_stale(self) -> bool:
        """True when the snapshot no longer matches the agent's current key."""
        current = await self._service.get_public_identity_for(self._agent_row_id)
        return current.agent_id != self._public_identity.agent_id

    async def sign(self, data: bytes) -> bytes:
        """Sign with the agent's current key."""
        return await self._service.sign(data, agent_row_id=self._agent_row_id)

    async def verify(
        self, public_key: bytes, data: bytes, signature: bytes
    ) -> bool:
        """Verify with any public key (stateless; no agent lookup)."""
        return await self._service.verify(public_key, data, signature)

    async def sign_json(self, payload: object) -> bytes:
        return await self._service.sign_json(
            payload, agent_row_id=self._agent_row_id
        )


__all__ = ["AgentIdentity"]
