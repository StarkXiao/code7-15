"""运行配置。

数据库路径与 PBKDF2 参数均可通过环境变量覆盖；
测试时使用 ``EBR_DB_PATH`` 指向临时数据库。
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "ebr.db"


def db_path() -> str:
    return os.environ.get("EBR_DB_PATH", str(DEFAULT_DB_PATH))


# PBKDF2-HMAC-SHA256 口令派生参数
PBKDF2_ITERATIONS = int(os.environ.get("EBR_PBKDF2_ITERATIONS", "200000"))
PBKDF2_SALT_BYTES = 16

# 电子签名 hash 链（签名记录之间也自成一条链）
GENESIS_HASH = "0" * 64

# 批次状态机：状态 -> 允许迁移到的状态集合
BATCH_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"IN_PRODUCTION"},
    "IN_PRODUCTION": {"PENDING_QA"},
    "PENDING_QA": {"RELEASED", "REJECTED", "IN_PRODUCTION"},  # QA 可驳回整改
    "RELEASED": set(),   # 终态
    "REJECTED": set(),   # 终态
}

# 各状态允许的操作角色（职责分离）
TRANSITION_ROLES: dict[tuple[str, str], frozenset[str]] = {
    ("DRAFT", "IN_PRODUCTION"): frozenset({"OPERATOR", "PRODUCTION_LEAD"}),
    ("IN_PRODUCTION", "PENDING_QA"): frozenset({"PRODUCTION_LEAD", "QA"}),
    ("PENDING_QA", "IN_PRODUCTION"): frozenset({"QA"}),
    ("PENDING_QA", "RELEASED"): frozenset({"QP"}),
    ("PENDING_QA", "REJECTED"): frozenset({"QA", "QP"}),
}

ROLES = ("ADMIN", "OPERATOR", "PRODUCTION_LEAD", "ANALYST", "QA", "QP")
