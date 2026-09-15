"""LLM provider abstraction.

The Nexus runtime never talks to a vendor SDK directly. It depends only on
:class:`LLMProvider`, so providers (OpenAI, Anthropic, Google, local models)
can be swapped without touching the agent runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# A chat message in the OpenAI-style shape: {"role": ..., "content": ...}.
Message = dict[str, str]


class LLMError(Exception):
    """Base class for all LLM provider failures.

    The API layer maps these to HTTP responses without leaking provider
    internals or credentials to clients.
    """


class LLMConfigurationError(LLMError):
    """The provider is not usable as configured (e.g. missing API key)."""


class LLMTimeoutError(LLMError):
    """The provider did not respond within the configured timeout."""


class LLMProviderError(LLMError):
    """The provider returned an error or an unusable response."""


class LLMProvider(ABC):
    """Abstract reasoning backend.

    Implementations translate the provider-neutral ``messages`` list into a
    vendor call and return the assistant's text.
    """

    #: Human-readable provider name, used in logs and error messages.
    name: str = "llm"

    #: Whether this provider can actually serve requests. ``False`` for the
    #: placeholder used when no provider is configured.
    configured: bool = True

    @abstractmethod
    async def generate(self, messages: list[Message]) -> str:
        """Return the assistant reply for ``messages``.

        Args:
            messages: Ordered conversation history, each item shaped as
                ``{"role": "system" | "user" | "assistant", "content": str}``.

        Raises:
            LLMConfigurationError: provider is misconfigured.
            LLMTimeoutError: the call exceeded the configured timeout.
            LLMProviderError: the provider failed or returned no content.
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release provider resources. Overridden by providers holding clients."""
        return None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<{type(self).__name__} name={self.name!r}>"


class UnconfiguredProvider(LLMProvider):
    """Placeholder used when no LLM provider is configured.

    Lets the runtime start and serve session endpoints while chat fails with a
    clear, actionable error instead of crashing at startup.
    """

    name = "unconfigured"
    configured = False

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def generate(self, messages: list[Message]) -> str:
        raise LLMConfigurationError(self._reason)


def coerce_message(role: str, content: str) -> Message:
    """Build a validated message dict.

    Kept here so every provider receives the same shape.
    """
    return {"role": role, "content": content}


__all__ = [
    "LLMConfigurationError",
    "LLMError",
    "LLMProvider",
    "LLMProviderError",
    "LLMTimeoutError",
    "Message",
    "UnconfiguredProvider",
    "coerce_message",
]
