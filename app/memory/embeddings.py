"""Embedding provider abstraction.

Separate from the chat ``LLMProvider`` on purpose: reasoning and vectorising
are different jobs and may use different vendors/models (see Part 2 spec §27).
"""

from __future__ import annotations

import hashlib
import logging
import math
from abc import ABC, abstractmethod

logger = logging.getLogger("nexus.memory.embeddings")


class EmbeddingError(Exception):
    """Raised when an embedding provider fails."""


class EmbeddingProvider(ABC):
    """Converts text into a fixed-dimension float vector."""

    #: Provider name for logs.
    name: str = "embedding"
    #: Vector dimensionality this provider produces.
    dimensions: int = 0

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``.

        Raises:
            EmbeddingError: on any provider failure.
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embeddings via an OpenAI-compatible ``/embeddings`` endpoint.

    Note: not every OpenAI-compatible chat endpoint offers embeddings (Groq
    currently does not) - point ``NEXUS_LLM_BASE_URL`` at real OpenAI or
    another provider that does, or use the local embedder.
    """

    name = "openai-embeddings"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        from openai import AsyncOpenAI  # imported lazily: only this provider needs the SDK

        self._model = model
        self.dimensions = dimensions
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout
        )

    async def embed(self, text: str) -> list[float]:
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=text,
                dimensions=self.dimensions,
            )
        except Exception as exc:  # noqa: BLE001 - normalise vendor errors
            raise EmbeddingError(
                f"Embedding provider failed: {type(exc).__name__}"
            ) from exc
        try:
            data = response.data[0].embedding
        except (IndexError, AttributeError, TypeError) as exc:
            raise EmbeddingError("Embedding provider returned no vector.") from exc
        if len(data) != self.dimensions:
            raise EmbeddingError(
                f"Embedding dimension mismatch: expected {self.dimensions}, "
                f"got {len(data)}."
            )
        return list(data)

    async def aclose(self) -> None:
        await self._client.close()


class LocalHashEmbeddingProvider(EmbeddingProvider):
    """Deterministic hash-based embedding for development and tests.

    No network, no cost, stable across processes. It has NO semantic
    understanding: two texts are similar only if they share character
    n-grams, so real semantic search needs the OpenAI provider. Sufficient
    for deterministic test vectors and for the dedup near-match test because
    we control the exact strings.

    Method: 3-gram character bag-of-features hashed into ``dimensions``
    buckets, L2-normalised.
    """

    name = "local-hash"
    dimensions = 256

    def __init__(self) -> None:
        self._ngram = 3

    async def embed(self, text: str) -> list[float]:
        return self.embed_sync(text)

    def embed_sync(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        normalized = " ".join(text.lower().split())
        if not normalized:
            return vector
        grams = normalized if len(normalized) <= self._ngram else (
            normalized[i : i + self._ngram]
            for i in range(len(normalized) - self._ngram + 1)
        )
        for gram in grams:
            # blake2b, not md5: same speed for bucketing, but md5 is flagged
            # by every security scanner and raises on FIPS-mode systems.
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector


class UnconfiguredEmbeddingProvider(EmbeddingProvider):
    """Placeholder when embeddings are unavailable; always errors."""

    name = "unconfigured"
    dimensions = 0

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def embed(self, text: str) -> list[float]:
        raise EmbeddingError(self._reason)


def build_embedding_provider(
    *,
    provider: str,
    api_key: str | None,
    model: str,
    dimensions: int,
    base_url: str | None = None,
    timeout: float = 30.0,
) -> EmbeddingProvider:
    """Factory mirroring ``app.llm.build_provider``."""
    if provider == "local":
        return LocalHashEmbeddingProvider()
    if provider == "openai":
        if not api_key:
            return UnconfiguredEmbeddingProvider(
                "NEXUS_EMBEDDING_PROVIDER=openai but no API key is set. "
                "Set NEXUS_LLM_API_KEY or switch to NEXUS_EMBEDDING_PROVIDER=local."
            )
        return OpenAIEmbeddingProvider(
            api_key=api_key,
            model=model,
            dimensions=dimensions,
            base_url=base_url,
            timeout=timeout,
        )
    raise ValueError(f"Unknown embedding provider: {provider!r}")


__all__ = [
    "EmbeddingError",
    "EmbeddingProvider",
    "LocalHashEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "UnconfiguredEmbeddingProvider",
    "build_embedding_provider",
]
