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
    ) -> None:
        self._session_factory = session_factory
        self._trusted = trusted_agents or TrustedAgentRepository()
        self._contacts = contacts or ContactRepository()
        self._discovery = discovery_service
        self._memory = memory_manager

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
            # 1. Search in contacts table
            matching_contacts = await self._contacts.find_by_name_or_alias(
                session, owner_id, clean_name
            )

            # 2. Search in trusted_agents table
            if hasattr(self._trusted, "list_active"):
                all_trusted = await self._trusted.list_active(session, owner_id)
            elif hasattr(self._trusted, "list_all"):
                all_trusted = await self._trusted.list_all(session, owner_id)
            else:
                all_trusted = []
            matching_trusted = [
                ta for ta in all_trusted
                if ta.status == TrustStatus.ACTIVE.value
                and (
                    clean_name.lower() in ta.display_name.lower()
                    or ta.display_name.lower() in clean_name.lower()
                )
            ]

        # Check for ambiguity across contacts
        distinct_contact_names = {c.display_name for c in matching_contacts}
        if len(distinct_contact_names) > 1:
            return TargetResolution(
                target_name=target_name,
                status=TargetResolutionStatus.AMBIGUOUS_AGENT,
                candidates=sorted(list(distinct_contact_names)),
            )

        # If exactly one contact match
        if len(matching_contacts) == 1:
            contact = matching_contacts[0]
            if contact.agent_id:
                # Verify if agent is actively trusted
                is_trusted = any(ta.agent_id == contact.agent_id for ta in all_trusted)
                if is_trusted:
                    return TargetResolution(
                        target_name=target_name,
                        status=TargetResolutionStatus.KNOWN_AGENT,
                        agent_id=contact.agent_id,
                        endpoint=contact.endpoint,
                        display_name=contact.display_name,
                        is_trusted=True,
                    )
                else:
                    # Discovered/known agent ID, but not yet trusted!
                    return TargetResolution(
                        target_name=target_name,
                        status=TargetResolutionStatus.DISCOVERED_AGENT,
                        agent_id=contact.agent_id,
                        endpoint=contact.endpoint,
                        display_name=contact.display_name,
                        is_trusted=False,
                    )

        # If no contact matched, but trusted agents matched
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

        # 3. Memory fallback: check relationship memories for agent hints
        if self._memory is not None:
            try:
                memories = await self._memory.get_relevant_memories(
                    owner_id, f"contact {clean_name} agent endpoint", limit=3
                )
                for m in memories:
                    # e.g. "Rahul's agent endpoint is https://..."
                    if "agent" in m.content.lower() and clean_name.lower() in m.content.lower():
                        logger.info("Found potential agent reference in memory for %s: %s", clean_name, m.content)
            except Exception:
                pass

        # 4. Unknown agent
        return TargetResolution(
            target_name=target_name,
            status=TargetResolutionStatus.UNKNOWN_AGENT,
        )

    def _clean_name(self, raw: str) -> str:
        """Strip prefixes like 'my friend', 'my colleague', etc."""
        cleaned = raw.strip()
        low = cleaned.lower()
        prefixes = [
            "my friend ", "my partner ", "my project partner ", "my colleague ",
            "my teammate ", "colleague ", "friend ", "partner "
        ]
        for p in prefixes:
            if low.startswith(p):
                cleaned = cleaned[len(p):].strip()
                break
        return cleaned
