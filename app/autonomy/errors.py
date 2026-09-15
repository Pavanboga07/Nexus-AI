"""Domain exceptions for Nexus Autonomy & Decision Engine (Part 10)."""

from __future__ import annotations


class AutonomyError(Exception):
    """Base exception for all autonomy and decision engine errors."""


class AutonomyDisabledError(AutonomyError):
    """Raised when an autonomous action is attempted while autonomy is OFF or disabled."""


class AutonomyModeRestrictedError(AutonomyError):
    """Raised when an action is not permitted in the current autonomy mode."""


class AutonomyLimitExceededError(AutonomyError):
    """Raised when an autonomous run reaches a hard limit (steps, tool calls, runtime, remote tasks)."""

    def __init__(self, limit_type: str, current_value: int | float, max_value: int | float) -> None:
        super().__init__(
            f"Autonomy limit exceeded: {limit_type} ({current_value} >= {max_value})"
        )
        self.limit_type = limit_type
        self.current_value = current_value
        self.max_value = max_value


class AutonomyApprovalRequiredError(AutonomyError):
    """Raised when an action requires owner approval (decision ASK)."""

    def __init__(self, action: str, purpose: str, risk_level: str) -> None:
        super().__init__(
            f"Action '{action}' for purpose '{purpose}' requires owner approval (risk: {risk_level})"
        )
        self.action = action
        self.purpose = purpose
        self.risk_level = risk_level


class AutonomyActionDeniedError(AutonomyError):
    """Raised when an action is explicitly denied by policy, security, or capability."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(f"Action '{action}' denied: {reason}")
        self.action = action
        self.reason = reason


class AutonomyInvalidActionError(AutonomyError):
    """Raised when a proposed action is not in the explicit allowlist or is malformed."""


class AutonomyRunNotFoundError(AutonomyError):
    """Raised when an autonomy run is not found or does not belong to the owner."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Autonomy run '{run_id}' not found")
        self.run_id = run_id


class AutonomyApprovalNotFoundError(AutonomyError):
    """Raised when a pending approval record is not found."""

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"Autonomy approval '{approval_id}' not found")
        self.approval_id = approval_id


class AutonomyConflictError(AutonomyError):
    """Raised when attempting an invalid transition on a run (e.g. approving a completed run)."""


__all__ = [
    "AutonomyActionDeniedError",
    "AutonomyApprovalNotFoundError",
    "AutonomyApprovalRequiredError",
    "AutonomyConflictError",
    "AutonomyDisabledError",
    "AutonomyError",
    "AutonomyInvalidActionError",
    "AutonomyLimitExceededError",
    "AutonomyModeRestrictedError",
    "AutonomyRunNotFoundError",
]
