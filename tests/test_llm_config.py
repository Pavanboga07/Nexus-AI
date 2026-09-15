"""Tests for provider-neutral LLM configuration (Option B).

Covers: NEXUS_LLM_* settings, legacy OPENAI_* fallbacks, extra-header JSON
parsing, and provider-name detection from the base URL.

All Settings instances pass ``_env_file=None`` so tests are hermetic and never
pick up the developer's real ``.env``.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.llm import (
    OpenAICompatibleProvider,
    UnconfiguredProvider,
    build_provider,
)
from app.llm.openai_adapter import _provider_name_for


# --- Settings resolution ----------------------------------------------------


def test_neutral_names_take_priority_over_legacy() -> None:
    settings = Settings(
        _env_file=None,
        nexus_llm_api_key="neutral-key",
        nexus_llm_model="neutral-model",
        nexus_llm_base_url="https://neutral.example/v1",
        openai_api_key="legacy-key",
        openai_model="legacy-model",
    )

    assert settings.llm_api_key == "neutral-key"
    assert settings.llm_model == "neutral-model"
    assert settings.llm_base_url == "https://neutral.example/v1"


def test_legacy_openai_names_still_work() -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="legacy-key",
        openai_model="legacy-model",
        openai_base_url="https://legacy.example/v1",
    )

    assert settings.llm_api_key == "legacy-key"
    assert settings.llm_model == "legacy-model"
    assert settings.llm_base_url == "https://legacy.example/v1"
    assert settings.llm_configured is True


def test_model_defaults_when_unset() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_api_key is None
    assert settings.llm_configured is False


def test_blank_strings_treated_as_unset() -> None:
    settings = Settings(
        _env_file=None, nexus_llm_api_key="  ", nexus_llm_model=""
    )

    assert settings.llm_api_key is None
    assert settings.llm_model == "gpt-4o-mini"


# --- Extra header parsing ---------------------------------------------------


def test_extra_headers_from_json_string() -> None:
    settings = Settings(
        _env_file=None,
        nexus_llm_extra_headers=(
            '{"HTTP-Referer": "https://x.example", "X-Title": "Nexus"}'
        ),
    )
    assert settings.llm_extra_headers == {
        "HTTP-Referer": "https://x.example",
        "X-Title": "Nexus",
    }


def test_extra_headers_from_dict() -> None:
    settings = Settings(
        _env_file=None, nexus_llm_extra_headers={"X-Title": "Nexus"}
    )
    assert settings.llm_extra_headers == {"X-Title": "Nexus"}


def test_extra_headers_unset_is_empty() -> None:
    assert Settings(_env_file=None).llm_extra_headers == {}
    assert (
        Settings(_env_file=None, nexus_llm_extra_headers="").llm_extra_headers
        == {}
    )


def test_extra_headers_invalid_json_rejected() -> None:
    with pytest.raises(Exception):
        Settings(_env_file=None, nexus_llm_extra_headers="not json")


def test_extra_headers_non_object_json_rejected() -> None:
    with pytest.raises(Exception):
        Settings(_env_file=None, nexus_llm_extra_headers='["a", "b"]')


# --- Provider name detection ------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (None, "openai"),
        ("https://api.openai.com/v1", "openai"),
        ("https://openrouter.ai/api/v1", "openrouter"),
        ("https://api.groq.com/openai/v1", "groq"),
        ("http://localhost:11434/v1", "ollama"),
        ("http://localhost:1234/v1", "lmstudio"),
        ("https://api.deepseek.com/v1", "deepseek"),
        ("https://my-company.example/v1", "my-company.example"),
    ],
)
def test_provider_name_detection(base_url: str | None, expected: str) -> None:
    assert _provider_name_for(base_url) == expected


def test_built_provider_reports_endpoint_name() -> None:
    settings = Settings(
        _env_file=None,
        nexus_llm_api_key="gsk-test",
        nexus_llm_base_url="https://api.groq.com/openai/v1",
        nexus_llm_model="openai/gpt-oss-20b",
    )

    provider = build_provider(settings)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "groq"
    assert provider.model == "openai/gpt-oss-20b"


def test_built_provider_without_key_is_unconfigured() -> None:
    provider = build_provider(Settings(_env_file=None))
    assert isinstance(provider, UnconfiguredProvider)


def test_built_provider_default_endpoint_is_named_openai() -> None:
    settings = Settings(_env_file=None, nexus_llm_api_key="sk-test")

    provider = build_provider(settings)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "openai"


def test_provider_accepts_extra_headers() -> None:
    provider = OpenAICompatibleProvider(
        api_key="sk-test",
        model="m",
        base_url="https://openrouter.ai/api/v1",
        extra_headers={"HTTP-Referer": "https://x.example"},
    )

    assert provider.name == "openrouter"
    # The SDK merges default headers into its custom transport headers.
    assert provider._client._custom_headers["HTTP-Referer"] == "https://x.example"
