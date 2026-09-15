"""The Nexus agent runtime.

This is the orchestrator. It owns session state and conversation flow; it does
NOT know which LLM vendor is in use. The LLM is a replaceable reasoning
component injected into the constructor.

    NexusAgent
        ├── SessionStore    (state - PostgreSQL since Part 2)
        ├── ContextBuilder  (prompt assembly + memory injection)
        ├── LLMProvider     (reasoning)
        └── MemoryManager?  (long-term memory, optional)

When a MemoryManager is configured (Part 2), each turn becomes:

    retrieve relevant memories
        -> build context (system + memory block + history)
        -> LLM
        -> persist reply
        -> extract candidate memories (background, non-blocking)

Memory failures never break the chat: retrieval errors yield an empty memory
block, extraction runs as a fire-and-forget task that logs failures.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from app.agent.context import ContextBuilder
from app.agent.session import Session, SessionStore
from app.llm.base import LLMProvider

if TYPE_CHECKING:
    from app.memory.manager import MemoryManager

logger = logging.getLogger("nexus.agent")


class NexusAgent:
    """Conversational agent core."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        sessions: SessionStore,
        context_builder: ContextBuilder,
        memory_manager: MemoryManager | None = None,
        memory_top_k: int = 5,
        memory_enabled: bool = True,
    ) -> None:
        self._provider = provider
        self._sessions = sessions
        self._context = context_builder
        self._memory = memory_manager
        self._memory_top_k = memory_top_k
        self._memory_enabled = memory_enabled

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def memory_enabled(self) -> bool:
        return self._memory is not None and self._memory_enabled

    # --- Owner resolution ---------------------------------------------------
    # Part 2: a single implicit owner. The store resolves it; the agent asks
    # through the same interface so Part 3+ can source identity differently.

    async def _owner_id(self) -> uuid.UUID:
        from app.database.session_store import DatabaseSessionStore

        if isinstance(self._sessions, DatabaseSessionStore):
            return await self._sessions._get_owner_id()
        # Non-DB stores (tests, Part 1 fallback) have no owner concept; use a
        # nil UUID so memory stays namespace-separated from real owners.
        return uuid.UUID(int=0)

    async def process_message(self, session_id: str, message: str) -> str:
        """Run one conversational turn and return the assistant's reply.

        Steps:
            1. Load the session (raises ``SessionNotFoundError`` if unknown).
            2. Persist the user's message.
            3. Retrieve relevant long-term memories (best effort).
            4. Build the LLM context from memory + full history.
            5. Call the provider.
            6. Persist the assistant's reply.
            7. Kick off background memory extraction (non-blocking).
            8. Return the reply.

        If the provider fails, the user's message stays in history so the turn
        can be retried without losing what was said.
        """
        session = await self._sessions.get_session(session_id)
        logger.info(
            "message_received session_id=%s chars=%d", session_id, len(message)
        )

        await self._sessions.add_message(session_id, "user", message)

        # Reload history including the just-added user message.
        session = await self._sessions.get_session(session_id)

        memories = await self._relevant_memories(message)

        messages = self._context.build(session, memories=memories)
        logger.info(
            "llm_request session_id=%s provider=%s messages=%d memories=%d",
            session_id,
            self._provider.name,
            len(messages),
            len(memories),
        )

        try:
            reply = await self._provider.generate(messages)
        except Exception:
            logger.warning(
                "llm_error session_id=%s provider=%s",
                session_id,
                self._provider.name,
                exc_info=True,
            )
            raise

        await self._sessions.add_message(session_id, "assistant", reply)
        logger.info(
            "llm_response session_id=%s provider=%s chars=%d",
            session_id,
            self._provider.name,
            len(reply),
        )

        self._schedule_extraction(session_id, message, reply)
        return reply

    async def _relevant_memories(self, query: str) -> list[str]:
        """Best-effort retrieval; never raises into the chat path."""
        if not self.memory_enabled:
            return []
        try:
            owner_id = await self._owner_id()
            found = await self._memory.get_relevant_memories(
                owner_id, query, limit=self._memory_top_k
            )
            return [m.content for m in found]
        except Exception:
            logger.warning("memory_retrieval_failed", exc_info=True)
            return []

    def _schedule_extraction(
        self, session_id: str, user_message: str, assistant_message: str
    ) -> None:
        """Fire-and-forget memory extraction (Part 2 spec §23).

        Runs on the event loop; failures are logged, never surfaced to the
        user, and never block the response.
        """
        if not self.memory_enabled:
            return
        task = asyncio.create_task(
            self._extract_and_store(session_id, user_message, assistant_message)
        )
        # Keep a reference so the task isn't garbage-collected mid-flight.
        self._background_tasks = getattr(self, "_background_tasks", set())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _extract_and_store(
        self, session_id: str, user_message: str, assistant_message: str
    ) -> None:
        from app.memory.extractor import MemoryExtractor

        try:
            extractor = MemoryExtractor(provider=self._provider)
            candidates = await extractor.extract(user_message, assistant_message)
            if not candidates:
                return
            owner_id = await self._owner_id()
            counts = await self._memory.store_candidates(
                owner_id, candidates, source_message_id=None
            )
            logger.info(
                "memory_extraction_complete session_id=%s stored=%d duplicates=%d",
                session_id,
                counts.get("stored", 0),
                counts.get("duplicates", 0),
            )
        except Exception:
            logger.warning("memory_extraction_error", exc_info=True)

    # --- Session lifecycle passthroughs ----------------------------------
    # The API layer talks to the agent, not the store, so the store can be
    # swapped for a persistent backend later without changing routes.

    async def create_session(self) -> Session:
        return await self._sessions.create_session()

    async def get_session(self, session_id: str) -> Session:
        return await self._sessions.get_session(session_id)

    async def clear_session(self, session_id: str) -> Session:
        return await self._sessions.clear_session(session_id)

    async def delete_session(self, session_id: str) -> None:
        await self._sessions.delete_session(session_id)

    async def list_sessions(self) -> list[str]:
        return await self._sessions.list_sessions()

    async def aclose(self) -> None:
        # Let in-flight background extraction finish (bounded by provider timeout).
        tasks = getattr(self, "_background_tasks", set())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._provider.aclose()


__all__ = ["NexusAgent"]
