"""Auth API schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=1024)
    new_password: str = Field(..., min_length=1, max_length=1024)


class AuthUserOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    owner_id: str
    email: str
    display_name: str | None = None


class SessionOut(BaseModel):
    """Returned by register/login so the client can set its cookie.

    The cookie itself is also set server-side; the token is included so
    non-browser clients (and tests) can use a bearer header instead.
    """

    token: str
    user: AuthUserOut
    expires_at: str


class AuthStatusOut(BaseModel):
    authenticated: bool
    auth_required: bool
    registration_open: bool
    user: AuthUserOut | None = None


class LogoutOut(BaseModel):
    logged_out: bool


class MessageOut(BaseModel):
    message: str


__all__ = [
    "AuthStatusOut",
    "AuthUserOut",
    "ChangePasswordRequest",
    "LoginRequest",
    "LogoutOut",
    "MessageOut",
    "RegisterRequest",
    "SessionOut",
]
