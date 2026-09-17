"""LLM provider package.

Exposes the provider-neutral interface plus a factory that builds the
configured provider. The agent runtime depends on the interface only.
"""

from __future__ import annotations

from app.config.settings import Settings
from app.llm.base import (
    LLMConfigurationError,
    LLMError,
    LLMProvider,
    LLMProviderError,
    LLMTimeoutError,
    Message,
    UnconfiguredProvider,
)
from app.llm.openai_adapter import OpenAICompatibleProvider

# Backwards-compatible alias: the adapter speaks the OpenAI chat-completions
# protocol against any compatible endpoint, not just OpenAI's.
OpenAIProvider = OpenAICompatibleProvider


def build_provider(settings: Settings) -> LLMProvider:
    """Construct the LLM provider selected by configuration.

    Today the OpenAI-compatible adapter covers OpenAI, OpenRouter, Groq,
    Ollama, LM Studio and other endpoints - selected via ``NEXUS_LLM_BASE_URL``.
    Adding a natively different provider (e.g. Anthropic's own protocol) later
    means adding a branch here; the agent runtime stays unchanged.

    If no API key is configured, returns an :class:`UnconfiguredProvider` so
    the runtime still starts and session endpoints keep working; chat will
    fail with a clear 503 until a key is provided.
    """
    api_key = settings.llm_api_key
    if not api_key:
        return UnconfiguredProvider(
            "NEXUS_LLM_API_KEY is not set. Configure it in your environment "
            "or .env file before sending chat requests."
        )
    return OpenAICompatibleProvider(
        api_key=api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        timeout=settings.nexus_llm_timeout,
        extra_headers=settings.llm_extra_headers,
        # M7: retry transient provider failures (5xx, connection, empty
        # response), not just a single empty response.
        max_attempts=settings.nexus_llm_max_attempts,
        retry_base_seconds=settings.nexus_llm_retry_base_seconds,
    )


__all__ = [
    "LLMConfigurationError",
    "LLMError",
    "LLMProvider",
    "LLMProviderError",
    "LLMTimeoutError",
    "Message",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "UnconfiguredProvider",
    "build_provider",
]
