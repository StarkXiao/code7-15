"""种子数据：演示用受控主数据与账号。

用法：``python scripts/seed.py``（默认库 data/ebr.db，可用 EBR_DB_PATH 覆盖）。
重复执行安全：已存在的编码会跳过。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ebr import masterdata  # noqa: E402
from ebr.audit import AuditTrail  # noqa: E402
from ebr.auth import hash_password  # noqa: E402
from ebr.db import connect, init_db  # noqa: E402
from ebr.models import utcnow_iso  # noqa: E402

PRODUCT_CODE = "AMP-250"

USERS = [
    # username, password, display_name, role
    ("admin", "admin123", "系统管理员", "ADMIN"),
    ("zhang.gong", "operator123", "张工（配料/操作）", "OPERATOR"),
    ("li.chejian", "lead123", "李车间（生产负责人）", "PRODUCTION_LEAD"),
    ("wang.huayan", "analyst123", "王化验（QC 化验员）", "ANALYST"),
    ("zhao.qa", "qa123", "赵 QA（质量保证）", "QA"),
    ("sun.qp", "qp123", "孙受权放行人 QP", "QP"),
]

MATERIALS = [
    # code, name, spec, category, unit
    ("MAT-API-001", "阿莫西林原料药", "CP2025 含量≥95.0%", "API", "kg"),
    ("MAT-EXC-001", "硬脂酸镁", "CP2025 药用级", "EXCIPIENT", "kg"),
    ("MAT-EXC-002", "微晶纤维素", "CP2025 PH-102", "EXCIPIENT", "kg"),
    ("MAT-PKG-001", "空心胶囊 0#", "CP2025 明胶胶囊", "PACKAGING", "粒"),
]

BOM = [
    # material, qty_required, tolerance%, sequence
    ("MAT-API-001", 25.50, 2.0, 10),
    ("MAT-EXC-001", 0.60, 5.0, 20),
    ("MAT-EXC-002", 4.20, 3.0, 30),
    ("MAT-PKG-001", 100000, 1.0, 40),
]

PROCESS_PARAMS = [
    # step, step_name, param, target, lo, hi, unit, critical
    (10, "混合制粒", "搅拌转速", 180, 170, 200, "rpm", True),
    (10, "混合制粒", "混合时间", 20, 18, 25, "min", False),
    (20, "干燥", "进风温度", 65, 60, 70, "℃", True),
    (20, "干燥", "颗粒水分", 2.5, 1.5, 3.5, "%", True),
    (30, "填充", "装量差异", 0, -7.5, 7.5, "%", True),
]

QC_SPECS = [
    # test, type, lo, hi, expected, unit, critical
    ("装量差异", "NUMERIC", -7.5, 7.5, None, "%", True),
    ("含量（阿莫西林）", "NUMERIC", 95.0, 105.0, None, "占标示量%", True),
    ("溶出度(30min)", "NUMERIC", 80.0, None, None, "%", True),
    ("水分", "NUMERIC", 0, 5.0, None, "%", False),
    ("性状", "TEXT", None, None, "内容物为白色或类白色粉末", "", False),
]


def seed() -> None:
    init_db()
    conn = connect()
    audit = AuditTrail()

    # ---- 用户 ----
    for username, password, name, role in USERS:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            continue
        salt, digest = hash_password(password)
        conn.execute(
            """INSERT INTO users (username, display_name, role, pwd_salt, pwd_hash,
                                  is_active, created_at)
               VALUES (?,?,?,?,?,1,?)""",
            (username, name, role, salt, digest, utcnow_iso()),
        )
    admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    audit.record(actor=dict(admin), action="SEED_USERS", entity_type="system",
                 entity_ref="seed", details={"users": [u[0] for u in USERS]}, conn=conn)

    # ---- 物料 ----
    for code, name, spec, category, unit in MATERIALS:
        if conn.execute("SELECT 1 FROM materials WHERE code = ?", (code,)).fetchone():
            continue
        conn.execute(
            """INSERT INTO materials (code, name, spec, category, unit, created_at)
               VALUES (?,?,?,?,?,?)""",
            (code, name, spec, category, unit, utcnow_iso()))

    # ---- 产品 ----
    if not conn.execute("SELECT 1 FROM products WHERE code = ?",
                        (PRODUCT_CODE,)).fetchone():
        conn.execute(
            """INSERT INTO products
               (code, name, dosage_form, spec, batch_size, batch_size_unit,
                yield_lower_pct, yield_upper_pct, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (PRODUCT_CODE, "阿莫西林胶囊", "胶囊剂", "0.25g", 100000, "粒",
             97.0, 102.0, utcnow_iso()),
        )
        audit.record(actor=dict(admin), action="SEED_PRODUCT", entity_type="product",
                     entity_ref=PRODUCT_CODE, conn=conn)
    conn.commit()
    conn.close()

    # ---- BOM / 工艺 / 质量标准（走服务层，确保审计一致；重复执行时跳过已存在项）----
    actor = {"id": admin["id"], "username": "admin", "display_name": admin["display_name"],
             "role": "ADMIN"}

    def exists(sql, *params):
        c = connect()
        try:
            return c.execute(sql, params).fetchone() is not None
        finally:
            c.close()

    for code, qty, tol, seq in BOM:
        if exists("""SELECT 1 FROM product_bom_items b
                     JOIN products p ON p.id = b.product_id
                     JOIN materials m ON m.id = b.material_id
                     WHERE p.code = ? AND m.code = ?""", PRODUCT_CODE, code):
            continue
        masterdata.add_bom_item(actor=actor, product_code=PRODUCT_CODE,
                                material_code=code, qty_required=qty,
                                tolerance_pct=tol, sequence_no=seq)
    for step, sname, pname, target, lo, hi, unit, crit in PROCESS_PARAMS:
        if exists("""SELECT 1 FROM process_parameters pp JOIN products p ON p.id = pp.product_id
                     WHERE p.code = ? AND pp.step_no = ? AND pp.param_name = ?""",
                  PRODUCT_CODE, step, pname):
            continue
        masterdata.add_process_parameter(
            actor=actor, product_code=PRODUCT_CODE, step_no=step, step_name=sname,
            param_name=pname, target=target, lower_limit=lo, upper_limit=hi,
            unit=unit, is_critical=crit)
    for tname, ttype, lo, hi, expected, unit, crit in QC_SPECS:
        if exists("""SELECT 1 FROM qc_specs q JOIN products p ON p.id = q.product_id
                     WHERE p.code = ? AND q.test_name = ?""", PRODUCT_CODE, tname):
            continue
        masterdata.add_qc_spec(
            actor=actor, product_code=PRODUCT_CODE, test_name=tname,
            test_type=ttype, lower_limit=lo, upper_limit=hi,
            expected_text=expected, unit=unit, is_critical=crit)

    print(f"种子完成 → 数据库: {os.environ.get('EBR_DB_PATH', 'data/ebr.db')}")
    print("账号: " + ", ".join(f"{u[0]}/{u[1]}" for u in USERS))


if __name__ == "__main__":
    seed()
