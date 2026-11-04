"""Sessions: self-contained signed tokens (JWT, HS256), so the gateway stores nothing.

> **JWT (JSON Web Token):** a small JSON document (who you are, your role, when it expires) plus a
> signature made with a secret only the gateway knows. Anyone can read it; nobody can change it
> without the signature breaking.

Claims: `sub` (trader_id, or the nickname for an admin), `role`, `nickname`, `iat`, `exp`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import jwt

ALGORITHM = "HS256"
SESSION_HOURS = 12


class Role(StrEnum):
    TRADER = "trader"
    BOT = "bot"
    ADMIN = "admin"


class InvalidTokenError(Exception):
    """Missing, malformed, wrongly signed or expired."""


@dataclass(frozen=True)
class Principal:
    """Who is calling, from a verified token."""

    sub: str
    role: Role
    nickname: str
    expires_at: datetime

    @property
    def trader_id(self) -> str | None:
        return None if self.role is Role.ADMIN else self.sub


class Sessions:
    def __init__(self, secret: str, *, hours: float = SESSION_HOURS) -> None:
        self._secret = secret
        self._lifetime = timedelta(hours=hours)

    def issue(
        self, sub: str, role: Role, nickname: str, now: datetime | None = None
    ) -> tuple[str, datetime]:
        issued = (now or datetime.now(UTC)).replace(microsecond=0)
        expires = issued + self._lifetime
        claims = {
            "sub": sub,
            "role": role.value,
            "nickname": nickname,
            "iat": issued,
            "exp": expires,
        }
        return jwt.encode(claims, self._secret, algorithm=ALGORITHM), expires

    def verify(self, token: str, now: datetime | None = None) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                # With an injected clock, expiry is checked below against that clock instead.
                options={
                    "require": ["sub", "role", "exp"],
                    "verify_exp": now is None,
                    "verify_iat": now is None,
                },
            )
            role = Role(claims["role"])
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise InvalidTokenError(str(exc)) from exc
        expires = datetime.fromtimestamp(claims["exp"], UTC)
        if now is not None and now >= expires:
            raise InvalidTokenError("token expired")
        return Principal(claims["sub"], role, claims.get("nickname", claims["sub"]), expires)
