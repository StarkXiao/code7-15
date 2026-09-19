"""认证：口令哈希 + 内存会话令牌。

单进程演示部署，令牌存内存（重启后需重新登录）。
口令哈希用 sha256(salt + password)（标准库内无 PBKDF2 限制，
hashlib.pbkdf2_hmac 也可用，这里选用它做 12 万轮拉伸）。
"""
import hashlib
import hmac
import secrets
from typing import Optional

from .database import connect
from .errors import Unauthorized

_TOKENS: dict[str, dict] = {}


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), 120_000)
    return dk.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(dk.hex(), expected_hash)


def login(db_path: str, username: str, password: str) -> tuple[dict, str]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1",
                           (username,)).fetchone()
        if not row or not verify_password(password, row["pwd_salt"], row["pwd_hash"]):
            raise Unauthorized("用户名或密码错误")
        user = {"id": row["id"], "username": row["username"],
                "display_name": row["display_name"], "role": row["role"]}
        token = secrets.token_urlsafe(32)
        _TOKENS[token] = user
        return user, token
    finally:
        conn.close()


def logout(token: str) -> None:
    _TOKENS.pop(token, None)


def current_user(token: Optional[str]) -> dict:
    if not token or token not in _TOKENS:
        raise Unauthorized()
    return _TOKENS[token]


def require_roles(user: dict, roles: tuple[str, ...]) -> None:
    if user["role"] not in roles and user["role"] != "admin":
        from .errors import Forbidden
        raise Forbidden(f"当前角色 {user['role']} 无权执行该操作")
