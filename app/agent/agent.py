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
    from app.tools.service import ToolService

logger = logging.getLogger("nexus.agent")

#: Tools the chat loop may offer the model (allowlist — never the registry).
_SEARCH_TOOL_ALLOWLIST = ("web_search", "web_fetch")

#: Purpose bound to every chat-turn tool execution (fits PURPOSE_PATTERN).
_WEB_RESEARCH_PURPOSE = "web-research"

#: User-facing fallback when a policy stop carries no accompanying text.
_APPROVAL_FALLBACK = "That needs your approval — check your inbox."


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
        tool_service: ToolService | None = None,
    ) -> None:
        self._provider = provider
        self._sessions = sessions
        self._context = context_builder
        self._memory = memory_manager
        self._memory_top_k = memory_top_k
        self._memory_enabled = memory_enabled
        self._tool_service = tool_service

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def memory_enabled(self) -> bool:
        return self._memory is not None and self._memory_enabled

    # --- Public collaborators (M5) -------------------------------------------
    # Other components used to reach through to ``agent._memory`` and
    # ``agent._provider``. Reaching into another object's private state makes
    # the dependency invisible and lets a rename silently break a caller, so
    # the collaborators this agent is willing to share are exposed explicitly.

    @property
    def memory(self) -> "MemoryManager | None":
        """The agent's memory manager, or None when memory is disabled."""
        return self._memory

    @property
    def provider(self) -> LLMProvider:
        """The injected reasoning provider."""
        return self._provider

    @property
    def sessions(self) -> SessionStore:
        """The session store backing this agent."""
        return self._sessions

    # --- Owner resolution ---------------------------------------------------
    # Multi-tenancy makes the acting owner a PER-REQUEST property, supplied by
    # the caller (see app/api/auth_context.py). The method below remains only
    # as a development fallback for deployments with authentication disabled;
    # production paths always pass owner_id explicitly.

    async def owner_id(self) -> uuid.UUID:
        """Legacy single-owner fallback (development only).

        With authentication enforced, callers must pass the authenticated owner
        instead. This exists so a local checkout with no accounts still works,
        and so pre-auth deployments can be adopted on first registration.

        Public in M5: route/service code used to call ``agent._owner_id()``,
        reaching into private state.
        """
        from app.database.session_store import DatabaseSessionStore

        if isinstance(self._sessions, DatabaseSessionStore):
            return await self._sessions.fallback_owner_id()
        # Non-DB stores (tests, Part 1 fallback) have no owner concept; use a
        # nil UUID so memory stays namespace-separated from real owners.
        return uuid.UUID(int=0)

    async def process_message(
        self, owner_id: uuid.UUID, session_id: str, message: str
    ) -> str:
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
        session = await self._sessions.get_session(owner_id, session_id)
        logger.info(
            "message_received session_id=%s chars=%d", session_id, len(message)
        )

        await self._sessions.add_message(owner_id, session_id, "user", message)

        # Reload history including the just-added user message.
        session = await self._sessions.get_session(owner_id, session_id)

        memories = await self._relevant_memories(owner_id, message)

        messages = self._context.build(session, memories=memories)
        logger.info(
            "llm_request session_id=%s provider=%s messages=%d memories=%d",
            session_id,
            self._provider.name,
            len(messages),
            len(memories),
        )

        try:
            reply = await self._generate_with_optional_tools(
                owner_id, messages
            )
        except Exception:
            logger.warning(
                "llm_error session_id=%s provider=%s",
                session_id,
                self._provider.name,
                exc_info=True,
            )
            raise

        await self._sessions.add_message(owner_id, session_id, "assistant", reply)
        logger.info(
            "llm_response session_id=%s provider=%s chars=%d",
            session_id,
            self._provider.name,
            len(reply),
        )

        self._schedule_extraction(owner_id, session_id, message, reply)
        return reply

    async def _generate_with_optional_tools(
        self, owner_id: uuid.UUID, messages: list[dict[str, str]]
    ) -> str:
        """Plain generate, or the bounded search loop when available.

        The loop runs only when a tool service is present, the provider
        offers ``generate_with_tools``, and at least one allowlisted search
        tool is registered. Anything else falls back to ``generate`` so old
        behaviour (and providers without tool support) is preserved.
        """
        tool_service = self._tool_service
        loop = getattr(self._provider, "generate_with_tools", None)
        if tool_service is None or not callable(loop):
            return await self._provider.generate(messages)
        schemas = self._search_tool_schemas(tool_service)
        if not schemas:
            return await self._provider.generate(messages)

        async def _executor(
            name: str, arguments: dict
        ) -> dict[str, object]:
            from app.tools.schemas import ToolInvocation

            try:
                result = await tool_service.execute(
                    owner_id,
                    ToolInvocation(
                        tool_name=name,
                        arguments=arguments,
                        purpose=_WEB_RESEARCH_PURPOSE,
                    ),
                )
            except Exception:
                logger.warning("chat_tool_error tool=%s", name, exc_info=True)
                return {
                    "text": "The search failed, so I'll answer from what I know.",
                    "stop": False,
                }
            if result.status in ("approval_required", "denied"):
                text = (
                    result.error.message
                    if result.error and result.error.message
                    else _APPROVAL_FALLBACK
                )
                return {"text": text, "stop": True}
            if result.success and result.data is not None:
                import json as _json

                return {"text": _json.dumps(result.data), "stop": False}
            err = (
                result.error.message
                if result.error and result.error.message
                else "unknown error"
            )
            return {
                "text": f"The search failed ({err}), so I'll answer from what I know.",
                "stop": False,
            }

        return await loop(messages, schemas, _executor)

    @staticmethod
    def _search_tool_schemas(tool_service: ToolService) -> list[dict]:
        """Allowlisted search tools in the OpenAI ``tools=`` shape."""
        schemas = []
        for meta in tool_service.list_tools():
            if meta.get("name") not in _SEARCH_TOOL_ALLOWLIST:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": meta.get("name"),
                        "description": meta.get("description", ""),
                        "parameters": meta.get("inputSchema", {"type": "object"}),
                    },
                }
            )
        return schemas

    async def _relevant_memories(self, owner_id: uuid.UUID, query: str) -> list[str]:
        """Best-effort retrieval; never raises into the chat path."""
        if not self.memory_enabled:
            return []
        try:
            found = await self._memory.get_relevant_memories(
                owner_id, query, limit=self._memory_top_k
            )
            return [m.content for m in found]
        except Exception:
            logger.warning("memory_retrieval_failed", exc_info=True)
            return []

    def _schedule_extraction(
        self,
        owner_id: uuid.UUID,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        """Fire-and-forget memory extraction (Part 2 spec §23).

        Runs on the event loop; failures are logged, never surfaced to the
        user, and never block the response.
        """
        if not self.memory_enabled:
            return
        task = asyncio.create_task(
            self._extract_and_store(
                owner_id, session_id, user_message, assistant_message
            )
        )
        # Keep a reference so the task isn't garbage-collected mid-flight.
        self._background_tasks = getattr(self, "_background_tasks", set())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _extract_and_store(
        self,
        owner_id: uuid.UUID,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        from app.memory.extractor import MemoryExtractor

        try:
            extractor = MemoryExtractor(provider=self._provider)
            candidates = await extractor.extract(user_message, assistant_message)
            if not candidates:
                return
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

    async def record_exchange(
        self,
        owner_id: uuid.UUID,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        """Persist a user/assistant exchange and queue memory extraction.

        Extracted from the chat route, which previously reached into
        ``agent._sessions`` and ``agent._schedule_extraction`` to do this. The
        route now has no knowledge of how history or extraction is stored - it
        only knows the agent can record an exchange.
        """
        await self._sessions.add_message(owner_id, session_id, "user", user_message)
        await self._sessions.add_message(
            owner_id, session_id, "assistant", assistant_message
        )
        self._schedule_extraction(
            owner_id, session_id, user_message, assistant_message
        )

    # --- Session lifecycle passthroughs ----------------------------------
    # The API layer talks to the agent, not the store, so the store can be
    # swapped for a persistent backend later without changing routes.
    # ``owner_id`` is required on every call: it is the tenancy boundary.

    async def create_session(self, owner_id: uuid.UUID) -> Session:
        return await self._sessions.create_session(owner_id)

    async def get_session(self, owner_id: uuid.UUID, session_id: str) -> Session:
        return await self._sessions.get_session(owner_id, session_id)

    async def clear_session(self, owner_id: uuid.UUID, session_id: str) -> Session:
        return await self._sessions.clear_session(owner_id, session_id)

    async def delete_session(self, owner_id: uuid.UUID, session_id: str) -> None:
        await self._sessions.delete_session(owner_id, session_id)

    async def list_sessions(self, owner_id: uuid.UUID) -> list[str]:
        return await self._sessions.list_sessions(owner_id)

    async def aclose(self) -> None:
        # Let in-flight background extraction finish (bounded by provider timeout).
        tasks = getattr(self, "_background_tasks", set())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._provider.aclose()


__all__ = ["NexusAgent"]
