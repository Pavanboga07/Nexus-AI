"""Tests for OpenAICompatibleProvider retry-on-empty-response behaviour.

Uses a stub client (no network) to reproduce the transient "200 with empty
choices" flake seen from some OpenAI-compatible providers (e.g. Groq).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.llm.base import LLMProviderError
from app.llm.openai_adapter import OpenAICompatibleProvider


@dataclass
class _Msg:
    content: str


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Completion:
    choices: list[_Choice] = field(default_factory=list)


class _StubCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        if not self._responses:
            # Fail loudly instead of IndexError: a retry budget larger than the
            # scripted responses is a TEST bug, and that should be obvious
            # rather than surfacing as a confusing provider error.
            raise AssertionError(
                f"stub provider called {self.calls} time(s) but only "
                f"{self.calls - 1} response(s) were scripted"
            )
        return self._responses.pop(0)


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubClient:
    def __init__(self, chat: _StubChat) -> None:
        self.chat = chat

    async def close(self) -> None:
        return None


def _make_provider(responses: list[Any]) -> tuple[OpenAICompatibleProvider, _StubCompletions]:
    provider = OpenAICompatibleProvider(api_key="sk-test", model="stub-model")
    completions = _StubCompletions(responses)
    provider._client = _StubClient(_StubChat(completions))  # type: ignore[assignment]
    return provider, completions


async def test_empty_choices_then_success_retries_once() -> None:
    provider, completions = _make_provider(
        [
            _Completion(choices=[]),  # transient flake
            _Completion(choices=[_Choice(_Msg("Hello Boss"))]),
        ]
    )

    reply = await provider.generate([{"role": "user", "content": "hi"}])

    assert reply == "Hello Boss"
    assert completions.calls == 2


async def test_persistently_empty_choices_raises_after_retries() -> None:
    """A persistently empty response exhausts the retry budget, then raises.

    M7 raised the budget from "one retry for an empty response" to a general
    transient-failure retry, so this now makes three attempts rather than two.
    """
    provider, completions = _make_provider(
        [
            _Completion(choices=[]),
            _Completion(choices=[]),
            _Completion(choices=[]),
        ]
    )
    provider._retry_base_seconds = 0  # keep the test fast

    with pytest.raises(LLMProviderError, match="no choices"):
        await provider.generate([{"role": "user", "content": "hi"}])

    assert completions.calls == 3


async def test_blank_content_also_retried() -> None:
    provider, completions = _make_provider(
        [
            _Completion(choices=[_Choice(_Msg("   "))]),  # blank content
            _Completion(choices=[_Choice(_Msg("real answer"))]),
        ]
    )

    reply = await provider.generate([{"role": "user", "content": "hi"}])

    assert reply == "real answer"
    assert completions.calls == 2


async def test_success_first_try_no_retry() -> None:
    provider, completions = _make_provider(
        [_Completion(choices=[_Choice(_Msg("immediate"))])]
    )

    reply = await provider.generate([{"role": "user", "content": "hi"}])

    assert reply == "immediate"
    assert completions.calls == 1


# --- M7: transient provider failures are retried -----------------------------


async def test_transient_5xx_is_retried_then_succeeds() -> None:
    """A 500 was previously terminal for the user's whole turn."""
    from openai import APIStatusError

    provider, completions = _make_provider([])
    provider._retry_base_seconds = 0

    async def _create(**kwargs: Any) -> Any:
        completions.calls += 1
        if completions.calls == 1:
            raise APIStatusError(
                "server error", response=_FakeResponse(503), body=None
            )
        return _Completion(choices=[_Choice(_Msg("recovered"))])

    completions.create = _create  # type: ignore[method-assign]

    reply = await provider.generate([{"role": "user", "content": "hi"}])

    assert reply == "recovered"
    assert completions.calls == 2


async def test_rate_limit_is_retried_then_succeeds() -> None:
    """429 is explicitly a 'try again later' signal, so it is retryable."""
    from openai import RateLimitError

    provider, completions = _make_provider([])
    provider._retry_base_seconds = 0

    async def _create(**kwargs: Any) -> Any:
        completions.calls += 1
        if completions.calls == 1:
            raise RateLimitError(
                "rate limited", response=_FakeResponse(429), body=None
            )
        return _Completion(choices=[_Choice(_Msg("after rate limit"))])

    completions.create = _create  # type: ignore[method-assign]

    reply = await provider.generate([{"role": "user", "content": "hi"}])
    assert reply == "after rate limit"
    assert completions.calls == 2


async def test_client_error_is_not_retried() -> None:
    """A 400 will fail identically every time; retrying only adds latency."""
    from openai import BadRequestError

    provider, completions = _make_provider([])
    provider._retry_base_seconds = 0

    async def _create(**kwargs: Any) -> Any:
        completions.calls += 1
        raise BadRequestError(
            "bad request", response=_FakeResponse(400), body=None
        )

    completions.create = _create  # type: ignore[method-assign]

    with pytest.raises(LLMProviderError):
        await provider.generate([{"role": "user", "content": "hi"}])
    assert completions.calls == 1, "a 4xx must not be retried"


async def test_auth_error_is_never_retried() -> None:
    """Retrying a rejected key risks lockout and cannot succeed."""
    from openai import AuthenticationError

    from app.llm.base import LLMConfigurationError

    provider, completions = _make_provider([])
    provider._retry_base_seconds = 0

    async def _create(**kwargs: Any) -> Any:
        completions.calls += 1
        raise AuthenticationError(
            "bad key", response=_FakeResponse(401), body=None
        )

    completions.create = _create  # type: ignore[method-assign]

    with pytest.raises(LLMConfigurationError):
        await provider.generate([{"role": "user", "content": "hi"}])
    assert completions.calls == 1


class _FakeResponse:
    """Minimal stand-in for an httpx.Response, which the SDK errors require."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.request = None

    def json(self) -> dict:
        return {}
