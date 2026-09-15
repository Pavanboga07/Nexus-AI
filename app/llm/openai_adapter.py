"""OpenAI-compatible implementation of :class:`~app.llm.base.LLMProvider`.

This is the ONLY module allowed to import the OpenAI SDK or know about
chat-completions request/response shapes. It works against any
OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, Ollama, LM Studio,
vLLM, ...) selected via the base URL.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from app.llm.base import (
    LLMConfigurationError,
    LLMProvider,
    LLMProviderError,
    LLMTimeoutError,
    Message,
)

logger = logging.getLogger("nexus.llm.openai_compatible")

# Host fragments used to give the provider a human-readable name in logs and
# /health output. The default covers unknown custom endpoints.
_HOST_LABELS: tuple[tuple[str, str], ...] = (
    ("openrouter.ai", "openrouter"),
    ("api.groq.com", "groq"),
    ("api.openai.com", "openai"),
    ("generativelanguage.googleapis.com", "google"),
    ("localhost:11434", "ollama"),
    ("localhost:1234", "lmstudio"),
    ("api.together.xyz", "together"),
    ("api.deepseek.com", "deepseek"),
    ("api.mistral.ai", "mistral"),
    ("api.x.ai", "xai"),
)


def _provider_name_for(base_url: str | None) -> str:
    """Derive a short provider label from the endpoint host."""
    if not base_url:
        return "openai"  # default OpenAI endpoint
    try:
        host = urlsplit(base_url).hostname or ""
        port = urlsplit(base_url).port
    except ValueError:
        return "custom"
    host_and_port = f"{host}:{port}" if port else host
    for fragment, label in _HOST_LABELS:
        if fragment in host_and_port:
            return label
    return host or "custom"


class OpenAICompatibleProvider(LLMProvider):
    """Reasoning backend for any OpenAI-compatible chat-completions API."""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if not api_key:
            raise LLMConfigurationError(
                "NEXUS_LLM_API_KEY is not set. Configure it in your "
                "environment or .env file before sending chat requests."
            )

        self._model = model
        self._timeout = timeout
        self._empty_response_retries = 1
        self._empty_response_backoff = 0.5
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            default_headers=extra_headers or None,
        )
        self.name = _provider_name_for(base_url)

    @property
    def model(self) -> str:
        return self._model

    async def generate(self, messages: list[Message]) -> str:
        logger.debug(
            "llm_request provider=%s model=%s messages=%d",
            self.name,
            self._model,
            len(messages),
        )
        # Some OpenAI-compatible providers (e.g. Groq's on-demand tier,
        # OpenRouter's free pool) intermittently return 200 with an empty
        # `choices` array. One retry with a short backoff absorbs that flake.
        last_error: LLMProviderError | None = None
        for attempt in range(1 + self._empty_response_retries):
            try:
                completion = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,  # type: ignore[arg-type]
                )
            except AuthenticationError as exc:
                raise LLMConfigurationError(
                    "The LLM provider rejected the configured API key."
                ) from exc
            except APITimeoutError as exc:
                raise LLMTimeoutError(
                    f"The LLM provider did not respond within {self._timeout:g}s."
                ) from exc
            except (APIConnectionError, httpx.TimeoutException) as exc:
                raise LLMTimeoutError("Could not reach the LLM provider.") from exc
            except RateLimitError as exc:
                raise LLMProviderError(
                    "LLM provider rate limit reached. Try again shortly."
                ) from exc
            except APIStatusError as exc:
                raise LLMProviderError(
                    f"The LLM provider returned an error (status {exc.status_code})."
                ) from exc
            except Exception as exc:  # noqa: BLE001 - normalise unknown failures
                raise LLMProviderError(
                    "Unexpected error from the LLM provider."
                ) from exc

            try:
                content = self._extract_content(completion)
            except LLMProviderError as exc:
                last_error = exc
                if attempt < self._empty_response_retries:
                    logger.warning(
                        "llm_empty_response provider=%s attempt=%d retrying",
                        self.name,
                        attempt + 1,
                    )
                    await asyncio.sleep(self._empty_response_backoff)
                    continue
                raise
            logger.debug(
                "llm_response provider=%s chars=%d", self.name, len(content)
            )
            return content

        raise last_error if last_error else LLMProviderError(  # pragma: no cover
            "Unexpected error from the LLM provider."
        )

    @staticmethod
    def _extract_content(completion: object) -> str:
        """Pull assistant text out of a chat completion, defensively."""
        try:
            choices = getattr(completion, "choices", None)
            if not choices:
                raise LLMProviderError("The LLM provider returned no choices.")
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
        except LLMProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMProviderError("Malformed response from the LLM provider.") from exc

        if not content or not str(content).strip():
            raise LLMProviderError("The LLM provider returned an empty response.")
        return str(content)

    async def aclose(self) -> None:
        await self._client.close()


__all__ = ["OpenAICompatibleProvider"]
