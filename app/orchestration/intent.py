"""Intent resolution from natural language.

Converts conversational user prompts into typed Intent models.
Treats LLM output as untrusted input and strictly validates with Pydantic.
Includes a deterministic rule-based fallback for tests and offline resilience.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.llm.base import LLMProvider, Message
from app.orchestration.schemas import Intent, IntentType

logger = logging.getLogger("nexus.orchestration.intent")

INTENT_SYSTEM_PROMPT = """You are the Nexus Intent Parser.
Your role is to extract the user's intended action into a structured JSON object.

Allowed intent_type values:
- "CHECK_AVAILABILITY": User wants to check someone's schedule or if they are free
- "COORDINATE_MEETING": User wants to find a meeting time between 2 or more people
- "CONTACT_AGENT": User wants to contact or connect with an agent
- "SEND_INFORMATION": User wants to send a message, update, or note to someone
- "REQUEST_INFORMATION": User wants to ask for specific information from someone
- "DELEGATE_TASK": User wants someone/agent to do a task (review doc, summarize, etc.)
- "CONFIRM_ACTION": User says "yes", "confirm", "book it", "accept" to a pending proposal
- "GENERAL_CHAT": Ordinary conversational chat (greetings, general questions)

Output ONLY valid JSON matching this schema:
{
    "goal": "<concise description of what user wants>",
    "intent_type": "<one of the allowed types>",
    "target": "<primary person/agent name or null>",
    "secondary_targets": ["<other person names if multi-agent>"],
    "purpose": "<purpose string for security evaluation, e.g. availability_check, meeting_coordination, status_update>",
    "requested_information": ["<items requested, e.g. availability>"],
    "constraints": {"<time/date/details>": "<value>"},
    "action_payload": {"<extra parameters>": "<value>"}
}
"""


class IntentResolver:
    """Parses natural language requests into structured Intent objects."""

    def __init__(self, llm_provider: LLMProvider | None = None) -> None:
        self._llm = llm_provider

    async def resolve_intent(
        self,
        message: str,
        active_target: str | None = None,
        active_intent_type: str | None = None,
    ) -> Intent:
        """Parse natural language into a validated Intent model."""
        clean_msg = message.strip()

        # 1. Try deterministic parser first for fast path / tests / common expressions
        deterministic = self._rule_based_parse(clean_msg, active_target, active_intent_type)
        if deterministic is not None:
            return deterministic

        # 2. If LLM provider is available, use structured prompt
        if self._llm is not None:
            try:
                intent = await self._llm_parse(clean_msg, active_target)
                if intent is not None:
                    return intent
            except Exception as exc:
                logger.warning("LLM intent parsing failed (%s), falling back to rule-based", exc)

        # 3. Default fallback if neither produced a specialized intent
        return Intent(
            goal=clean_msg,
            intent_type=IntentType.GENERAL_CHAT,
            target=None,
            purpose="chat",
        )

    async def _llm_parse(self, message: str, active_target: str | None) -> Intent | None:
        """Call LLM provider and validate output schema."""
        assert self._llm is not None

        prompt = f"User request: \"{message}\""
        if active_target:
            prompt += f"\nContext: The user previously mentioned or interacted with: {active_target}"

        messages = [
            Message(role="system", content=INTENT_SYSTEM_PROMPT),
            Message(role="user", content=prompt),
        ]

        raw = await self._llm.generate(messages)
        content = raw.strip() if isinstance(raw, str) else getattr(raw, "content", "").strip()

        # Strip markdown json fences if present
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            data = json.loads(content)
            return Intent.model_validate(data)
        except Exception as exc:
            logger.warning("Failed to validate LLM intent output: %s (raw: %s)", exc, content)
            return None

    def _normalize_target(self, raw_target: str) -> str:
        t = raw_target.strip()
        if t.lower().startswith("nexus:ed25519:"):
            return t.lower()
        if t.startswith("@"):
            return "@" + t[1:].lower()
        return t.capitalize()

    def _rule_based_parse(
        self,
        msg: str,
        active_target: str | None = None,
        active_intent_type: str | None = None,
    ) -> Intent | None:
        """Deterministic rule-based intent parsing."""
        low = msg.lower()
        target_pat = r"(nexus:ed25519:[0-9a-f]{32}|@[a-z0-9_.-]+|[a-z0-9_]+)"

        # Confirmations: "book it", "confirm it", "yes", "go ahead"
        if low in {"book it", "confirm it", "confirm", "yes", "yes please", "do it", "go ahead", "i'll take it", "take it"}:
            return Intent(
                goal="Confirm and proceed with proposed action",
                intent_type=IntentType.CONFIRM_ACTION,
                target=active_target,
                purpose="confirmation",
                action_payload={"confirmed": True},
            )

        # Multi-agent coordination: "Ask Rahul and Priya when they're both free"
        multi_match = re.search(r"(?:ask|check)\s+" + target_pat + r"\s+and\s+" + target_pat + r"\s+(?:when|if)\s+(?:they're|they are|both)", low)
        if multi_match:
            p1 = self._normalize_target(multi_match.group(1))
            p2 = self._normalize_target(multi_match.group(2))
            return Intent(
                goal=msg,
                intent_type=IntentType.COORDINATE_MEETING,
                target=p1,
                secondary_targets=[p2],
                purpose="meeting_coordination",
                requested_information=["availability"],
            )

        # Alternative time / negotiation:
        # "Tell Rahul 8 PM works instead", "Ask him if 8 PM works instead"
        if ("works instead" in low or "works for me" in low or "can we do" in low or "instead" in low):
            target_match = re.search(r"(?:tell|ask|suggest to)\s+" + target_pat, low)
            target_name = self._normalize_target(target_match.group(1)) if target_match else active_target
            time_match = re.search(r"([0-9]+(?:\s*[ap]m|:00)?)", low)
            proposed = time_match.group(1) if time_match else None
            return Intent(
                goal=msg,
                intent_type=IntentType.NEGOTIATE,
                target=target_name,
                purpose="meeting_negotiation",
                constraints={"proposed_time": proposed} if proposed else {},
            )

        # Availability checks:
        # "Ask Rahul if he's free tomorrow after 6 PM"
        # "Ask nexus:ed25519:... if he is free tomorrow"
        # "Ask @rahul if he is free tomorrow after 6 PM"
        # "See if Rahul can meet Saturday evening"
        # "Is Rahul free tomorrow?"
        avail_match = re.search(
            r"(?:ask|see if|check if|is)\s+" + target_pat + r"\s+(?:if\s+(?:he's|she's|they're|he\s+is|she\s+is|they\s+are)|is|can\s+meet|free|available)",
            low,
        )
        if avail_match:
            target_name = self._normalize_target(avail_match.group(1))
            constraints: dict[str, Any] = {}
            if "tomorrow" in low:
                constraints["date"] = "tomorrow"
            elif "saturday" in low:
                constraints["date"] = "Saturday"
            elif "tonight" in low:
                constraints["date"] = "tonight"
            elif "today" in low:
                constraints["date"] = "today"

            # Check time
            time_match = re.search(r"(?:after|at)\s+([0-9]+(?:\s*[ap]m|:00)?)", low)
            if time_match:
                constraints["time"] = time_match.group(1)

            return Intent(
                goal=msg,
                intent_type=IntentType.CHECK_AVAILABILITY,
                target=target_name,
                purpose="availability_check",
                requested_information=["availability"],
                constraints=constraints,
            )

        # Coordinate meeting: "Find a time when Rahul and I can meet this week"
        meet_match = re.search(r"(?:coordinate|find a time|schedule|plan a meeting)\s+(?:with\s+)?" + target_pat, low)
        if meet_match:
            target_name = self._normalize_target(meet_match.group(1))
            return Intent(
                goal=msg,
                intent_type=IntentType.COORDINATE_MEETING,
                target=target_name,
                purpose="meeting_coordination",
                requested_information=["availability"],
            )

        # Send info: "Tell Priya I'll be 15 minutes late"
        tell_match = re.search(r"(?:tell|inform|notify|message)\s+" + target_pat + r"\s+(.*)", low)
        if tell_match:
            target_name = self._normalize_target(tell_match.group(1))
            content = tell_match.group(2)
            return Intent(
                goal=msg,
                intent_type=IntentType.SEND_INFORMATION,
                target=target_name,
                purpose="notification",
                action_payload={"message": content},
            )

        # Delegate task: "Ask Rahul to review my project"
        delegate_match = re.search(r"ask\s+" + target_pat + r"\s+to\s+(.*)", low)
        if delegate_match:
            target_name = self._normalize_target(delegate_match.group(1))
            task_desc = delegate_match.group(2)
            return Intent(
                goal=msg,
                intent_type=IntentType.DELEGATE_TASK,
                target=target_name,
                purpose="task_delegation",
                action_payload={"task_description": task_desc},
            )

        # Contact / Connect: "Connect to Rahul"
        connect_match = re.search(r"(?:connect to|reach out to|contact)\s+" + target_pat, low)
        if connect_match:
            target_name = self._normalize_target(connect_match.group(1))
            return Intent(
                goal=msg,
                intent_type=IntentType.CONTACT_AGENT,
                target=target_name,
                purpose="contact",
            )

        return None
