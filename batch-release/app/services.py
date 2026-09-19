"""业务服务层：电子批记录（EBR）、规则引擎、偏差、放行、完整性校验。

所有写操作：
1. 走显式状态机与角色校验；
2. 在同一事务内追加审计链条目（before/after + 哈希链）；
3. 业务记录保存 record_hash，供事后逐条复算。
"""
from datetime import datetime, timezone
from typing import Optional

from . import master_data as md
from .auth import hash_password, require_roles
from .database import append_audit, connect, transaction, utcnow
from .errors import Conflict, NotFound, ValidationError
from .hashing import record_hash, sha256_hex

# ---- 状态机 ----
ALLOWED_TRANSITIONS = {
    "created":         {"weighing"},
    "weighing":        {"in_production"},
    "in_production":   {"production_done", "weighing"},  # 允许补称
    "production_done": {"qc_sampling"},
    "qc_sampling":     {"pending_review", "production_done"},
    "qc_done":         {"pending_review"},
    "pending_review":  {"qc_sampling", "released", "rejected"},
    "released":        set(),
    "rejected":        set(),
}

PRODUCTION_ROLES = ("operator", "production_lead")


# ============ 初始化 / 主数据 ============

def seed(db_path: str) -> None:
    """创建主数据与账号（幂等）。"""
    conn = connect(db_path)
    try:
        with transaction(conn):
            prod = conn.execute("SELECT id FROM products WHERE code = ?",
                                (md.PRODUCT["code"],)).fetchone()
            if prod:
                return
            cur = conn.execute(
                "INSERT INTO products (code,name,dosage_form,strength) VALUES (?,?,?,?)",
                (md.PRODUCT["code"], md.PRODUCT["name"],
                 md.PRODUCT["dosage_form"], md.PRODUCT["strength"]))
            product_id = cur.lastrowid

            material_ids = {}
            for code, name, spec, cat, qty, uom, tol in md.BOM:
                r = conn.execute(
                    "INSERT INTO materials (code,name,spec,category) VALUES (?,?,?,?)",
                    (code, name, spec, cat))
                material_ids[code] = r.lastrowid
                conn.execute(
                    """INSERT INTO bom_items (product_id,material_id,planned_qty,uom,tolerance_pct)
                       VALUES (?,?,?,?,?)""",
                    (product_id, r.lastrowid, qty, uom, tol))

            step_ids = {}
            for step_no, name in md.PROCESS_STEPS:
                r = conn.execute(
                    "INSERT INTO process_steps (product_id,step_no,name) VALUES (?,?,?)",
                    (product_id, step_no, name))
                step_ids[step_no] = r.lastrowid
                for pname, lo, hi, uom, is_pf in md.PROCESS_PARAMS.get(step_no, []):
                    conn.execute(
                        """INSERT INTO process_param_specs
                           (step_id,name,lower_limit,upper_limit,uom,is_pass_fail)
                           VALUES (?,?,?,?,?,?)""",
                        (r.lastrowid, pname, lo, hi, uom, 1 if is_pf else 0))

            for tname, ttype, lo, hi, uom, risk in md.QC_SPECS:
                conn.execute(
                    """INSERT INTO qc_specs (product_id,test_name,test_type,lower_limit,upper_limit,uom,risk)
                       VALUES (?,?,?,?,?,?,?)""",
                    (product_id, tname, ttype, lo, hi, uom, risk))

            for username, display, role, pwd in md.USERS:
                h, salt = hash_password(pwd)
                conn.execute(
                    """INSERT INTO users (username,display_name,role,pwd_hash,pwd_salt,active,created_at)
                       VALUES (?,?,?,?,?,1,?)""",
                    (username, display, role, h, salt, utcnow()))

            append_audit(conn, actor_id=None, actor_name="SYSTEM",
                         action="seed_master_data", entity_type="system",
                         entity_id=product_id,
                         after={"product": md.PRODUCT, "users": len(md.USERS)})
    finally:
        conn.close()


def get_catalog(db_path: str) -> dict:
    conn = connect(db_path)
    try:
        product = dict(conn.execute("SELECT * FROM products LIMIT 1").fetchone())
        pid = product["id"]
        bom = [dict(r) for r in conn.execute(
            """SELECT b.*, m.code AS material_code, m.name AS material_name, m.category
               FROM bom_items b JOIN materials m ON m.id = b.material_id
               WHERE b.product_id = ? ORDER BY b.id""", (pid,))]
        steps = []
        for r in conn.execute(
                "SELECT * FROM process_steps WHERE product_id=? ORDER BY step_no", (pid,)):
            s = dict(r)
            s["params"] = [dict(p) for p in conn.execute(
                "SELECT * FROM process_param_specs WHERE step_id=? ORDER BY id", (s["id"],))]
            steps.append(s)
        qc = [dict(r) for r in conn.execute(
            "SELECT * FROM qc_specs WHERE product_id=? ORDER BY id", (pid,))]
        return {"product": product, "bom": bom, "steps": steps, "qc_specs": qc,
                "yield_window": {"lower": md.YIELD_LOWER, "upper": md.YIELD_UPPER}}
    finally:
        conn.close()


# ============ 批次 ============

def _get_batch(conn, batch_id: int) -> dict:
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise NotFound(f"批次 {batch_id} 不存在")
    return dict(row)


def _change_status(conn, batch: dict, new_status: str):
    if new_status not in ALLOWED_TRANSITIONS[batch["status"]]:
        raise Conflict(f"批次状态 {batch['status']} 不允许迁移到 {new_status}")
    batch["status"] = new_status
    conn.execute("UPDATE batches SET status=? WHERE id=?", (new_status, batch["id"]))


def list_batches(db_path: str) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT b.*, p.name AS product_name, p.code AS product_code,
                      u.display_name AS created_by_name
               FROM batches b JOIN products p ON p.id=b.product_id
               JOIN users u ON u.id=b.created_by
               ORDER BY b.id DESC""").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_batch(db_path: str, actor: dict, batch_no: Optional[str] = None,
                 batch_size: Optional[int] = None) -> dict:
    require_roles(actor, PRODUCTION_ROLES)
    conn = connect(db_path)
    try:
        with transaction(conn):
            product = conn.execute("SELECT * FROM products LIMIT 1").fetchone()
            size = batch_size or md.PRODUCT["batch_size"]
            if not batch_no:
                day = datetime.now(timezone.utc).strftime("%y%m%d")
                n = conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] + 1
                batch_no = f"VC{day}-{n:02d}"
            exists = conn.execute("SELECT 1 FROM batches WHERE batch_no=?",
                                  (batch_no,)).fetchone()
            if exists:
                raise Conflict(f"批号 {batch_no} 已存在")
            cur = conn.execute(
                """INSERT INTO batches (batch_no,product_id,batch_size,status,created_by,created_at)
                   VALUES (?,?,?,'created',?,?)""",
                (batch_no, product["id"], size, actor["id"], utcnow()))
            bid = cur.lastrowid
            batch = _get_batch(conn, bid)
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="batch_created", entity_type="batch", entity_id=bid,
                         batch_id=bid, after=_batch_snapshot(conn, bid))
            return batch
    finally:
        conn.close()


def start_weighing(db_path: str, batch_id: int, actor: dict) -> dict:
    require_roles(actor, PRODUCTION_ROLES)
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] == "weighing":
                return batch
            before = dict(batch)
            _change_status(conn, batch, "weighing")
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="batch_status_changed", entity_type="batch",
                         entity_id=batch_id, batch_id=batch_id, reason="开始配料称量",
                         before={"status": before["status"]}, after={"status": "weighing"})
            return _get_batch(conn, batch_id)
    finally:
        conn.close()


# ============ 投料记录 ============

def record_weighing(db_path: str, batch_id: int, material_code: str,
                    actual_qty: float, actor: dict) -> dict:
    require_roles(actor, PRODUCTION_ROLES)
    if not isinstance(actual_qty, (int, float)) or actual_qty <= 0:
        raise ValidationError("实际投料量必须为正数")
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] not in ("created", "weighing", "in_production"):
                raise Conflict(f"批次状态 {batch['status']} 下不能登记投料")
            bom = conn.execute(
                """SELECT b.*, m.code material_code, m.name material_name
                   FROM bom_items b JOIN materials m ON m.id=b.material_id
                   WHERE b.product_id=? AND m.code=?""",
                (batch["product_id"], material_code)).fetchone()
            if not bom:
                raise ValidationError(f"BOM 中不存在物料 {material_code}")
            dup = conn.execute(
                "SELECT id FROM weighing_records WHERE batch_id=? AND material_id=?",
                (batch_id, bom["material_id"])).fetchone()
            if dup:
                raise Conflict("该物料已登记投料，一条 EBR 仅允许一次称量记录")

            ts = utcnow()
            h = record_hash({
                "batch_id": batch_id, "material_id": bom["material_id"],
                "planned_qty": bom["planned_qty"], "actual_qty": actual_qty,
                "uom": bom["uom"], "weighed_by": actor["id"], "weighed_at": ts,
            })
            cur = conn.execute(
                """INSERT INTO weighing_records
                   (batch_id,material_id,planned_qty,actual_qty,uom,weighed_by,weighed_at,record_hash)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (batch_id, bom["material_id"], bom["planned_qty"], actual_qty,
                 bom["uom"], actor["id"], ts, h))
            rec = dict(conn.execute("SELECT * FROM weighing_records WHERE id=?",
                                    (cur.lastrowid,)).fetchone())

            # 自动偏差：超 BOM 公差 → major；偏差 > 5% → critical
            dev_pct = abs(actual_qty - bom["planned_qty"]) / bom["planned_qty"] * 100
            auto_dev = None
            if dev_pct > bom["tolerance_pct"]:
                severity = "critical" if dev_pct > 5.0 else "major"
                auto_dev = _raise_deviation(
                    conn, batch_id, "auto_weighing", "投料量超出工艺公差", severity,
                    f"物料 {material_code}（{bom['material_name']}）理论 {bom['planned_qty']}"
                    f"{bom['uom']}，实际 {actual_qty}{bom['uom']}，偏差 {dev_pct:.2f}%"
                    f"（公差 ±{bom['tolerance_pct']}%）",
                    actor, dedup=f"weigh:{batch_id}:{bom['material_id']}")

            if batch["status"] == "created":
                _change_status(conn, batch, "weighing")
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="weighing_recorded", entity_type="weighing_record",
                         entity_id=cur.lastrowid, batch_id=batch_id,
                         after={"record": rec, "deviation_pct": round(dev_pct, 3),
                                "auto_deviation_id": auto_dev})
            return {"record": rec, "deviation_pct": round(dev_pct, 3),
                    "auto_deviation_id": auto_dev}
    finally:
        conn.close()


def check_weighing(db_path: str, batch_id: int, weighing_id: int, actor: dict) -> dict:
    """第二人复核投料。"""
    require_roles(actor, PRODUCTION_ROLES)
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] not in ("created", "weighing", "in_production"):
                raise Conflict("当前批次状态不能复核投料")
            rec = conn.execute("SELECT * FROM weighing_records WHERE id=? AND batch_id=?",
                               (weighing_id, batch_id)).fetchone()
            if not rec:
                raise NotFound("投料记录不存在")
            if rec["checked_by"]:
                raise Conflict("该投料记录已完成双人复核")
            if rec["weighed_by"] == actor["id"]:
                raise ValidationError("双人复核要求：复核人不能与称量人为同一人")
            ts = utcnow()
            conn.execute("UPDATE weighing_records SET checked_by=?, checked_at=? WHERE id=?",
                         (actor["id"], ts, weighing_id))
            new_rec = dict(conn.execute("SELECT * FROM weighing_records WHERE id=?",
                                        (weighing_id,)).fetchone())
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="weighing_checked", entity_type="weighing_record",
                         entity_id=weighing_id, batch_id=batch_id,
                         reason="第二人双人复核确认",
                         before={"checked_by": None}, after={"checked_by": actor["id"]})
            return new_rec
    finally:
        conn.close()


# ============ 工艺执行 ============

def start_step(db_path: str, batch_id: int, step_no: int, actor: dict) -> dict:
    require_roles(actor, PRODUCTION_ROLES)
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            step = conn.execute(
                "SELECT * FROM process_steps WHERE product_id=? AND step_no=?",
                (batch["product_id"], step_no)).fetchone()
            if not step:
                raise ValidationError(f"产品工艺中不存在步骤 {step_no}")
            if batch["status"] not in ("weighing", "in_production"):
                raise Conflict(f"批次状态 {batch['status']} 下不能开始工艺步骤")

            row = conn.execute(
                "SELECT * FROM process_step_records WHERE batch_id=? AND step_no=?",
                (batch_id, step_no)).fetchone()
            ts = utcnow()
            if row:
                if row["started_at"]:
                    raise Conflict("该步骤已开始")
                conn.execute(
                    "UPDATE process_step_records SET started_at=?, operator_id=? WHERE id=?",
                    (ts, actor["id"], row["id"]))
                sid = row["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO process_step_records (batch_id,step_no,started_at,operator_id)
                       VALUES (?,?,?,?)""", (batch_id, step_no, ts, actor["id"]))
                sid = cur.lastrowid

            if step_no >= 20 and batch["status"] == "weighing":
                _change_status(conn, batch, "in_production")
            conn.execute("UPDATE batches SET current_step_no=? WHERE id=?",
                         (step_no, batch_id))
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="step_started", entity_type="process_step",
                         entity_id=sid, batch_id=batch_id,
                         after={"step_no": step_no, "step_name": step["name"]})
            return dict(conn.execute("SELECT * FROM process_step_records WHERE id=?",
                                     (sid,)).fetchone())
    finally:
        conn.close()


def record_param(db_path: str, batch_id: int, step_no: int, param_name: str,
                 value, actor: dict) -> dict:
    """登记工艺参数。定性项目 value 传 true/false；数值传数字。"""
    require_roles(actor, PRODUCTION_ROLES)
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] not in ("weighing", "in_production"):
                raise Conflict(f"批次状态 {batch['status']} 下不能登记工艺参数")
            step_rec = conn.execute(
                "SELECT * FROM process_step_records WHERE batch_id=? AND step_no=?",
                (batch_id, step_no)).fetchone()
            if not step_rec or not step_rec["started_at"]:
                raise Conflict("请先开始该工艺步骤，再登记参数")
            spec = conn.execute(
                """SELECT ps.* FROM process_param_specs ps
                   JOIN process_steps s ON s.id=ps.step_id
                   WHERE s.product_id=? AND s.step_no=? AND ps.name=?""",
                (batch["product_id"], step_no, param_name)).fetchone()
            if not spec:
                raise ValidationError(f"步骤 {step_no} 不存在参数 {param_name}")
            if conn.execute(
                    "SELECT 1 FROM process_param_records WHERE batch_id=? AND step_no=? AND param_name=?",
                    (batch_id, step_no, param_name)).fetchone():
                raise Conflict("该参数已登记（EBR 不允许覆盖；如需更正请走记录更正流程）")

            numeric_value = pass_fail = None
            if spec["is_pass_fail"]:
                if not isinstance(value, bool):
                    raise ValidationError("定性参数（如密封性）值必须为 true/false")
                pass_fail = 1 if value else 0
                conforms = bool(value)
            else:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValidationError("数值参数必须传数字")
                numeric_value = float(value)
                conforms = ((spec["lower_limit"] is None or numeric_value >= spec["lower_limit"])
                            and (spec["upper_limit"] is None or numeric_value <= spec["upper_limit"]))

            ts = utcnow()
            h = record_hash({
                "batch_id": batch_id, "step_no": step_no, "param_name": param_name,
                "numeric_value": numeric_value, "pass_fail": pass_fail,
                "recorded_by": actor["id"], "recorded_at": ts,
            })
            cur = conn.execute(
                """INSERT INTO process_param_records
                   (batch_id,step_no,param_name,numeric_value,pass_fail,recorded_by,recorded_at,record_hash)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (batch_id, step_no, param_name, numeric_value, pass_fail,
                 actor["id"], ts, h))
            rec = dict(conn.execute("SELECT * FROM process_param_records WHERE id=?",
                                    (cur.lastrowid,)).fetchone())

            auto_dev = None
            if not conforms:
                severity = "critical" if spec["is_pass_fail"] else "major"
                limit_txt = ("合格" if spec["is_pass_fail"]
                             else f"限度 [{spec['lower_limit']}, {spec['upper_limit']}] {spec['uom'] or ''}")
                actual_txt = ("不合格" if spec["is_pass_fail"] else f"{numeric_value} {spec['uom'] or ''}")
                auto_dev = _raise_deviation(
                    conn, batch_id, "auto_process", "工艺参数超出规定限度", severity,
                    f"步骤{step_no} 参数「{param_name}」实测 {actual_txt}，规定 {limit_txt}",
                    actor, dedup=f"param:{batch_id}:{step_no}:{param_name}")

            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="param_recorded", entity_type="process_param",
                         entity_id=cur.lastrowid, batch_id=batch_id,
                         after={"record": rec, "conforms": conforms,
                                "auto_deviation_id": auto_dev})
            return {"record": rec, "conforms": conforms, "auto_deviation_id": auto_dev}
    finally:
        conn.close()


def finish_step(db_path: str, batch_id: int, step_no: int, actor: dict) -> dict:
    require_roles(actor, PRODUCTION_ROLES)
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] not in ("weighing", "in_production"):
                raise Conflict("当前批次状态不能结束工艺步骤")
            rec = conn.execute(
                "SELECT * FROM process_step_records WHERE batch_id=? AND step_no=?",
                (batch_id, step_no)).fetchone()
            if not rec or not rec["started_at"]:
                raise Conflict("步骤尚未开始")
            if rec["finished_at"]:
                raise Conflict("步骤已结束")
            ts = utcnow()
            conn.execute("UPDATE process_step_records SET finished_at=? WHERE id=?",
                         (ts, rec["id"]))
            step = conn.execute(
                "SELECT * FROM process_steps WHERE product_id=? AND step_no=?",
                (batch["product_id"], step_no)).fetchone()
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="step_finished", entity_type="process_step",
                         entity_id=rec["id"], batch_id=batch_id,
                         after={"step_no": step_no, "step_name": step["name"]})
            return dict(conn.execute("SELECT * FROM process_step_records WHERE id=?",
                                     (rec["id"],)).fetchone())
    finally:
        conn.close()


def finish_production(db_path: str, batch_id: int, actual_yield_pct: float,
                      actor: dict) -> dict:
    """生产结束：要求全部工艺步骤完成并记录参数；填报实际收率。"""
    require_roles(actor, ("production_lead",))
    if not isinstance(actual_yield_pct, (int, float)) or actual_yield_pct <= 0:
        raise ValidationError("实际收率必须为正数（百分比）")
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] != "in_production":
                raise Conflict(f"批次状态 {batch['status']} 下不能结束生产")
            missing = _check_production_complete(conn, batch)
            if missing:
                raise Conflict("生产记录不完整：" + "；".join(missing))

            conn.execute("UPDATE batches SET actual_yield_pct=? WHERE id=?",
                         (actual_yield_pct, batch_id))
            auto_dev = None
            if not (md.YIELD_LOWER <= actual_yield_pct <= md.YIELD_UPPER):
                auto_dev = _raise_deviation(
                    conn, batch_id, "auto_process", "收率超出规定范围", "major",
                    f"实际收率 {actual_yield_pct}% 超出放行窗口 "
                    f"{md.YIELD_LOWER}%~{md.YIELD_UPPER}%",
                    actor, dedup=f"yield:{batch_id}")
            _change_status(conn, batch, "production_done")
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="batch_status_changed", entity_type="batch",
                         entity_id=batch_id, batch_id=batch_id,
                         reason=f"生产结束，实际收率 {actual_yield_pct}%",
                         before={"status": "in_production"},
                         after={"status": "production_done",
                                "actual_yield_pct": actual_yield_pct,
                                "auto_deviation_id": auto_dev})
            return _get_batch(conn, batch_id)
    finally:
        conn.close()


def _check_production_complete(conn, batch: dict) -> list[str]:
    problems = []
    steps = conn.execute("SELECT * FROM process_steps WHERE product_id=? ORDER BY step_no",
                         (batch["product_id"],)).fetchall()
    for s in steps:
        rec = conn.execute(
            "SELECT * FROM process_step_records WHERE batch_id=? AND step_no=?",
            (batch["id"], s["step_no"])).fetchone()
        if not rec or not rec["started_at"] or not rec["finished_at"]:
            problems.append(f"步骤{s['step_no']}「{s['name']}」未完成")
            continue
        for p in conn.execute("SELECT * FROM process_param_specs WHERE step_id=?", (s["id"],)):
            pr = conn.execute(
                "SELECT 1 FROM process_param_records WHERE batch_id=? AND step_no=? AND param_name=?",
                (batch["id"], s["step_no"], p["name"])).fetchone()
            if not pr:
                problems.append(f"步骤{s['step_no']} 缺少参数「{p['name']}」")
    return problems


# ============ QC 检验 ============

def start_qc(db_path: str, batch_id: int, actor: dict) -> dict:
    require_roles(actor, ("qc",))
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            before_status = batch["status"]
            if batch["status"] == "qc_sampling":
                return batch
            _change_status(conn, batch, "qc_sampling")
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="batch_status_changed", entity_type="batch",
                         entity_id=batch_id, batch_id=batch_id, reason="QC 取样开始检验",
                         before={"status": before_status},
                         after={"status": "qc_sampling"})
            return _get_batch(conn, batch_id)
    finally:
        conn.close()


def record_qc(db_path: str, batch_id: int, test_name: str, value, actor: dict) -> dict:
    require_roles(actor, ("qc",))
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] != "qc_sampling":
                raise Conflict(f"批次状态 {batch['status']} 下不能登记检验结果")
            spec = conn.execute(
                "SELECT * FROM qc_specs WHERE product_id=? AND test_name=?",
                (batch["product_id"], test_name)).fetchone()
            if not spec:
                raise ValidationError(f"质量标准中不存在检验项目 {test_name}")
            if conn.execute("SELECT 1 FROM qc_results WHERE batch_id=? AND test_name=?",
                            (batch_id, test_name)).fetchone():
                raise Conflict("该检验项目已有结果（EBR 不允许覆盖）")

            numeric_value = pass_fail = None
            if spec["test_type"] == "pass_fail":
                if not isinstance(value, bool):
                    raise ValidationError("定性检验结果必须为 true/false")
                pass_fail = 1 if value else 0
            else:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValidationError("数值检验结果必须传数字")
                numeric_value = float(value)
            conforms = ((spec["test_type"] == "pass_fail" and bool(value))
                        or (spec["test_type"] == "numeric"
                            and (spec["lower_limit"] is None or numeric_value >= spec["lower_limit"])
                            and (spec["upper_limit"] is None or numeric_value <= spec["upper_limit"])))
            conforms = 1 if conforms else 0

            ts = utcnow()
            h = record_hash({
                "batch_id": batch_id, "test_name": test_name,
                "test_type": spec["test_type"], "numeric_value": numeric_value,
                "pass_fail": pass_fail, "result_conforms": conforms,
                "tested_by": actor["id"], "tested_at": ts,
            })
            cur = conn.execute(
                """INSERT INTO qc_results
                   (batch_id,test_name,test_type,numeric_value,pass_fail,result_conforms,
                    tested_by,tested_at,record_hash)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (batch_id, test_name, spec["test_type"], numeric_value, pass_fail,
                 conforms, actor["id"], ts, h))
            rec = dict(conn.execute("SELECT * FROM qc_results WHERE id=?",
                                    (cur.lastrowid,)).fetchone())

            auto_dev = None
            if not conforms:
                auto_dev = _raise_deviation(
                    conn, batch_id, "auto_qc", "检验结果不符合质量标准（OOS）",
                    spec["risk"],
                    f"检验项目「{test_name}」结果 {value} {spec['uom'] or ''}，"
                    f"标准 [{spec['lower_limit']}, {spec['upper_limit']}]，"
                    f"风险等级 {spec['risk']}",
                    actor, dedup=f"qc:{batch_id}:{test_name}")
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="qc_recorded", entity_type="qc_result",
                         entity_id=cur.lastrowid, batch_id=batch_id,
                         after={"record": rec, "auto_deviation_id": auto_dev})
            return {"record": rec, "auto_deviation_id": auto_dev}
    finally:
        conn.close()


def check_qc(db_path: str, batch_id: int, qc_result_id: int, actor: dict) -> dict:
    require_roles(actor, ("qc",))
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] != "qc_sampling":
                raise Conflict("当前批次状态不能复核检验结果")
            rec = conn.execute("SELECT * FROM qc_results WHERE id=? AND batch_id=?",
                               (qc_result_id, batch_id)).fetchone()
            if not rec:
                raise NotFound("检验结果不存在")
            if rec["checked_by"]:
                raise Conflict("该检验结果已复核")
            if rec["tested_by"] == actor["id"]:
                raise ValidationError("双人复核要求：复核人不能与检验人为同一人")
            ts = utcnow()
            conn.execute("UPDATE qc_results SET checked_by=?, checked_at=? WHERE id=?",
                         (actor["id"], ts, qc_result_id))
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="qc_checked", entity_type="qc_result",
                         entity_id=qc_result_id, batch_id=batch_id,
                         reason="QC 第二人复核",
                         before={"checked_by": None}, after={"checked_by": actor["id"]})
            return dict(conn.execute("SELECT * FROM qc_results WHERE id=?",
                                    (qc_result_id,)).fetchone())
    finally:
        conn.close()


def submit_for_review(db_path: str, batch_id: int, actor: dict) -> dict:
    """QC 完成全部检验并双人复核后，提交 QA 放行审核（进入冻结态）。"""
    require_roles(actor, ("qc", "qa"))
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] not in ("qc_sampling", "production_done"):
                raise Conflict(f"批次状态 {batch['status']} 下不能提交放行审核")

            problems = []
            # 投料齐套 + 双人复核
            bom = conn.execute("SELECT * FROM bom_items WHERE product_id=?",
                               (batch["product_id"],)).fetchall()
            for b in bom:
                r = conn.execute(
                    "SELECT * FROM weighing_records WHERE batch_id=? AND material_id=?",
                    (batch_id, b["material_id"])).fetchone()
                if not r:
                    problems.append(f"缺少物料 {b['material_id']} 的投料记录")
                elif not r["checked_by"]:
                    problems.append(f"物料 {b['material_id']} 投料未经双人复核")
            problems += _check_production_complete(conn, batch)
            if batch["actual_yield_pct"] is None:
                problems.append("未填报实际收率")
            # QC 齐套 + 复核
            specs = conn.execute("SELECT * FROM qc_specs WHERE product_id=?",
                                 (batch["product_id"],)).fetchall()
            for s in specs:
                r = conn.execute("SELECT * FROM qc_results WHERE batch_id=? AND test_name=?",
                                 (batch_id, s["test_name"])).fetchone()
                if not r:
                    problems.append(f"缺少检验项目「{s['test_name']}」结果")
                elif not r["checked_by"]:
                    problems.append(f"检验项目「{s['test_name']}」未经双人复核")
            if problems:
                raise Conflict("EBR 不完整，不能提交 QA：" + "；".join(problems))

            ts = utcnow()
            conn.execute("UPDATE batches SET status='pending_review', submitted_at=? WHERE id=?",
                         (ts, batch_id))
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="batch_submitted", entity_type="batch",
                         entity_id=batch_id, batch_id=batch_id,
                         reason="EBR 完整，提交 QA 放行审核",
                         before={"status": batch["status"]},
                         after={"status": "pending_review", "submitted_at": ts})
            return _get_batch(conn, batch_id)
    finally:
        conn.close()


# ============ 偏差 ============

def _raise_deviation(conn, batch_id, source, category, severity, description,
                     actor, dedup: Optional[str] = None) -> Optional[int]:
    """在当前事务内登记偏差（自动去重）。"""
    if dedup:
        exists = conn.execute("SELECT id FROM deviations WHERE dedup_key=?", (dedup,)).fetchone()
        if exists:
            return exists["id"]
    cur = conn.execute(
        """INSERT INTO deviations (batch_id,source,category,severity,description,status,
                                   raised_by,raised_at,dedup_key)
           VALUES (?,?,?,?,?,'open',?,?,?)""",
        (batch_id, source, category, severity, description, actor["id"], utcnow(), dedup))
    dev_id = cur.lastrowid
    append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                 action="deviation_auto_raised" if source.startswith("auto") else "deviation_raised",
                 entity_type="deviation", entity_id=dev_id, batch_id=batch_id,
                 after={"id": dev_id, "severity": severity, "category": category,
                        "description": description})
    return dev_id


def list_deviations(db_path: str, batch_id: Optional[int] = None) -> list[dict]:
    conn = connect(db_path)
    try:
        sql = """SELECT d.*, u.display_name raised_by_name,
                        c.display_name closed_by_name
                 FROM deviations d JOIN users u ON u.id=d.raised_by
                 LEFT JOIN users c ON c.id=d.closed_by"""
        args = ()
        if batch_id:
            sql += " WHERE d.batch_id=?"
            args = (batch_id,)
        sql += " ORDER BY d.id"
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def raise_manual_deviation(db_path, batch_id, severity, description, actor) -> dict:
    require_roles(actor, ("qa",))
    if severity not in ("minor", "major", "critical"):
        raise ValidationError("偏差等级无效")
    conn = connect(db_path)
    try:
        with transaction(conn):
            _get_batch(conn, batch_id)
            dev_id = _raise_deviation(
                conn, batch_id, "manual", "人工登记偏差", severity, description, actor,
                dedup=None)
            return dict(conn.execute("SELECT * FROM deviations WHERE id=?", (dev_id,)).fetchone())
    finally:
        conn.close()


def close_deviation(db_path, deviation_id: int, disposition: str,
                    capa_summary: str, actor: dict) -> dict:
    """QA 关闭偏差：必须给处置结论 + CAPA 摘要。
    disposition=accepted 表示调查后接受（偏差关闭，不再阻断）；
    disposition=rejected 表示判废/退回（批次永远不得放行）。
    """
    require_roles(actor, ("qa",))
    if disposition not in ("accepted", "rejected"):
        raise ValidationError("处置结论必须为 accepted 或 rejected")
    if not capa_summary or len(capa_summary.strip()) < 5:
        raise ValidationError("请填写 CAPA/调查结论摘要（至少 5 个字）")
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute("SELECT * FROM deviations WHERE id=?", (deviation_id,)).fetchone()
            if not row:
                raise NotFound("偏差不存在")
            if row["status"] == "closed":
                raise Conflict("偏差已关闭")
            before = dict(row)
            ts = utcnow()
            conn.execute(
                """UPDATE deviations SET status='closed', disposition=?, capa_summary=?,
                                         closed_by=?, closed_at=? WHERE id=?""",
                (disposition, capa_summary.strip(), actor["id"], ts, deviation_id))
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="deviation_closed", entity_type="deviation",
                         entity_id=deviation_id, batch_id=row["batch_id"],
                         reason=f"QA 偏差处置：{disposition}",
                         before={"status": before["status"], "disposition": None},
                         after={"status": "closed", "disposition": disposition,
                                "capa_summary": capa_summary.strip()})
            return dict(conn.execute("SELECT * FROM deviations WHERE id=?",
                                     (deviation_id,)).fetchone())
    finally:
        conn.close()


# ============ 放行规则引擎 ============

def _dispositioned_nonconformity(conn, batch_id: int, dedup_key: str) -> Optional[dict]:
    """原始数据不符合项与其自动偏差的联动判定。

    返回：
      None                      未关联偏差（数据异常/偏差被删）→ 照常阻断
      {"gate": "open", ...}     偏差未关闭 → 阻断
      {"gate": "accepted", ...} QA 调查接受并关闭（含 CAPA）→ 解除阻断
      {"gate": "rejected", ...} QA 判废/退回 → 永久 critical 阻断
    """
    d = conn.execute("SELECT * FROM deviations WHERE dedup_key=?", (dedup_key,)).fetchone()
    if not d:
        return None
    if d["status"] == "open":
        return {"gate": "open", "severity": d["severity"], "dev_id": d["id"]}
    return {"gate": d["disposition"], "severity": d["severity"], "dev_id": d["id"]}


def evaluate_blockers(conn, batch: dict) -> list[dict]:
    """返回阻断项列表；空列表 = 允许放行。每条含 severity 与处置提示。

    数据不符合项（投料超量/工艺OOS/检验OOS/收率超限）均在产生时自动登记偏差，
    规则引擎按偏差处置结论联动：
      未关闭 → 阻断；accepted（QA 接受 + CAPA）→ 解除；rejected（判废）→ 永久阻断。
    """
    bid = batch["id"]
    blockers: list[dict] = []

    def add_nonconformity(dedup_key: str, code: str, severity: str, message: str):
        disp = _dispositioned_nonconformity(conn, bid, dedup_key)
        if disp is None:
            # 数据 OOS 却查不到偏差调查：可能是绕过应用直接写库
            blockers.append({"code": code, "severity": severity,
                             "message": message + "（且未关联偏差调查）"})
        elif disp["gate"] == "open":
            blockers.append({"code": code, "severity": disp["severity"],
                             "message": message + f"（偏差 #{disp['dev_id']} 待 QA 处置）"})
        elif disp["gate"] == "rejected":
            blockers.append({"code": "DEVIATION_REJECTED", "severity": "critical",
                             "message": f"偏差 #{disp['dev_id']} 经 QA 调查判废/退回，批次不得放行"})
        # accepted：QA 已评估接受且 CAPA 已记录，不再阻断

    # 1) 投料齐套 / 双人复核 / 投料偏差
    bom = conn.execute("SELECT * FROM bom_items WHERE product_id=?",
                       (batch["product_id"],)).fetchall()
    for b in bom:
        mat = conn.execute("SELECT code,name FROM materials WHERE id=?",
                           (b["material_id"],)).fetchone()
        r = conn.execute("SELECT * FROM weighing_records WHERE batch_id=? AND material_id=?",
                         (bid, b["material_id"])).fetchone()
        if not r:
            blockers.append({"code": "WEIGH_MISSING", "severity": "critical",
                             "message": f"缺少物料 {mat['code']}（{mat['name']}）的投料记录"})
            continue
        if not r["checked_by"]:
            blockers.append({"code": "WEIGH_NOT_CHECKED", "severity": "major",
                             "message": f"物料 {mat['code']} 投料未经第二人复核"})
        dev_pct = abs(r["actual_qty"] - b["planned_qty"]) / b["planned_qty"] * 100
        if dev_pct > b["tolerance_pct"]:
            add_nonconformity(
                f"weigh:{bid}:{b['material_id']}", "WEIGH_OUT_OF_TOLERANCE",
                "critical" if dev_pct > 5 else "major",
                f"物料 {mat['code']} 投料偏差 {dev_pct:.2f}% 超过公差 ±{b['tolerance_pct']}%")

    # 2) 工艺步骤 / 参数完整性
    for s in conn.execute("SELECT * FROM process_steps WHERE product_id=? ORDER BY step_no",
                          (batch["product_id"],)):
        rec = conn.execute(
            "SELECT * FROM process_step_records WHERE batch_id=? AND step_no=?",
            (bid, s["step_no"])).fetchone()
        if not rec or not rec["started_at"] or not rec["finished_at"]:
            blockers.append({"code": "STEP_INCOMPLETE", "severity": "critical",
                             "message": f"工艺步骤{s['step_no']}「{s['name']}」未执行完成"})
            continue
        for p in conn.execute("SELECT * FROM process_param_specs WHERE step_id=?", (s["id"],)):
            pr = conn.execute(
                "SELECT * FROM process_param_records WHERE batch_id=? AND step_no=? AND param_name=?",
                (bid, s["step_no"], p["name"])).fetchone()
            if not pr:
                blockers.append({"code": "PARAM_MISSING", "severity": "major",
                                 "message": f"步骤{s['step_no']} 缺少参数「{p['name']}」记录"})
                continue
            if p["is_pass_fail"]:
                if not pr["pass_fail"]:
                    add_nonconformity(
                        f"param:{bid}:{s['step_no']}:{p['name']}", "PARAM_OOS",
                        "critical", f"步骤{s['step_no']}「{p['name']}」检验不合格")
            else:
                v = pr["numeric_value"]
                if ((p["lower_limit"] is not None and v < p["lower_limit"])
                        or (p["upper_limit"] is not None and v > p["upper_limit"])):
                    add_nonconformity(
                        f"param:{bid}:{s['step_no']}:{p['name']}", "PARAM_OOS",
                        "major",
                        f"步骤{s['step_no']} 参数「{p['name']}」实测 {v} 超出限度 "
                        f"[{p['lower_limit']}, {p['upper_limit']}]")

    # 3) 收率
    if batch["actual_yield_pct"] is None:
        blockers.append({"code": "YIELD_MISSING", "severity": "major",
                         "message": "未填报实际收率"})
    elif not (md.YIELD_LOWER <= batch["actual_yield_pct"] <= md.YIELD_UPPER):
        add_nonconformity(
            f"yield:{bid}", "YIELD_OOS", "major",
            f"实际收率 {batch['actual_yield_pct']}% 超出窗口 "
            f"{md.YIELD_LOWER}%~{md.YIELD_UPPER}%")

    # 4) QC 齐套 / 复核 / 符合性
    for s in conn.execute("SELECT * FROM qc_specs WHERE product_id=?",
                          (batch["product_id"],)):
        r = conn.execute("SELECT * FROM qc_results WHERE batch_id=? AND test_name=?",
                         (bid, s["test_name"])).fetchone()
        if not r:
            blockers.append({"code": "QC_MISSING", "severity": "critical",
                             "message": f"缺少 QC 检验项目「{s['test_name']}」"})
            continue
        if not r["checked_by"]:
            blockers.append({"code": "QC_NOT_CHECKED", "severity": "major",
                             "message": f"检验「{s['test_name']}」未经第二人复核"})
        if not r["result_conforms"]:
            add_nonconformity(
                f"qc:{bid}:{s['test_name']}", "QC_OOS", s["risk"],
                f"检验「{s['test_name']}」OOS 不符合标准（{s['risk']}）")

    # 5) 人工登记的偏差：自动偏差未关闭已在上面的不符合项中反映，
    #    这里只兜底没有对应自动不符合项的人工偏差（dedup_key 为空）
    for d in conn.execute(
            "SELECT * FROM deviations WHERE batch_id=? AND dedup_key IS NULL", (bid,)):
        if d["status"] == "open":
            blockers.append({"code": "DEVIATION_OPEN", "severity": d["severity"],
                             "message": f"存在未关闭人工偏差 #{d['id']}（{d['severity']}）："
                                        f"{d['description'][:60]}"})
        elif d["disposition"] == "rejected":
            blockers.append({"code": "DEVIATION_REJECTED", "severity": "critical",
                             "message": f"人工偏差 #{d['id']} 经 QA 判废/退回，批次不得放行"})

    # 排序：critical 在前
    order = {"critical": 0, "major": 1, "minor": 2}
    blockers.sort(key=lambda b: order.get(b["severity"], 9))
    return blockers


def review_status(db_path: str, batch_id: int) -> dict:
    conn = connect(db_path)
    try:
        batch = _get_batch(conn, batch_id)
        blockers = evaluate_blockers(conn, batch)
        counts = {"critical": 0, "major": 0, "minor": 0}
        for b in blockers:
            counts[b["severity"]] = counts.get(b["severity"], 0) + 1
        return {"batch": batch, "blockers": blockers, "blocker_counts": counts,
                "can_release": batch["status"] == "pending_review" and not blockers}
    finally:
        conn.close()


def return_for_retest(db_path: str, batch_id: int, reason: str, actor: dict) -> dict:
    """QA 在审核阶段退回补检：pending_review → qc_sampling（解冻 EBR，全程留痕）。

    终态（已放行/已拒绝）不可退回。退回原因与再提交事件都会进入审计链，
    因此“退回—补录—重提”不是绕过审核，而是可追溯的正式流程。
    """
    require_roles(actor, ("qa",))
    if not reason or len(reason.strip()) < 4:
        raise ValidationError("请填写退回原因（至少 4 个字）")
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] != "pending_review":
                raise Conflict(f"仅待审核批次可退回补检，当前为 {batch['status']}")
            conn.execute("UPDATE batches SET status='qc_sampling' WHERE id=?", (batch_id,))
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="batch_returned_retest", entity_type="batch",
                         entity_id=batch_id, batch_id=batch_id,
                         reason=f"QA 退回补检/补录：{reason.strip()}",
                         before={"status": "pending_review"},
                         after={"status": "qc_sampling"})
            return _get_batch(conn, batch_id)
    finally:
        conn.close()


def release_batch(db_path: str, batch_id: int, comment: str, password: str,
                  actor: dict) -> dict:
    """QA 放行：规则引擎一票否决 + 电子签名（重输口令）。

    注意：被阻断的放行尝试本身也是 GMP 关键事件，必须在独立事务里落审计后提交，
    不能与随后抛出的异常同事务（否则回滚会抹掉“谁试图强行放行”）。
    """
    require_roles(actor, ("qa",))
    if not comment or len(comment.strip()) < 4:
        raise ValidationError("请填写放行意见（至少 4 个字）")
    from .auth import verify_password

    conn = connect(db_path)
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (actor["id"],)).fetchone()
        if not verify_password(password, u["pwd_salt"], u["pwd_hash"]):
            raise ValidationError("电子签名失败：口令不正确")

        batch = _get_batch(conn, batch_id)
        blockers = evaluate_blockers(conn, batch)

        if batch["status"] != "pending_review" or blockers:
            # 独立事务：阻断留痕，先落盘
            with transaction(conn):
                append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                             action="release_blocked", entity_type="batch",
                             entity_id=batch_id, batch_id=batch_id,
                             reason=(f"尝试放行但状态为 {batch['status']}"
                                     if batch["status"] != "pending_review"
                                     else f"规则引擎阻断：{len(blockers)} 项不符合"),
                             after={"status": batch["status"], "blockers": blockers})
            if batch["status"] != "pending_review":
                raise Conflict(f"仅 pending_review 状态可放行，当前为 {batch['status']}")
            raise Conflict(f"系统已阻断放行：存在 {len(blockers)} 项不符合项，"
                           f"请先处理偏差或拒绝放行该批次")

        with transaction(conn):
            ts = utcnow()
            signature = f"{actor['username']}#{actor['display_name']}#{ts}"
            conn.execute(
                """UPDATE batches SET status='released', reviewed_by=?, reviewed_at=?,
                                      release_comment=?, e_signature=? WHERE id=?""",
                (actor["id"], ts, comment.strip(), signature, batch_id))
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="batch_released", entity_type="batch",
                         entity_id=batch_id, batch_id=batch_id,
                         reason=f"QA 电子签名放行：{comment.strip()}",
                         before={"status": "pending_review"},
                         after={"status": "released", "e_signature": signature,
                                "release_comment": comment.strip()})
            return _get_batch(conn, batch_id)
    finally:
        conn.close()


def reject_batch(db_path: str, batch_id: int, reason: str, actor: dict) -> dict:
    require_roles(actor, ("qa",))
    if not reason or len(reason.strip()) < 4:
        raise ValidationError("请填写拒绝放行原因（至少 4 个字）")
    conn = connect(db_path)
    try:
        with transaction(conn):
            batch = _get_batch(conn, batch_id)
            if batch["status"] != "pending_review":
                raise Conflict(f"仅 pending_review 状态可拒绝放行，当前为 {batch['status']}")
            blockers = evaluate_blockers(conn, batch)
            ts = utcnow()
            conn.execute(
                """UPDATE batches SET status='rejected', reviewed_by=?, reviewed_at=?,
                                      release_comment=? WHERE id=?""",
                (actor["id"], ts, reason.strip(), batch_id))
            append_audit(conn, actor_id=actor["id"], actor_name=actor["display_name"],
                         action="batch_rejected", entity_type="batch",
                         entity_id=batch_id, batch_id=batch_id,
                         reason=f"QA 拒绝放行：{reason.strip()}",
                         before={"status": "pending_review"},
                         after={"status": "rejected", "reason": reason.strip(),
                                "blockers_at_reject": blockers})
            return _get_batch(conn, batch_id)
    finally:
        conn.close()


# ============ EBR 聚合 ============

def _batch_snapshot(conn, batch_id: int) -> dict:
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    return dict(b) if b else {}


def get_ebr(db_path: str, batch_id: int) -> dict:
    conn = connect(db_path)
    try:
        batch = _get_batch(conn, batch_id)
        weigh = [dict(r) for r in conn.execute(
            """SELECT w.*, m.code material_code, m.name material_name,
                      u.display_name weighed_by_name, c.display_name checked_by_name
               FROM weighing_records w
               JOIN materials m ON m.id=w.material_id
               JOIN users u ON u.id=w.weighed_by
               LEFT JOIN users c ON c.id=w.checked_by
               WHERE w.batch_id=? ORDER BY w.id""", (batch_id,))]
        steps = []
        for r in conn.execute(
                """SELECT sr.*, s.name step_name FROM process_step_records sr
                   JOIN process_steps s ON s.step_no=sr.step_no
                        AND s.product_id=(SELECT product_id FROM batches WHERE id=?)
                   WHERE sr.batch_id=? ORDER BY sr.step_no""", (batch_id, batch_id)):
            s = dict(r)
            s["params"] = [dict(p) for p in conn.execute(
                "SELECT * FROM process_param_records WHERE batch_id=? AND step_no=? ORDER BY id",
                (batch_id, s["step_no"]))]
            steps.append(s)
        qc = [dict(r) for r in conn.execute(
            """SELECT q.*, u.display_name tested_by_name, c.display_name checked_by_name
               FROM qc_results q JOIN users u ON u.id=q.tested_by
               LEFT JOIN users c ON c.id=q.checked_by
               WHERE q.batch_id=? ORDER BY q.id""", (batch_id,))]
        devs = [dict(r) for r in conn.execute(
            """SELECT d.*, u.display_name raised_by_name, c.display_name closed_by_name
               FROM deviations d JOIN users u ON u.id=d.raised_by
               LEFT JOIN users c ON c.id=d.closed_by
               WHERE d.batch_id=? ORDER BY d.id""", (batch_id,))]
        return {"batch": batch, "weighing": weigh, "steps": steps,
                "qc_results": qc, "deviations": devs}
    finally:
        conn.close()


# ============ 审计与完整性 ============

def list_audit(db_path: str, batch_id: Optional[int] = None, limit: int = 200) -> list[dict]:
    conn = connect(db_path)
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE batch_id=? ORDER BY id LIMIT ?",
                (batch_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_chain(db_path: str) -> dict:
    """重放审计哈希链。"""
    from .hashing import canon, chain_hash
    from .database import GENESIS_HASH
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        prev = GENESIS_HASH
        for r in rows:
            payload = {
                "ts": r["ts"], "actor_id": r["actor_id"], "actor_name": r["actor_name"],
                "action": r["action"], "entity_type": r["entity_type"],
                "entity_id": r["entity_id"], "batch_id": r["batch_id"],
                "reason": r["reason"], "before_data": r["before_data"],
                "after_data": r["after_data"],
            }
            expected = chain_hash(prev, payload)
            if r["prev_hash"] != prev:
                return {"ok": False, "broken_at": r["id"],
                        "reason": "prev_hash 链接断裂（可能有记录被删除/插入）"}
            if r["entry_hash"] != expected:
                return {"ok": False, "broken_at": r["id"],
                        "reason": "entry_hash 复算不一致（记录内容被篡改）"}
            prev = r["entry_hash"]
        return {"ok": True, "entries": len(rows), "tail_hash": prev}
    finally:
        conn.close()


def verify_batch_records(db_path: str, batch_id: int) -> dict:
    """逐条复算 EBR 业务记录的 record_hash。"""
    conn = connect(db_path)
    try:
        _get_batch(conn, batch_id)
        bad = []
        checked = 0

        for r in conn.execute("SELECT * FROM weighing_records WHERE batch_id=?", (batch_id,)):
            checked += 1
            h = record_hash({
                "batch_id": r["batch_id"], "material_id": r["material_id"],
                "planned_qty": r["planned_qty"], "actual_qty": r["actual_qty"],
                "uom": r["uom"], "weighed_by": r["weighed_by"], "weighed_at": r["weighed_at"]})
            if h != r["record_hash"]:
                bad.append({"entity": "weighing_record", "id": r["id"],
                            "reason": "投料记录字段与保存的哈希不一致"})

        for r in conn.execute("SELECT * FROM process_param_records WHERE batch_id=?", (batch_id,)):
            checked += 1
            h = record_hash({
                "batch_id": r["batch_id"], "step_no": r["step_no"],
                "param_name": r["param_name"], "numeric_value": r["numeric_value"],
                "pass_fail": r["pass_fail"], "recorded_by": r["recorded_by"],
                "recorded_at": r["recorded_at"]})
            if h != r["record_hash"]:
                bad.append({"entity": "process_param_record", "id": r["id"],
                            "reason": "工艺参数记录字段与保存的哈希不一致"})

        for r in conn.execute("SELECT * FROM qc_results WHERE batch_id=?", (batch_id,)):
            checked += 1
            h = record_hash({
                "batch_id": r["batch_id"], "test_name": r["test_name"],
                "test_type": r["test_type"], "numeric_value": r["numeric_value"],
                "pass_fail": r["pass_fail"], "result_conforms": r["result_conforms"],
                "tested_by": r["tested_by"], "tested_at": r["tested_at"]})
            if h != r["record_hash"]:
                bad.append({"entity": "qc_result", "id": r["id"],
                            "reason": "QC 检验记录字段与保存的哈希不一致"})

        # 批次快照指纹：把全部记录哈希 + 批次关键状态串联
        parts = []
        b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        parts.append(f"batch:{b['status']}:{b['actual_yield_pct']}:{b['e_signature']}")
        for r in conn.execute(
                "SELECT record_hash FROM weighing_records WHERE batch_id=? ORDER BY id", (batch_id,)):
            parts.append(r["record_hash"])
        for r in conn.execute(
                "SELECT record_hash FROM process_param_records WHERE batch_id=? ORDER BY id",
                (batch_id,)):
            parts.append(r["record_hash"])
        for r in conn.execute(
                "SELECT record_hash FROM qc_results WHERE batch_id=? ORDER BY id", (batch_id,)):
            parts.append(r["record_hash"])
        state_hash = sha256_hex("|".join(parts))

        return {"ok": not bad, "records_checked": checked, "mismatches": bad,
                "state_hash": state_hash}
    finally:
        conn.close()


def full_integrity_report(db_path: str, batch_id: Optional[int] = None) -> dict:
    report = {"chain": verify_chain(db_path)}
    if batch_id is not None:
        report["records"] = verify_batch_records(db_path, batch_id)
        report["ok"] = report["chain"]["ok"] and report["records"]["ok"]
    else:
        report["ok"] = report["chain"]["ok"]
    return report
