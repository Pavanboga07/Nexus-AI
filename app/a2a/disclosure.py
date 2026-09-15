"""Minimum disclosure boundary between memory and A2A responses (spec §20).

The A2A handler NEVER gets raw MemoryManager results. It asks this module to
build an ``A2ADisclosure`` from policy-approved memories, shaped by the
policy's ``disclosure_scope``:

    none     -> empty disclosure (nothing crosses the boundary)
    category -> "owner has availability information" - the category exists,
                no content
    summary  -> at most N short, content-free summary lines describing WHAT
                KIND of information is held (e.g. "1 memory about meeting
                preferences"), never the memory text itself
    exact    -> the exact memory contents, capped in count and size

This is deliberately conservative: "summary" never leaks memory text. A
future version may generate natural-language summaries through the LLM
under a stricter policy; for Part 6 the boundary is mechanical and
auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.a2a.models import TaskStatus
from app.policy.models import DisclosureScope

#: Hard caps regardless of scope: even "exact" disclosures are bounded.
MAX_EXACT_ITEMS = 3
MAX_ITEM_CHARS = 500


@dataclass(frozen=True)
class A2ADisclosure:
    """Only explicitly authorized data, already scope-shaped."""

    task_status: TaskStatus
    #: category name when scope=category; None otherwise
    data_category: str | None = None
    #: summary lines when scope=summary
    summary: list[str] = field(default_factory=list)
    #: exact memory contents when scope=exact
    content: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"status": self.task_status.value}
        if self.data_category is not None:
            payload["category_available"] = True
        if self.summary:
            payload["summary"] = self.summary
        if self.content:
            payload["memories"] = self.content
        return payload


def build_disclosure(
    *,
    scope: DisclosureScope,
    data_category: str,
    memories: list[str],
    task_status: TaskStatus = TaskStatus.COMPLETED,
) -> A2ADisclosure:
    """Shape raw (already policy-approved) memory contents by scope."""
    if scope is DisclosureScope.NONE:
        return A2ADisclosure(task_status=task_status)

    if scope is DisclosureScope.CATEGORY:
        # Acknowledge the category exists; no content crosses the boundary.
        return A2ADisclosure(
            task_status=task_status, data_category=data_category
        )

    if scope is DisclosureScope.SUMMARY:
        # Describe the KIND of information held, not its content.
        summary = []
        if memories:
            summary.append(
                f"{len(memories)} memor{'y' if len(memories) == 1 else 'ies'} "
                f"regarding {data_category}"
            )
        return A2ADisclosure(task_status=task_status, summary=summary)

    # DisclosureScope.EXACT: bounded exact contents.
    capped = [m[:MAX_ITEM_CHARS] for m in memories[:MAX_EXACT_ITEMS]]
    return A2ADisclosure(task_status=task_status, content=capped)


__all__ = ["A2ADisclosure", "MAX_EXACT_ITEMS", "MAX_ITEM_CHARS", "build_disclosure"]
