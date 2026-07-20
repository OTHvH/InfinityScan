"""Pydantic schemas for authentication endpoints.

These schemas are used for request validation and response serialization
in the auth-related routes. All use strict validation with extra="forbid".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(_Strict):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_]+$")
    email: EmailStr | None = None
    password: str = Field(..., min_length=8, max_length=128)


class LoginIn(_Strict):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)


class UserOut(_Strict):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: str | None
    role: str
    is_active: bool
    created_at: str


class RegisterOut(_Strict):
    user: UserOut


class LoginOut(_Strict):
    """Returned after successful login.  Tokens are set as httpOnly cookies,
    not in the response body."""
    user: UserOut
