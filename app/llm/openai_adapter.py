"""OpenAI-compatible implementation of :class:`~app.llm.base.LLMProvider`.

This is the ONLY module allowed to import the OpenAI SDK or know about
chat-completions request/response shapes. It works against any
OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, Ollama, LM Studio,
vLLM, ...) selected via the base URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
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


#: Max function-calling rounds per chat turn (Phase D).
_MAX_TOOL_ROUNDS = 2

#: User-facing fallback when a policy stop carries no accompanying text.
_APPROVAL_FALLBACK = "That needs your approval — check your inbox."


def _assistant_message(completion: object) -> dict[str, Any]:
    """Assistant message as a plain dict, tolerating SDK objects or dicts."""
    choices = getattr(completion, "choices", None)
    if not choices:
        return {}
    raw = choices[0]
    if isinstance(raw, dict):
        message = raw.get("message", {})
    else:
        message = getattr(raw, "message", {})
    if isinstance(message, dict):
        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
        }
    return {
        "content": getattr(message, "content", None),
        "tool_calls": getattr(message, "tool_calls", None),
    }


def _parse_tool_call(tc: Any) -> tuple[str, str, dict[str, Any]]:
    """Return (call_id, function name, parsed arguments) for one tool call."""
    if isinstance(tc, dict):
        fn = tc.get("function", {}) or {}
        call_id = str(tc.get("id") or "")
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments", {})
    else:
        fn = getattr(tc, "function", None)
        call_id = str(getattr(tc, "id", "") or "")
        name = str(getattr(fn, "name", "") or "") if fn is not None else ""
        raw_args = getattr(fn, "arguments", {}) if fn is not None else {}
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            parsed = {}
    elif isinstance(raw_args, dict):
        parsed = raw_args
    else:
        parsed = {}
    return call_id, name, parsed


def _new_call_id() -> str:
    """Synthesize a tool-call id (mirrors ``req_<hex>`` shape in tools/service)."""
    return f"call_{uuid.uuid4().hex[:16]}"


def _tool_call_payload(tc: Any) -> dict[str, Any]:
    """Serialize one tool call back into the OpenAI request shape."""
    call_id, name, args = _parse_tool_call(tc)
    if name and not call_id:
        call_id = _new_call_id()
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _normalize_executor_result(
    result: str | Mapping[str, Any],
) -> tuple[str, bool]:
    """Split an executor return into (text, stop) — plain str never stops."""
    if isinstance(result, str):
        return result, False
    if isinstance(result, Mapping):
        text = result.get("text", "")
        return (str(text), bool(result.get("stop", False)))
    return str(result), False


def _escape_retrieved(text: str) -> str:
    """Neutralise literal quarantine delimiters inside untrusted text."""
    return text.replace("<retrieved>", "[retrieved]").replace(
        "</retrieved>", "[/retrieved]"
    )


#: Caps for the deterministic exhaustion digest (no model compose round).
_DIGEST_MAX_ITEMS = 5
_DIGEST_MAX_CHARS = 2000
_DIGEST_SNIPPET_CHARS = 200


def _parse_digest_items(texts: list[str]) -> list[tuple[str, str, str]]:
    """Extract citable (title, url, snippet) items from tool result texts.

    Understands the two shapes the chat executor produces
    (``json.dumps(result.data)``): web_search ``{"results": [{title, url,
    snippet}]}`` and web_fetch ``{"url", "title", "text"}``. Plain
    non-JSON texts, failure strings, and empty strings yield no items.
    An item is usable only with a non-empty url AND a non-empty
    title/snippet (search) or title/text (fetch); url-only entries are
    skipped as uninformative.
    """
    items: list[tuple[str, str, str]] = []
    for text in texts:
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        results = data.get("results")
        if isinstance(results, list):
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("url") or "").strip()
                title = str(entry.get("title") or "").strip()
                snippet = str(entry.get("snippet") or "").strip()
                if not url or (not title and not snippet):
                    continue
                items.append(
                    (
                        title or url,
                        url,
                        snippet[:_DIGEST_SNIPPET_CHARS],
                    )
                )
                if len(items) >= _DIGEST_MAX_ITEMS:
                    return items
        elif isinstance(data.get("url"), str) and str(data.get("url")).strip():
            url = str(data.get("url")).strip()
            title = str(data.get("title") or "").strip()
            raw = str(data.get("text") or "").strip()
            if not title and not raw:
                continue
            items.append(
                (title or url, url, raw[:_DIGEST_SNIPPET_CHARS])
            )
            if len(items) >= _DIGEST_MAX_ITEMS:
                return items
    return items


def _build_tool_digest(texts: list[str]) -> str | None:
    """Render collected tool outputs as a cited list, or None if unusable.

    "No usable items" = zero parseable result items (see
    :func:`_parse_digest_items`); unparseable plain/failure texts do not
    count. Returns None so the caller falls back to the existing
    "I looked that up..." text. Capped at 5 items / 2000 chars total.
    """
    items = _parse_digest_items(texts)
    if not items:
        return None
    lines = ["Here's what I found:"]
    for index, (title, url, snippet) in enumerate(items[:_DIGEST_MAX_ITEMS], 1):
        clean_title = " ".join(title.split())
        clean_snippet = " ".join(snippet.split())
        if clean_snippet:
            lines.append(f"{index}. {clean_title} ({url}): {clean_snippet}")
        else:
            lines.append(f"{index}. {clean_title} ({url})")
    digest = "\n".join(lines)
    if len(digest) > _DIGEST_MAX_CHARS:
        digest = digest[:_DIGEST_MAX_CHARS]
    return digest


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

    async def generate_with_tools(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, Any]],
        executor: Callable[[str, dict[str, Any]], Awaitable[str | Mapping[str, Any]]],
    ) -> str:
        """Run a bounded function-calling loop (Phase D, max 2 tool rounds).

        Args:
            messages: Ordered conversation history (same shape as generate).
            tool_schemas: OpenAI ``tools=`` payload (caller allowlists).
            executor: ``await executor(name, arguments)`` returning either
                plain text (tool output, loop continues) or a mapping
                ``{"text": str, "stop": bool}`` — ``stop=True`` ends the loop
                immediately (policy approval/denial path).

        Persistent tool-calls stop after 2 rounds with best-effort text; an
        approval stop with empty text falls back to a user-facing message.
        Never raises for policy outcomes — those arrive via ``stop``.
        """
        history: list[dict[str, Any]] = [dict(m) for m in messages]
        last_content = ""
        has_tool_results = False
        tool_texts: list[str] = []
        for _ in range(_MAX_TOOL_ROUNDS):
            completion = await self._client.chat.completions.create(
                model=self._model,
                messages=history,  # type: ignore[arg-type]
                tools=tool_schemas,  # type: ignore[arg-type]
            )
            message = _assistant_message(completion)
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            # D4 follow-up: some providers (observed: Groq) emit tool calls
            # with an EMPTY function name. Those cannot execute and must not
            # be echoed back verbatim (Groq 400 "Tools should have a name!").
            valid_calls: list[tuple[str, str, dict[str, Any]]] = []
            for tc in tool_calls:
                call_id, name, args = _parse_tool_call(tc)
                if not name:
                    logger.warning(
                        "llm_tool_call_dropped provider=%s reason=empty_name",
                        self.name,
                    )
                    continue
                if not call_id:
                    call_id = _new_call_id()
                    logger.debug(
                        "llm_tool_call_id_synthesized provider=%s name=%s",
                        self.name,
                        name,
                    )
                valid_calls.append((call_id, name, args))
            if not valid_calls:
                if isinstance(content, str) and content.strip():
                    return content
                return self._extract_content(completion)
            if isinstance(content, str) and content.strip():
                last_content = content
            history.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                        for call_id, name, args in valid_calls
                    ],
                }
            )
            stop_text: str | None = None
            for call_id, name, args in valid_calls:
                result = await executor(name, args)
                text, stop = _normalize_executor_result(result)
                tool_texts.append(text)
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        # Quarantine (Phase D): retrieved content is
                        # untrusted data — delimit it so the model treats
                        # it as data, never as instructions.
                        # Escape: a page containing the literal delimiters
                        # would break out of the wrapper, so neutralise
                        # them first with bracketed forms ([retrieved] /
                        # [/retrieved]) — inert (no angle brackets) and
                        # still readable. Done here so search, fetch, and
                        # policy-stop messages are all covered.
                        "content": f"<retrieved>\n{_escape_retrieved(text)}\n</retrieved>",
                    }
                )
                if stop:
                    stop_text = text or _APPROVAL_FALLBACK
                    break
            has_tool_results = True
            if stop_text is not None:
                return stop_text
        if last_content.strip():
            return last_content
        if has_tool_results:
            # No model compose round: any further provider call is
            # unreliable here (no-tools call 400s on orphaned tool
            # messages; tools+tool_choice:none 400s on tool_use_failed),
            # so build the reply deterministically from collected outputs.
            digest = _build_tool_digest(tool_texts)
            if digest:
                return digest
        return (
            "I looked that up but couldn't put together an answer. "
            "Please try again."
        )

    async def aclose(self) -> None:
        await self._client.close()


__all__ = ["OpenAICompatibleProvider"]
