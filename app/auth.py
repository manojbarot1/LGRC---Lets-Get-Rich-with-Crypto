"""JWT-based authentication utilities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import jwt
from fastapi import Request
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

if TYPE_CHECKING:
    from app.models import User

COOKIE = "lgrc_session"
_ALGO = "HS256"
_EXPIRE_DAYS = 30

_crypt = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _crypt.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _crypt.verify(plain, hashed)


def create_token(user_id: int, username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=_EXPIRE_DAYS)
    return jwt.encode(
        {"user_id": user_id, "username": username, "exp": exp},
        get_settings().secret_key,
        algorithm=_ALGO,
    )


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, get_settings().secret_key, algorithms=[_ALGO])
    except Exception:
        return None


async def get_current_user(request: Request, session: AsyncSession) -> "User | None":
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    from app.models import User
    return (await session.execute(
        select(User).where(User.id == payload["user_id"], User.is_active == True)
    )).scalar_one_or_none()


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)
