-- 药品批放行管理系统 数据库结构 (SQLite)
-- 设计原则：审计日志只增不改（触发器级防护）；批次进入 QA 审核后业务记录冻结。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('operator','production_lead','qc','qa','admin')),
    pwd_hash      TEXT NOT NULL,          -- sha256(salt + password)
    pwd_salt      TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,   -- 物料编码
    name            TEXT NOT NULL,
    spec            TEXT,                   -- 规格
    category        TEXT NOT NULL CHECK (category IN ('raw','packaging'))
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    dosage_form     TEXT NOT NULL,          -- 剂型
    strength        TEXT NOT NULL           -- 规格/标示量
);

-- 产品 BOM：理论投料量（单批次标准批量）
CREATE TABLE IF NOT EXISTS bom_items (
    id                  INTEGER PRIMARY KEY,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    material_id         INTEGER NOT NULL REFERENCES materials(id),
    planned_qty         REAL NOT NULL,
    uom                 TEXT NOT NULL,      -- 计量单位
    tolerance_pct       REAL NOT NULL DEFAULT 3.0,  -- 允许偏差 %
    UNIQUE (product_id, material_id)
);

-- 工艺步骤
CREATE TABLE IF NOT EXISTS process_steps (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    step_no         INTEGER NOT NULL,
    name            TEXT NOT NULL,
    UNIQUE (product_id, step_no)
);

-- 工艺参数标准（带上下限；包装密封性等定性项目用 pass/fail）
CREATE TABLE IF NOT EXISTS process_param_specs (
    id              INTEGER PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES process_steps(id),
    name            TEXT NOT NULL,
    lower_limit     REAL,
    upper_limit     REAL,
    uom             TEXT,
    is_pass_fail    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (step_id, name)
);

-- QC 质量标准
CREATE TABLE IF NOT EXISTS qc_specs (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    test_name       TEXT NOT NULL,
    test_type       TEXT NOT NULL CHECK (test_type IN ('numeric','pass_fail')),
    lower_limit     REAL,
    upper_limit     REAL,
    uom             TEXT,
    risk            TEXT NOT NULL CHECK (risk IN ('critical','major','minor')),
    UNIQUE (product_id, test_name)
);

CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY,
    batch_no        TEXT NOT NULL UNIQUE,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    batch_size      INTEGER NOT NULL,       -- 批量（片）
    status          TEXT NOT NULL DEFAULT 'created'
                    CHECK (status IN ('created','weighing','in_production','production_done',
                                      'qc_sampling','qc_done','pending_review','released','rejected')),
    planned_yield_pct REAL,                 -- 收率要求下限/上限在规则引擎中固定
    actual_yield_pct REAL,
    current_step_no INTEGER,
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL,
    submitted_at    TEXT,
    reviewed_by     INTEGER REFERENCES users(id),
    reviewed_at     TEXT,
    release_comment TEXT,
    e_signature     TEXT                    -- 放行电子签名：username#fullname#timestamp
);

-- 投料记录（强制双人：操作人 + 复核人）
CREATE TABLE IF NOT EXISTS weighing_records (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    material_id     INTEGER NOT NULL REFERENCES materials(id),
    planned_qty     REAL NOT NULL,
    actual_qty      REAL NOT NULL,
    uom             TEXT NOT NULL,
    weighed_by      INTEGER NOT NULL REFERENCES users(id),
    checked_by      INTEGER REFERENCES users(id),
    checked_at      TEXT,
    weighed_at      TEXT NOT NULL,
    record_hash     TEXT NOT NULL,
    UNIQUE (batch_id, material_id)
);

-- 工艺步骤执行记录（每步一条，证明步骤完成）
CREATE TABLE IF NOT EXISTS process_step_records (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    step_no         INTEGER NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    operator_id     INTEGER REFERENCES users(id),
    UNIQUE (batch_id, step_no)
);

-- 工艺参数记录
CREATE TABLE IF NOT EXISTS process_param_records (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    step_no         INTEGER NOT NULL,
    param_name      TEXT NOT NULL,
    numeric_value   REAL,
    pass_fail       INTEGER,                -- 1 合格 / 0 不合格（定性项目）
    recorded_by     INTEGER NOT NULL REFERENCES users(id),
    recorded_at     TEXT NOT NULL,
    record_hash     TEXT NOT NULL,
    UNIQUE (batch_id, step_no, param_name)
);

-- QC 检验结果（录入人 + 复核人）
CREATE TABLE IF NOT EXISTS qc_results (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    test_name       TEXT NOT NULL,
    test_type       TEXT NOT NULL,
    numeric_value   REAL,
    pass_fail       INTEGER,
    result_conforms INTEGER,                -- 1 符合标准 / 0 不符合
    tested_by       INTEGER NOT NULL REFERENCES users(id),
    checked_by      INTEGER REFERENCES users(id),
    checked_at      TEXT,
    tested_at       TEXT NOT NULL,
    record_hash     TEXT NOT NULL,
    UNIQUE (batch_id, test_name)
);

CREATE TABLE IF NOT EXISTS deviations (
    id              INTEGER PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    source          TEXT NOT NULL CHECK (source IN ('auto_weighing','auto_process','auto_qc','manual')),
    category        TEXT NOT NULL,          -- 如 投料超限 / 工艺OOS / 检验OOS
    severity        TEXT NOT NULL CHECK (severity IN ('critical','major','minor')),
    description     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    disposition     TEXT,                   -- accepted(继续生产) / rejected(报废/退回)
    capa_summary    TEXT,
    raised_by       INTEGER NOT NULL REFERENCES users(id),
    raised_at       TEXT NOT NULL,
    closed_by       INTEGER REFERENCES users(id),
    closed_at       TEXT,
    dedup_key       TEXT UNIQUE             -- 自动偏差去重
);

-- 审计追踪：只增。hash = sha256(prev_hash | canonical_json(payload))
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    actor_id        INTEGER,
    actor_name      TEXT NOT NULL,
    action          TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT,
    batch_id        INTEGER,
    reason          TEXT,
    before_data     TEXT,
    after_data      TEXT,
    prev_hash       TEXT NOT NULL,
    entry_hash      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_weigh_batch   ON weighing_records(batch_id);
CREATE INDEX IF NOT EXISTS idx_param_batch   ON process_param_records(batch_id);
CREATE INDEX IF NOT EXISTS idx_qc_batch      ON qc_results(batch_id);
CREATE INDEX IF NOT EXISTS idx_dev_batch     ON deviations(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_batch   ON audit_log(batch_id);

-- ===== 审计日志不可变：禁止 UPDATE / DELETE（DBA 直改也会被拒）=====
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, '审计日志为只增记录，禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, '审计日志为只增记录，禁止删除');
END;

-- ===== EBR 冻结：批次 pending_review / released / rejected 后，业务记录不可改 =====
CREATE TRIGGER IF NOT EXISTS trg_freeze_weigh_upd
BEFORE UPDATE ON weighing_records
WHEN (SELECT status FROM batches WHERE id = NEW.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已进入 QA 审核/终审状态，电子批记录已冻结');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_weigh_del
BEFORE DELETE ON weighing_records
WHEN (SELECT status FROM batches WHERE id = OLD.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已冻结，禁止删除投料记录');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_weigh_ins
BEFORE INSERT ON weighing_records
WHEN (SELECT status FROM batches WHERE id = NEW.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已进入 QA 审核/终审，禁止补插投料记录');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_param_upd
BEFORE UPDATE ON process_param_records
WHEN (SELECT status FROM batches WHERE id = NEW.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已进入 QA 审核/终审状态，电子批记录已冻结');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_param_del
BEFORE DELETE ON process_param_records
WHEN (SELECT status FROM batches WHERE id = OLD.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已冻结，禁止删除工艺记录');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_param_ins
BEFORE INSERT ON process_param_records
WHEN (SELECT status FROM batches WHERE id = NEW.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已进入 QA 审核/终审，禁止补插工艺参数记录');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_step_upd
BEFORE UPDATE ON process_step_records
WHEN (SELECT status FROM batches WHERE id = NEW.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已冻结，禁止修改工艺步骤');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_step_ins
BEFORE INSERT ON process_step_records
WHEN (SELECT status FROM batches WHERE id = NEW.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已进入 QA 审核/终审，禁止补插工艺步骤');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_qc_upd
BEFORE UPDATE ON qc_results
WHEN (SELECT status FROM batches WHERE id = NEW.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已进入 QA 审核/终审状态，电子批记录已冻结');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_qc_ins
BEFORE INSERT ON qc_results
WHEN (SELECT status FROM batches WHERE id = NEW.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已进入 QA 审核/终审，禁止补插 QC 结果');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_qc_del
BEFORE DELETE ON qc_results
WHEN (SELECT status FROM batches WHERE id = OLD.batch_id)
     IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已冻结，禁止删除检验记录');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_dev_upd
BEFORE UPDATE ON deviations
WHEN NEW.status = 'open'
     AND (SELECT status FROM batches WHERE id = NEW.batch_id)
         IN ('pending_review','released','rejected')
BEGIN
    SELECT RAISE(ABORT, '批次已冻结，不能变更偏差（QA 关闭历史偏差除外）');
END;
