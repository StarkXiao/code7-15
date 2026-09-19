"""通用领域工具：时间戳与行对象规范化。"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """统一 ISO-8601 UTC 时间戳（秒级精度，含 Z 后缀）。"""
    return utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def user_badge(user: dict | None) -> dict:
    """审计用的操作者摘要。未登录操作（如登录失败）用 SYSTEM。"""
    if not user:
        return {"id": None, "name": "SYSTEM", "role": "ANONYMOUS"}
    return {"id": user["id"], "name": user["display_name"], "role": user["role"]}
