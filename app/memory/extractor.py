"""LLM-based memory extraction from conversation turns.

Flow (Part 2 spec §11):

    conversation turn
        -> LLM extraction (structured JSON, mocked in tests)
        -> validation (clamp, type-check, drop junk)
        -> candidate memories

The extractor NEVER writes to the database - persistence is the
MemoryManager's job, after deduplication.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.llm.base import LLMProvider

logger = logging.getLogger("nexus.memory.extractor")

VALID_MEMORY_TYPES = {"semantic", "episodic", "relationship"}

EXTRACTION_PROMPT = """You are the memory extraction component of Nexus, a personal AI assistant.

Given the latest exchange in a conversation, extract facts worth remembering
long-term about the user. The user's name is Boss.

Return ONLY a JSON object with this exact shape:
{
  "memories": [
    {
      "type": "semantic" | "episodic" | "relationship",
      "content": "<one-sentence third-person statement>",
      "importance": <number 0.0-1.0>,
      "confidence": <number 0.0-1.0>
    }
  ]
}

Types:
- semantic: stable facts and preferences (e.g. "Boss prefers meetings after 6 PM.")
- episodic: events that happened (e.g. "Boss and Rahul discussed the Nexus architecture.")
- relationship: people and their relation to Boss (e.g. "Rahul is Boss's college project partner.")

Rules:
- Extract ONLY durable information; greetings, questions about facts, and chit-chat produce NO memories.
- Write each memory as a single self-contained third-person sentence.
- Return {"memories": []} when nothing is worth remembering.
- Output raw JSON only, no markdown fences, no commentary."""


class CandidateMemory(BaseModel):
    """One validated candidate memory (post-validation).

    The LLM emits ``type``; internally we use ``memory_type`` (matches the
    DB column), so accept both via alias.
    """

    model_config = ConfigDict(populate_by_name=True)

    memory_type: str = Field(
        alias="type", pattern="^(semantic|episodic|relationship)$"
    )
    content: str = Field(min_length=3, max_length=1000)
    # Accept any numeric value here and clamp in the validator: LLMs
    # occasionally emit 7.5 or -1, and the spec says clamp, not reject.
    importance: float = 0.5
    confidence: float = 0.8

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("importance", "confidence", mode="before")
    @classmethod
    def _clamp(cls, value: object) -> float:
        """Never trust the LLM's numbers blindly - coerce and clamp."""
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.5
        if number != number:  # NaN check
            return 0.5
        return max(0.0, min(1.0, number))


class ExtractionResult(BaseModel):
    memories: list[CandidateMemory] = Field(default_factory=list)


@dataclass
class MemoryExtractor:
    """Turns a conversation turn into validated candidate memories."""

    provider: LLMProvider

    _JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

    async def extract(
        self, user_message: str, assistant_message: str
    ) -> list[CandidateMemory]:
        """Extract candidates from one user/assistant exchange.

        Returns [] on any extraction failure - memory extraction must never
        break the chat flow.
        """
        prompt = (
            f"{EXTRACTION_PROMPT}\n\n"
            f"User said:\n{user_message}\n\n"
            f"Assistant replied:\n{assistant_message}"
        )
        try:
            raw = await self.provider.generate(
                [{"role": "user", "content": prompt}]
            )
        except Exception:
            logger.warning(
                "memory_extraction_failed provider_error=true", exc_info=True
            )
            return []

        candidates = self._parse(raw)
        logger.info(
            "memory_extracted candidates=%d", len(candidates)
        )
        return candidates

    def _parse(self, raw: str) -> list[CandidateMemory]:
        """Parse and validate the LLM's JSON, tolerating markdown fences."""
        text = raw.strip()
        fence_match = self._JSON_FENCE.search(text)
        if fence_match:
            text = fence_match.group(1)

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("memory_extraction_parse_error chars=%d", len(text))
            return []

        try:
            result = ExtractionResult.model_validate(payload)
        except ValidationError:
            logger.warning("memory_extraction_validation_error")
            return []

        return result.memories


__all__ = [
    "CandidateMemory",
    "ExtractionResult",
    "MemoryExtractor",
    "VALID_MEMORY_TYPES",
]
