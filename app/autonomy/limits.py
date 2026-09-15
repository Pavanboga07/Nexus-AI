"""Deterministic risk classification and execution limit checks (Part 10).

The LLM is NEVER permitted to classify risk. Risk classification is strictly
deterministic and governed by hardcoded safety rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from app.autonomy.models import (
    ActionType,
    AutonomyConfig,
    AutonomyRun,
    RiskLevel,
)

# Sensitive data categories that raise risk levels
CRITICAL_DATA_CATEGORIES = frozenset(
    {"financial", "bank", "payment", "credit", "crypto", "funds", "secrets", "credentials", "private_key", "password"}
)

HIGH_RISK_DATA_CATEGORIES = frozenset(
    {"health", "identity", "location", "personal", "contacts", "medical"}
)

# Consequential tools that are automatically CRITICAL or HIGH risk
CRITICAL_TOOLS = frozenset(
    {"transfer_funds", "delete_account", "execute_command", "run_shell", "wipe_database", "modify_keys"}
)

LOW_RISK_TOOLS = frozenset(
    {"calculator", "get_current_time", "time", "date", "echo", "weather"}
)


def classify_risk(
    *,
    action_type: str | ActionType,
    data_categories: Sequence[str] | None = None,
    target_agent_id: str | None = None,
    is_trusted: bool = True,
    tool_name: str | None = None,
) -> RiskLevel:
    """Classify the risk of a proposed action deterministically."""
    if isinstance(action_type, ActionType):
        action_val = action_type.value
    else:
        action_val = str(action_type).lower()

    cats = {str(c).lower() for c in (data_categories or [])}

    # Rule 1: Any interaction with an untrusted agent is CRITICAL
    if target_agent_id and not is_trusted:
        return RiskLevel.CRITICAL

    # Rule 2: Any critical data category is CRITICAL
    if bool(cats & CRITICAL_DATA_CATEGORIES):
        return RiskLevel.CRITICAL

    # Rule 3: Critical tools are CRITICAL
    if tool_name and tool_name.lower() in CRITICAL_TOOLS:
        return RiskLevel.CRITICAL

    # Rule 4: High risk data categories make the action HIGH
    if bool(cats & HIGH_RISK_DATA_CATEGORIES):
        return RiskLevel.HIGH

    # Rule 5: Action type specific heuristics
    if action_val in {ActionType.SEND_MESSAGE.value, ActionType.CONTACT_AGENT.value, ActionType.CREATE_TASK.value}:
        # External communication with trusted agent
        if cats:
            return RiskLevel.HIGH
        return RiskLevel.MEDIUM

    if action_val == ActionType.EXECUTE_TOOL.value:
        if tool_name and tool_name.lower() in LOW_RISK_TOOLS:
            return RiskLevel.LOW
        return RiskLevel.HIGH

    if action_val == ActionType.CREATE_WORKFLOW.value:
        return RiskLevel.MEDIUM

    if action_val in {
        ActionType.READ_MEMORY.value,
        ActionType.READ_CONTEXT.value,
        ActionType.UPDATE_MEMORY.value,
        ActionType.SCHEDULE_ACTION.value,
        ActionType.REQUEST_APPROVAL.value,
    }:
        return RiskLevel.LOW

    # Default unknown actions to HIGH
    return RiskLevel.HIGH


def check_limits(
    run: AutonomyRun,
    config: AutonomyConfig,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Verify that an autonomous run has not exceeded its budget or bounds.

    Returns:
        (is_within_limits, violation_reason)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # 1. Step count limit
    if run.steps_executed >= config.max_steps_per_run:
        return False, f"max_steps_exceeded: executed {run.steps_executed} of {config.max_steps_per_run} steps"

    # 2. Tool calls limit
    if run.tool_calls >= config.max_tool_calls:
        return False, f"max_tool_calls_exceeded: made {run.tool_calls} of {config.max_tool_calls} tool calls"

    # 3. Remote tasks limit
    if run.remote_tasks >= config.max_remote_tasks:
        return False, f"max_remote_tasks_exceeded: dispatched {run.remote_tasks} of {config.max_remote_tasks} tasks"

    # 4. Runtime duration limit
    if run.started_at is not None:
        elapsed = (now - run.started_at).total_seconds()
        if elapsed >= config.max_runtime_seconds:
            return False, f"max_runtime_exceeded: elapsed {int(elapsed)}s of {config.max_runtime_seconds}s limit"

    return True, None


__all__ = [
    "CRITICAL_DATA_CATEGORIES",
    "CRITICAL_TOOLS",
    "HIGH_RISK_DATA_CATEGORIES",
    "LOW_RISK_TOOLS",
    "check_limits",
    "classify_risk",
]
