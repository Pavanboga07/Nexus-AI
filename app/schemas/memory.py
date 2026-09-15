"""Pydantic v2 schemas for the memory API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MemoryTypeLiteral = Literal["semantic", "episodic", "relationship"]


class MemoryOut(BaseModel):
    id: str
    memory_type: MemoryTypeLiteral
    content: str
    importance: float
    confidence: float
    created_at: str | None = None
    last_accessed_at: str | None = None
    metadata: dict = Field(default_factory=dict)


class MemoryListResponse(BaseModel):
    memories: list[MemoryOut]
    total: int


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=50)
    memory_types: list[MemoryTypeLiteral] | None = None


class MemorySearchResultOut(BaseModel):
    memory: MemoryOut
    similarity: float


class MemorySearchResponse(BaseModel):
    results: list[MemorySearchResultOut]
    total: int


class MemoryDeleteResponse(BaseModel):
    deleted: bool
    id: str


__all__ = [
    "MemoryDeleteResponse",
    "MemoryListResponse",
    "MemoryOut",
    "MemorySearchRequest",
    "MemorySearchResponse",
    "MemorySearchResultOut",
    "MemoryTypeLiteral",
]
