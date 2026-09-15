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
from app.a2a.models import TrustedAgent
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
    ) -> None:
        self._identity = identity_service
        self._a2a = a2a_service
        self._allow_local = allow_local_endpoints
        self._timeout = timeout_seconds
        self._max_card_bytes = max_card_bytes

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

        # 3. Schema validation
        try:
            validate_card_schema(card)
        except CardValidationError as exc:
            raise A2AError(
                A2AErrorCode.INVALID_CARD, f"Card validation failed: {exc}"
            ) from None

        # 4. agent_id <-> public_key consistency
        if not signing.agent_id_matches_key(
            card["agent_id"], card["public_key"]
        ):
            raise A2AError(
                A2AErrorCode.INVALID_CARD,
                "Card agent_id does not match the card's public_key.",
            )

        # 5. Signature verification
        if not signing.verify_card_signature(card, card["public_key"]):
            raise A2AError(
                A2AErrorCode.CARD_SIGNATURE_INVALID,
                "Card signature verification failed.",
            )

        # 6. Time window
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

    async def discover_and_register(
        self,
        owner_id,
        url: str,
        *,
        display_name: str | None = None,
    ) -> tuple[TrustedAgent, dict[str, Any]]:
        """Fetch a remote card, verify it, and register the agent.

        Returns the TrustedAgent record and the verified card dict.
        Uses ``display_name`` override if provided, otherwise the card's
        own ``display_name``.

        Raises A2AError (CONFLICT) if the agent is already registered.
        """
        card = await self.fetch_card(url)

        name = display_name or card["display_name"]
        endpoint = card["endpoint"]

        agent = await self._a2a.register_trusted_agent(
            owner_id,
            agent_id=card["agent_id"],
            public_key=card["public_key"],
            display_name=name,
            endpoint=endpoint,
        )

        logger.info(
            "discovery_registered agent_id=%s display_name=%s",
            card["agent_id"],
            name,
        )
        return agent, card

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
