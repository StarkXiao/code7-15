"""认证与会话。

- 口令使用 PBKDF2-HMAC-SHA256（200000 轮）加盐派生，系统不存明文；
- 会话 token 为 ``secrets`` 生成的随机串，进程内映射（演示实现；
  生产应放 Redis/DB 并设置过期）；
- 登录成功/失败都写审计链 —— 失败尝试同样不可抵赖。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from . import config
from .audit import AuditTrail
from .db import connect
from .errors import AuthError
from .models import utcnow_iso


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(config.PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, config.PBKDF2_ITERATIONS
    )
    return salt, digest


def verify_password(password: str, salt: bytes, expected: bytes) -> bool:
    _, digest = hash_password(password, salt)
    return hmac.compare_digest(digest, expected)


class SessionStore:
    def __init__(self):
        self._tokens: dict[str, dict] = {}

    def issue(self, user: dict) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = {"user_id": user["id"], "created_at": utcnow_iso()}
        return token

    def resolve(self, token: str) -> dict | None:
        meta = self._tokens.get(token)
        if not meta:
            return None
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ? AND is_active = 1", (meta["user_id"],)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)


def authenticate(username: str, password: str, sessions: SessionStore,
                 audit: AuditTrail) -> tuple[str, dict]:
    """校验账号口令并签发 token；失败抛 AuthError 并记审计。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None or not verify_password(
            password, bytes(row["pwd_salt"]), bytes(row["pwd_hash"])
        ):
            audit.record(
                actor=None,
                action="USER_LOGIN",
                entity_type="user",
                entity_ref=username,
                result="DENIED",
                reason="用户名或口令错误",
            )
            raise AuthError("用户名或口令错误")
        if not row["is_active"]:
            audit.record(
                actor=None,
                action="USER_LOGIN",
                entity_type="user",
                entity_ref=username,
                result="DENIED",
                reason="账号已停用",
            )
            raise AuthError("账号已停用")
        user = dict(row)
    finally:
        conn.close()

    token = sessions.issue(user)
    audit.record(
        actor=user, action="USER_LOGIN", entity_type="user",
        entity_ref=username, result="SUCCESS", reason="登录成功",
    )
    return token, user
