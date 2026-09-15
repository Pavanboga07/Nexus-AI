"""Application configuration.

All runtime configuration is sourced from environment variables (optionally
loaded from a local ``.env`` file) and validated by Pydantic. Nothing in the
codebase should read ``os.environ`` directly - import :func:`get_settings`
instead so configuration stays in one place.

LLM providers use provider-neutral names (``NEXUS_LLM_*``). The legacy
``OPENAI_*`` names are still honoured as fallbacks so existing ``.env`` files
keep working.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYSTEM_PROMPT = """You are Nexus, a personal AI assistant.

Your role is to assist your owner with reasoning, conversation, planning, and
everyday tasks.

You should be helpful, concise, honest about uncertainty, and never claim to
have performed an action that you did not perform.

You currently have no external tools and no persistent memory."""

DEFAULT_LLM_MODEL = "gpt-4o-mini"


class Settings(BaseSettings):
    """Validated runtime settings.

    Field names map to environment variables case-insensitively, so
    ``nexus_llm_api_key`` is populated from ``NEXUS_LLM_API_KEY``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM provider (provider-neutral) ----------------------------------
    nexus_llm_api_key: str | None = Field(default=None)
    nexus_llm_base_url: str | None = Field(default=None)
    nexus_llm_model: str | None = Field(default=None)
    # JSON object of extra HTTP headers, e.g. for OpenRouter attribution:
    #   NEXUS_LLM_EXTRA_HEADERS={"HTTP-Referer": "https://myapp", "X-Title": "Nexus"}
    nexus_llm_extra_headers: dict[str, str] | None = Field(default=None)

    # --- Legacy OpenAI aliases (fallbacks, kept for compatibility) --------
    openai_api_key: str | None = Field(default=None)
    openai_model: str | None = Field(default=None)
    openai_base_url: str | None = Field(default=None)

    # --- Runtime ----------------------------------------------------------
    nexus_env: Literal["development", "staging", "production"] = "development"
    nexus_host: str = "127.0.0.1"
    nexus_port: int = 8000
    nexus_llm_timeout: float = Field(default=60.0, gt=0)
    nexus_max_session_messages: int = Field(default=100, gt=0)
    nexus_log_level: str = "INFO"

    # --- Database (Part 2) ------------------------------------------------
    database_url: str = "postgresql+asyncpg://nexus:nexus@localhost:5433/nexus"
    nexus_db_echo: bool = False

    # --- Embeddings (Part 2) ----------------------------------------------
    # "openai" uses the chat provider's key/endpoint against /embeddings;
    # "local" is a deterministic hash embedder for dev/test (Groq has no
    # embeddings API).
    nexus_embedding_provider: Literal["openai", "local"] = "local"
    nexus_embedding_model: str = "text-embedding-3-small"
    nexus_embedding_dimensions: int = Field(default=1536, ge=2)

    # --- Memory (Part 2) ---------------------------------------------------
    nexus_memory_top_k: int = Field(default=5, ge=1)
    nexus_memory_enabled: bool = True
    # Memories whose cosine similarity to an existing memory exceeds this are
    # treated as duplicates (update instead of insert).
    nexus_memory_dedup_threshold: float = Field(default=0.92, gt=0, le=1)

    # --- Identity (Part 3) ---------------------------------------------------
    # Secret used to encrypt the agent's Ed25519 private key at rest. Generate
    # with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    # Never commit the real value. If an identity exists and this is missing
    # or wrong, startup fails.
    nexus_identity_key: str | None = None

    # --- Tools (Part 5) -------------------------------------------------------
    # Execution boundary for the tool subsystem.
    nexus_tool_timeout_seconds: float = Field(default=10.0, gt=0)
    # Tools returning more than this many bytes fail safely (no truncation).
    nexus_tool_max_result_bytes: int = Field(default=65_536, gt=0)

    # --- A2A (Part 6) -----------------------------------------------------------
    # Inbound messages with a timestamp further in the future than this are
    # rejected. Production deployments should synchronise clocks (NTP).
    nexus_a2a_max_clock_skew_seconds: float = Field(default=30.0, gt=0)
    # Outbound messages expire after this many seconds; receivers reject
    # expired messages.
    nexus_a2a_message_ttl_seconds: float = Field(default=60.0, gt=0)
    # Per-agent inbound rate limit (requests per minute).
    nexus_a2a_rate_limit_per_minute: int = Field(default=60, gt=0)
    # Maximum accepted inbound/outbound message size in bytes.
    nexus_a2a_max_message_bytes: int = Field(default=65_536, gt=0)
    # Outbound HTTP timeout: a remote agent cannot hang the runtime.
    nexus_a2a_timeout_seconds: float = Field(default=10.0, gt=0)
    # Allow http(s)://localhost / loopback / private-range endpoints.
    # Safe default is False (production); local development and tests set
    # this to True for the two-instance loopback demo.
    nexus_a2a_allow_local_endpoints: bool = False

    # --- Discovery (Part 7) -------------------------------------------------------
    # Display name exposed in the agent card. Defaults to "Nexus Agent".
    nexus_agent_display_name: str = "Nexus Agent"
    # Base URL for this agent's A2A endpoint (used in the card).
    # Example: "https://my-agent.example.com" or "http://127.0.0.1:8000"
    nexus_agent_endpoint: str | None = None
    # Purposes this agent supports (exposed in the card).
    nexus_agent_supported_purposes: str = "scheduling,information"
    # Card TTL in seconds (how long a published card is valid).
    nexus_discovery_card_ttl_seconds: int = Field(default=3600, gt=0)
    # HTTP timeout for fetching remote cards.
    nexus_discovery_timeout_seconds: float = Field(default=10.0, gt=0)
    # Maximum remote card response size in bytes.
    nexus_discovery_max_card_bytes: int = Field(default=65_536, gt=0)

    @property
    def agent_supported_purposes_list(self) -> list[str]:
        """Parse comma-separated purposes into a list."""
        return [
            p.strip() for p in self.nexus_agent_supported_purposes.split(",")
            if p.strip()
        ]

    # --- Task Delegation & Negotiation (Part 8) -----------------------------------
    # Maximum allowed negotiation rounds between agents before termination.
    nexus_a2a_max_negotiation_rounds: int = Field(default=3, ge=1, le=10)
    # Task validity TTL in seconds.
    nexus_a2a_task_ttl_seconds: float = Field(default=3600.0, gt=0)

    # --- Proactive Workflows & Orchestration (Part 9) -----------------------------
    # Default TTL for newly created workflows in seconds.
    nexus_workflow_default_ttl_seconds: int = Field(default=3600, gt=0)
    # Maximum execution attempts per workflow step before permanent failure.
    nexus_workflow_max_step_attempts: int = Field(default=3, ge=1, le=10)

    # --- Autonomy & Decision Engine (Part 10) -------------------------------------
    nexus_autonomy_default_mode: str = Field(default="bounded")
    nexus_autonomy_max_steps: int = Field(default=10, ge=1, le=50)
    nexus_autonomy_max_runtime_seconds: int = Field(default=3600, ge=10, le=86400)
    nexus_autonomy_max_remote_tasks: int = Field(default=5, ge=0, le=20)
    nexus_autonomy_max_tool_calls: int = Field(default=10, ge=0, le=50)

    # --- Gateway Relay (Part 11) --------------------------------------------------
    # WebSocket URL of the hosted Nexus Gateway relay
    nexus_gateway_url: str | None = Field(default=None)



    @property
    def local_embedding_dimensions(self) -> int:
        """The local embedder always produces 256 dims regardless of the
        openai-dimension setting."""
        return 256

    @property
    def embedding_dimensions_effective(self) -> int:
        if self.nexus_embedding_provider == "local":
            return self.local_embedding_dimensions
        return self.nexus_embedding_dimensions

    # --- Agent identity ---------------------------------------------------
    # Kept configurable so Part 3 (Agent Identity) can replace it without
    # touching the agent runtime.
    nexus_system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT)

    @field_validator(
        "nexus_llm_api_key",
        "nexus_llm_base_url",
        "nexus_llm_model",
        "openai_api_key",
        "openai_base_url",
        "openai_model",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat empty strings from ``.env`` as "not configured"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("nexus_llm_extra_headers", mode="before")
    @classmethod
    def _parse_extra_headers(cls, value: object) -> object:
        """Accept a JSON object (string or dict) of header names to values."""
        if value is None or value == "":
            return None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "NEXUS_LLM_EXTRA_HEADERS must be a JSON object of "
                    '{"header-name": "value"} pairs.'
                ) from exc
        else:
            parsed = value
        if not isinstance(parsed, dict):
            raise ValueError(
                "NEXUS_LLM_EXTRA_HEADERS must be a JSON object of "
                '{"header-name": "value"} pairs.'
            )
        return {str(k): str(v) for k, v in parsed.items()}

    @field_validator("nexus_log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    # --- Resolved LLM configuration ---------------------------------------
    # NEXUS_LLM_* wins over the legacy OPENAI_* alias.

    @property
    def llm_api_key(self) -> str | None:
        return self.nexus_llm_api_key or self.openai_api_key

    @property
    def llm_base_url(self) -> str | None:
        return self.nexus_llm_base_url or self.openai_base_url

    @property
    def llm_model(self) -> str:
        return self.nexus_llm_model or self.openai_model or DEFAULT_LLM_MODEL

    @property
    def llm_extra_headers(self) -> dict[str, str]:
        if self.nexus_llm_extra_headers:
            return dict(self.nexus_llm_extra_headers)
        return {}

    @property
    def is_development(self) -> bool:
        return self.nexus_env == "development"

    @property
    def llm_configured(self) -> bool:
        """Whether an API key is present for the configured provider."""
        return bool(self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so repeated calls are cheap; tests can call
    ``get_settings.cache_clear()`` to force a reload.
    """
    return Settings()
