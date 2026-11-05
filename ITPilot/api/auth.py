"""Small bearer-token boundary for the demo API.

User tokens bind a request to one user ID. The separate administrator token is
reserved for operations that change shared state or expose operational data.
"""

import json
import os
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    is_admin: bool = False


@dataclass(frozen=True)
class AuthConfig:
    user_tokens: Dict[str, str]
    admin_token: str


def _valid_token(value: object) -> bool:
    return (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{24,}", value) is not None
            and "replace_with" not in value)


@lru_cache(maxsize=1)
def get_auth_config() -> AuthConfig:
    """Read credentials once per process and fail closed on missing configuration."""
    try:
        user_tokens = json.loads(os.getenv("ITPILOT_USER_TOKENS", ""))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ITPILOT_USER_TOKENS must be a JSON object") from exc
    admin_token = os.getenv("ITPILOT_ADMIN_TOKEN", "")
    if (
        not isinstance(user_tokens, dict)
        or not user_tokens
        or not all(isinstance(user_id, str) and user_id.strip() and _valid_token(token)
                   for user_id, token in user_tokens.items())
        or not _valid_token(admin_token)
    ):
        raise RuntimeError("Configure non-placeholder user and admin tokens (at least 24 characters)")
    tokens = list(user_tokens.values()) + [admin_token]
    if len(set(tokens)) != len(tokens):
        raise RuntimeError("Every user and administrator must have a distinct token")
    return AuthConfig(user_tokens=user_tokens, admin_token=admin_token)


def _token(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    if credentials is None or not credentials.credentials:
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    return credentials.credentials


async def require_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Principal:
    token = _token(credentials)
    for user_id, expected in get_auth_config().user_tokens.items():
        if secrets.compare_digest(token, expected):
            return Principal(user_id=user_id)
    raise HTTPException(403, "User token required")


async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Principal:
    if secrets.compare_digest(_token(credentials), get_auth_config().admin_token):
        return Principal(user_id="admin", is_admin=True)
    raise HTTPException(403, "Administrator token required")
