"""Domain exceptions for Natural Language Agent Orchestration."""

from __future__ import annotations


class OrchestrationError(Exception):
    """Base exception for orchestration failures."""

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class TargetResolutionError(OrchestrationError):
    """Raised when target person/agent cannot be resolved."""
    pass


class AmbiguousTargetError(OrchestrationError):
    """Raised when target name matches multiple candidates."""

    def __init__(self, name: str, candidates: list[str]) -> None:
        cand_str = ", ".join(candidates)
        msg = f"Multiple candidates found for '{name}': {cand_str}"
        user_msg = f"I found multiple contacts matching '{name}': {cand_str}. Which one did you mean?"
        super().__init__(msg, user_message=user_msg)
        self.name = name
        self.candidates = candidates


class UntrustedAgentError(OrchestrationError):
    """Raised when target agent is discovered but not yet trusted by owner."""

    def __init__(self, target_name: str, agent_id: str) -> None:
        msg = f"Agent for '{target_name}' ({agent_id}) is not trusted."
        user_msg = f"I found {target_name}'s Nexus agent, but you haven't trusted it yet. Would you like to connect with {target_name}'s agent?"
        super().__init__(msg, user_message=user_msg)
        self.target_name = target_name
        self.agent_id = agent_id


class OrchestrationPolicyError(OrchestrationError):
    """Raised when policy engine denies an orchestration action."""
    pass


class OrchestrationExecutionError(OrchestrationError):
    """Raised when task or workflow execution fails."""
    pass
