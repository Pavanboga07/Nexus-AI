"""Tests for the health endpoint and provider factory defaults."""

from __future__ import annotations

import httpx

from app.config.settings import Settings
from app.llm import UnconfiguredProvider, build_provider


async def test_health_returns_public_minimal_payload(client: httpx.AsyncClient) -> None:
    """`/health` is the public liveness contract.

    It must be reachable without a session (load balancers, orchestrators,
    monitoring) and must NOT disclose which subsystems are configured, since
    that is configuration disclosure to an anonymous caller. The detailed
    report lives at the authenticated /system/status.
    """
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert set(body) == {"status", "version"}, (
        f"public /health leaks detail: {sorted(body)}"
    )


async def test_system_status_reports_subsystem_detail(client: httpx.AsyncClient) -> None:
    """The detailed report is available to a resolved (here: dev) caller."""
    response = await client.get("/system/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["llm_provider"] == "fake"
    assert "environment" in body
    assert "identity" in body


def test_build_provider_without_key_is_unconfigured() -> None:
    """No API key must not crash startup - it yields a placeholder provider."""
    settings = Settings(_env_file=None)

    provider = build_provider(settings)

    assert isinstance(provider, UnconfiguredProvider)
    assert provider.configured is False
    assert settings.llm_configured is False


def test_build_provider_with_key_is_openai() -> None:
    settings = Settings(
        _env_file=None, openai_api_key="sk-test", openai_model="gpt-4o-mini"
    )

    provider = build_provider(settings)

    assert provider.name == "openai"
    assert provider.configured is True
    assert settings.llm_configured is True
