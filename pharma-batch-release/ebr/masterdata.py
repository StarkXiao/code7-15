"""主数据服务：物料、产品、BOM、工艺参数标准、成品质量标准。

主数据属于受控文件，创建即进入审计链；批次执行时**快照**引用当时标准，
后续主数据修订不影响在制批次的合规判定依据（快照在 EBR 汇总时取当前值）。
"""

from __future__ import annotations

from .audit import AuditTrail
from .db import connect
from .errors import NotFoundError, PermissionDeniedError, ValidationError
from .models import utcnow_iso

# 主数据是受控文件，只有 ADMIN / QA 可以建档
_MASTER_DATA_ROLES = ("ADMIN", "QA")


def _require_master_data_role(actor: dict, action: str, ref: str) -> None:
    if actor["role"] not in _MASTER_DATA_ROLES:
        AuditTrail().record(
            actor=actor, action=action, entity_type="authorization",
            entity_ref=ref, result="DENIED",
            reason=f"角色 {actor['role']} 无权维护主数据（需要 ADMIN/QA）")
        raise PermissionDeniedError("主数据维护需要 ADMIN 或 QA 角色")


# ---------------------------------------------------------------- 物料 / 产品

def create_material(*, actor: dict, code: str, name: str, spec: str,
                    category: str, unit: str) -> dict:
    _require_master_data_role(actor, "MATERIAL_CREATE", code)
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO materials (code, name, spec, category, unit, created_at)
               VALUES (?,?,?,?,?,?)""",
            (code, name, spec, category, unit, utcnow_iso()),
        )
        AuditTrail().record(
            actor=actor, action="MATERIAL_CREATE", entity_type="material",
            entity_ref=code, details={"name": name, "category": category}, conn=conn,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM materials WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def create_product(*, actor: dict, code: str, name: str, dosage_form: str,
                   spec: str, batch_size: float, batch_size_unit: str,
                   yield_lower_pct: float, yield_upper_pct: float) -> dict:
    _require_master_data_role(actor, "PRODUCT_CREATE", code)
    if not (0 < yield_lower_pct <= yield_upper_pct):
        raise ValidationError("收率上下限不合法，要求 0 < 下限 <= 上限")
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO products
               (code, name, dosage_form, spec, batch_size, batch_size_unit,
                yield_lower_pct, yield_upper_pct, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (code, name, dosage_form, spec, batch_size, batch_size_unit,
             yield_lower_pct, yield_upper_pct, utcnow_iso()),
        )
        AuditTrail().record(
            actor=actor, action="PRODUCT_CREATE", entity_type="product",
            entity_ref=code, details={"name": name, "dosage_form": dosage_form}, conn=conn,
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def add_bom_item(*, actor: dict, product_code: str, material_code: str,
                 qty_required: float, tolerance_pct: float, sequence_no: int) -> dict:
    _require_master_data_role(actor, "BOM_ADD_ITEM", product_code)
    conn = connect()
    try:
        product = _get(conn, "products", "code", product_code, "产品")
        material = _get(conn, "materials", "code", material_code, "物料")
        cur = conn.execute(
            """INSERT INTO product_bom_items
               (product_id, material_id, qty_required, tolerance_pct, sequence_no)
               VALUES (?,?,?,?,?)""",
            (product["id"], material["id"], qty_required, tolerance_pct, sequence_no),
        )
        AuditTrail().record(
            actor=actor, action="BOM_ADD_ITEM", entity_type="product",
            entity_ref=product_code,
            details={"material": material_code, "qty_required": qty_required,
                     "tolerance_pct": tolerance_pct},
            conn=conn,
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM product_bom_items WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def add_process_parameter(*, actor: dict, product_code: str, step_no: int,
                          step_name: str, param_name: str, target: float | None,
                          lower_limit: float | None, upper_limit: float | None,
                          unit: str = "", is_critical: bool = False) -> dict:
    _require_master_data_role(actor, "PROCESS_PARAM_DEFINE", product_code)
    conn = connect()
    try:
        product = _get(conn, "products", "code", product_code, "产品")
        _validate_band(target, lower_limit, upper_limit)
        cur = conn.execute(
            """INSERT INTO process_parameters
               (product_id, step_no, step_name, param_name, target, lower_limit,
                upper_limit, unit, is_critical)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (product["id"], step_no, step_name, param_name, target, lower_limit,
             upper_limit, unit, int(is_critical)),
        )
        AuditTrail().record(
            actor=actor, action="PROCESS_PARAM_DEFINE", entity_type="product",
            entity_ref=product_code,
            details={"step": step_name, "param": param_name,
                     "critical": is_critical, "band": [lower_limit, upper_limit]},
            conn=conn,
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM process_parameters WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def add_qc_spec(*, actor: dict, product_code: str, test_name: str,
                test_type: str, lower_limit: float | None = None,
                upper_limit: float | None = None, expected_text: str | None = None,
                unit: str = "", is_critical: bool = False) -> dict:
    _require_master_data_role(actor, "QC_SPEC_DEFINE", product_code)
    conn = connect()
    try:
        product = _get(conn, "products", "code", product_code, "产品")
        if test_type == "NUMERIC":
            if lower_limit is None and upper_limit is None:
                raise ValidationError("数值型质量标准至少给出单侧限度")
            if (lower_limit is not None and upper_limit is not None
                    and lower_limit > upper_limit):
                raise ValidationError("质量标准下限不得大于上限")
        elif expected_text is None:
            raise ValidationError("文本型质量标准必须给出期望值")
        cur = conn.execute(
            """INSERT INTO qc_specs
               (product_id, test_name, test_type, lower_limit, upper_limit,
                expected_text, unit, is_critical)
               VALUES (?,?,?,?,?,?,?,?)""",
            (product["id"], test_name, test_type, lower_limit, upper_limit,
             expected_text, unit, int(is_critical)),
        )
        AuditTrail().record(
            actor=actor, action="QC_SPEC_DEFINE", entity_type="product",
            entity_ref=product_code,
            details={"test": test_name, "critical": is_critical}, conn=conn,
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM qc_specs WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------- 读取

def get_product_by_code(conn, code: str) -> dict:
    return _get(conn, "products", "code", code, "产品")


def list_products() -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM products ORDER BY id")]
    finally:
        conn.close()


def list_materials() -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM materials ORDER BY id")]
    finally:
        conn.close()


def load_product_masterdata(conn, product_id: int) -> dict:
    """加载产品全部受控标准，供规则引擎使用。"""
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        raise NotFoundError(f"产品 id={product_id} 不存在")
    bom = conn.execute(
        """SELECT b.*, m.code AS material_code, m.name AS material_name, m.unit AS unit
           FROM product_bom_items b JOIN materials m ON m.id = b.material_id
           WHERE b.product_id = ? ORDER BY b.sequence_no""",
        (product_id,),
    ).fetchall()
    params = conn.execute(
        "SELECT * FROM process_parameters WHERE product_id = ? ORDER BY step_no, id",
        (product_id,),
    ).fetchall()
    specs = conn.execute(
        "SELECT * FROM qc_specs WHERE product_id = ? ORDER BY id", (product_id,)
    ).fetchall()
    return {
        "product": dict(product),
        "bom": [dict(r) for r in bom],
        "process_parameters": [dict(r) for r in params],
        "qc_specs": [dict(r) for r in specs],
    }


# ---------------------------------------------------------------- helpers

def _get(conn, table: str, key: str, value: str, label: str) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (value,)).fetchone()
    if not row:
        raise NotFoundError(f"{label} '{value}' 不存在")
    return dict(row)


def _validate_band(target, lower, upper) -> None:
    if lower is None and upper is None:
        return  # 仅记录目标值的非受控参数
    if lower is not None and upper is not None and lower > upper:
        raise ValidationError("工艺参数下限不得大于上限")
    if target is not None:
        if lower is not None and target < lower:
            raise ValidationError("目标值低于下限")
        if upper is not None and target > upper:
            raise ValidationError("目标值高于上限")
