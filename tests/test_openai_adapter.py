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


# --- D4: bounded tool-calling loop --------------------------------------------


@dataclass
class _Func:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Func


@dataclass
class _ToolMsg:
    content: str | None
    tool_calls: list[_ToolCall] | None = None


@dataclass
class _ToolChoice:
    message: _ToolMsg


@dataclass
class _ToolCompletion:
    choices: list[_ToolChoice] = field(default_factory=list)


def _tool_call_completion(
    name: str, arguments: dict[str, Any], content: str | None = None
) -> _ToolCompletion:
    import json

    return _ToolCompletion(
        choices=[
            _ToolChoice(
                _ToolMsg(
                    content=content,
                    tool_calls=[
                        _ToolCall(
                            id="call_1",
                            function=_Func(
                                name=name, arguments=json.dumps(arguments)
                            ),
                        )
                    ],
                )
            )
        ]
    )


def _text_completion(text: str) -> _ToolCompletion:
    return _ToolCompletion(choices=[_ToolChoice(_ToolMsg(content=text))])


class _ToolsStubCompletions:
    """Stub recording create() kwargs (to assert tools= passthrough)."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.kwargs_history: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.kwargs_history.append(kwargs)
        assert self._responses, (
            f"stub called {self.calls} time(s) but no responses remain"
        )
        return self._responses.pop(0)


def _tools_provider(
    responses: list[Any],
) -> tuple[OpenAICompatibleProvider, _ToolsStubCompletions]:
    provider = OpenAICompatibleProvider(api_key="sk-test", model="stub-model")
    completions = _ToolsStubCompletions(responses)
    provider._client = _StubClient(_StubChat(completions))  # type: ignore[assignment]
    return provider, completions


def _search_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object"},
            },
        }
    ]


async def test_generate_with_tools_runs_executor_and_returns_final() -> None:
    provider, completions = _tools_provider(
        [
            _tool_call_completion(
                "web_search", {"query": "nexus ai", "count": 5}
            ),
            _text_completion("cited answer"),
        ]
    )
    seen: list[tuple[str, dict[str, Any]]] = []

    async def _executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        seen.append((name, args))
        return {"text": "tool output", "stop": False}

    reply = await provider.generate_with_tools(
        [{"role": "user", "content": "hi"}], _search_schemas(), _executor
    )

    assert reply == "cited answer"
    assert seen == [("web_search", {"query": "nexus ai", "count": 5})]
    assert completions.calls == 2
    assert completions.kwargs_history[0]["tools"] == _search_schemas()


async def test_generate_with_tools_stops_after_two_rounds() -> None:
    """A model that always tool-calls must not loop forever."""
    provider, completions = _tools_provider(
        [
            _tool_call_completion("web_search", {"query": "q"}, content="partial"),
            _tool_call_completion("web_search", {"query": "q"}, content="partial"),
            _tool_call_completion("web_search", {"query": "q"}, content="never"),
        ]
    )

    async def _executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"text": "tool output", "stop": False}

    reply = await provider.generate_with_tools(
        [{"role": "user", "content": "hi"}], _search_schemas(), _executor
    )

    assert completions.calls == 2
    assert reply == "partial"


async def test_generate_with_tools_stops_on_approval() -> None:
    provider, completions = _tools_provider(
        [_tool_call_completion("web_search", {"query": "q"})]
    )

    async def _executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"text": "That needs your approval", "stop": True}

    reply = await provider.generate_with_tools(
        [{"role": "user", "content": "hi"}], _search_schemas(), _executor
    )

    assert reply == "That needs your approval"
    assert completions.calls == 1


async def test_generate_with_tools_stops_sibling_calls_on_first_stop() -> None:
    import json

    provider, completions = _tools_provider(
        [
            _ToolCompletion(
                choices=[
                    _ToolChoice(
                        _ToolMsg(
                            content=None,
                            tool_calls=[
                                _ToolCall(
                                    id="call_1",
                                    function=_Func(
                                        name="web_search",
                                        arguments=json.dumps({"query": "q"}),
                                    ),
                                ),
                                _ToolCall(
                                    id="call_2",
                                    function=_Func(
                                        name="web_search",
                                        arguments=json.dumps({"query": "q2"}),
                                    ),
                                ),
                            ],
                        )
                    )
                ]
            )
        ]
    )
    seen: list[tuple[str, dict[str, Any]]] = []

    async def _executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        seen.append((name, args))
        return {"text": "That needs your approval", "stop": True}

    reply = await provider.generate_with_tools(
        [{"role": "user", "content": "hi"}], _search_schemas(), _executor
    )

    assert reply == "That needs your approval"
    assert len(seen) == 1
    assert completions.calls == 1


async def test_generate_with_tools_exhausted_composes_final_answer() -> None:
    """Search+fetch exhausts both rounds → one final compose call (no tools)."""
    provider, completions = _tools_provider(
        [
            _tool_call_completion("web_search", {"query": "q"}),
            _tool_call_completion("web_fetch", {"url": "https://example.com"}),
            _text_completion("composed answer"),
        ]
    )

    async def _executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"text": "tool output", "stop": False}

    reply = await provider.generate_with_tools(
        [{"role": "user", "content": "hi"}], _search_schemas(), _executor
    )

    assert reply == "composed answer"
    assert completions.calls == 3
    assert "tools" not in completions.kwargs_history[2]


async def test_generate_with_tools_exhausted_compose_empty_keeps_fallback() -> None:
    """Persistent tool-calls + empty compose call → fallback text preserved."""
    provider, completions = _tools_provider(
        [
            _tool_call_completion("web_search", {"query": "q"}),
            _tool_call_completion("web_fetch", {"url": "https://example.com"}),
            _text_completion("   "),
        ]
    )

    async def _executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"text": "tool output", "stop": False}

    reply = await provider.generate_with_tools(
        [{"role": "user", "content": "hi"}], _search_schemas(), _executor
    )

    assert reply == (
        "I looked that up but couldn't put together an answer. "
        "Please try again."
    )
    assert completions.calls == 3
