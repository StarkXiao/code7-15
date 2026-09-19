"""SQLite 连接管理 + 审计日志追加。

- WAL 模式，读写并发；
- 每次操作一个连接（短事务）；
- append_audit 在同一事务内写链，链头取库内最后一条 entry_hash。
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from .hashing import canon, chain_hash, norm_num

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

GENESIS_HASH = "0" * 64

# 审计 payload 只取这些列（不含 id / 链字段本身）
_PAYLOAD_COLS = [
    "ts", "actor_id", "actor_name", "action", "entity_type", "entity_id",
    "batch_id", "reason", "before_data", "after_data",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(db_path: str) -> None:
    """幂等建表。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        ddl = f.read()
    conn = connect(db_path)
    try:
        conn.executescript(ddl)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def append_audit(
    conn: sqlite3.Connection,
    *,
    actor_id: Optional[int],
    actor_name: str,
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    batch_id: Optional[int] = None,
    reason: Optional[str] = None,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
) -> int:
    """在当前事务内追加一条审计记录并计算哈希链。返回新行 id。"""
    ts = utcnow()
    before_s = canon(before) if before is not None else None
    after_s = canon(after) if after is not None else None

    row = conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = row["entry_hash"] if row else GENESIS_HASH

    payload = {
        "ts": ts,
        "actor_id": actor_id,
        "actor_name": actor_name,
        "action": action,
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "batch_id": batch_id,
        "reason": reason,
        "before_data": before_s,
        "after_data": after_s,
    }
    entry_hash = chain_hash(prev_hash, payload)

    cur = conn.execute(
        """INSERT INTO audit_log
           (ts, actor_id, actor_name, action, entity_type, entity_id, batch_id,
            reason, before_data, after_data, prev_hash, entry_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, actor_id, actor_name, action, entity_type,
         str(entity_id) if entity_id is not None else None, batch_id,
         reason, before_s, after_s, prev_hash, entry_hash),
    )
    return cur.lastrowid
