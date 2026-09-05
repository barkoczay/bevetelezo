"""Egyszerű username/password autentikáció.

4-5 fő, mindenki mindent csinálhat — nincs jogosultsági szint. Az
azonosítás célja a naplózás (ki csinálta) és a bevételezés zárolása.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import AppUser
from src.db.session import get_db

settings = get_settings()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# A bcrypt legfeljebb 72 bájtot dolgoz fel; a hosszabb jelszót levágjuk,
# hogy ne dobjon hibát.
_MAX_PASSWORD_BYTES = 72

# Egyszerű brute force védelem. 4-5 felhasználós rendszernél a memóriában
# tartott számláló elég — nem kell külön Redis.
_MAX_ATTEMPTS = 8
_LOCKOUT_SECONDS = 300
_failed_attempts: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(username: str) -> None:
    """Túl sok sikertelen próbálkozás után átmenetileg tiltunk."""
    now = time.time()
    attempts = [t for t in _failed_attempts[username] if now - t < _LOCKOUT_SECONDS]
    _failed_attempts[username] = attempts
    if len(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Túl sok sikertelen bejelentkezés. Próbáld újra néhány perc múlva.",
        )


def record_failure(username: str) -> None:
    _failed_attempts[username].append(time.time())


def clear_failures(username: str) -> None:
    _failed_attempts.pop(username, None)


def _encode(password: str) -> bytes:
    return password.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_encode(plain), hashed.encode())
    except ValueError:
        return False


def create_token(user: AppUser) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    payload = {"sub": str(user.id), "name": user.display_name, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def authenticate(db: Session, username: str, password: str) -> AppUser | None:
    user = db.scalar(select(AppUser).where(AppUser.username == username))
    if user is None or not user.active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> AppUser:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Érvénytelen vagy lejárt bejelentkezés.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise credentials_error

    user = db.get(AppUser, user_id)
    if user is None or not user.active:
        raise credentials_error
    return user
