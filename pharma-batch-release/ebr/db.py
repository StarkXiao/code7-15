"""SQLite 连接管理与建表脚本。

设计要点
--------
- 每条工作单元使用独立连接（``connect`` 上下文），提交/回滚边界清晰；
- 开启外键约束、WAL 与 ``CHECK`` 约束，让数据库自身兜住状态合法性；
- 审计链的完整性由应用层 SHA-256 链保证（见 :mod:`ebr.audit`），
  ``audit_log`` 只允许 INSERT，触发器在数据库层禁止 UPDATE/DELETE。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN
                    ('ADMIN','OPERATOR','PRODUCTION_LEAD','ANALYST','QA','QP')),
    pwd_salt        BLOB NOT NULL,
    pwd_hash        BLOB NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,      -- 物料编码
    name            TEXT NOT NULL,             -- 名称
    spec            TEXT NOT NULL,             -- 规格/质量标准
    category        TEXT NOT NULL CHECK (category IN ('API','EXCIPIENT','PACKAGING')),
    unit            TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,      -- 产品编码
    name            TEXT NOT NULL,
    dosage_form     TEXT NOT NULL,             -- 剂型
    spec            TEXT NOT NULL,             -- 规格
    batch_size      REAL NOT NULL CHECK (batch_size > 0),
    batch_size_unit TEXT NOT NULL,
    yield_lower_pct REAL NOT NULL CHECK (yield_lower_pct > 0 AND yield_lower_pct <= 100),
    yield_upper_pct REAL NOT NULL CHECK (yield_upper_pct >= yield_lower_pct),
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_bom_items (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    material_id     INTEGER NOT NULL REFERENCES materials(id),
    qty_required    REAL NOT NULL CHECK (qty_required > 0),
    tolerance_pct   REAL NOT NULL DEFAULT 5 CHECK (tolerance_pct >= 0),
    sequence_no     INTEGER NOT NULL,
    UNIQUE (product_id, material_id)
);

-- 工艺参数标准（关键工艺参数 CPP：is_critical=1）
CREATE TABLE IF NOT EXISTS process_parameters (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    step_no         INTEGER NOT NULL,
    step_name       TEXT NOT NULL,
    param_name      TEXT NOT NULL,
    target          REAL,
    lower_limit     REAL,
    upper_limit     REAL,
    unit            TEXT NOT NULL DEFAULT '',
    is_critical     INTEGER NOT NULL DEFAULT 0 CHECK (is_critical IN (0,1)),
    UNIQUE (product_id, step_no, param_name)
);

-- 成品质量标准（关键质量属性 CQA：is_critical=1）
CREATE TABLE IF NOT EXISTS qc_specs (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    test_name       TEXT NOT NULL,
    test_type       TEXT NOT NULL CHECK (test_type IN ('NUMERIC','TEXT')),
    lower_limit     REAL,
    upper_limit     REAL,
    expected_text   TEXT,
    unit            TEXT NOT NULL DEFAULT '',
    is_critical     INTEGER NOT NULL DEFAULT 0 CHECK (is_critical IN (0,1)),
    UNIQUE (product_id, test_name)
);

CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY,
    batch_no        TEXT NOT NULL UNIQUE,      -- 批号
    product_id      INTEGER NOT NULL REFERENCES products(id),
    planned_size    REAL NOT NULL CHECK (planned_size > 0),
    actual_size     REAL CHECK (actual_size IS NULL OR actual_size >= 0),
    status          TEXT NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT','IN_PRODUCTION','PENDING_QA','RELEASED','REJECTED')),
    started_at      TEXT,
    completed_at    TEXT,
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL
);

-- 投料/称配记录（双人复核）
CREATE TABLE IF NOT EXISTS dispensing_records (
    id                  INTEGER PRIMARY KEY,
    batch_id            INTEGER NOT NULL REFERENCES batches(id),
    material_id         INTEGER NOT NULL REFERENCES materials(id),
    step_no             INTEGER NOT NULL,
    qty_required        REAL NOT NULL,
    qty_actual          REAL NOT NULL CHECK (qty_actual > 0),
    unit                TEXT NOT NULL,
    weighed_by          INTEGER NOT NULL REFERENCES users(id),
    weighed_at          TEXT NOT NULL,
    verified_by         INTEGER REFERENCES users(id),
    verified_at         TEXT,
    UNIQUE (batch_id, material_id)
);

-- 工艺执行记录
CREATE TABLE IF NOT EXISTS process_records (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    parameter_id    INTEGER NOT NULL REFERENCES process_parameters(id),
    step_no         INTEGER NOT NULL,
    step_name       TEXT NOT NULL,
    param_name      TEXT NOT NULL,
    target          REAL,
    lower_limit     REAL,
    upper_limit     REAL,
    unit            TEXT NOT NULL DEFAULT '',
    is_critical     INTEGER NOT NULL DEFAULT 0,
    actual_value    REAL NOT NULL,
    recorded_by     INTEGER NOT NULL REFERENCES users(id),
    recorded_at     TEXT NOT NULL,
    UNIQUE (batch_id, parameter_id)
);

-- QC 检验记录（检验 + 复核）
CREATE TABLE IF NOT EXISTS qc_records (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    spec_id         INTEGER NOT NULL REFERENCES qc_specs(id),
    test_name       TEXT NOT NULL,
    test_type       TEXT NOT NULL,
    lower_limit     REAL,
    upper_limit     REAL,
    expected_text   TEXT,
    unit            TEXT NOT NULL DEFAULT '',
    is_critical     INTEGER NOT NULL DEFAULT 0,
    numeric_value   REAL,
    text_value      TEXT,
    result          TEXT NOT NULL CHECK (result IN ('PASS','OOS')),
    invalidated     INTEGER NOT NULL DEFAULT 0 CHECK (invalidated IN (0,1)),
    tested_by       INTEGER NOT NULL REFERENCES users(id),
    tested_at       TEXT NOT NULL,
    reviewed_by     INTEGER REFERENCES users(id),
    reviewed_at     TEXT
);

-- 每个检验项目只允许有一条“生效中”记录；作废的化验室差错记录保留待重测
CREATE UNIQUE INDEX IF NOT EXISTS idx_qc_one_active
    ON qc_records(batch_id, spec_id) WHERE invalidated = 0;

-- 偏差
CREATE TABLE IF NOT EXISTS deviations (
    id              INTEGER PRIMARY KEY,
    dev_no          TEXT NOT NULL UNIQUE,      -- 偏差编号 DEV-<批号>-<序号>
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    category        TEXT NOT NULL CHECK (category IN
                    ('MINOR','MAJOR','CRITICAL')),
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    -- 关联对象：QC 超标 / 关键工艺超标 / 收率超标 / 其他
    source_type     TEXT NOT NULL CHECK (source_type IN
                    ('QC_OOS','PROCESS_OOT','YIELD','OTHER')),
    source_ref      TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN','CLOSED_REJECTED','CLOSED_EFFECTIVE')),
    root_cause      TEXT NOT NULL DEFAULT '',
    root_cause_confirmed INTEGER NOT NULL DEFAULT 0
                    CHECK (root_cause_confirmed IN (0,1)),
    is_lab_error    INTEGER NOT NULL DEFAULT 0 CHECK (is_lab_error IN (0,1)),
    closed_by       INTEGER REFERENCES users(id),
    closed_at       TEXT,
    raised_by       INTEGER NOT NULL REFERENCES users(id),
    raised_at       TEXT NOT NULL
);

-- CAPA（纠正与预防措施），有效性需 QA 确认
CREATE TABLE IF NOT EXISTS capas (
    id                  INTEGER PRIMARY KEY,
    deviation_id        INTEGER NOT NULL REFERENCES deviations(id),
    action              TEXT NOT NULL,
    owner               INTEGER NOT NULL REFERENCES users(id),
    due_date            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN','CLOSED_PENDING','EFFECTIVE','INEFFECTIVE')),
    closed_at           TEXT,
    verified_by         INTEGER REFERENCES users(id),
    verified_at         TEXT,
    verification_note   TEXT NOT NULL DEFAULT ''
);

-- 电子签名（放行 / 拒放，本身也是一条哈希链）
CREATE TABLE IF NOT EXISTS release_decisions (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    decision        TEXT NOT NULL CHECK (decision IN ('RELEASE','REJECT')),
    meaning         TEXT NOT NULL,             -- 签名含义声明
    signer_id       INTEGER NOT NULL REFERENCES users(id),
    signed_at       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    gate_snapshot   TEXT NOT NULL,             -- 放行时刻 10 道门禁快照（JSON）
    sig_hash        TEXT NOT NULL UNIQUE       -- 链上哈希
);

-- 审计追踪：只增不删，哈希链见应用层
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    actor_id        INTEGER,
    actor_name      TEXT NOT NULL,
    actor_role      TEXT NOT NULL,
    action          TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_ref      TEXT NOT NULL DEFAULT '',
    result          TEXT NOT NULL CHECK (result IN ('SUCCESS','DENIED','BLOCKED','FAILED')),
    reason          TEXT NOT NULL DEFAULT '',
    details_json    TEXT NOT NULL DEFAULT '{}',
    prev_hash       TEXT NOT NULL,
    entry_hash      TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_disp_batch ON dispensing_records(batch_id);
CREATE INDEX IF NOT EXISTS idx_pr_batch ON process_records(batch_id);
CREATE INDEX IF NOT EXISTS idx_qc_batch ON qc_records(batch_id);
CREATE INDEX IF NOT EXISTS idx_dev_batch ON deviations(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_ref);

-- 审计日志防篡改触发器：只允许 INSERT
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
"""


def connect(db_file: str | None = None) -> sqlite3.Connection:
    path = db_file or config.db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(db_file: str | None = None) -> None:
    conn = connect(db_file)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def dumps(obj) -> str:
    """统一 JSON 序列化（审计快照等）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
