"""Context assembly.

Turns a :class:`~app.agent.session.Session` plus any retrieved long-term
memories into the provider-neutral message list handed to the LLM. Keeping
this separate from the agent means future concerns - retrieved memories
(Part 2, done), identity (Part 3), policy decisions (Part 4), tool schemas
(Part 5) - can be layered in here without touching orchestration.

Layout produced:

    [system prompt]
    [system: RELEVANT MEMORY block]   (only when memories exist)
    [*conversation history]
"""

from __future__ import annotations

from app.agent.session import Session
from app.llm.base import Message

MEMORY_BLOCK_HEADER = "RELEVANT MEMORY (long-term facts about the owner):"
MEMORY_BLOCK_FOOTER = (
    "Use these memories when they are relevant; do not repeat them verbatim "
    "unless asked."
)


class ContextBuilder:
    """Builds the message list sent to the LLM."""

    def __init__(self, *, system_prompt: str) -> None:
        self._system_prompt = system_prompt

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def build(
        self, session: Session, memories: list[str] | None = None
    ) -> list[Message]:
        """Return ``[system, (memory block), *history]``.

        Args:
            session: the conversation snapshot.
            memories: human-readable memory contents to inject. Only the
                caller decides which memories are relevant (and, in Part 4,
                which are allowed); this class just formats them.

        The system prompt is injected on every call rather than stored in the
        session, so changing it takes effect immediately and it can never be
        trimmed away by history limits.
        """
        messages: list[Message] = [
            {"role": "system", "content": self._system_prompt}
        ]
        if memories:
            lines = "\n".join(f"- {m}" for m in memories)
            messages.append(
                {
                    "role": "system",
                    "content": f"{MEMORY_BLOCK_HEADER}\n{lines}\n{MEMORY_BLOCK_FOOTER}",
                }
            )
        messages.extend(dict(m) for m in session.messages)
        return messages


__all__ = ["ContextBuilder"]
