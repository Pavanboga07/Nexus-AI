"""Errors for Part 9 Workflows."""

from __future__ import annotations


class WorkflowError(Exception):
    """Base exception for workflow subsystem."""

    def __init__(self, message: str, code: str = "WORKFLOW_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class WorkflowNotFoundError(WorkflowError):
    def __init__(self, message: str = "Workflow not found.") -> None:
        super().__init__(message, code="NOT_FOUND")


class WorkflowAccessDeniedError(WorkflowError):
    def __init__(self, message: str = "Access to workflow denied.") -> None:
        super().__init__(message, code="FORBIDDEN")


class WorkflowConflictError(WorkflowError):
    def __init__(self, message: str = "Workflow in conflict state.") -> None:
        super().__init__(message, code="CONFLICT")


class WorkflowExpiredError(WorkflowError):
    def __init__(self, message: str = "Workflow has expired.") -> None:
        super().__init__(message, code="EXPIRED")
