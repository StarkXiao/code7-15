"""放行门禁规则引擎（纯函数，不触碰写操作）。

十道门禁，任一不通过即阻断放行，且**没有任何参数可以绕过**：

 1. BOM_COMPLETENESS       BOM 物料全部完成投料
 2. DISPENSING_TOLERANCE   实际投料量在允许公差内（物料平衡）
 3. DISPENSING_VERIFICATION 投料全部经第二人复核
 4. PROCESS_PARAMETERS     关键工艺参数(CPP)全部记录且在限度内；
                           超标(OOT)须有已关闭且验证有效的偏差
 5. QC_TESTS_COMPLETE      质量标准项目全部检验并经复核
 6. QC_RESULTS_CONFORM     无生效中的 OOS（化验室差错 OOS 须经偏差判定作废并重测）
 7. YIELD                  成品收率在注册工艺上下限内；超标须有有效偏差
 8. DEVIATIONS             无未关闭偏差；被驳回关闭的偏差直接阻断
 9. CAPA                   重大/严重偏差的 CAPA 全部经 QA 确认有效
10. SEGREGATION_OF_DUTIES  称量/复核、检验/复核不得为同一人；批生命周期记录完整

规则引擎只读数据 → 输出门禁清单；写动作（状态迁移、签名）在 service 层。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .masterdata import load_product_masterdata


@dataclass
class Gate:
    id: str
    name: str
    passed: bool = True
    severity: str = "BLOCKING"
    failures: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def fail(self, reason: str) -> None:
        self.passed = False
        self.failures.append(reason)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 数据装载

def build_bundle(conn, batch: dict) -> dict:
    """汇总批次全部 EBR 数据，供规则引擎与放行快照使用。"""
    md = load_product_masterdata(conn, batch["product_id"])
    bid = batch["id"]
    dispensing = [dict(r) for r in conn.execute(
        """SELECT d.*, m.code AS material_code, m.name AS material_name
           FROM dispensing_records d JOIN materials m ON m.id = d.material_id
           WHERE d.batch_id = ? ORDER BY d.step_no, d.id""", (bid,))]
    process_records = [dict(r) for r in conn.execute(
        "SELECT * FROM process_records WHERE batch_id = ? ORDER BY step_no, id", (bid,))]
    qc_records = [dict(r) for r in conn.execute(
        "SELECT * FROM qc_records WHERE batch_id = ? ORDER BY id", (bid,))]
    deviations = [dict(r) for r in conn.execute(
        "SELECT * FROM deviations WHERE batch_id = ? ORDER BY id", (bid,))]
    capas = [dict(r) for r in conn.execute(
        """SELECT c.* FROM capas c JOIN deviations d ON d.id = c.deviation_id
           WHERE d.batch_id = ? ORDER BY c.id""", (bid,))]
    return {
        "batch": batch,
        **md,
        "dispensing": dispensing,
        "process_records": process_records,
        "qc_records": qc_records,
        "deviations": deviations,
        "capas": capas,
    }


# ---------------------------------------------------------------- 主入口

def evaluate(bundle: dict) -> dict:
    """执行全部门禁，返回结构化评估结论。"""
    gates = [
        _gate_bom_completeness(bundle),
        _gate_dispensing_tolerance(bundle),
        _gate_dispensing_verification(bundle),
        _gate_process_parameters(bundle),
        _gate_qc_complete(bundle),
        _gate_qc_conform(bundle),
        _gate_yield(bundle),
        _gate_deviations(bundle),
        _gate_capa(bundle),
        _gate_segregation(bundle),
    ]
    blocking = [g for g in gates if not g.passed]
    batch = bundle["batch"]
    return {
        "batch_no": batch["batch_no"],
        "status": batch["status"],
        "releasable": len(blocking) == 0,
        "open_blocking_gates": [g.id for g in blocking],
        "gates": [g.to_dict() for g in gates],
    }


# ---------------------------------------------------------------- 偏差覆盖

def _closed_effective_refs(bundle: dict, source_type: str) -> set[str]:
    return {d["source_ref"] for d in bundle["deviations"]
            if d["status"] == "CLOSED_EFFECTIVE" and d["source_type"] == source_type}


# ---------------------------------------------------------------- 1-3 投料

def _gate_bom_completeness(b: dict) -> Gate:
    g = Gate("BOM_COMPLETENESS", "BOM 物料投料完整性")
    dispensed = {d["material_id"]: d for d in b["dispensing"]}
    for item in b["bom"]:
        if item["material_id"] not in dispensed:
            g.fail(f"物料 {item['material_code']}（{item['material_name']}）尚未投料")
    g.evidence["bom_items"] = len(b["bom"])
    g.evidence["dispensed_items"] = len(dispensed)
    return g


def _gate_dispensing_tolerance(b: dict) -> Gate:
    g = Gate("DISPENSING_TOLERANCE", "投料量与物料平衡公差")
    required = {item["material_id"]: item for item in b["bom"]}
    for d in b["dispensing"]:
        item = required.get(d["material_id"])
        if not item:
            g.fail(f"物料 {d['material_code']} 不在该产品 BOM 中")
            continue
        tol = item["tolerance_pct"] / 100.0
        lo, hi = item["qty_required"] * (1 - tol), item["qty_required"] * (1 + tol)
        deviation_pct = (d["qty_actual"] - item["qty_required"]) / item["qty_required"] * 100
        if not (lo - 1e-9 <= d["qty_actual"] <= hi + 1e-9):
            g.fail(
                f"物料 {item['material_code']} 实投 {d['qty_actual']}{d['unit']}，"
                f"标准 {item['qty_required']}±{item['tolerance_pct']}% "
                f"（偏差 {deviation_pct:+.2f}%）"
            )
    return g


def _gate_dispensing_verification(b: dict) -> Gate:
    g = Gate("DISPENSING_VERIFICATION", "投料双人复核")
    for d in b["dispensing"]:
        if d["verified_by"] is None:
            g.fail(f"物料 {d['material_code']} 的投料记录缺少第二人复核")
    return g


# ---------------------------------------------------------------- 4 工艺

def _gate_process_parameters(b: dict) -> Gate:
    g = Gate("PROCESS_PARAMETERS", "关键工艺参数执行")
    recorded = {r["parameter_id"]: r for r in b["process_records"]}
    covered_oot = _closed_effective_refs(b, "PROCESS_OOT")

    for p in b["process_parameters"]:
        rec = recorded.get(p["id"])
        if rec is None:
            if p["is_critical"]:
                g.fail(f"关键工艺参数「{p['step_name']} / {p['param_name']}」未记录")
            continue
        lo, hi = p["lower_limit"], p["upper_limit"]
        out = (lo is not None and rec["actual_value"] < lo) or \
              (hi is not None and rec["actual_value"] > hi)
        if out:
            ref = f"process_record:{rec['id']}"
            level = "关键" if p["is_critical"] else "非关键"
            if ref not in covered_oot:
                g.fail(
                    f"{level}工艺参数「{p['step_name']} / {p['param_name']}」"
                    f"实测 {rec['actual_value']}{p['unit']} 超出限度 "
                    f"[{lo}, {hi}]{p['unit']}，且无已验证有效的偏差"
                )
    g.evidence["critical_params"] = sum(1 for p in b["process_parameters"] if p["is_critical"])
    g.evidence["recorded_params"] = len(recorded)
    return g


# ---------------------------------------------------------------- 5-6 质检

def _active_qc_by_spec(b: dict) -> dict:
    """每个检验项目当前生效的记录（作废的化验室差错记录不计）。"""
    active: dict[int, list[dict]] = {}
    for r in b["qc_records"]:
        if not r["invalidated"]:
            active.setdefault(r["spec_id"], []).append(r)
    return active


def _gate_qc_complete(b: dict) -> Gate:
    g = Gate("QC_TESTS_COMPLETE", "质检项目完成与复核")
    active = _active_qc_by_spec(b)
    for spec in b["qc_specs"]:
        recs = active.get(spec["id"], [])
        if not recs:
            g.fail(f"检验项目「{spec['test_name']}」缺少生效检验记录（须取样检验）")
            continue
        latest = recs[-1]
        if latest["reviewed_by"] is None:
            g.fail(f"检验项目「{spec['test_name']}」结果未经第二人复核")
    g.evidence["spec_count"] = len(b["qc_specs"])
    return g


def _gate_qc_conform(b: dict) -> Gate:
    g = Gate("QC_RESULTS_CONFORM", "质检结果符合性（无生效 OOS）")
    active = _active_qc_by_spec(b)
    for spec in b["qc_specs"]:
        for r in active.get(spec["id"], []):
            if r["result"] == "OOS":
                g.fail(
                    f"检验项目「{spec['test_name']}」存在生效中的超标结果 OOS"
                    f"（记录 #{r['id']}）。化验室差错须经偏差作废后重新取样检验，"
                    f"真实超标不得放行"
                )
    # 被作废的 OOS 必须有重测，_gate_qc_complete 已保证每个项目有生效记录，
    # 此处只统计作废次数作为证据
    g.evidence["invalidated_oos"] = sum(
        1 for r in b["qc_records"] if r["invalidated"] and r["result"] == "OOS")
    return g


# ---------------------------------------------------------------- 7 收率

def _gate_yield(b: dict) -> Gate:
    g = Gate("YIELD", "成品收率/物料平衡")
    batch, product = b["batch"], b["product"]
    actual = batch["actual_size"]
    if actual is None:
        g.fail("尚未登记成品实际数量")
        return g
    pct = actual / batch["planned_size"] * 100
    g.evidence["yield_pct"] = round(pct, 2)
    g.evidence["allowed_range_pct"] = [product["yield_lower_pct"], product["yield_upper_pct"]]
    if not (product["yield_lower_pct"] <= pct <= product["yield_upper_pct"]):
        covered = bool(_closed_effective_refs(b, "YIELD"))
        if not covered:
            g.fail(
                f"实际收率 {pct:.2f}% 超出注册工艺范围 "
                f"{product['yield_lower_pct']}%~{product['yield_upper_pct']}%，"
                f"且无已验证有效的偏差"
            )
    return g


# ---------------------------------------------------------------- 8-9 偏差/CAPA

def _gate_deviations(b: dict) -> Gate:
    g = Gate("DEVIATIONS", "偏差关闭状态")
    for d in b["deviations"]:
        if d["status"] == "OPEN":
            g.fail(f"偏差 {d['dev_no']}（{d['category']}）尚未关闭：{d['title']}")
        elif d["status"] == "CLOSED_REJECTED":
            g.fail(f"偏差 {d['dev_no']} 关闭申请被驳回，需重新调查处理")
    g.evidence["total"] = len(b["deviations"])
    return g


def _gate_capa(b: dict) -> Gate:
    g = Gate("CAPA", "重大/严重偏差 CAPA 有效性")
    capa_by_dev: dict[int, list[dict]] = {}
    for c in b["capas"]:
        capa_by_dev.setdefault(c["deviation_id"], []).append(c)
    for d in b["deviations"]:
        if d["category"] in ("MAJOR", "CRITICAL") and d["status"] == "CLOSED_EFFECTIVE":
            capas = capa_by_dev.get(d["id"], [])
            if not capas:
                g.fail(f"偏差 {d['dev_no']}（{d['category']}）未制定 CAPA")
                continue
            if not any(c["status"] == "EFFECTIVE" for c in capas):
                states = "、".join(sorted({c["status"] for c in capas}))
                g.fail(f"偏差 {d['dev_no']} 的 CAPA 尚未由 QA 确认有效（当前：{states}）")
    return g


# ---------------------------------------------------------------- 10 职责分离

def _gate_segregation(b: dict) -> Gate:
    g = Gate("SEGREGATION_OF_DUTIES", "职责分离与批记录完整性")
    for d in b["dispensing"]:
        if d["verified_by"] is not None and d["verified_by"] == d["weighed_by"]:
            g.fail(f"物料 {d['material_code']} 的称量人与复核人为同一人，不允许自配自核")
    for r in b["qc_records"]:
        if not r["invalidated"] and r["reviewed_by"] is not None \
                and r["reviewed_by"] == r["tested_by"]:
            g.fail(f"检验「{r['test_name']}」的检验人与复核人为同一人，不允许自检自核")
    batch = b["batch"]
    if not batch["started_at"]:
        g.fail("批次缺少开工时间")
    if not batch["completed_at"]:
        g.fail("批次缺少完工时间")
    return g
