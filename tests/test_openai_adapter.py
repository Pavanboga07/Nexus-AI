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


async def test_persistently_empty_choices_raises_after_retry() -> None:
    provider, completions = _make_provider(
        [
            _Completion(choices=[]),
            _Completion(choices=[]),
        ]
    )
    provider._empty_response_backoff = 0  # keep the test fast

    with pytest.raises(LLMProviderError, match="no choices"):
        await provider.generate([{"role": "user", "content": "hi"}])

    assert completions.calls == 2


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
