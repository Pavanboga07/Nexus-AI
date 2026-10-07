"""DiscoveryService: fetch, verify, and register remote agents (Part 7).

The discovery flow:

    POST /a2a/discover  {"url": "https://remote/.well-known/nexus-agent.json"}
                  │
                  ▼
         validate_endpoint (SSRF protection)
                  │
                  ▼
           HTTP GET url (timeout, size-cap, no-redirect)
                  │
                  ▼
           validate_card_schema
                  │
                  ▼
           agent_id <-> public_key consistency
                  │
                  ▼
           verify_card_signature (Ed25519)
                  │
                  ▼
           validate_card_time_window
                  │
                  ▼
      A2AService.register_trusted_agent()
                  │
                  ▼
           TrustedAgent record (persisted)

Every network fetch goes through the same SSRF protections as A2A
transport (Part 6 §22): scheme restriction, loopback/private rejection
unless explicitly allowed, no redirects, size cap, timeout.

Discovery ONLY provides recognition: registered ≠ authorized.
PolicyService still gates every future interaction.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.a2a import signing
from app.a2a.cards import (
    CardValidationError,
    validate_card_schema,
    validate_card_time_window,
)
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.models import TrustedAgent, TrustedAgentCard
from app.a2a.repository import TrustedAgentCardRepository
from app.a2a.service import A2AService
from app.a2a.transport import validate_endpoint
from app.identity.service import IdentityService

logger = logging.getLogger("nexus.a2a.discovery")


class DiscoveryService:
    """Fetch, verify, and register remote agent cards."""

    def __init__(
        self,
        *,
        identity_service: IdentityService,
        a2a_service: A2AService,
        allow_local_endpoints: bool = False,
        timeout_seconds: float = 10.0,
        max_card_bytes: int = 65_536,
        session_factory: Any = None,
    ) -> None:
        self._identity = identity_service
        self._a2a = a2a_service
        self._allow_local = allow_local_endpoints
        self._timeout = timeout_seconds
        self._max_card_bytes = max_card_bytes
        #: Optional: when provided, verified cards are cached in PostgreSQL so
        #: capability discovery does not re-fetch the peer on every lookup.
        self._session_factory = session_factory
        self._card_repo = TrustedAgentCardRepository()

    async def fetch_card(self, url: str) -> dict[str, Any]:
        """Fetch a remote agent card by URL.

        Steps:
        1. SSRF-validate the URL
        2. HTTP GET with timeout, size-cap, no-redirect
        3. Validate card schema
        4. Verify agent_id <-> public_key consistency
        5. Verify Ed25519 signature
        6. Verify time window (issued_at, expires_at)

        Returns the validated (and signed) card dict.
        Raises A2AError on any failure.
        """
        # 1. SSRF protection
        try:
            validate_endpoint(url, allow_local=self._allow_local)
        except A2AError:
            raise

        # 2. HTTP GET
        card = await self._http_get_card(url)
        return self.verify_card(card)

    def verify_card(
        self, card: dict[str, Any], expected_agent_id: str | None = None
    ) -> dict[str, Any]:
        """Verify the structural schema, fingerprint consistency, Ed25519 signature,
        and temporal validity of an agent card."""
        # 1. Schema validation
        try:
            validate_card_schema(card)
        except CardValidationError as exc:
            raise A2AError(
                A2AErrorCode.INVALID_CARD, f"Card validation failed: {exc}"
            ) from None

        # 2. agent_id <-> public_key consistency
        if not signing.agent_id_matches_key(
            card["agent_id"], card["public_key"]
        ):
            raise A2AError(
                A2AErrorCode.INVALID_CARD,
                "Card agent_id does not match the card's public_key.",
            )

        if expected_agent_id and card["agent_id"] != expected_agent_id:
            raise A2AError(
                A2AErrorCode.INVALID_CARD,
                f"Card agent_id '{card['agent_id']}' does not match expected '{expected_agent_id}'.",
            )

        # 3. Signature verification
        if not signing.verify_card_signature(card, card["public_key"]):
            raise A2AError(
                A2AErrorCode.CARD_SIGNATURE_INVALID,
                "Card signature verification failed.",
            )

        # 4. Time window
        try:
            validate_card_time_window(card)
        except CardValidationError as exc:
            raise A2AError(
                A2AErrorCode.CARD_EXPIRED, f"Card time window invalid: {exc}"
            ) from None

        logger.info(
            "card_verified agent_id=%s display_name=%s endpoint=%s",
            card["agent_id"],
            card.get("display_name"),
            card.get("endpoint"),
        )
        return card

    async def fetch_card_from_gateway(
        self, agent_id: str, gateway_url: str
    ) -> dict[str, Any]:
        """Fetch and cryptographically verify an agent card from the Gateway directory."""
        base_http = (
            gateway_url.replace("wss://", "https://").replace("ws://", "http://")
        )
        # Strip a trailing "/ws" path SUFFIX (not str.rstrip, which strips
        # characters — it would mangle hosts like ".../news").
        if base_http.endswith("/ws"):
            base_http = base_http[: -len("/ws")]
        base_http = base_http.rstrip("/")
        # The gateway serves GET /directory/{agent_id} (relay/routes.py) and
        # wraps the card as {"agent_id": ..., "card": {...}}.
        url = f"{base_http}/directory/{agent_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise A2AError(
                    A2AErrorCode.NOT_FOUND,
                    f"Agent card not found on Gateway for agent_id {agent_id} (HTTP {resp.status_code})",
                )
            data = resp.json()
            card = data.get("card") if isinstance(data, dict) else None
            if not isinstance(card, dict):
                raise A2AError(
                    A2AErrorCode.INVALID_CARD,
                    "Gateway directory response has no 'card' object.",
                )
            if isinstance(data.get("agent_id"), str) and data["agent_id"] != agent_id:
                raise A2AError(
                    A2AErrorCode.INVALID_CARD,
                    "Gateway directory wrapper agent_id does not match the request.",
                )
            return self.verify_card(card, expected_agent_id=agent_id)

    async def register_verified_card(
        self,
        owner_id,
        card: dict[str, Any],
        *,
        display_name: str | None = None,
        expected_agent_id: str | None = None,
    ) -> tuple[TrustedAgent, dict[str, Any]]:
        """THE single writer for a discovered remote agent.

        Every path that turns a card into local state (direct fetch, gateway
        directory lookup, operator-supplied card) must funnel through here so
        the verification invariant cannot be bypassed:

            no card enters local state without a verified Ed25519 signature
            over its canonical form, with agent_id <-> public_key consistency
            and a valid time window.

        Verification happens HERE rather than at the call sites, so a caller
        cannot forget it. Raises A2AError on any verification failure.

        Returns the TrustedAgent record and the verified card.
        """
        verified = self.verify_card(card, expected_agent_id=expected_agent_id)
        agent_id = verified["agent_id"]

        name = display_name or verified["display_name"]
        endpoint = verified["endpoint"]

        agent = await self._a2a.register_trusted_agent(
            owner_id,
            agent_id=agent_id,
            public_key=verified["public_key"],
            display_name=name,
            endpoint=endpoint,
        )

        # Persist the VERIFIED card so capability discovery does not have to
        # re-fetch the peer, and so the cached card is trustworthy by
        # construction (nothing unverified is ever written here).
        await self._store_card(owner_id, agent_id, verified)

        logger.info(
            "verified_card_registered agent_id=%s display_name=%s endpoint=%s "
            "capabilities=%d",
            agent_id,
            name,
            endpoint,
            len(verified.get("capabilities") or []),
        )
        return agent, verified

    async def _store_card(
        self, owner_id, agent_id: str, verified_card: dict[str, Any]
    ) -> None:
        """Persist a previously verified card (best effort, never raises)."""
        if self._session_factory is None:
            return
        try:
            expires_at = None
            raw_expiry = verified_card.get("expires_at")
            if isinstance(raw_expiry, str):
                from app.a2a.schemas import parse_iso

                expires_at = parse_iso(raw_expiry)
            async with self._session_factory() as session:
                await self._card_repo.upsert(
                    session,
                    TrustedAgentCard(
                        owner_id=owner_id,
                        agent_id=agent_id,
                        card=verified_card,
                        card_expires_at=expires_at,
                    ),
                )
                await session.commit()
        except Exception as exc:
            # Caching a card must never break the discovery that produced it.
            logger.warning("card_cache_write_failed agent_id=%s: %s", agent_id, exc)

    async def get_known_card(
        self, owner_id, agent_id: str
    ) -> dict[str, Any] | None:
        """Return a previously VERIFIED, unexpired card, or None."""
        if self._session_factory is None:
            return None
        from app.a2a.repository import TrustedAgentCardRepository

        async with self._session_factory() as session:
            row = await TrustedAgentCardRepository().get(
                session, owner_id, agent_id
            )
        return row.card if row is not None else None

    async def list_known_cards(self, owner_id) -> list[dict[str, Any]]:
        """Return all previously VERIFIED, unexpired cards for this owner.

        These are safe to treat as attested: every row was written only after
        ``register_verified_card`` verified its signature and time window.
        """
        if self._session_factory is None:
            return []
        from app.a2a.repository import TrustedAgentCardRepository

        async with self._session_factory() as session:
            rows = await TrustedAgentCardRepository().list_for_owner(
                session, owner_id
            )
        return [row.card for row in rows]

    async def discover_and_register(
        self,
        owner_id,
        url: str,
        *,
        display_name: str | None = None,
    ) -> tuple[TrustedAgent, dict[str, Any]]:
        """Fetch a remote card by URL, verify it, and register the agent.

        Returns the TrustedAgent record and the verified card dict.
        Uses ``display_name`` override if provided, otherwise the card's
        own ``display_name``.

        Raises A2AError (CONFLICT) if the agent is already registered.
        """
        card = await self.fetch_card(url)
        return await self.register_verified_card(
            owner_id, card, display_name=display_name
        )

    # --- Private HTTP helper ------------------------------------------------

    async def _http_get_card(self, url: str) -> dict[str, Any]:
        """GET a remote card URL with SSRF, timeout, size, and redirect
        protections.  Mirrors the safety properties of Part 6's
        ``HttpA2ATransport.send()``."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException:
            raise A2AError(
                A2AErrorCode.DISCOVERY_FAILED,
                "Remote agent did not respond in time.",
            ) from None
        except httpx.HTTPError as exc:
            raise A2AError(
                A2AErrorCode.DISCOVERY_FAILED,
                f"Failed to fetch agent card ({type(exc).__name__}).",
            ) from None

        if response.status_code in {301, 302, 303, 307, 308}:
            raise A2AError(
                A2AErrorCode.DISCOVERY_FAILED,
                "Remote agent attempted a redirect; refusing to follow.",
            )
        if len(response.content) > self._max_card_bytes:
            raise A2AError(
                A2AErrorCode.DISCOVERY_FAILED,
                "Remote agent card response exceeds the size limit.",
            )
        if response.status_code >= 400:
            raise A2AError(
                A2AErrorCode.DISCOVERY_FAILED,
                f"Remote agent returned HTTP {response.status_code}.",
            )
        try:
            return response.json()
        except ValueError:
            raise A2AError(
                A2AErrorCode.INVALID_CARD,
                "Remote agent returned a non-JSON response.",
            ) from None


__all__ = ["DiscoveryService"]
