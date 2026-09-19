"""哈希链式审计追踪。

完整性机制
----------
每条审计记录保存前一条记录的 ``entry_hash``，自身哈希为：

    entry_hash = SHA256(prev_hash | 规范化字段 | details_json)

- 任何一条记录被改动或删除，后续链接全部断裂，:meth:`verify_chain` 立刻发现；
- 数据库层另有 UPDATE/DELETE 触发器兜底；
- 业务方法与审计写入在**同一事务**内提交（见 ``conn`` 参数），
  不存在"业务做了但审计没记"的窗口；
- 放行被门禁阻断（BLOCKED）、权限被拒（DENIED）同样落链。
"""

from __future__ import annotations

import hashlib

from . import config
from .db import connect, dumps
from .errors import AuditChainBrokenError
from .models import user_badge, utcnow_iso

# 参与哈希的字段顺序，固定不可调整（否则历史链全部失配）
_HASH_FIELDS = (
    "ts", "actor_id", "actor_name", "actor_role", "action",
    "entity_type", "entity_ref", "result", "reason", "details_json",
)


class AuditTrail:
    def record(self, *, actor: dict | None, action: str, entity_type: str,
               entity_ref: str = "", result: str = "SUCCESS",
               reason: str = "", details: dict | None = None,
               conn=None) -> dict:
        """追加一条审计记录。

        ``conn`` 给定时，INSERT 随调用方事务一起提交（业务-审计原子性）；
        否则独立提交（如登录场景）。
        """
        badge = user_badge(actor)
        entry = {
            "ts": utcnow_iso(),
            "actor_id": badge["id"],
            "actor_name": badge["name"],
            "actor_role": badge["role"],
            "action": action,
            "entity_type": entity_type,
            "entity_ref": str(entity_ref),
            "result": result,
            "reason": reason,
            "details_json": dumps(details or {}),
        }

        own_conn = conn is None
        conn = conn or connect()
        try:
            prev = conn.execute(
                "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            entry["prev_hash"] = prev["entry_hash"] if prev else config.GENESIS_HASH
            entry["entry_hash"] = _compute_hash(entry)
            conn.execute(
                """INSERT INTO audit_log
                   (ts, actor_id, actor_name, actor_role, action, entity_type,
                    entity_ref, result, reason, details_json, prev_hash, entry_hash)
                   VALUES (:ts, :actor_id, :actor_name, :actor_role, :action,
                           :entity_type, :entity_ref, :result, :reason,
                           :details_json, :prev_hash, :entry_hash)""",
                entry,
            )
            if own_conn:
                conn.commit()
        finally:
            if own_conn:
                conn.close()
        return entry

    def list(self, entity_type: str | None = None, entity_ref: str | None = None,
             limit: int = 200) -> list[dict]:
        conn = connect()
        try:
            sql = "SELECT * FROM audit_log"
            where, params = [], []
            if entity_type:
                where.append("entity_type = ?")
                params.append(entity_type)
            if entity_ref:
                where.append("entity_ref = ?")
                params.append(str(entity_ref))
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def verify_chain(self) -> dict:
        """从头逐条重算哈希链。

        返回 ``{"ok": bool, "entries_checked": n, "broken_at": id|None, "reason": ...}``
        发现断裂时抛 :class:`AuditChainBrokenError`。
        """
        conn = connect()
        try:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
        finally:
            conn.close()

        prev_hash = config.GENESIS_HASH
        for row in rows:
            rec = dict(row)
            if rec["prev_hash"] != prev_hash:
                raise AuditChainBrokenError(
                    "审计链断裂：前序哈希不匹配",
                    details={"broken_at": rec["id"], "batch_ts": rec["ts"]},
                )
            expected = _compute_hash(rec)
            if expected != rec["entry_hash"]:
                raise AuditChainBrokenError(
                    "审计链断裂：记录内容与哈希不一致（疑似被篡改）",
                    details={"broken_at": rec["id"], "action": rec["action"]},
                )
            prev_hash = rec["entry_hash"]
        return {"ok": True, "entries_checked": len(rows)}


def _compute_hash(entry: dict) -> str:
    material = "|".join(str(entry[f]) for f in _HASH_FIELDS)
    return hashlib.sha256((entry["prev_hash"] + "|" + material).encode("utf-8")).hexdigest()
