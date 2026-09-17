"""OpenAI-compatible implementation of :class:`~app.llm.base.LLMProvider`.

This is the ONLY module allowed to import the OpenAI SDK or know about
chat-completions request/response shapes. It works against any
OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, Ollama, LM Studio,
vLLM, ...) selected via the base URL.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
from app.observability import LLM_LATENCY

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
        max_attempts: int = 3,
        retry_base_seconds: float = 0.5,
    ) -> None:
        if not api_key:
            raise LLMConfigurationError(
                "NEXUS_LLM_API_KEY is not set. Configure it in your "
                "environment or .env file before sending chat requests."
            )

        self._model = model
        self._timeout = timeout
        # M7: retry transient failures, not just empty responses. A single
        # empty-response retry was previously the entire resilience story, so a
        # 502 or a dropped connection was terminal for the user's turn.
        self._max_attempts = max(1, int(max_attempts))
        self._retry_base_seconds = max(0.0, float(retry_base_seconds))
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

    @staticmethod
    def _is_retryable_status(status_code: int | None) -> bool:
        """5xx and 429 are worth retrying; 4xx generally is not.

        429 (rate limit) is included: it is explicitly a "try again later"
        signal. A 400 (malformed request) is not: the same request will fail
        the same way, so retrying only adds latency.
        """
        if status_code is None:
            return True
        return status_code >= 500 or status_code == 429

    async def generate(self, messages: list[Message]) -> str:
        """Generate, recording latency and outcome (M11).

        The wrapper exists so the metric cannot be forgotten by a new code path
        and so the observed duration is the *whole* call including retries and
        backoff - which is what a caller waits for, and what a latency alert
        should fire on. Timing only the successful attempt would hide the
        retry storm that is usually the actual incident.
        """
        started = time.perf_counter()
        try:
            result = await self._generate_with_retries(messages)
        except Exception as exc:
            LLM_LATENCY.observe(
                time.perf_counter() - started,
                provider=self.name,
                outcome=type(exc).__name__,
            )
            raise
        LLM_LATENCY.observe(
            time.perf_counter() - started,
            provider=self.name,
            outcome="success",
        )
        return result

    async def _generate_with_retries(self, messages: list[Message]) -> str:
        logger.debug(
            "llm_request provider=%s model=%s messages=%d",
            self.name,
            self._model,
            len(messages),
        )
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                completion = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,  # type: ignore[arg-type]
                )
            except AuthenticationError as exc:
                # Never retry: the key is wrong, and retrying risks lockout.
                raise LLMConfigurationError(
                    "The LLM provider rejected the configured API key."
                ) from exc
            except APITimeoutError as exc:
                last_error = LLMTimeoutError(
                    f"The LLM provider did not respond within {self._timeout:g}s."
                )
                if await self._backoff(attempt, "timeout"):
                    continue
                raise last_error from exc
            except (APIConnectionError, httpx.TimeoutException) as exc:
                last_error = LLMTimeoutError("Could not reach the LLM provider.")
                if await self._backoff(attempt, "connection_error"):
                    continue
                raise last_error from exc
            except RateLimitError as exc:
                last_error = LLMProviderError(
                    "LLM provider rate limit reached. Try again shortly."
                )
                if await self._backoff(attempt, "rate_limited"):
                    continue
                raise last_error from exc
            except APIStatusError as exc:
                last_error = LLMProviderError(
                    f"The LLM provider returned an error (status {exc.status_code})."
                )
                if self._is_retryable_status(
                    getattr(exc, "status_code", None)
                ) and await self._backoff(attempt, f"status_{exc.status_code}"):
                    continue
                raise last_error from exc
            except Exception as exc:  # noqa: BLE001 - normalise unknown failures
                raise LLMProviderError(
                    "Unexpected error from the LLM provider."
                ) from exc

            try:
                content = self._extract_content(completion)
            except LLMProviderError as exc:
                # Some compatible providers return 200 with an empty choices
                # array; that is transient, so retry it too.
                last_error = exc
                if await self._backoff(attempt, "empty_response"):
                    continue
                raise

            logger.debug(
                "llm_response provider=%s chars=%d", self.name, len(content)
            )
            return content

        raise last_error if last_error else LLMProviderError(  # pragma: no cover
            "Unexpected error from the LLM provider."
        )

    async def _backoff(self, attempt: int, reason: str) -> bool:
        """Sleep before the next attempt. False when attempts are exhausted.

        Exponential with jitter: without jitter, every request that failed
        during an outage retries in lockstep and keeps the provider saturated.
        """
        if attempt >= self._max_attempts:
            return False
        from app.jobs.queue import compute_backoff_seconds

        delay = compute_backoff_seconds(
            attempt, base_seconds=self._retry_base_seconds or 0.5, max_seconds=30.0
        )
        logger.warning(
            "llm_retry provider=%s attempt=%d/%d reason=%s delay=%.2fs",
            self.name,
            attempt,
            self._max_attempts,
            reason,
            delay,
        )
        await asyncio.sleep(delay)
        return True

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
