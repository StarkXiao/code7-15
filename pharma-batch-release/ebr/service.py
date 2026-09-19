"""电子批记录服务层：批次全生命周期操作。

所有写方法满足三条硬约束：

1. **状态机 + 角色双校验**。非法状态迁移 / 越权角色一律抛 ConflictError /
   PermissionDeniedError，且审计留痕（DENIED）。
2. **业务与审计同一事务**。审计 INSERT 与业务 UPDATE 一起提交。
3. **放行不可绕过**。:meth:`release` 内联执行门禁引擎，任何 BLOCKING 门禁
   失败都写 BLOCKED 审计并抛 ReleaseBlockedError —— 方法签名上根本没有
   ``force`` 之类的开关。
"""

from __future__ import annotations

import hashlib

from . import config
from .audit import AuditTrail
from .db import connect, dumps, transaction
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ReleaseBlockedError,
    ValidationError,
)
from .masterdata import load_product_masterdata
from .models import utcnow_iso
from .rules import build_bundle, evaluate

audit_trail = AuditTrail()


# ======================================================================
# 辅助
# ======================================================================

def _require_roles(actor: dict, allowed: tuple[str, ...], action: str, ref: str = ""):
    if actor["role"] not in allowed:
        audit_trail.record(
            actor=actor, action=action, entity_type="authorization",
            entity_ref=ref, result="DENIED",
            reason=f"角色 {actor['role']} 无权执行 {action}（需要 {'/'.join(allowed)}）",
        )
        raise PermissionDeniedError(
            f"角色 {actor['role']} 无权执行该操作（需要 {'/'.join(allowed)}）")


def _require_state(batch: dict, allowed: tuple[str, ...]):
    if batch["status"] not in allowed:
        raise ConflictError(
            f"批次 {batch['batch_no']} 当前状态 {batch['status']}，"
            f"该操作仅在 {'/'.join(allowed)} 下允许")


def _require_reason(reason: str, field: str = "整改/操作原因"):
    if not reason or not reason.strip():
        raise ValidationError(f"{field}不能为空（审计要求）")


def _get_batch(conn, batch_no: str) -> dict:
    row = conn.execute("SELECT * FROM batches WHERE batch_no = ?", (batch_no,)).fetchone()
    if not row:
        raise NotFoundError(f"批次 '{batch_no}' 不存在")
    return dict(row)


def _conditional_transition(conn, batch_id: int, expected: str, target: str) -> None:
    """带条件的 UPDATE 抢状态，杜绝并发下的重复迁移。"""
    cur = conn.execute(
        "UPDATE batches SET status = ? WHERE id = ? AND status = ?",
        (target, batch_id, expected),
    )
    if cur.rowcount != 1:
        raise ConflictError(f"批次状态已被其他操作改变，已离开 {expected}，请刷新后重试")


# ======================================================================
# 批次生命周期
# ======================================================================

def create_batch(*, actor: dict, product_code: str, planned_size: float,
                 batch_no: str | None = None) -> dict:
    _require_roles(actor, ("OPERATOR", "PRODUCTION_LEAD", "QA"), "BATCH_CREATE")
    if planned_size <= 0:
        raise ValidationError("计划批量必须为正数")
    conn = connect()
    try:
        product = conn.execute(
            "SELECT * FROM products WHERE code = ? AND is_active = 1", (product_code,)
        ).fetchone()
        if not product:
            raise NotFoundError(f"产品 '{product_code}' 不存在或已停用")
        if batch_no is None:
            day = utcnow_iso()[:10].replace("-", "")
            n = conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] + 1
            batch_no = f"B{day}-{n:03d}"
        cur = conn.execute(
            """INSERT INTO batches
               (batch_no, product_id, planned_size, status, created_by, created_at)
               VALUES (?,?,?,?,?,?)""",
            (batch_no, product["id"], planned_size, "DRAFT", actor["id"], utcnow_iso()),
        )
        audit_trail.record(
            actor=actor, action="BATCH_CREATE", entity_type="batch",
            entity_ref=batch_no,
            details={"product": product_code, "planned_size": planned_size}, conn=conn,
        )
        conn.commit()
        return _get_batch(conn, batch_no)
    finally:
        conn.close()


def start_production(*, actor: dict, batch_no: str) -> dict:
    _require_roles(actor, ("OPERATOR", "PRODUCTION_LEAD"), "BATCH_START", batch_no)
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("DRAFT",))
        _conditional_transition(conn, batch["id"], "DRAFT", "IN_PRODUCTION")
        conn.execute("UPDATE batches SET started_at = COALESCE(started_at, ?) WHERE id = ?",
                     (utcnow_iso(), batch["id"]))
        audit_trail.record(
            actor=actor, action="BATCH_START", entity_type="batch",
            entity_ref=batch_no, details={}, conn=conn)
        conn.commit()
        return _get_batch(conn, batch_no)
    finally:
        conn.close()


def complete_production(*, actor: dict, batch_no: str, actual_size: float) -> dict:
    """完工报交：IN_PRODUCTION → PENDING_QA，进入 QA 评审队列。"""
    _require_roles(actor, ("PRODUCTION_LEAD", "QA"), "BATCH_COMPLETE", batch_no)
    if actual_size < 0:
        raise ValidationError("实际成品数量不能为负")
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("IN_PRODUCTION",))
        conn.execute("UPDATE batches SET actual_size = ?, completed_at = ? WHERE id = ?",
                     (actual_size, utcnow_iso(), batch["id"]))
        _conditional_transition(conn, batch["id"], "IN_PRODUCTION", "PENDING_QA")
        audit_trail.record(
            actor=actor, action="BATCH_COMPLETE", entity_type="batch",
            entity_ref=batch_no, details={"actual_size": actual_size}, conn=conn)
        conn.commit()
        return _get_batch(conn, batch_no)
    finally:
        conn.close()


def reopen_for_correction(*, actor: dict, batch_no: str, reason: str) -> dict:
    """QA 驳回整改：PENDING_QA → IN_PRODUCTION。原因必填并入审计。"""
    _require_roles(actor, ("QA",), "BATCH_REOPEN", batch_no)
    _require_reason(reason)
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("PENDING_QA",))
        _conditional_transition(conn, batch["id"], "PENDING_QA", "IN_PRODUCTION")
        audit_trail.record(
            actor=actor, action="BATCH_REOPEN", entity_type="batch",
            entity_ref=batch_no, reason=reason, conn=conn)
        conn.commit()
        return _get_batch(conn, batch_no)
    finally:
        conn.close()


# ======================================================================
# 投料（双人复核）
# ======================================================================

def dispense_material(*, actor: dict, batch_no: str, material_code: str,
                      qty_actual: float) -> dict:
    _require_roles(actor, ("OPERATOR",), "DISPENSE", batch_no)
    if qty_actual <= 0:
        raise ValidationError("实际投料量必须为正数")
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("IN_PRODUCTION",))
        bom = conn.execute(
            """SELECT b.*, m.name, m.unit, m.id AS material_id
               FROM product_bom_items b JOIN materials m ON m.id = b.material_id
               JOIN products p ON p.id = b.product_id
               WHERE p.id = ? AND m.code = ?""",
            (batch["product_id"], material_code),
        ).fetchone()
        if not bom:
            raise ValidationError(f"物料 {material_code} 不在该批次产品的 BOM 中")
        exists = conn.execute(
            "SELECT 1 FROM dispensing_records WHERE batch_id = ? AND material_id = ?",
            (batch["id"], bom["material_id"])).fetchone()
        if exists:
            raise ConflictError(f"物料 {material_code} 已投料，记录不可重复提交")
        cur = conn.execute(
            """INSERT INTO dispensing_records
               (batch_id, material_id, step_no, qty_required, qty_actual, unit,
                weighed_by, weighed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (batch["id"], bom["material_id"], bom["sequence_no"],
             bom["qty_required"], qty_actual, bom["unit"], actor["id"], utcnow_iso()),
        )
        audit_trail.record(
            actor=actor, action="DISPENSE", entity_type="batch",
            entity_ref=batch_no,
            details={"material": material_code, "qty_actual": qty_actual,
                     "qty_required": bom["qty_required"], "record_id": cur.lastrowid},
            conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM dispensing_records WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def verify_dispensing(*, actor: dict, batch_no: str, material_code: str) -> dict:
    _require_roles(actor, ("OPERATOR", "PRODUCTION_LEAD", "QA"),
                   "DISPENSE_VERIFY", batch_no)
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("IN_PRODUCTION", "PENDING_QA"))
        rec = conn.execute(
            """SELECT d.* FROM dispensing_records d
               JOIN materials m ON m.id = d.material_id
               WHERE d.batch_id = ? AND m.code = ?""",
            (batch["id"], material_code)).fetchone()
        if not rec:
            raise NotFoundError(f"批次 {batch_no} 没有物料 {material_code} 的投料记录")
        if rec["verified_by"] is not None:
            raise ConflictError("该投料记录已完成复核")
        if rec["weighed_by"] == actor["id"]:
            audit_trail.record(
                actor=actor, action="DISPENSE_VERIFY", entity_type="batch",
                entity_ref=batch_no, result="DENIED",
                reason="自配自核被拒（称量人与复核人不得为同一人）", conn=conn)
            conn.commit()
            raise PermissionDeniedError("称量人与复核人不得为同一人")
        conn.execute(
            "UPDATE dispensing_records SET verified_by = ?, verified_at = ? WHERE id = ?",
            (actor["id"], utcnow_iso(), rec["id"]))
        audit_trail.record(
            actor=actor, action="DISPENSE_VERIFY", entity_type="batch",
            entity_ref=batch_no,
            details={"material": material_code, "record_id": rec["id"]}, conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM dispensing_records WHERE id = ?",
                                 (rec["id"],)).fetchone())
    finally:
        conn.close()


# ======================================================================
# 工艺执行
# ======================================================================

def record_process_value(*, actor: dict, batch_no: str, step_no: int,
                         param_name: str, actual_value: float) -> dict:
    _require_roles(actor, ("OPERATOR", "PRODUCTION_LEAD"), "PROCESS_RECORD", batch_no)
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("IN_PRODUCTION",))
        param = conn.execute(
            """SELECT * FROM process_parameters
               WHERE product_id = ? AND step_no = ? AND param_name = ?""",
            (batch["product_id"], step_no, param_name)).fetchone()
        if not param:
            raise NotFoundError(f"工艺标准中不存在步骤 {step_no} 参数「{param_name}」")
        dup = conn.execute(
            "SELECT 1 FROM process_records WHERE batch_id = ? AND parameter_id = ?",
            (batch["id"], param["id"])).fetchone()
        if dup:
            raise ConflictError("该工艺参数已记录，如需更正请走偏差流程")
        cur = conn.execute(
            """INSERT INTO process_records
               (batch_id, parameter_id, step_no, step_name, param_name, target,
                lower_limit, upper_limit, unit, is_critical, actual_value,
                recorded_by, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (batch["id"], param["id"], param["step_no"], param["step_name"],
             param["param_name"], param["target"], param["lower_limit"],
             param["upper_limit"], param["unit"], param["is_critical"],
             actual_value, actor["id"], utcnow_iso()),
        )
        out = ((param["lower_limit"] is not None and actual_value < param["lower_limit"])
               or (param["upper_limit"] is not None and actual_value > param["upper_limit"]))
        audit_trail.record(
            actor=actor, action="PROCESS_RECORD", entity_type="batch",
            entity_ref=batch_no,
            details={"step": param["step_name"], "param": param_name,
                     "actual_value": actual_value,
                     "in_spec": not out, "critical": bool(param["is_critical"])},
            result="SUCCESS" if not out else "FAILED",
            reason="参数超出规定限度（OOT）" if out else "",
            conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM process_records WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


# ======================================================================
# QC 检验与复核
# ======================================================================

def record_qc_result(*, actor: dict, batch_no: str, test_name: str,
                     numeric_value: float | None = None,
                     text_value: str | None = None) -> dict:
    _require_roles(actor, ("ANALYST",), "QC_TEST", batch_no)
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("IN_PRODUCTION", "PENDING_QA"))
        spec = conn.execute(
            "SELECT * FROM qc_specs WHERE product_id = ? AND test_name = ?",
            (batch["product_id"], test_name)).fetchone()
        if not spec:
            raise NotFoundError(f"质量标准中不存在检验项目「{test_name}」")

        if spec["test_type"] == "NUMERIC":
            if numeric_value is None:
                raise ValidationError(f"「{test_name}」是数值型检验，需要 numeric_value")
            text_value = None
            result = "PASS"
            if spec["lower_limit"] is not None and numeric_value < spec["lower_limit"]:
                result = "OOS"
            if spec["upper_limit"] is not None and numeric_value > spec["upper_limit"]:
                result = "OOS"
        else:
            if text_value is None:
                raise ValidationError(f"「{test_name}」是文本型检验，需要 text_value")
            numeric_value = None
            result = "PASS" if text_value.strip() == spec["expected_text"] else "OOS"

        rec = conn.execute(
            """SELECT * FROM qc_records WHERE batch_id = ? AND spec_id = ?
               AND invalidated = 0""",
            (batch["id"], spec["id"])).fetchone()
        if rec:
            raise ConflictError(
                f"「{test_name}」已有生效检验记录；重测前须按偏差流程将原记录作废")
        cur = conn.execute(
            """INSERT INTO qc_records
               (batch_id, spec_id, test_name, test_type, lower_limit, upper_limit,
                expected_text, unit, is_critical, numeric_value, text_value, result,
                tested_by, tested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (batch["id"], spec["id"], spec["test_name"], spec["test_type"],
             spec["lower_limit"], spec["upper_limit"], spec["expected_text"],
             spec["unit"], spec["is_critical"], numeric_value, text_value, result,
             actor["id"], utcnow_iso()),
        )
        audit_trail.record(
            actor=actor, action="QC_TEST", entity_type="batch",
            entity_ref=batch_no,
            details={"test": test_name, "numeric_value": numeric_value,
                     "text_value": text_value, "result": result,
                     "critical": bool(spec["is_critical"])},
            result="SUCCESS" if result == "PASS" else "FAILED",
            reason="检验结果超出标准（OOS）" if result == "OOS" else "",
            conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM qc_records WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def review_qc_result(*, actor: dict, batch_no: str, test_name: str) -> dict:
    _require_roles(actor, ("ANALYST", "QA"), "QC_REVIEW", batch_no)
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("IN_PRODUCTION", "PENDING_QA"))
        rec = conn.execute(
            """SELECT q.* FROM qc_records q JOIN qc_specs s ON s.id = q.spec_id
               WHERE q.batch_id = ? AND s.test_name = ? AND q.invalidated = 0
               ORDER BY q.id DESC""",
            (batch["id"], test_name)).fetchone()
        if not rec:
            raise NotFoundError(f"批次 {batch_no} 检验项目「{test_name}」无生效记录")
        if rec["reviewed_by"] is not None:
            raise ConflictError("该检验记录已完成复核")
        if rec["tested_by"] == actor["id"]:
            audit_trail.record(
                actor=actor, action="QC_REVIEW", entity_type="batch",
                entity_ref=batch_no, result="DENIED",
                reason="自检自核被拒（检验人与复核人不得为同一人）", conn=conn)
            conn.commit()
            raise PermissionDeniedError("检验人与复核人不得为同一人")
        conn.execute("UPDATE qc_records SET reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                     (actor["id"], utcnow_iso(), rec["id"]))
        audit_trail.record(
            actor=actor, action="QC_REVIEW", entity_type="batch",
            entity_ref=batch_no,
            details={"test": test_name, "record_id": rec["id"], "result": rec["result"]},
            conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM qc_records WHERE id = ?",
                                 (rec["id"],)).fetchone())
    finally:
        conn.close()


# ======================================================================
# 偏差与 CAPA
# ======================================================================

def raise_deviation(*, actor: dict, batch_no: str, category: str, title: str,
                    description: str, source_type: str = "OTHER",
                    source_ref: str = "") -> dict:
    _require_roles(actor, ("OPERATOR", "PRODUCTION_LEAD", "ANALYST", "QA", "QP"),
                   "DEVIATION_RAISE", batch_no)
    if category not in ("MINOR", "MAJOR", "CRITICAL"):
        raise ValidationError("偏差等级必须为 MINOR/MAJOR/CRITICAL")
    if source_type not in ("QC_OOS", "PROCESS_OOT", "YIELD", "OTHER"):
        raise ValidationError("偏差来源类型不合法")
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("IN_PRODUCTION", "PENDING_QA"))
        seq = conn.execute("SELECT COUNT(*) c FROM deviations WHERE batch_id = ?",
                           (batch["id"],)).fetchone()["c"] + 1
        dev_no = f"DEV-{batch_no}-{seq:02d}"
        cur = conn.execute(
            """INSERT INTO deviations
               (dev_no, batch_id, category, title, description, source_type, source_ref,
                status, raised_by, raised_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (dev_no, batch["id"], category, title, description, source_type,
             source_ref, "OPEN", actor["id"], utcnow_iso()),
        )
        audit_trail.record(
            actor=actor, action="DEVIATION_RAISE", entity_type="deviation",
            entity_ref=dev_no,
            details={"batch_no": batch_no, "category": category, "title": title,
                     "source_type": source_type, "source_ref": source_ref}, conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM deviations WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def close_deviation(*, actor: dict, dev_no: str, root_cause: str,
                    root_cause_confirmed: bool, is_lab_error: bool = False,
                    outcome: str = "EFFECTIVE") -> dict:
    """QA 关闭偏差。

    outcome=EFFECTIVE → CLOSED_EFFECTIVE（纠正措施经验证有效）；
    outcome=REJECTED  → CLOSED_REJECTED（关闭申请驳回，门禁继续阻断）。

    对化验室差错类 QC_OOS 偏差，关闭时将原 OOS 记录作废，必须重新取样检验。
    """
    _require_roles(actor, ("QA",), "DEVIATION_CLOSE", dev_no)
    _require_reason(root_cause, "根本原因")
    if outcome not in ("EFFECTIVE", "REJECTED"):
        raise ValidationError("关闭结论必须为 EFFECTIVE 或 REJECTED")
    conn = connect()
    try:
        dev = conn.execute("SELECT * FROM deviations WHERE dev_no = ?", (dev_no,)).fetchone()
        if not dev:
            raise NotFoundError(f"偏差 {dev_no} 不存在")
        if dev["status"] != "OPEN":
            raise ConflictError(f"偏差 {dev_no} 已关闭")
        new_status = "CLOSED_EFFECTIVE" if outcome == "EFFECTIVE" else "CLOSED_REJECTED"
        conn.execute(
            """UPDATE deviations SET status = ?, root_cause = ?,
                   root_cause_confirmed = ?, is_lab_error = ?,
                   closed_by = ?, closed_at = ? WHERE id = ?""",
            (new_status, root_cause, int(root_cause_confirmed), int(is_lab_error),
             actor["id"], utcnow_iso(), dev["id"]),
        )
        details = {"outcome": new_status, "lab_error": is_lab_error,
                   "root_cause_confirmed": root_cause_confirmed}
        invalidated = []
        if (new_status == "CLOSED_EFFECTIVE" and is_lab_error
                and dev["source_type"] == "QC_OOS"
                and dev["source_ref"].startswith("qc_record:")):
            rec_id = int(dev["source_ref"].split(":", 1)[1])
            cur = conn.execute(
                "UPDATE qc_records SET invalidated = 1 WHERE id = ? AND result = 'OOS'",
                (rec_id,))
            if cur.rowcount != 1:
                raise ConflictError("偏差指向的 QC OOS 记录不存在或已非 OOS，无法作废")
            invalidated.append(rec_id)
            details["invalidated_qc_record"] = rec_id
        audit_trail.record(
            actor=actor, action="DEVIATION_CLOSE", entity_type="deviation",
            entity_ref=dev_no, details=details,
            result="SUCCESS" if new_status == "CLOSED_EFFECTIVE" else "DENIED",
            reason=root_cause, conn=conn)
        conn.commit()
        out = dict(conn.execute("SELECT * FROM deviations WHERE id = ?",
                                (dev["id"],)).fetchone())
        out["invalidated_qc_records"] = invalidated
        return out
    finally:
        conn.close()


def create_capa(*, actor: dict, dev_no: str, action: str, owner_username: str,
                due_date: str) -> dict:
    _require_roles(actor, ("QA",), "CAPA_CREATE", dev_no)
    conn = connect()
    try:
        dev = conn.execute("SELECT * FROM deviations WHERE dev_no = ?", (dev_no,)).fetchone()
        if not dev:
            raise NotFoundError(f"偏差 {dev_no} 不存在")
        owner = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1",
                             (owner_username,)).fetchone()
        if not owner:
            raise NotFoundError(f"责任人账号 {owner_username} 不存在")
        cur = conn.execute(
            """INSERT INTO capas (deviation_id, action, owner, due_date, status)
               VALUES (?,?,?,?,'OPEN')""",
            (dev["id"], action, owner["id"], due_date))
        audit_trail.record(
            actor=actor, action="CAPA_CREATE", entity_type="deviation",
            entity_ref=dev_no,
            details={"action": action, "owner": owner_username, "due_date": due_date},
            conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM capas WHERE id = ?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def implement_capa(*, actor: dict, capa_id: int) -> dict:
    """责任人完成措施，提交 QA 验证（CLOSED_PENDING）。"""
    conn = connect()
    try:
        capa = conn.execute("SELECT * FROM capas WHERE id = ?", (capa_id,)).fetchone()
        if not capa:
            raise NotFoundError(f"CAPA #{capa_id} 不存在")
        if capa["owner"] != actor["id"]:
            _require_roles(actor, ("QA",), "CAPA_IMPLEMENT", f"capa:{capa_id}")
        if capa["status"] != "OPEN":
            raise ConflictError(f"CAPA #{capa_id} 当前状态 {capa['status']}，无法再提交")
        conn.execute(
            "UPDATE capas SET status = 'CLOSED_PENDING', closed_at = ? WHERE id = ?",
            (utcnow_iso(), capa_id))
        dev = conn.execute("SELECT dev_no FROM deviations WHERE id = ?",
                           (capa["deviation_id"],)).fetchone()
        audit_trail.record(
            actor=actor, action="CAPA_IMPLEMENT", entity_type="deviation",
            entity_ref=dev["dev_no"], details={"capa_id": capa_id}, conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM capas WHERE id = ?", (capa_id,)).fetchone())
    finally:
        conn.close()


def verify_capa(*, actor: dict, capa_id: int, effective: bool, note: str) -> dict:
    """QA 验证 CAPA 有效性。验证人不得是 CAPA 责任人（职责分离）。"""
    _require_roles(actor, ("QA",), "CAPA_VERIFY", f"capa:{capa_id}")
    _require_reason(note, "验证结论说明")
    conn = connect()
    try:
        capa = conn.execute("SELECT * FROM capas WHERE id = ?", (capa_id,)).fetchone()
        if not capa:
            raise NotFoundError(f"CAPA #{capa_id} 不存在")
        if capa["status"] != "CLOSED_PENDING":
            raise ConflictError("该 CAPA 尚未实施完成，无法验证")
        if capa["owner"] == actor["id"]:
            raise PermissionDeniedError("CAPA 验证人不得是措施责任人")
        new_status = "EFFECTIVE" if effective else "INEFFECTIVE"
        conn.execute(
            """UPDATE capas SET status = ?, verified_by = ?, verified_at = ?,
                   verification_note = ? WHERE id = ?""",
            (new_status, actor["id"], utcnow_iso(), note, capa_id))
        dev = conn.execute("SELECT dev_no FROM deviations WHERE id = ?",
                           (capa["deviation_id"],)).fetchone()
        audit_trail.record(
            actor=actor, action="CAPA_VERIFY", entity_type="deviation",
            entity_ref=dev["dev_no"],
            details={"capa_id": capa_id, "verdict": new_status},
            result="SUCCESS" if effective else "DENIED", reason=note, conn=conn)
        conn.commit()
        return dict(conn.execute("SELECT * FROM capas WHERE id = ?", (capa_id,)).fetchone())
    finally:
        conn.close()


# ======================================================================
# 放行 / 拒放（电子签名）
# ======================================================================

_RELEASE_MEANING = (
    "本人已完整审核该批次电子批记录（投料、工艺、质检、偏差与 CAPA），"
    "确认其符合注册工艺、质量标准及 GMP 要求，同意放行；"
    "本人知悉此电子签名与手写签名具有同等法律效力并承担放行责任。"
)
_REJECT_MEANING = (
    "本人已审核该批次电子批记录，因存在不可接受的质量风险或未闭环问题，"
    "决定拒绝放行并按不合格品流程处置；本人对此决定负责。"
)


def evaluate_batch(batch_no: str) -> dict:
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        return evaluate(build_bundle(conn, batch))
    finally:
        conn.close()


def get_ebr(batch_no: str) -> dict:
    """电子批记录汇总视图（API / 打印放行报告用）。"""
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        bundle = build_bundle(conn, batch)
        bundle["evaluation"] = evaluate(bundle)
        bundle["decisions"] = [dict(r) for r in conn.execute(
            "SELECT * FROM release_decisions WHERE batch_id = ? ORDER BY id",
            (batch["id"],)).fetchall()]
        return bundle
    finally:
        conn.close()


def _sign(conn, *, batch: dict, actor: dict, decision: str, reason: str,
          evaluation: dict) -> dict:
    prev = conn.execute(
        "SELECT sig_hash FROM release_decisions ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = prev["sig_hash"] if prev else config.GENESIS_HASH
    signed_at = utcnow_iso()
    meaning = _RELEASE_MEANING if decision == "RELEASE" else _REJECT_MEANING
    snapshot = dumps(evaluation)
    material = "|".join([
        prev_hash, str(batch["id"]), decision, str(actor["id"]), signed_at, reason, snapshot,
    ])
    sig_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    cur = conn.execute(
        """INSERT INTO release_decisions
           (batch_id, decision, meaning, signer_id, signed_at, reason,
            gate_snapshot, sig_hash)
           VALUES (?,?,?,?,?,?,?,?)""",
        (batch["id"], decision, meaning, actor["id"], signed_at, reason,
         snapshot, sig_hash),
    )
    row = conn.execute("SELECT * FROM release_decisions WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    return dict(row)


def release_batch(*, actor: dict, batch_no: str, reason: str) -> dict:
    """受权放行人(QP) 电子签名放行。门禁不过 → 硬性阻断（无 force 开关）。"""
    _require_roles(actor, ("QP",), "BATCH_RELEASE", batch_no)
    _require_reason(reason, "放行意见")
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("PENDING_QA",))
        evaluation = evaluate(build_bundle(conn, batch))

        if not evaluation["releasable"]:
            # 阻断本身必须留痕：把失败门禁明细写入审计链
            audit_trail.record(
                actor=actor, action="BATCH_RELEASE", entity_type="batch",
                entity_ref=batch_no, result="BLOCKED",
                reason="放行门禁未通过，系统阻断放行："
                       + "；".join(evaluation["open_blocking_gates"]),
                details={"open_gates": evaluation["open_blocking_gates"],
                         "gates": evaluation["gates"]},
                conn=conn)
            conn.commit()
            raise ReleaseBlockedError(
                f"批次 {batch_no} 未通过放行门禁，已被系统阻断放行",
                gates=evaluation["gates"])

        _conditional_transition(conn, batch["id"], "PENDING_QA", "RELEASED")
        decision = _sign(conn, batch=batch, actor=actor, decision="RELEASE",
                         reason=reason, evaluation=evaluation)
        audit_trail.record(
            actor=actor, action="BATCH_RELEASE", entity_type="batch",
            entity_ref=batch_no, result="SUCCESS", reason=reason,
            details={"decision_id": decision["id"], "sig_hash": decision["sig_hash"]},
            conn=conn)
        conn.commit()
        return {"batch_no": batch_no, "status": "RELEASED",
                "decision": decision, "evaluation": evaluation}
    finally:
        conn.close()


def reject_batch(*, actor: dict, batch_no: str, reason: str) -> dict:
    """QA/QP 拒放。门禁结论会快照进签名记录，但拒放不要求门禁全部通过。"""
    _require_roles(actor, ("QA", "QP"), "BATCH_REJECT", batch_no)
    _require_reason(reason, "拒放理由")
    conn = connect()
    try:
        batch = _get_batch(conn, batch_no)
        _require_state(batch, ("PENDING_QA",))
        evaluation = evaluate(build_bundle(conn, batch))
        _conditional_transition(conn, batch["id"], "PENDING_QA", "REJECTED")
        decision = _sign(conn, batch=batch, actor=actor, decision="REJECT",
                         reason=reason, evaluation=evaluation)
        audit_trail.record(
            actor=actor, action="BATCH_REJECT", entity_type="batch",
            entity_ref=batch_no, result="SUCCESS", reason=reason,
            details={"decision_id": decision["id"], "sig_hash": decision["sig_hash"],
                     "open_gates": evaluation["open_blocking_gates"]},
            conn=conn)
        conn.commit()
        return {"batch_no": batch_no, "status": "REJECTED",
                "decision": decision, "evaluation": evaluation}
    finally:
        conn.close()


def verify_signature_chain() -> dict:
    """独立校验放行签名链（重算每条签名哈希，与审计链相互印证）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT rd.*, b.id AS batch_pk FROM release_decisions rd "
            "JOIN batches b ON b.id = rd.batch_id ORDER BY rd.id ASC").fetchall()
        prev_hash = config.GENESIS_HASH
        for r in rows:
            material = "|".join([
                prev_hash, str(r["batch_pk"]), r["decision"], str(r["signer_id"]),
                r["signed_at"], r["reason"], r["gate_snapshot"],
            ])
            expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if expected != r["sig_hash"]:
                raise ConflictError(
                    f"放行签名 #{r['id']} 哈希不匹配（签名记录疑似被篡改）")
            prev_hash = r["sig_hash"]
        return {"ok": True, "signatures": len(rows)}
    finally:
        conn.close()
