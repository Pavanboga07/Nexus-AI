"""Target resolution: Maps human names and aliases to Agent identities.

Disambiguates multiple matches, checks the trusted-agent registry,
and invokes discovery when necessary.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a.models import TrustStatus
from app.a2a.repository import TrustedAgentRepository
from app.orchestration.models import Contact
from app.orchestration.repository import ContactRepository
from app.orchestration.schemas import TargetResolution, TargetResolutionStatus

import httpx

logger = logging.getLogger("nexus.orchestration.target_resolver")


class TargetResolver:
    """Resolves a natural language person reference to an agent identity."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        trusted_agents: TrustedAgentRepository | None = None,
        contacts: ContactRepository | None = None,
        discovery_service: Any = None,
        memory_manager: Any = None,
        gateway_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._trusted = trusted_agents or TrustedAgentRepository()
        self._contacts = contacts or ContactRepository()
        self._discovery = discovery_service
        self._memory = memory_manager
        self._gateway_url = gateway_url
        self._http_client = http_client
        self._discovered_cards: dict[str, dict[str, Any]] = {}

    def get_discovered_card(self, agent_id: str) -> dict[str, Any] | None:
        return self._discovered_cards.get(agent_id)

    def _get_base_http(self) -> str | None:
        if not self._gateway_url:
            return None
        return (
            self._gateway_url.replace("wss://", "https://")
            .replace("ws://", "http://")
            .rstrip("/ws")
            .rstrip("/")
        )

    async def _http_get(self, url: str) -> httpx.Response | None:
        if self._http_client:
            return await self._http_client.get(url)
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.get(url)

    def _verify_card(
        self, card: dict[str, Any], expected_agent_id: str | None = None
    ) -> dict[str, Any] | None:
        """Verify the integrity, signature, and time window of an agent card."""
        try:
            if self._discovery and hasattr(self._discovery, "verify_card"):
                return self._discovery.verify_card(card, expected_agent_id=expected_agent_id)
            from app.a2a import signing
            from app.a2a.cards import validate_card_schema, validate_card_time_window
            validate_card_schema(card)
            if expected_agent_id and card.get("agent_id") != expected_agent_id:
                return None
            if not signing.agent_id_matches_key(card["agent_id"], card["public_key"]):
                return None
            if not signing.verify_card_signature(card, card["public_key"]):
                return None
            validate_card_time_window(card)
            return card
        except Exception as exc:
            logger.warning("Card verification failed: %s", exc)
            return None

    async def resolve(
        self,
        owner_id: uuid.UUID,
        target_name: str,
    ) -> TargetResolution:
        """Resolve target_name into a TargetResolution."""
        clean_name = self._clean_name(target_name)
        if not clean_name:
            return TargetResolution(
                target_name=target_name,
                status=TargetResolutionStatus.UNKNOWN_AGENT,
            )

        async with self._session_factory() as session:
            if hasattr(self._trusted, "list_active"):
                all_trusted = await self._trusted.list_active(session, owner_id)
            elif hasattr(self._trusted, "list_all"):
                all_trusted = await self._trusted.list_all(session, owner_id)
            else:
                all_trusted = []

        # =========================================================================
        # TIER 1: Exact Agent ID (nexus:ed25519:<fingerprint>)
        # =========================================================================
        if clean_name.startswith("nexus:ed25519:"):
            agent_id = clean_name.lower()

            # 1. Check local trusted registry
            matching_ta = [
                ta for ta in all_trusted
                if ta.agent_id == agent_id and ta.status == TrustStatus.ACTIVE.value
            ]
            if matching_ta:
                ta = matching_ta[0]
                return TargetResolution(
                    target_name=target_name,
                    status=TargetResolutionStatus.KNOWN_AGENT,
                    agent_id=ta.agent_id,
                    endpoint=ta.endpoint,
                    display_name=ta.display_name,
                    is_trusted=True,
                )

            # 2. Check Gateway directory
            base_http = self._get_base_http()
            if base_http:
                try:
                    url = f"{base_http}/agents/{agent_id}"
                    resp = await self._http_get(url)
                    if resp and resp.status_code == 200:
                        agent_data = resp.json()
                        card = agent_data.get("agent_card")
                        if card:
                            verified_card = self._verify_card(card, expected_agent_id=agent_id)
                            if verified_card is not None:
                                self._discovered_cards[agent_id] = verified_card
                                return TargetResolution(
                                    target_name=target_name,
                                    status=TargetResolutionStatus.DISCOVERED_AGENT,
                                    agent_id=agent_id,
                                    endpoint=verified_card.get("endpoint", self._gateway_url),
                                    display_name=verified_card.get("display_name") or agent_data.get("display_name") or agent_id,
                                    is_trusted=False,
                                    card=verified_card,
                                )
                            else:
                                logger.warning("Agent card signature/integrity check failed for %s", agent_id)
                                return TargetResolution(
                                    target_name=target_name,
                                    status=TargetResolutionStatus.UNKNOWN_AGENT,
                                )
                        else:
                            from app.a2a import signing
                            pk = agent_data.get("public_key")
                            if pk and signing.agent_id_matches_key(agent_id, pk):
                                card_dict = {
                                    "agent_id": agent_id,
                                    "public_key": pk,
                                    "display_name": agent_data.get("display_name") or agent_id,
                                    "handle": agent_data.get("handle"),
                                    "endpoint": self._gateway_url,
                                    "is_online": agent_data.get("is_online", False),
                                }
                                self._discovered_cards[agent_id] = card_dict
                                return TargetResolution(
                                    target_name=target_name,
                                    status=TargetResolutionStatus.DISCOVERED_AGENT,
                                    agent_id=agent_id,
                                    endpoint=self._gateway_url,
                                    display_name=agent_data.get("display_name") or agent_id,
                                    is_trusted=False,
                                    card=card_dict,
                                )
                except Exception as exc:
                    logger.warning("Gateway agent lookup failed for %s: %s", agent_id, exc)

            # 3. Check local contacts with agent_id
            async with self._session_factory() as session:
                contacts = await self._contacts.find_by_name_or_alias(session, owner_id, clean_name)
            for c in contacts:
                if c.agent_id == agent_id:
                    return TargetResolution(
                        target_name=target_name,
                        status=TargetResolutionStatus.DISCOVERED_AGENT,
                        agent_id=c.agent_id,
                        endpoint=c.endpoint,
                        display_name=c.display_name,
                        is_trusted=False,
                    )

            return TargetResolution(
                target_name=target_name,
                status=TargetResolutionStatus.UNKNOWN_AGENT,
            )

        # =========================================================================
        # TIER 2: Public Handle (@handle)
        # =========================================================================
        if clean_name.startswith("@"):
            handle_query = clean_name.lstrip("@").lower()

            # 1. Check local contacts
            async with self._session_factory() as session:
                matching_contacts = await self._contacts.find_by_name_or_alias(
                    session, owner_id, clean_name
                )
                if not matching_contacts:
                    matching_contacts = await self._contacts.find_by_name_or_alias(
                        session, owner_id, handle_query
                    )

            if len(matching_contacts) == 1 and matching_contacts[0].agent_id:
                c = matching_contacts[0]
                is_trusted = any(ta.agent_id == c.agent_id for ta in all_trusted if ta.status == TrustStatus.ACTIVE.value)
                return TargetResolution(
                    target_name=target_name,
                    status=TargetResolutionStatus.KNOWN_AGENT if is_trusted else TargetResolutionStatus.DISCOVERED_AGENT,
                    agent_id=c.agent_id,
                    endpoint=c.endpoint,
                    display_name=c.display_name,
                    is_trusted=is_trusted,
                )
            elif len(matching_contacts) > 1:
                return TargetResolution(
                    target_name=target_name,
                    status=TargetResolutionStatus.AMBIGUOUS_AGENT,
                    candidates=[c.display_name for c in matching_contacts],
                )

            # 2. Query Gateway for handle
            base_http = self._get_base_http()
            if base_http:
                try:
                    url = f"{base_http}/agents/handle/{handle_query}"
                    resp = await self._http_get(url)
                    if resp and resp.status_code == 200:
                        agent_data = resp.json()
                        if "agents" in agent_data:
                            cands = agent_data.get("agents", [])
                            if len(cands) == 1:
                                agent_data = cands[0]
                            elif len(cands) > 1:
                                return TargetResolution(
                                    target_name=target_name,
                                    status=TargetResolutionStatus.AMBIGUOUS_AGENT,
                                    candidates=[
                                        c.get("display_name") or c.get("handle") or c["agent_id"]
                                        for c in cands
                                    ],
                                )
                            else:
                                agent_data = {}

                        if "agent_id" in agent_data:
                            agent_id = agent_data["agent_id"]
                            card = agent_data.get("agent_card")
                            if card:
                                verified_card = self._verify_card(card, expected_agent_id=agent_id)
                                if verified_card is None:
                                    return TargetResolution(
                                        target_name=target_name,
                                        status=TargetResolutionStatus.UNKNOWN_AGENT,
                                    )
                                self._discovered_cards[agent_id] = verified_card
                            else:
                                verified_card = agent_data
                                self._discovered_cards[agent_id] = agent_data

                            is_trusted = any(ta.agent_id == agent_id for ta in all_trusted if ta.status == TrustStatus.ACTIVE.value)
                            return TargetResolution(
                                target_name=target_name,
                                status=TargetResolutionStatus.KNOWN_AGENT if is_trusted else TargetResolutionStatus.DISCOVERED_AGENT,
                                agent_id=agent_id,
                                endpoint=verified_card.get("endpoint", self._gateway_url),
                                display_name=verified_card.get("display_name") or agent_data.get("display_name") or f"@{handle_query}",
                                is_trusted=is_trusted,
                                card=verified_card,
                            )
                except Exception as exc:
                    logger.warning("Gateway handle lookup failed for @%s: %s", handle_query, exc)

            return TargetResolution(
                target_name=target_name,
                status=TargetResolutionStatus.UNKNOWN_AGENT,
            )

        # =========================================================================
        # TIER 3: Display Name (e.g. "Rahul")
        # =========================================================================
        # 1. Search local contacts
        async with self._session_factory() as session:
            matching_contacts = await self._contacts.find_by_name_or_alias(
                session, owner_id, clean_name
            )

        matching_trusted = [
            ta for ta in all_trusted
            if ta.status == TrustStatus.ACTIVE.value
            and (
                clean_name.lower() in ta.display_name.lower()
                or ta.display_name.lower() in clean_name.lower()
            )
        ]

        distinct_contact_names = {c.display_name for c in matching_contacts}
        if len(distinct_contact_names) > 1:
            return TargetResolution(
                target_name=target_name,
                status=TargetResolutionStatus.AMBIGUOUS_AGENT,
                candidates=sorted(list(distinct_contact_names)),
            )

        if len(matching_contacts) == 1:
            contact = matching_contacts[0]
            if contact.agent_id:
                is_trusted = any(ta.agent_id == contact.agent_id for ta in all_trusted if ta.status == TrustStatus.ACTIVE.value)
                return TargetResolution(
                    target_name=target_name,
                    status=TargetResolutionStatus.KNOWN_AGENT if is_trusted else TargetResolutionStatus.DISCOVERED_AGENT,
                    agent_id=contact.agent_id,
                    endpoint=contact.endpoint,
                    display_name=contact.display_name,
                    is_trusted=is_trusted,
                )

        if len(matching_trusted) == 1:
            ta = matching_trusted[0]
            return TargetResolution(
                target_name=target_name,
                status=TargetResolutionStatus.KNOWN_AGENT,
                agent_id=ta.agent_id,
                endpoint=ta.endpoint,
                display_name=ta.display_name,
                is_trusted=True,
            )
        elif len(matching_trusted) > 1:
            return TargetResolution(
                target_name=target_name,
                status=TargetResolutionStatus.AMBIGUOUS_AGENT,
                candidates=[ta.display_name for ta in matching_trusted],
            )

        # 2. Gateway directory search
        base_http = self._get_base_http()
        if base_http:
            try:
                search_url = f"{base_http}/agents/search?q={clean_name}"
                resp = await self._http_get(search_url)
                if resp and resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("agents", [])
                    if len(candidates) > 1:
                        return TargetResolution(
                            target_name=target_name,
                            status=TargetResolutionStatus.AMBIGUOUS_AGENT,
                            candidates=[
                                c.get("display_name") or c.get("handle") or c["agent_id"]
                                for c in candidates
                            ],
                        )
                    elif len(candidates) == 1:
                        c = candidates[0]
                        agent_id = c["agent_id"]
                        card = c.get("agent_card")
                        if card:
                            verified_card = self._verify_card(card, expected_agent_id=agent_id)
                            if verified_card is None:
                                return TargetResolution(
                                    target_name=target_name,
                                    status=TargetResolutionStatus.UNKNOWN_AGENT,
                                )
                            self._discovered_cards[agent_id] = verified_card
                        else:
                            self._discovered_cards[agent_id] = c

                        is_trusted = any(ta.agent_id == agent_id for ta in all_trusted if ta.status == TrustStatus.ACTIVE.value)
                        return TargetResolution(
                            target_name=target_name,
                            status=TargetResolutionStatus.KNOWN_AGENT if is_trusted else TargetResolutionStatus.DISCOVERED_AGENT,
                            agent_id=agent_id,
                            endpoint=c.get("endpoint", self._gateway_url),
                            display_name=c.get("display_name") or clean_name,
                            is_trusted=is_trusted,
                            card=self._discovered_cards.get(agent_id),
                        )
            except Exception as exc:
                logger.warning("Gateway directory search failed for %s: %s", clean_name, exc)

        # 3. Known discovered Agent Cards in DiscoveryService
        if self._discovery is not None:
            try:
                cards = (
                    await self._discovery.list_known_cards(owner_id)
                    if hasattr(self._discovery, "list_known_cards")
                    else []
                )
                matching_cards = [
                    card
                    for card in cards
                    if clean_name.lower() in card.display_name.lower()
                    or (getattr(card, "handle", None) and clean_name.lower() == card.handle.lower())
                ]
                if len(matching_cards) == 1:
                    card = matching_cards[0]
                    card_dict = card.model_dump() if hasattr(card, "model_dump") else dict(card)
                    self._discovered_cards[card.agent_id] = card_dict
                    return TargetResolution(
                        target_name=target_name,
                        status=TargetResolutionStatus.DISCOVERED_AGENT,
                        agent_id=card.agent_id,
                        endpoint=card.endpoint,
                        display_name=card.display_name,
                        is_trusted=False,
                        card=card_dict,
                    )
                elif len(matching_cards) > 1:
                    return TargetResolution(
                        target_name=target_name,
                        status=TargetResolutionStatus.AMBIGUOUS_AGENT,
                        candidates=[c.display_name for c in matching_cards],
                    )
            except Exception as exc:
                logger.warning("DiscoveryService lookup failed for %s: %s", clean_name, exc)

        # 4. Memory fallback: check relationship memories for agent hints
        if self._memory is not None:
            try:
                memories = await self._memory.get_relevant_memories(
                    owner_id, f"contact {clean_name} agent endpoint", limit=3
                )
                for m in memories:
                    if "agent" in m.content.lower() and clean_name.lower() in m.content.lower():
                        logger.info("Found potential agent reference in memory for %s: %s", clean_name, m.content)
            except Exception:
                pass

        # 5. Unknown agent
        return TargetResolution(
            target_name=target_name,
            status=TargetResolutionStatus.UNKNOWN_AGENT,
        )

    def _clean_name(self, raw: str) -> str:
        """Strip prefixes like 'my friend', 'my colleague', etc."""
        cleaned = raw.strip()
        low = cleaned.lower()
        if low.startswith("nexus:ed25519:"):
            return low
        if low.startswith("@"):
            return "@" + low[1:]
        prefixes = [
            "my friend ", "my partner ", "my project partner ", "my colleague ",
            "my teammate ", "colleague ", "friend ", "partner "
        ]
        for p in prefixes:
            if low.startswith(p):
                cleaned = cleaned[len(p):].strip()
                low = cleaned.lower()
                break
        if low.startswith("nexus:ed25519:"):
            return low
        if low.startswith("@"):
            return "@" + low[1:]
        return cleaned
